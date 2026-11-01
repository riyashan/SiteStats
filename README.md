# Site Checker

A Python 3 tool that checks favekad.com pages with `requests`: HTTP status, load time, title, meta description, redirects and images. Results go to the terminal and to a CSV file. In quick mode it also saves phone-size (390x844) screenshots of a few pages with Playwright.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium      # one-time browser download, used for --quick screenshots
```

## Usage

There are only two options, and you must pick one:

```bash
python site_checker.py --quick
python site_checker.py --full
```

| Mode | What it checks | Screenshots |
|---|---|---|
| `--quick` | The 7 fixed pages (below), then 50 random pages from `https://favekad.com/sitemap.xml`: 57 pages | 3 fixed pages + 3 random pages |
| `--full` | Every URL in the sitemap, in sitemap order. No fixed pages, no random sampling | None |

Both modes run the same checks, write the same CSV, and stop with Ctrl+C at any time without losing the rows so far (the summary is still printed).

A full run takes at least one second per page (the start of the run prints the estimate), plus image checks.

## Fixed pages (quick mode)

- https://favekad.com/
- https://favekad.com/gallery
- https://favekad.com/pricing
- https://favekad.com/deepavali-ecards
- https://favekad.com/ecards
- https://favekad.com/malaysian-ecards
- https://favekad.com/hari-raya-ecards

Screenshots are taken for home, gallery and deepavali-ecards, plus 3 random sitemap pages.

## Settings

Everything else is a constant at the top of `site_checker.py`. Edit the file to change them:

| Constant | Meaning | Default |
|---|---|---|
| `DEFAULT_SITEMAP` | Sitemap URL | `https://favekad.com/sitemap.xml` |
| `QUICK_FIXED_PAGES` | Pages checked on every quick run | the 7 pages above |
| `QUICK_SAMPLE` | Random sitemap pages in quick mode | 50 |
| `SHOT_PAGES` / `SHOT_RANDOM` | Quick-mode screenshot pages / random extras | 3 pages / 3 |
| `SLOW_SECONDS` | Flag pages slower than this | 3 |
| `TITLE_MIN` | Flag titles shorter than this many characters | 10 |
| `MAX_IMAGES_PER_PAGE` | New image requests per page (0 = skip image checks) | 10 |
| `REQUEST_TIMEOUT` | Seconds to wait for a page or image | 10 |
| `SCREENSHOT_DIR` | Screenshot folder | `screenshots` |
| `SCREENSHOT_TIME_LIMIT` | Seconds a page may keep loading files before its screenshot | 60 |

## CSV

Written to `site-check-YYYY-MM-DD.csv` in the folder you run the script from. If you run it again the same day, rows are added to the same file; use `checked_at` to tell runs apart. Rows are written as each page finishes.

`checked_at,url,status,final_url,load_ms,bytes,title,title_ok,meta_description_ok,broken_images,broken_image_urls,problem`

| Column | Meaning |
|---|---|
| `checked_at` | Local time of the check, `YYYY-MM-DD HH:MM:SS` (no timezone) |
| `url` | URL taken from the sitemap or fixed list |
| `status` | Final HTTP status code (blank if the request failed) |
| `final_url` | URL after following redirects |
| `load_ms` | Time to download the HTML in milliseconds (excludes rate-limit waiting) |
| `bytes` | Size of the HTML response |
| `title` | Text of the `<title>` tag |
| `title_ok` | `TRUE` if the page has a non-empty `<title>` of at least 10 characters |
| `meta_description_ok` | `TRUE` if a non-empty `<meta name="description">` exists |
| `broken_images` | Number of `<img src>` and `og:image` URLs that do not return 200 (blank if image checks are off) |
| `broken_image_urls` | Those URLs, separated by ` \| ` |
| `problem` | Everything flagged for the row, separated by `; ` (blank = no problems) |

## What gets flagged

| Flag text | When |
|---|---|
| `HTTP 404 Not Found` (any code) | the final status is not 200 |
| connection / timeout / SSL message | the request failed |
| `redirected (1 hop)` | the URL redirected before reaching the final page (see `final_url`) |
| `slow load (3.4s)` | load time over 3 seconds |
| `missing title` | no `<title>` tag |
| `empty title` | `<title>` exists but is blank |
| `title too short (5 chars)` | title shorter than 10 characters |
| `missing meta description` | no `<meta name="description">`, or its content is empty |
| `3 broken image(s) (incl. og:image)` | an `<img src>` or `og:image` URL does not return 200 |

Title, meta description and image checks run only on pages that return 200. Quick mode may also add a screenshot note (partial or failed screenshot).

## Images

- Every unique image URL is requested once per run and the result is cached. A logo used on 50 pages costs one request, and if it is broken it is reported on every page that uses it.
- HEAD is used first. GET (headers only, no body) is used only if the server refuses HEAD (400, 403, 405 or 501). Image bodies are never downloaded.
- Redirects are followed. Only the final status matters: it must be 200. Content type is not checked.
- URLs match ignoring the `#fragment`, upper/lower case in the host, and default ports (`:80`, `:443`).
- `MAX_IMAGES_PER_PAGE` limits new requests per page; cached images do not count. `og:image` is checked first.
- The end of a run prints how many unique images were requested and how many repeats came from the cache.

## Screenshots (quick mode)

- Pages are opened in headless Chromium as a phone: 390x844 viewport, touch and mobile mode, scale 1, so each PNG is exactly 390x844.
- Files are saved as `screenshots/<date>-<page>.png`, for example `screenshots/2026-10-31-home.png`. Running again the same day overwrites the same page's file.
- Only pages that return 200 are screenshotted. The CSV has no screenshot column.
- Each page has a time limit (`SCREENSHOT_TIME_LIMIT`, 60 s). When it runs out the browser skips the files it has not loaded yet, the screenshot is saved with what has loaded, and `problem` says how many files were skipped.
- If Playwright or its browser is missing, the tool prints why, skips screenshots and carries on with the checks.

**Screenshots not saved?**

1. Look at the start and the end of the run for a line beginning `Screenshots`. It gives the full folder path or the reason none were taken.
2. `Playwright is not installed`: run `pip install playwright`, then `playwright install chromium`.
3. `Executable doesn't exist`: run `playwright install chromium` with the same Python you use for the script.
4. Files go to the `screenshots` folder inside the folder you ran the script from.
5. `--full` never takes screenshots.

## Rate limiting

- Requests are sent strictly one at a time (no threads or parallelism).
- At most 1 request per second, with a 0.05 s safety margin (real spacing is about 1.05 s).
- The sitemap download, every redirect hop, every image check and every request the screenshot browser makes count as requests.
- CSS, JavaScript, fonts and images the browser has already loaded are reused on later pages instead of being requested again.
- Sends `User-Agent: FaveKad-QA/1.0 (Roshan, for Redz)`.

## Notes

- "Load time" is the HTML download only. It does not include images, CSS, JS or browser rendering.
- Title, meta description and images are read with BeautifulSoup (`html.parser`). The meta description is `<meta name="description">` only; `og:image` also covers `og:image:url` and `og:image:secure_url`.
- `requests` does not run JavaScript. If a page builds its title, meta tags or images in the browser, this tool only sees what the server sends.
- The exit code is `0` if every page returned a status below 400, otherwise `1`.
