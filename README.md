# Bakkei
 
Bakkei looks at the videos on your X (Twitter) home timeline, picks the ones people are reacting to most, stitches them into a single compilation, and posts it to a Facebook page.
 
It's one Python file. You don't need an X API key or a developer account, because it logs in with your browser cookies and reads the timeline the same way you would. It also keeps a list of what it has already posted, so you won't get the same clip twice.
 
## What happens when you run it
 
1. It opens a headless Chromium window and logs into X with your exported `cookies.txt`.
2. It scrolls your home timeline and collects tweets along with their engagement numbers.
3. It scores each tweet (formula below) and sorts them, best first.
4. It drops anything listed in `twitter_used.txt`.
5. It downloads the video from each remaining tweet with `yt-dlp`. Tweets without video are skipped.
6. It joins the clips into one MP4 with `moviepy`.
7. It uploads the result to Facebook through the Graph API.
8. It writes the tweet URLs to `twitter_used.txt` so tomorrow's run starts fresh.
If you already have a video you want to post, `--upload-file` skips all of that and just does step 7.
 
## Why a real browser?
 
X's home timeline is built in the browser by JavaScript. The page source is basically empty, and there's no simple endpoint to call with `requests`. The tweets only exist once a real browser has rendered them, so that's what Bakkei uses.
 
The engagement numbers come from each tweet's `aria-label`. On screen X shows something like "13.3K views", but the accessibility label has the exact figure (`13380 views`). It's the most accurate data on the page, so that's what gets parsed.
 
## Setup
 
You'll need Python 3.9+, plus `ffmpeg` and `curl` installed on your system.
 
**1. Install the Python packages**
 
```bash
pip install playwright yt-dlp moviepy python-dotenv
playwright install chromium
```
 
Bakkei uses the `moviepy` 2.x import style (`from moviepy import ...`), so make sure you're on version 2 or newer.
 
**2. Export your X cookies**
 
Log into x.com in your normal browser, then use an extension like "Get cookies.txt LOCALLY" to export a Netscape-format `cookies.txt`. Put it in the project folder. Either an `x.com` or `twitter.com` export works, since Bakkei copies each cookie to both domains.
 
**3. Create a `.env` file**
 
```env
FB_ACCESS_TOKEN=your_facebook_page_token
FACEBOOK_ID=your_page_or_group_id
COOKIES_FILE=cookies.txt
USED_URLS_FILE=twitter_used.txt
SCORE_WEIGHTS={"likes": 1.0, "retweets": 2.0, "replies": 1.5, "bookmarks": 1.0, "views": 0.01}
```
 
Only the first two are required. The rest have defaults. The token needs to be a Page access token that's allowed to publish videos to that page. Meta changes the exact permissions now and then, so check their Graph API docs if uploads get rejected.
 
**4. Run it**
 
```bash
python main.py --count 5
```
 
## Options
 
| Flag | What it does |
| --- | --- |
| `--count N` | How many tweets to pull from the timeline (default 5). See the note below. |
| `--upload-file PATH` | Upload a local video and skip X entirely. |
| `--show-browser` | Show the browser window. Useful for debugging or solving a bot check. |
| `--timeout MS` | How long to wait for the timeline to load (default 45000). |
| `--cookies PATH` | Use a different cookies file than the one in `.env`. |
| `--used-file PATH` | Use a different used-URLs file than the one in `.env`. |
| `--fb-token TOKEN` | Override the Facebook token. |
| `--fb-id ID` | Override the Facebook page or group ID. |
 
A few examples:
 
```bash
# Normal run
python main.py --count 5
 
# Post a video you already made
python main.py --upload-file ./my_compilation.mp4
 
# X is being difficult, so watch what the browser is doing
python main.py --count 5 --show-browser --timeout 90000
```
 
To run it on a schedule, a cron line like this works (every 6 hours):
 
```
0 */6 * * * cd /path/to/Bakkei && ./.venv/bin/python main.py --count 5 >> cron.log 2>&1
```
 
## How scoring works
 
Each tweet gets one number:
 
```
score = likes×1.0 + retweets×2.0 + replies×1.5 + bookmarks×1.0 + views×0.01
```
 
Reposts count double because sharing something is a stronger signal than liking it. Views are scaled way down because they're usually 10 to 100 times bigger than every other number and would drown out the rest.
 
You can change any of this with `SCORE_WEIGHTS` in `.env`. If you only care about likes, set the others to 0. If you want reach, raise `views`. Any weight you leave out falls back to its default.
 
```env
SCORE_WEIGHTS={"likes": 3.0, "retweets": 5.0, "replies": 1.0, "bookmarks": 0.5, "views": 0.001}
```
 
## Not posting the same thing twice
 
After a run finishes, the tweet URLs that were downloaded get appended to `twitter_used.txt`. Next time, those are filtered out before any downloading happens. To start over, delete the file or empty it.
 
`--upload-file` doesn't touch this list, since there's no tweet involved.
 
## Things worth knowing
 
These are the rough edges I'd want to know about before running it unattended.
 
- **`--count` counts tweets, not videos.** Bakkei takes the first N tweets it sees on the timeline, throws out the ones you've already used, and then tries to download video from what's left. Text-only and image tweets just get skipped. So `--count 5` can easily give you fewer than five clips. Ranking also only happens within that batch, not across your whole timeline.
- **A failed upload can still mark tweets as used.** The upload runs through `curl`, and the script doesn't check whether Facebook accepted the video before saving the URLs. If an upload fails (expired token, wrong ID), check Facebook before assuming it went through, and remove the URLs from `twitter_used.txt` if you want to retry them.
- **The caption is hard-coded.** It's a hashtag line set in `upload_video()`. There's no flag or `.env` setting for it yet, so edit the default in `main.py` to match what you're posting.
- **Clips are padded, not cropped.** When clips have different sizes, they're combined onto the largest canvas, so you'll see black bars on the smaller ones.
- **`downloads_videos/` is wiped on every run.** Don't keep anything in there.
- **Limited scrolling.** It scrolls the timeline up to 10 times looking for tweets, then stops.
- **Facebook tokens expire.** Long-lived page tokens last roughly 60 days. If uploads suddenly stop working, start there.
- **X changes its markup a lot.** If tweets stop being found, the selectors at the top of the scraper section probably need an update.
## When something breaks
 
X doesn't love automation, so errors are normal. When the timeline never loads, Bakkei looks at the page and tells you what it thinks went wrong, then saves `debug/x_debug.png` and `debug/x_debug.html` so you can see for yourself.
 
| What you see | Probably means | Try this |
| --- | --- | --- |
| Redirected to a login page | Cookies expired or came from the wrong site | Export a fresh `cookies.txt` while logged into x.com |
| Bot-verification challenge | X flagged the headless browser | Run once with `--show-browser` and solve it by hand |
| Timed out waiting for tweets | Slow connection, or X changed its markup | Raise `--timeout`, then look at `debug/x_debug.html` |
| No videos downloaded | None of the tweets had video | Normal, so just run it again later |
| `yt-dlp` fails on a tweet | The video needs login or is region-locked | That tweet is skipped |
| Upload seems to do nothing | Bad token, bad page ID, or a rejected file | Check the token and `FACEBOOK_ID`, and see the note above about failed uploads |
 
## About the anti-detection bits
 
The browser uses a normal desktop Chrome user agent, hides `navigator.webdriver`, and turns off Chromium's `AutomationControlled` flag. That clears the most obvious giveaways, but it won't beat X forever. If you run this often, expect to babysit it with `--show-browser` once in a while.
 
## Please read this part
 
- **Other people made these videos.** Reposting someone's clip to your page without permission can get you copyright strikes or your page removed, and it isn't fair to the creator. At the very least, credit them. Better, only use it on content you have the right to share.
- **This breaks X's terms.** Scraping with session cookies is against X's rules, and accounts get suspended for it. A throwaway account is safer than your main one, and don't run it every few minutes.
- **Protect your secrets.** `cookies.txt` is a full login for your X account, and `.env` holds your Facebook token. Never commit either one. Rotate the token if it leaks.
A `.gitignore` that covers it:
 
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
 
## What's in the folder
 
```
.
├── main.py              # the whole thing
├── .env                 # your secrets (not committed)
├── cookies.txt          # your X session (not committed)
├── twitter_used.txt     # created automatically, tweets already posted
├── downloads_videos/    # temporary clips and the final compilation
└── debug/               # screenshots and HTML dumps from failed runs
```
 
## License
 
MIT. Use it however you like, and don't blame me if X bans your account.
