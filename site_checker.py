#!/usr/bin/env python3
"""Check favekad.com pages for status, load time, title, meta description and images.

Two modes (the only command-line options):
  python site_checker.py --quick   7 fixed pages + 50 random sitemap pages; phone screenshots of a few
  python site_checker.py --full    EVERY URL in the sitemap (no fixed pages, no screenshots)

Everything else (sitemap URL, fixed pages, limits) is set in the constants at the top of this file.

Every run writes a CSV with these columns:
  checked_at,url,status,final_url,load_ms,bytes,title,title_ok,
  meta_description_ok,broken_images,broken_image_urls,problem

Politeness rules enforced by this tool:
  * Strictly one request at a time (no threads, no parallelism).
  * At most 1 HTTP request per second. That covers the sitemap download,
    every redirect hop, every image check and every request the screenshot
    browser makes (images/CSS/JS shared between pages are cached, not refetched).
  * User-Agent: FaveKad-QA/1.0 (Roshan, for Redz)
"""

import argparse
import csv
import gzip
import os
import random
import re
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "FaveKad-QA/1.0 (Roshan, for Redz)"
DEFAULT_SITEMAP = "https://favekad.com/sitemap.xml"
QUICK_FIXED_PAGES = [      # --quick: checked on every run, in this order
    "https://favekad.com/",
    "https://favekad.com/gallery",
    "https://favekad.com/pricing",
    "https://favekad.com/deepavali-ecards",
    "https://favekad.com/ecards",
    "https://favekad.com/malaysian-ecards",
    "https://favekad.com/hari-raya-ecards",
]
SHOT_PAGES = [              # --quick: these pages get a phone screenshot ...
    "https://favekad.com/",
    "https://favekad.com/gallery",
    "https://favekad.com/deepavali-ecards",
]
SHOT_RANDOM = 3             # ... plus this many random sitemap pages
QUICK_SAMPLE = 50           # --quick: random sitemap pages checked besides the fixed pages
REQUEST_TIMEOUT = 10.0      # seconds to wait for a page / image
SLOW_SECONDS = 3.0          # flag pages that load slower than this
MAX_IMAGES_PER_PAGE = 10    # new image requests per page (0 = skip image checks)
SCREENSHOT_DIR = "screenshots"
SCREENSHOT_TIME_LIMIT = 20.0  # seconds a page may keep loading files before the screenshot is taken
MIN_INTERVAL = 1.0          # seconds between request starts (hard floor)
SAFETY_MARGIN = 0.05        # extra spacing so timing jitter never lets two requests land inside 1 second
MAX_REDIRECTS = 10
MAX_CHILD_SITEMAPS = 50     # safety cap when a sitemap index lists child sitemaps

# A title shorter than this many characters is flagged
TITLE_MIN = 10

CSV_COLUMNS = ["checked_at", "url", "status", "final_url", "load_ms", "bytes", "title",
               "title_ok", "meta_description_ok", "broken_images", "broken_image_urls", "problem"]

_last_request_start: Optional[float] = None  # time.monotonic() of last request


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
def _throttle(interval: float = MIN_INTERVAL) -> None:
    """Block until at least `interval` seconds have passed since the last request started."""
    global _last_request_start
    interval = max(interval, MIN_INTERVAL)  # never go faster than 1 req/sec
    if _last_request_start is not None:
        wait = _last_request_start + interval + SAFETY_MARGIN - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _last_request_start = time.monotonic()


# --------------------------------------------------------------------------
# HTML parsing (BeautifulSoup)
# --------------------------------------------------------------------------
@dataclass
class ParsedPage:
    title: Optional[str] = None          # None = no <title> tag, "" = empty <title>
    description: Optional[str] = None    # None = no <meta name="description">
    images: List[str] = field(default_factory=list)      # <img src> URLs
    og_images: List[str] = field(default_factory=list)   # og:image URLs


_OG_IMAGE_NAMES = {"og:image", "og:image:url", "og:image:secure_url"}


def parse_html(body: bytes, charset: Optional[str], page_url: str) -> ParsedPage:
    """Read the title, meta description, <img> and og:image URLs from an HTML page with BeautifulSoup."""
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
    page = ParsedPage()

    # <title>: first one that is not inside an inline SVG
    for tag in soup.find_all("title"):
        if tag.find_parent("svg") is None:
            page.title = " ".join(tag.get_text().split())
            break

    # <meta name="description" content="...">  (name matched case-insensitively)
    meta = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta is not None:
        page.description = " ".join((meta.get("content") or "").split())

    base_tag = soup.find("base", href=True)
    base = urljoin(page_url, base_tag["href"].strip()) if base_tag else page_url

    def clean(src: str) -> Optional[str]:
        src = (src or "").strip()
        if not src or src.startswith(("data:", "blob:", "javascript:")):
            return None
        full = urldefrag(urljoin(base, src))[0]
        return full if urlparse(full).scheme in ("http", "https") else None

    # <meta property="og:image" content="...">
    seen = set()
    for m in soup.find_all("meta"):
        if (m.get("property") or m.get("name") or "").strip().lower() in _OG_IMAGE_NAMES:
            full = clean(m.get("content"))
            if full and full not in seen:
                seen.add(full)
                page.og_images.append(full)

    # <img src> (or lazy-load data-src)
    seen = set()
    for img in soup.find_all("img"):
        full = clean(img.get("src") or img.get("data-src"))
        if full and full not in seen:
            seen.add(full)
            page.images.append(full)

    return page


def _header_charset(response: requests.Response) -> Optional[str]:
    """Charset from the Content-Type header, or None so BeautifulSoup detects it itself."""
    ctype = response.headers.get("Content-Type", "")
    return response.encoding if "charset" in ctype.lower() else None


# --------------------------------------------------------------------------
# Single-page check
# --------------------------------------------------------------------------
@dataclass
class CheckResult:
    url: str
    ok: bool
    status_code: Optional[int] = None
    reason: str = ""
    final_url: str = ""
    redirects: int = 0
    size_bytes: int = 0
    ttfb: float = 0.0        # request time until final response headers arrive (seconds)
    total_time: float = 0.0  # request time until the full body is downloaded (seconds)
    error: str = ""
    # page analysis
    checked_at: str = ""
    is_html: bool = False
    title: str = ""
    meta_description: str = ""
    image_urls: List[str] = field(default_factory=list)       # <img src>
    og_image_urls: List[str] = field(default_factory=list)    # og:image
    has_title_tag: bool = False
    title_ok: Optional[bool] = None
    meta_ok: Optional[bool] = None
    broken_images: Optional[int] = None
    broken_image_urls: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    screenshot: str = ""      # path of the phone screenshot (not part of the CSV)


def normalize_url(url: str) -> str:
    """Add https:// if the user did not supply a scheme."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def check_website(url: str, timeout: float = 10.0,
                  session: Optional[requests.Session] = None,
                  interval: float = MIN_INTERVAL) -> CheckResult:
    """Request the URL (sequentially, rate limited), measure status + load time, parse the HTML.

    Times cover only time spent talking to the server; waiting for the
    rate limiter is not counted.
    """
    url = normalize_url(url)
    session = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}

    current = url
    hops = 0
    spent = 0.0  # time spent in completed redirect hops

    while True:
        _throttle(interval)
        t0 = time.perf_counter()
        try:
            # stream=True separates header time from body download time;
            # allow_redirects=False so each hop is throttled individually
            response = session.get(current, timeout=timeout, headers=headers,
                                   stream=True, allow_redirects=False)
            ttfb_hop = time.perf_counter() - t0

            if response.is_redirect:
                response.close()
                spent += time.perf_counter() - t0
                hops += 1
                if hops > MAX_REDIRECTS:
                    return CheckResult(url=url, ok=False, error=f"Too many redirects (>{MAX_REDIRECTS})")
                current = urljoin(current, response.headers["Location"])
                continue

            body = response.content  # downloads the full body
            total_hop = time.perf_counter() - t0

            result = CheckResult(
                url=url,
                ok=response.ok,  # True for status < 400
                status_code=response.status_code,
                reason=response.reason or "",
                final_url=current,
                redirects=hops,
                size_bytes=len(body),
                ttfb=spent + ttfb_hop,
                total_time=spent + total_hop,
            )

            ctype = response.headers.get("Content-Type", "").lower()
            if result.ok and ("html" in ctype or not ctype):
                result.is_html = True
                try:
                    parsed = parse_html(body, _header_charset(response), current)
                    result.has_title_tag = parsed.title is not None
                    result.title = parsed.title or ""
                    result.meta_description = parsed.description or ""
                    result.image_urls = parsed.images
                    result.og_image_urls = parsed.og_images
                except Exception:  # malformed HTML should never crash the run
                    pass
            return result
        except requests.exceptions.Timeout:
            return CheckResult(url=url, ok=False, error=f"Timed out after {timeout}s")
        except requests.exceptions.SSLError as exc:
            return CheckResult(url=url, ok=False, error=f"SSL error: {exc}")
        except requests.exceptions.ConnectionError as exc:
            return CheckResult(url=url, ok=False, error=f"Connection error: {exc}")
        except requests.exceptions.RequestException as exc:
            return CheckResult(url=url, ok=False, error=f"Request failed: {exc}")


class ImageCache(dict):
    """image key -> final HTTP status (None if the request failed). Each unique image URL is requested once per run."""

    def __init__(self) -> None:
        super().__init__()
        self.checked = 0   # unique image URLs actually requested
        self.hits = 0      # lookups answered from the cache (no request)


def _image_key(url: str) -> str:
    """Cache key: same image, same key (scheme/host case and default ports ignored, fragment dropped)."""
    u = urlparse(url)
    host = u.netloc.lower()
    if (u.scheme == "http" and host.endswith(":80")) or (u.scheme == "https" and host.endswith(":443")):
        host = host.rsplit(":", 1)[0]
    return f"{u.scheme.lower()}://{host}{u.path or '/'}" + (f"?{u.query}" if u.query else "")


def check_image(url: str, timeout: float, session: requests.Session, interval: float) -> Optional[int]:
    """Return the final HTTP status of an image URL, or None if the request failed.

    HEAD first; GET (headers only, no body) only if the server refuses HEAD (400, 403, 405, 501).
    """
    headers = {"User-Agent": USER_AGENT}
    for method in ("HEAD", "GET"):
        current = url
        status = None
        for _ in range(MAX_REDIRECTS + 1):
            _throttle(interval)
            try:
                resp = session.request(method, current, headers=headers, timeout=timeout,
                                       allow_redirects=False, stream=True)
            except requests.exceptions.RequestException:
                return None
            try:
                if resp.is_redirect:
                    current = urljoin(current, resp.headers["Location"])
                    continue
                status = resp.status_code
            finally:
                resp.close()  # we only need headers; never download the image body
            break
        if status is None:        # too many redirects
            return None
        if method == "HEAD" and status in (400, 403, 405, 501):
            continue              # some servers reject HEAD; retry with GET
        return status
    return None


def evaluate_page(r: CheckResult, session: requests.Session, args, image_cache: ImageCache) -> None:
    """Fill in title_ok, meta_ok, broken images and the problem list for a result.

    A row is flagged when:
      * the request failed, or the final status is not 200
      * the URL redirected
      * the load time is over SLOW_SECONDS (default 3)
      * the title is missing, empty, or shorter than TITLE_MIN characters
      * the meta description is missing (or empty)
      * an <img src> or og:image URL does not return 200
    """
    problems = r.problems

    if r.error:
        problems.append(r.error[:150])
        return

    if r.status_code != 200:
        problems.append(f"HTTP {r.status_code} {r.reason}".strip())
    if r.redirects:
        problems.append(f"redirected ({r.redirects} hop{'s' if r.redirects > 1 else ''})")
    if r.total_time > args.slow:
        problems.append(f"slow load ({r.total_time:.1f}s)")

    if r.status_code == 200 and r.is_html:
        # --- title ---
        n = len(r.title)
        if not r.has_title_tag:
            r.title_ok = False
            problems.append("missing title")
        elif n == 0:
            r.title_ok = False
            problems.append("empty title")
        elif n < TITLE_MIN:
            r.title_ok = False
            problems.append(f"title too short ({n} chars)")
        else:
            r.title_ok = True

        # --- meta description ---
        r.meta_ok = bool(r.meta_description)
        if not r.meta_ok:
            problems.append("missing meta description")

        # --- images: <img src> and og:image must return 200 ---
        # Every unique URL is requested once per run (HEAD first), then answered from the cache.
        # MAX_IMAGES_PER_PAGE limits NEW requests per page; cached images are free. og:image is checked first.
        if args.max_images > 0:
            og_keys = {_image_key(u) for u in r.og_image_urls}
            candidates = r.og_image_urls + [u for u in r.image_urls if _image_key(u) not in og_keys]
            broken, og_broken = [], False
            new_requests = 0
            for img in candidates:
                key = _image_key(img)
                if key in image_cache:
                    image_cache.hits += 1
                elif new_requests < args.max_images:
                    image_cache[key] = check_image(img, args.timeout, session, args.interval)
                    image_cache.checked += 1
                    new_requests += 1
                else:
                    continue                  # budget for new checks on this page is used up
                if image_cache[key] != 200:
                    broken.append(img)
                    og_broken = og_broken or key in og_keys
            r.broken_images = len(broken)
            r.broken_image_urls = broken
            if broken:
                problems.append(f"{len(broken)} broken image(s)" + (" (incl. og:image)" if og_broken else ""))


def check_and_record(url: str, args, session, image_cache: ImageCache, logger: Optional["CsvLogger"],
                     shooter: Optional["Screenshotter"] = None) -> CheckResult:
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    r = check_website(url, timeout=args.timeout, session=session, interval=args.interval)
    r.checked_at = started
    evaluate_page(r, session, args, image_cache)
    if shooter is not None and r.ok and r.is_html:
        r.screenshot, note = shooter.capture(r.url)
        if note:
            r.problems.append(note)
    if logger:
        logger.write(r)
    return r


# --------------------------------------------------------------------------
# Phone-size screenshots (Playwright)
# --------------------------------------------------------------------------
VIEWPORT = {"width": 390, "height": 844}           # phone size
CACHEABLE_TYPES = {"stylesheet", "script", "font", "image", "media"}
MAX_CACHED_BYTES = 5 * 1024 * 1024
NETWORK_IDLE_MS = 10_000                            # how long to wait for lazy content after "load"
# Cookie banner: the "No thanks" button is tapped before each screenshot (matched by its text, case-insensitive)
COOKIE_BUTTON = r"text=/^\s*no,?\s*thanks[.!]?\s*$/i >> visible=true"
COOKIE_WAIT_MS = 4000          # how long to wait for the banner to appear (until it has been dismissed once)
COOKIE_WAIT_AFTER_MS = 1000    # shorter wait on later pages: the "No thanks" choice is usually remembered
FETCH_TIMEOUT_MS = 30_000                           # max time for a single browser request
SCREENSHOT_TIMEOUT_MS = 30_000                      # max time for the screenshot call itself
_STRIP_HEADERS = {"content-encoding", "content-length", "transfer-encoding"}


class Screenshotter:
    """Takes 390x844 phone screenshots with headless Chromium.

    Every request the browser makes goes through our own route handler, which
      * waits for the shared 1-request-per-second throttle and fetches one request at a time,
      * does not follow redirects itself (the browser follows them, so each hop is throttled),
      * keeps CSS / JS / fonts / images in memory, so assets shared by many pages are fetched once.
    """

    def __init__(self, base_dir: str, interval: float, timeout: float, full_page: bool = False) -> None:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # lazy: optional dependency
        self._timeout_error = PWTimeout
        self.interval = interval
        self.budget = timeout                 # seconds a page may keep requesting files
        self.full_page = full_page
        self.date = datetime.now().strftime("%Y-%m-%d")
        self.out_dir = os.path.abspath(base_dir)
        self.network_requests = 0
        self.cache_hits = 0
        self.failures: List[str] = []
        self.cookie_taps = 0        # pages where "No thanks" was tapped
        self.cookie_missed = 0      # pages where no such button was found
        self._deadline: Optional[float] = None
        self._skipped = 0

        # make sure the folder exists and is writable before opening a browser
        os.makedirs(self.out_dir, exist_ok=True)
        probe = os.path.join(self.out_dir, ".write-test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        self._cache: Dict[str, tuple] = {}
        self._used_names: set = set()

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                viewport=VIEWPORT, device_scale_factor=1, is_mobile=True, has_touch=True,
                user_agent=USER_AGENT)
            self._context.route("**/*", self._handle)
        except Exception:
            self._pw.stop()
            raise

    def _handle(self, route) -> None:
        request = route.request
        url = request.url
        try:
            cached = self._cache.get(url) if request.method == "GET" else None
            if cached:
                self.cache_hits += 1
                route.fulfill(status=cached[0], headers=cached[1], body=cached[2])
                return
            if not url.startswith(("http://", "https://")):
                route.continue_()
                return

            if self._deadline is not None and time.monotonic() > self._deadline:
                self._skipped += 1       # budget used up: skip remaining files without any request
                route.abort()
                return

            _throttle(self.interval)
            self.network_requests += 1
            resp = route.fetch(max_redirects=0, timeout=FETCH_TIMEOUT_MS)

            if request.method == "GET" and request.resource_type in CACHEABLE_TYPES and resp.status == 200:
                body = resp.body()
                if len(body) <= MAX_CACHED_BYTES:
                    headers = {k: v for k, v in resp.headers.items() if k.lower() not in _STRIP_HEADERS}
                    headers["content-length"] = str(len(body))
                    self._cache[url] = (200, headers, body)
            route.fulfill(response=resp)
        except Exception:
            try:
                route.abort()
            except Exception:
                pass

    def _unique_path(self, url: str) -> str:
        u = urlparse(url)
        name = u.path.strip("/") or "home"
        if u.query:
            name += "-" + u.query
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:80] or "page"
        base = f"{self.date}-{name}"          # e.g. 2026-10-31-gallery
        candidate, n = base, 1
        while candidate in self._used_names:
            n += 1
            candidate = f"{base}-{n}"
        self._used_names.add(candidate)
        return os.path.join(self.out_dir, candidate + ".png")

    def _dismiss_cookie_banner(self, page) -> bool:
        """Tap "No thanks" on the cookie banner (main page or an iframe). Returns True if it was tapped."""
        wait_ms = COOKIE_WAIT_AFTER_MS if self.cookie_taps else COOKIE_WAIT_MS
        give_up = time.monotonic() + wait_ms / 1000
        while True:
            for frame in page.frames:
                button = frame.locator(COOKIE_BUTTON).first
                if frame.locator(COOKIE_BUTTON).count() > 0:
                    try:
                        button.tap(timeout=3000)       # a real touch tap (the context has touch enabled)
                    except Exception:
                        button.click(timeout=3000)
                    page.wait_for_timeout(700)         # let the banner slide away before the screenshot
                    return True
            if time.monotonic() >= give_up:
                return False
            page.wait_for_timeout(250)

    def capture(self, url: str) -> Tuple[str, str]:
        """Open the page in the phone viewport and save a PNG. Returns (path, note); note is a problem text.

        The page may keep requesting files (one per second) for `budget` seconds. After that the
        remaining files are skipped and the screenshot is taken with what has loaded.
        """
        path = self._unique_path(url)
        page = self._context.new_page()
        self._deadline = time.monotonic() + self.budget
        self._skipped = 0
        try:
            try:
                page.goto(url, wait_until="load", timeout=int((self.budget + 30) * 1000))
            except self._timeout_error:
                pass
            else:
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
                except self._timeout_error:
                    pass
            try:
                if self._dismiss_cookie_banner(page):
                    self.cookie_taps += 1
                else:
                    self.cookie_missed += 1
            except Exception:
                self.cookie_missed += 1                # never let the banner step block the screenshot
            try:
                page.screenshot(path=path, full_page=self.full_page, timeout=SCREENSHOT_TIMEOUT_MS)
            except Exception:
                try:
                    page.evaluate("window.stop()")   # cancel anything still loading, then retry once
                except Exception:
                    pass
                page.screenshot(path=path, full_page=self.full_page, timeout=SCREENSHOT_TIMEOUT_MS)
            note = ""
            if self._skipped:
                note = (f"screenshot is partial: {self._skipped} file(s) skipped after "
                        f"{self.budget:.0f}s time limit")
            return path, note
        except Exception as exc:
            text = str(exc).strip()
            reason = text.splitlines()[0][:150] if text else type(exc).__name__
            self.failures.append(reason)
            return "", "screenshot failed: " + reason
        finally:
            self._deadline = None
            try:
                page.close()
            except Exception:
                pass

    def close(self) -> None:
        for close in (self._context.close, self._browser.close, self._pw.stop):
            try:
                close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# CSV output
# --------------------------------------------------------------------------
def _flag(value: Optional[bool]) -> str:
    return "" if value is None else ("TRUE" if value else "FALSE")


class CsvLogger:
    """Writes one row per checked URL immediately, so partial runs are never lost."""

    def __init__(self, path: str) -> None:
        self.path = path
        # Append, so several runs on the same day share one file (checked_at tells the runs apart).
        # utf-8-sig (BOM) only for a brand-new file, so Excel shows non-English titles correctly.
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        self._fh = open(path, "a", newline="", encoding="utf-8-sig" if is_new else "utf-8")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(CSV_COLUMNS)
            self._fh.flush()

    def write(self, r: CheckResult) -> None:
        self._writer.writerow([
            r.checked_at,
            r.url,
            r.status_code or "",
            r.final_url,
            "" if r.error else round(r.total_time * 1000),
            "" if r.error else r.size_bytes,
            r.title,
            _flag(r.title_ok),
            _flag(r.meta_ok),
            "" if r.broken_images is None else r.broken_images,
            " | ".join(r.broken_image_urls),
            "; ".join(r.problems),
        ])
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------
# Sitemap handling
# --------------------------------------------------------------------------
def _fetch_bytes(url: str, timeout: float, session: requests.Session, interval: float) -> bytes:
    """Throttled GET with manual redirects; returns the body or raises RequestException."""
    headers = {"User-Agent": USER_AGENT}
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _throttle(interval)
        resp = session.get(current, timeout=timeout, headers=headers, allow_redirects=False)
        if resp.is_redirect:
            current = urljoin(current, resp.headers["Location"])
            continue
        resp.raise_for_status()
        return resp.content
    raise requests.exceptions.TooManyRedirects(f"Too many redirects fetching {url}")


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _parse_sitemap(data: bytes) -> Tuple[bool, List[str]]:
    """Return (is_index, [loc, ...]) for a sitemap or sitemap-index document."""
    if data[:2] == b"\x1f\x8b":  # .xml.gz
        data = gzip.decompress(data)
    root = ET.fromstring(data)
    is_index = _local(root.tag) == "sitemapindex"
    locs = []
    for entry in root:                      # <url> or <sitemap>
        for child in entry:                 # direct <loc> only (ignores <image:loc>)
            if _local(child.tag) == "loc" and child.text and child.text.strip():
                locs.append(child.text.strip())
                break
    return is_index, locs


def get_sitemap_urls(sitemap_url: str, timeout: float, session: requests.Session,
                     interval: float, _depth: int = 0, _budget: Optional[list] = None) -> List[str]:
    """Download a sitemap (following sitemap indexes) and return its page URLs, de-duplicated."""
    if _budget is None:
        _budget = [MAX_CHILD_SITEMAPS]
    data = _fetch_bytes(sitemap_url, timeout, session, interval)
    is_index, locs = _parse_sitemap(data)

    if not is_index:
        urls = locs
    else:
        urls = []
        if _depth >= 2:
            return urls
        for child in locs:
            if _budget[0] <= 0:
                break
            _budget[0] -= 1
            try:
                urls.extend(get_sitemap_urls(child, timeout, session, interval, _depth + 1, _budget))
            except (requests.exceptions.RequestException, ET.ParseError, OSError) as exc:
                print(f"  ! skipped child sitemap {child}: {exc}")

    seen, unique = set(), []
    for u in urls:
        if urlparse(u).scheme in ("http", "https") and u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


# --------------------------------------------------------------------------
# Console output
# --------------------------------------------------------------------------
def print_result(prefix: str, r: CheckResult, show_url: bool = False) -> None:
    tail = f" | {r.url}" if show_url else ""
    if r.error:
        print(f"{prefix} FAIL  {r.error[:150]}{tail}")
        return
    state = "OK  " if r.ok else "FAIL"
    line = (f"{prefix} {state} {r.status_code} {r.reason} | "
            f"total: {r.total_time * 1000:.0f} ms | size: {r.size_bytes / 1024:.1f} KB"
            f"{tail}")
    if r.screenshot:
        line += "\n      screenshot: " + r.screenshot
    if r.problems:
        line += "\n      problem: " + "; ".join(r.problems)
    print(line)


def percentile(sorted_vals: List[float], pct: float) -> float:
    idx = min(len(sorted_vals) - 1, max(0, int(round(pct / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def print_summary(results: List[CheckResult], slow_limit: float, many: bool) -> None:
    successes = [r for r in results if not r.error]
    errors = len(results) - len(successes)

    print("\n--- Summary ---")
    print(f"Checked   : {len(results)} page(s)")

    if successes:
        codes = Counter(r.status_code for r in successes)
        print("Status    : " + ", ".join(f"{c} x{n}" for c, n in sorted(codes.items())))
        if errors:
            print(f"Errors    : {errors} (timeout / connection / SSL)")

        times = sorted(r.total_time for r in successes)
        line = (f"Load time : min {times[0] * 1000:.0f} ms | avg {statistics.mean(times) * 1000:.0f} ms | "
                f"median {statistics.median(times) * 1000:.0f} ms | ")
        if many:
            line += f"p95 {percentile(times, 95) * 1000:.0f} ms | "
        line += f"max {times[-1] * 1000:.0f} ms"
        print(line)

        if many:
            print("Slowest   :")
            for r in sorted(successes, key=lambda x: x.total_time, reverse=True)[:5]:
                print(f"  {r.total_time * 1000:6.0f} ms  {r.url}")
        else:
            print(f"Final URL : {successes[-1].final_url}")

        if statistics.mean(times) > slow_limit:
            print(f"WARNING   : average load time is above {slow_limit}s")
    else:
        print(f"Errors    : {errors} (timeout / connection / SSL)")

    pages = [r for r in results if r.is_html and r.ok]
    if pages:
        bad_title = sum(1 for r in pages if r.title_ok is False)
        bad_meta = sum(1 for r in pages if r.meta_ok is False)
        print(f"Titles    : {bad_title}/{len(pages)} pages with a title problem")
        print(f"Meta desc : {bad_meta}/{len(pages)} pages with a meta description problem")
        checked_imgs = [r for r in pages if r.broken_images is not None]
        if checked_imgs:
            total = sum(r.broken_images for r in checked_imgs)
            with_broken = sum(1 for r in checked_imgs if r.broken_images)
            print(f"Images    : {total} broken image(s) on {with_broken}/{len(checked_imgs)} pages")

    with_problems = [r for r in results if r.problems]
    print(f"Problems  : {len(with_problems)}/{len(results)} page(s) flagged")
    if many:
        for r in with_problems:
            print(f"  - {'; '.join(r.problems)}  <- {r.url}")


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def _key(url: str) -> str:
    """Comparison key so https://x.com/a and https://x.com/a/ count as the same page."""
    u = urlparse(url)
    return f"{u.scheme.lower()}://{u.netloc.lower()}{u.path.rstrip('/')}"


def run_sitemap(args, session, logger, shooter=None, image_cache: Optional[ImageCache] = None) -> List[CheckResult]:
    sitemap_url = normalize_url(args.sitemap)

    # pages that are checked on every run (de-duplicated, order kept)
    fixed, fixed_keys = [], set()
    for u in args.always or []:
        u = normalize_url(u)
        if _key(u) not in fixed_keys:
            fixed_keys.add(_key(u))
            fixed.append(u)

    print(f"Fetching sitemap: {sitemap_url}")
    urls: List[str] = []
    try:
        urls = get_sitemap_urls(sitemap_url, args.timeout, session, args.interval)
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: could not download sitemap: {exc}")
    except ET.ParseError as exc:
        print(f"ERROR: sitemap is not valid XML: {exc}")

    if not urls and not fixed:
        print("ERROR: no URLs to check.")
        return []
    if not urls:
        print("WARNING: no sitemap URLs available; checking only the fixed pages.")

    # random sample from the sitemap, excluding pages that are already in the fixed list
    pool = [u for u in urls if _key(u) not in fixed_keys]
    rng = random.Random()  # a different random sample every run
    if args.full:
        n, sample = len(pool), list(pool)          # the entire sitemap, in sitemap order
    else:
        n = min(args.sample, len(pool))
        sample = rng.sample(pool, n)
    queue = fixed + sample
    total = len(queue)

    # Quick mode screenshots only a few pages: the SHOT_PAGES in this run plus SHOT_RANDOM random ones
    # from the sitemap sample. (Full mode has no screenshots, so `shooter` is None there.)
    shot_urls = set()
    if shooter is not None:
        wanted = {_key(u) for u in SHOT_PAGES}
        shot_urls = {u for u in queue if _key(u) in wanted}
        others = [u for u in sample if u not in shot_urls]
        shot_urls |= set(rng.sample(others, min(SHOT_RANDOM, len(others))))

    if args.full:
        print(f"Sitemap has {len(urls)} URLs. Full mode: checking all {total}.")
    else:
        print(f"Sitemap has {len(urls)} URLs. Quick mode: {len(fixed)} fixed page(s) + {n} random = {total}.")
    extra = f" plus up to {args.max_images} new image checks per page" if args.max_images else ""
    secs = total * args.interval
    duration = f"{secs:.0f}s" if secs < 120 else f"about {secs / 60:.0f} min"
    print(f"One request at a time, >= {args.interval:g}s apart: at least {duration}{extra}.\n")
    if shooter:
        print(f"Phone screenshots ({VIEWPORT['width']}x{VIEWPORT['height']}): {len(shot_urls)} of {total} pages "
              f"-> {shooter.out_dir}\n"
              "(the browser's requests are throttled too, so those pages take longer).\n")

    results, cache = [], (image_cache if image_cache is not None else ImageCache())
    width = len(str(total))
    try:
        for i, u in enumerate(queue, 1):
            r = check_and_record(u, args, session, cache, logger, shooter if u in shot_urls else None)
            print_result(f"[{i:>{width}}/{total}]", r, show_url=True)
            results.append(r)
    except KeyboardInterrupt:
        print("\nInterrupted: showing results so far.")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check favekad.com pages: status, load time, title, meta description and images.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--quick", action="store_true",
                      help=f"{len(QUICK_FIXED_PAGES)} fixed pages + {QUICK_SAMPLE} random sitemap pages, "
                           "with phone screenshots of a few")
    mode.add_argument("--full", action="store_true",
                      help="every URL in the sitemap (no fixed pages, no screenshots)")
    flags = parser.parse_args()

    args = argparse.Namespace(                      # settings come from the constants at the top of the file
        full=flags.full,
        sitemap=DEFAULT_SITEMAP,
        always=[] if flags.full else list(QUICK_FIXED_PAGES),
        sample=QUICK_SAMPLE,
        timeout=REQUEST_TIMEOUT,
        interval=MIN_INTERVAL,
        slow=SLOW_SECONDS,
        max_images=MAX_IMAGES_PER_PAGE,
    )

    logger = CsvLogger(f"site-check-{datetime.now():%Y-%m-%d}.csv")

    shooter = None
    shots_off_reason = ""
    if not flags.full:                              # full mode never takes screenshots
        try:
            shooter = Screenshotter(SCREENSHOT_DIR, args.interval, SCREENSHOT_TIME_LIMIT)
        except ImportError:
            shots_off_reason = "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            print(f"Screenshots disabled: {shots_off_reason}\n")
        except Exception as exc:
            first = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:150]
            shots_off_reason = f"{first}  (try: playwright install chromium)"
            print(f"Screenshots disabled: {shots_off_reason}\n")

    image_cache = ImageCache()
    try:
        with requests.Session() as session:
            results = run_sitemap(args, session, logger, shooter, image_cache)
    finally:
        if shooter:
            shooter.close()
        logger.close()

    if not results:
        return 1

    print_summary(results, args.slow, many=True)
    if image_cache.checked or image_cache.hits:
        print(f"Image checks: {image_cache.checked} unique image URL(s) requested once each (HEAD first), "
              f"{image_cache.hits} repeat(s) answered from the cache")
    if shooter:
        saved = sum(1 for r in results if r.screenshot)
        print(f"Screenshots: {saved} saved in {shooter.out_dir} "
              f"({shooter.network_requests} browser requests, {shooter.cache_hits} served from cache)")
        if shooter.failures:
            print(f"Screenshots: {len(shooter.failures)} FAILED, first error: {shooter.failures[0]}")
        pages = shooter.cookie_taps + shooter.cookie_missed
        if pages:
            note = ""
            if not shooter.cookie_taps:
                note = " (no 'No thanks' button was found: the banner uses different wording, or none was shown)"
            print(f"Cookie banner: tapped 'No thanks' on {shooter.cookie_taps} of {pages} screenshot page(s){note}")
    elif shots_off_reason:
        print(f"Screenshots: NONE TAKEN - {shots_off_reason}")
    print(f"CSV saved : {logger.path}")

    # Exit code 0 only if every page loaded successfully (status < 400)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())