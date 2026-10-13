# Twitter → Facebook Video Autoposter

A Python bot that scrapes the top videos from your X (Twitter) home timeline, ranks them by engagement, stitches them into one compilation, and uploads the result to a Facebook page. Runs headless, dedupes itself, and keeps a paper trail.

---

## What it actually does

Every time you run it, the script:

1. **Logs into X** using your exported `cookies.txt` (no API key, no developer account).
2. **Reads your home timeline** with a real headless Chromium browser — because X's timeline is client-rendered and plain HTTP requests see nothing useful.
3. **Parses engagement metrics** straight from each tweet's `aria-label` (the hidden element that has the *exact* counts, not the abbreviated "13.3K" you see on screen).
4. **Scores every tweet** with a weighted formula you can tune, then sorts highest-first.
5. **Skips anything already posted** — tracked in `twitter_used.txt`.
6. **Downloads the videos** with `yt-dlp`, using the same cookies for auth.
7. **Concatenates them** into a single MP4 with `moviepy`.
8. **Uploads to Facebook** via the Graph API.
9. **Records the used tweet URLs** so tomorrow's run doesn't repeat today's posts.

You can also skip all of that and just upload a local video with `--upload-file`.

---

## Why Playwright?

Because X's home timeline is a virtualized, JS-rendered list. There's no HTML in the page source, no JSON endpoint you can hit with `requests`, nothing. The tweets only exist in the DOM after JavaScript mounts them. So we drive a real browser.

## Why parse the `aria-label`?

Because the visible numbers lie. X shows you "13.3K views" but the accessibility label on that same element says `13380 views`. Exact counts, no rounding. It's the cleanest data source on the page, and it's right there in the DOM.

---

## Setup

### 1. Install dependencies

```bash
pip install playwright yt-dlp moviepy python-dotenv
playwright install chromium

### 2. Export your X cookies

Use a browser extension like [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/) while logged into `x.com`. Save the file as `cookies.txt` in the project root.

The script handles the `twitter.com` ↔ `x.com` domain split automatically, so either export works.

### 3. Create a `.env` file

```env
FB_ACCESS_TOKEN=your_facebook_page_token
FACEBOOK_ID=your_page_or_group_id
COOKIES_FILE=cookies.txt
USED_URLS_FILE=twitter_used.txt
SCORE_WEIGHTS={"likes": 1.0, "retweets": 2.0, "replies": 1.5, "bookmarks": 1.0, "views": 0.01}
```

`SCORE_WEIGHTS` is optional — leave it out and the defaults kick in.

### 4. Run it

```bash
# Scrape, rank, compile, upload (top 5 new videos)
python twitter2.py --count 5

# Upload a local file directly, skip all the scraping
python twitter2.py --upload-file my_video.mp4

# If X throws a bot challenge, run once with a visible browser
python twitter2.py --count 5 --show-browser

# Slow connection? Give it more time
python twitter2.py --count 5 --timeout 60000
```

---

## Command-line flags

| Flag | What it does |
| :--- | :--- |
| `--count N` | How many new videos to fetch (default: 5) |
| `--upload-file PATH` | Upload a local video, skip Twitter entirely |
| `--show-browser` | Run with a visible browser window (for debugging) |
| `--timeout MS` | How long to wait for the timeline to load (default: 45000) |
| `--cookies PATH` | Override the cookies file from `.env` |
| `--used-file PATH` | Override the used-URLs file from `.env` |
| `--fb-token TOKEN` | Override the Facebook token from `.env` |
| `--fb-id ID` | Override the Facebook page/group ID from `.env` |

---

## How scoring works

Each tweet gets a score based on five metrics:

```
score = likes×1.0 + retweets×2.0 + replies×1.5 + bookmarks×1.0 + views×0.01
```

Retweets count double because they're a stronger signal than a like. Views are weighted down heavily because raw view counts run 10–100× higher than everything else and would otherwise drown out the rest.

Tune the weights in `.env` under `SCORE_WEIGHTS` to match what "good" means for your feed. If you only care about likes, set everything else to 0. If you want viral reach, crank up views.

Example override:

```env
SCORE_WEIGHTS={"likes": 3.0, "retweets": 5.0, "replies": 1.0, "bookmarks": 0.5, "views": 0.001}
```

---

## The deduplication system

Every successfully uploaded tweet URL gets appended to `twitter_used.txt`. On the next run, those URLs are filtered out before downloading. So you never re-post the same video twice.

To reset: just delete or empty the file.

Note: `--upload-file` mode does **not** touch `twitter_used.txt`, since there's no tweet URL involved.

---

## What happens when things break

X is hostile to automation, so failures are expected. When the timeline never loads, the script doesn't just time out silently — it:

- Detects **login redirects** → tells you your cookies are expired
- Detects **bot challenges** → tells you to run with `--show-browser` once
- Detects **slow loads** → tells you to bump `--timeout`
- Saves a **screenshot** and **full HTML dump** to `debug/x_debug.png` and `debug/x_debug.html`

So when it fails, you get a real diagnosis, not a stack trace.

---

## Anti-detection

The scraper uses:
- A realistic desktop Chrome user agent
- `navigator.webdriver` patched to `undefined`
- The `AutomationControlled` blink feature disabled

It's not bulletproof — X's bot detection evolves constantly — but it removes the most obvious tells. If you're running this frequently, expect to occasionally need `--show-browser` to clear a challenge manually.

---

## Project layout

```
.
├── twitter2.py          # the whole thing
├── .env                 # your secrets (don't commit this)
├── cookies.txt          # your X session (definitely don't commit this)
├── twitter_used.txt     # auto-generated list of already-posted tweets
├── downloads_videos/    # temp dir for downloaded + compiled videos
└── debug/               # screenshots and HTML dumps when scraping fails
```

---

## Usage examples (copy-paste ready)

### Basic daily run

```bash
python twitter2.py --count 5
```

### Upload a pre-made compilation

```bash
python twitter2.py --upload-file ./my_compilation.mp4
```

### Debug a login issue

```bash
python twitter2.py --count 5 --show-browser --timeout 90000
```

### Override everything from CLI

```bash
python twitter2.py \
  --count 10 \
  --cookies ./alt_cookies.txt \
  --used-file ./alt_used.txt \
  --fb-token NEW_TOKEN_HERE \
  --fb-id 999999999999
```

### Run on a schedule (cron, every 6 hours)

```cron
0 */6 * * * cd /home/alpha/asura && /home/alpha/asura/.venv/bin/python twitter2.py --count 5 >> cron.log 2>&1
```

---

## Security notes

- **Add `.env` to `.gitignore`.** It contains your Facebook access token.
- **Never commit `cookies.txt`.** It's a full session for your X account — anyone with the file *is* you on X.
- Rotate your Facebook token if it ever leaks.
- Consider using a throwaway X account for this. Aggressive scraping can get accounts flagged.

A minimal `.gitignore`:

```
.env
*.env
cookies.txt
twitter_used.txt
downloads_videos/
debug/
__pycache__/
*.pyc
.venv/
cron.log
```

---

## Requirements

- Python 3.9+
- `playwright` + Chromium
- `yt-dlp`
- `moviepy`
- `python-dotenv`

---

## Known limitations

- **X changes its DOM constantly.** Selectors may break. If tweets stop being found, check `debug/x_debug.html` to see what X is actually serving now.
- **Rate limits are real.** Don't run this every 5 minutes on the same account unless you want a suspension.
- **Facebook Graph API video uploads** can be slow for large compilations. The script waits for curl to finish; if your compilation is huge, expect a long pause.
- **No retry logic on uploads.** If Facebook rejects a video, the tweet URLs aren't marked as used, so the next run will retry them — which is intentional, but worth knowing.
- **Facebook tokens expire.** Long-lived page tokens last ~60 days. If uploads suddenly fail with an auth error, that's probably why.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| `Redirected to a login page` | Cookies expired | Re-export `cookies.txt` while logged into x.com |
| `bot-verification challenge` | X flagged the headless browser | Run once with `--show-browser` and solve it manually |
| `Timed out waiting for tweets` | Slow load, or X changed markup | Bump `--timeout`, or check `debug/x_debug.html` |
| No videos downloaded | Tweets have no video media | Normal — those tweets get skipped silently |
| Facebook upload fails silently | Expired token, or bad page ID | Regenerate token, verify `FACEBOOK_ID` |
| `yt-dlp` fails on a tweet | Media requires login, or region-locked | Tweet is skipped; nothing you can do |

---

## License

MIT. Do what you want. Just don't blame me if X bans your account.
```
