#!/usr/bin/env python3
"""
Scrape the first N *new* videos from your X (Twitter) home timeline,
rank them by engagement score (highest first), concatenate, and upload to Facebook.

All secrets (Facebook token, IDs, file paths, weights) are loaded from a .env file.

Alternatively, use --upload-file to upload a local video directly.

Usage:
    # Normal scraping & compilation mode (score‑sorted)
    python twitter2.py --count 5

    # Direct upload mode (skip Twitter scraping)
    python twitter2.py --upload-file /path/to/video.mp4

Requires:
    pip install playwright yt-dlp moviepy python-dotenv
    playwright install chromium
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from moviepy import VideoFileClip, concatenate_videoclips

# ----------------------------------------------------------------------
# Load environment variables from .env
# ----------------------------------------------------------------------
load_dotenv()

FB_ACCESS_TOKEN = os.getenv("FB_ACCESS_TOKEN")
FACEBOOK_ID = os.getenv("FACEBOOK_ID")
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")
USED_URLS_FILE = os.getenv("USED_URLS_FILE", "twitter_used.txt")

# Parse SCORE_WEIGHTS from JSON string in .env, or fall back to defaults
_weights_env = os.getenv("SCORE_WEIGHTS")
if _weights_env:
    try:
        SCORE_WEIGHTS = json.loads(_weights_env)
    except json.JSONDecodeError:
        print("Warning: SCORE_WEIGHTS in .env is not valid JSON, using defaults.", file=sys.stderr)
        SCORE_WEIGHTS = {}
else:
    SCORE_WEIGHTS = {}

DEFAULT_SCORE_WEIGHTS = {
    "likes": 1.0,
    "retweets": 2.0,
    "replies": 1.5,
    "bookmarks": 1.0,
    "views": 0.01,
}
# merge: env weights override defaults
SCORE_WEIGHTS = {**DEFAULT_SCORE_WEIGHTS, **SCORE_WEIGHTS}


# ----------------------------------------------------------------------
# Validate required secrets
# ----------------------------------------------------------------------
def validate_env():
    """Ensure required env variables are present."""
    missing = []
    if not FB_ACCESS_TOKEN:
        missing.append("FB_ACCESS_TOKEN")
    if not FACEBOOK_ID:
        missing.append("FACEBOOK_ID")
    if missing:
        print(f"Error: Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        print("Please create a .env file with these values.", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------
# Cookie parsing (duplicates cookies across twitter.com/x.com)
# ----------------------------------------------------------------------
def _domain_variants(domain):
    """Duplicate cookie domains between twitter.com and x.com."""
    variants = {domain}
    if "twitter.com" in domain:
        variants.add(domain.replace("twitter.com", "x.com"))
    if "x.com" in domain:
        variants.add(domain.replace("x.com", "twitter.com"))
    return variants


def load_netscape_cookies(path):
    """
    Parse a Netscape cookies.txt file into Playwright cookie dicts.
    Handles the '#HttpOnly_<domain>' prefix and duplicates each cookie
    across both twitter.com and x.com domains.
    """
    cookies = []
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            http_only = False
            if line.startswith("#HttpOnly_"):
                http_only = True
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) != 7:
                continue

            domain, _include_subdomains, path_, secure, expiry, name, value = parts
            base = {
                "name": name,
                "value": value,
                "path": path_ or "/",
                "expires": int(expiry) if expiry.isdigit() and int(expiry) > 0 else -1,
                "httpOnly": http_only,
                "secure": secure.upper() == "TRUE",
                "sameSite": "Lax",
            }
            for dom_variant in _domain_variants(domain):
                cookies.append({**base, "domain": dom_variant})

    if not cookies:
        raise ValueError(f"No cookies parsed from {path}. Is it a valid Netscape cookies.txt?")
    return cookies


# ----------------------------------------------------------------------
# Playwright scraper with diagnostics
# ----------------------------------------------------------------------
LOGIN_URL_MARKERS = ("/login", "/i/flow/login", "/logout", "/account/access")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _diagnose_and_raise(page, cookies_path, debug_dir="debug"):
    """
    Called when tweets never appear. Inspects the page and raises a specific,
    actionable error – and saves a screenshot + HTML snapshot.
    """
    url = page.url
    try:
        html = page.content()
    except Exception:
        html = ""

    os.makedirs(debug_dir, exist_ok=True)
    shot_path = os.path.join(debug_dir, "x_debug.png")
    html_path = os.path.join(debug_dir, "x_debug.html")
    try:
        page.screenshot(path=shot_path, full_page=True)
    except Exception:
        shot_path = None
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        html_path = None

    if any(marker in url for marker in LOGIN_URL_MARKERS):
        reason = (
            f"Redirected to a login page ({url}) -- the cookies in '{cookies_path}' "
            "were not accepted. They're most likely expired, or were exported from "
            "the wrong domain. Re-export a fresh cookies.txt while logged into https://x.com."
        )
    elif "arkose" in html.lower() or "challenge" in url.lower() or "denied" in html.lower():
        reason = (
            "X appears to be showing a bot-verification challenge instead of the "
            "timeline. Try running with --show-browser once to solve it manually."
        )
    else:
        reason = (
            f"Timed out waiting for tweets to appear (page url: {url}). Try a longer "
            "--timeout, or run with --show-browser to see what's on screen."
        )

    debug_note = f"\nSaved a screenshot/HTML snapshot to '{debug_dir}/' for inspection." \
        if (shot_path or html_path) else ""
    raise RuntimeError(reason + debug_note)


# ----------------------------------------------------------------------
# Engagement parsing and scoring
# ----------------------------------------------------------------------
TWEET_SELECTOR = 'article[data-testid="tweet"]'
PERMALINK_SELECTOR = 'a[role="link"]:has(time)'
METRICS_SELECTOR = 'div[role="group"][aria-label*="views" i]'
STATUS_RE = re.compile(r"^https://(?:x|twitter)\.com/[^/]+/status/\d+/?$")

METRIC_PATTERNS = {
    "replies": re.compile(r'([\d,]+)\s+repl', re.I),
    "retweets": re.compile(r'([\d,]+)\s+repost', re.I),
    "likes": re.compile(r'([\d,]+)\s+like', re.I),
    "bookmarks": re.compile(r'([\d,]+)\s+bookmark', re.I),
    "views": re.compile(r'([\d,]+)\s+view', re.I),
}


def parse_metrics(aria_label):
    """Extract raw engagement counts from a combined-metrics aria-label string."""
    metrics = {}
    for key, pattern in METRIC_PATTERNS.items():
        m = pattern.search(aria_label or "")
        metrics[key] = int(m.group(1).replace(",", "")) if m else 0
    return metrics


def compute_score(metrics, weights=None):
    """Combine raw engagement counts into a single weighted score."""
    weights = weights or SCORE_WEIGHTS
    return round(sum(metrics.get(k, 0) * w for k, w in weights.items()), 2)


# ----------------------------------------------------------------------
# Main scraper function
# ----------------------------------------------------------------------
def get_top_tweets_with_score(cookies_path, count=5, weights=None, sort_by_score=False,
                              headless=True, timeout_ms=45000, max_scrolls=10):
    """
    Return up to `count` tweets from the home timeline, each as:
        {"link": str, "score": float, "metrics": {...}}
    """
    cookies = load_netscape_cookies(cookies_path)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            context.add_cookies(cookies)
            page = context.new_page()

            page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=timeout_ms)

            try:
                page.wait_for_selector(TWEET_SELECTOR, timeout=timeout_ms)
            except PlaywrightTimeoutError:
                _diagnose_and_raise(page, cookies_path)

            seen = set()
            for _ in range(max_scrolls):
                for article in page.query_selector_all(TWEET_SELECTOR):
                    anchor = article.query_selector(PERMALINK_SELECTOR)
                    if not anchor:
                        continue
                    href = anchor.get_attribute("href")
                    if not href:
                        continue
                    if href.startswith("/"):
                        href = "https://x.com" + href
                    if not STATUS_RE.match(href) or href in seen:
                        continue

                    metrics_el = article.query_selector(METRICS_SELECTOR)
                    aria_label = metrics_el.get_attribute("aria-label") if metrics_el else ""
                    metrics = parse_metrics(aria_label)
                    score = compute_score(metrics, weights)

                    seen.add(href)
                    results.append({
                        "link": href,
                        "score": score,
                        "metrics": metrics
                    })

                    if len(results) >= count:
                        break
                if len(results) >= count:
                    break
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(1000)
        finally:
            browser.close()

    results = results[:count]
    if sort_by_score:
        results.sort(key=lambda t: t["score"], reverse=True)
    return results


# ----------------------------------------------------------------------
# Video download (yt-dlp)
# ----------------------------------------------------------------------
def download_tweet_video(tweet_url, output_path, cookies_path):
    """Download the best video+audio from a tweet using yt-dlp."""
    cmd = [
        "yt-dlp",
        "--cookies", cookies_path,
        "-o", str(output_path),
        "--no-warnings",
        "--quiet",
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        tweet_url
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False


# ----------------------------------------------------------------------
# Upload to Facebook
# ----------------------------------------------------------------------
def upload_video(video_path, fb_token=None, fb_id=None, caption=None):
    """Upload a video to Facebook using curl."""
    fb_token = fb_token or FB_ACCESS_TOKEN
    fb_id = fb_id or FACEBOOK_ID
    if not fb_token or not fb_id:
        raise RuntimeError("Facebook token/ID not set. Check your .env file.")

    if caption is None:
        caption = "Follow for more #WildlifePhotography #NatureLovers #AnimalPlanet #EcoFriendly #NaturePhotography #WildlifeConservation #PetLovers #NatureInspiration #AnimalRescue #ExploreNature"
    cmd = [
        "curl", "-X", "POST",
        f"https://graph.facebook.com/v21.0/{fb_id}/videos",
        "-F", f"access_token={fb_token}",
        "-F", f"source=@{video_path}",
        "-F", f"description={caption}"
    ]
    subprocess.run(cmd, capture_output=True)
    print(f"Uploaded {video_path} to Facebook.")


# ----------------------------------------------------------------------
# Helper: read/write used URLs
# ----------------------------------------------------------------------
def load_used_urls(filename):
    """Return a set of URLs already processed (from file)."""
    if not os.path.exists(filename):
        return set()
    with open(filename, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_used_urls(filename, urls):
    """Append a list of URLs to the used file."""
    with open(filename, "a", encoding="utf-8") as f:
        for url in urls:
            f.write(url + "\n")


# ----------------------------------------------------------------------
# Main pipeline
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Download first N *new* videos from X home timeline, "
                    "rank by engagement (highest first), compile, and upload to Facebook. "
                    "Or use --upload-file to upload a local video directly."
    )
    parser.add_argument("--count", type=int, default=5, help="Number of *new* videos to fetch (default: 5)")
    parser.add_argument("--show-browser", action="store_true", help="Show browser window (debug)")
    parser.add_argument("--timeout", type=int, default=45000,
                        help="Timeout in ms for page load / tweets to appear (default: 45000)")
    parser.add_argument("--upload-file", help="Path to a local video file to upload directly (skips Twitter scraping)")
    parser.add_argument("--cookies", default=None, help="Override path to cookies.txt (defaults to COOKIES_FILE in .env)")
    parser.add_argument("--used-file", default=None, help="Override path to used URLs file (defaults to USED_URLS_FILE in .env)")
    parser.add_argument("--fb-token", default=None, help="Override Facebook access token")
    parser.add_argument("--fb-id", default=None, help="Override Facebook page/group ID")

    args = parser.parse_args()

    # Validate required env
    validate_env()

    # Resolve paths (command-line overrides .env)
    cookies_path = args.cookies or COOKIES_FILE
    used_file = args.used_file or USED_URLS_FILE
    fb_token = args.fb_token or FB_ACCESS_TOKEN
    fb_id = args.fb_id or FACEBOOK_ID

    # ------------------------------------------------------------------
    # DIRECT UPLOAD MODE
    # ------------------------------------------------------------------
    if args.upload_file:
        video_path = Path(args.upload_file)
        if not video_path.exists():
            print(f"Error: File '{video_path}' does not exist.", file=sys.stderr)
            sys.exit(1)
        upload_video(video_path, fb_token, fb_id)
        sys.exit(0)

    # ------------------------------------------------------------------
    # SCRAPE & COMPILE MODE
    # ------------------------------------------------------------------
    if not os.path.exists(cookies_path):
        print(f"Error: Cookies file '{cookies_path}' not found.", file=sys.stderr)
        sys.exit(1)

    # 1. Load already used URLs
    used_urls = load_used_urls(used_file)
    print(f"Loaded {len(used_urls)} previously used tweet URLs from '{used_file}'.")

    # 2. Fetch tweets with engagement data
    print(f"Fetching up to {args.count} tweets from home timeline with scores...")
    try:
        tweets_data = get_top_tweets_with_score(
            cookies_path,
            count=args.count,
            headless=not args.show_browser,
            timeout_ms=args.timeout,
            sort_by_score=False
        )
    except Exception as e:
        print(f"Error during scraping: {e}", file=sys.stderr)
        sys.exit(1)

    if not tweets_data:
        print("No tweets found on the homepage.")
        sys.exit(0)

    # 3. Filter out already used URLs
    new_tweets = [item for item in tweets_data if item["link"] not in used_urls]
    print(f"Found {len(tweets_data)} total, {len(new_tweets)} are new.")

    if not new_tweets:
        print("All visible tweets have already been processed. Exiting.")
        sys.exit(0)

    # 4. Sort new tweets by score descending
    new_tweets.sort(key=lambda x: x["score"], reverse=True)
    print("Sorted by engagement score (highest first):")
    for idx, item in enumerate(new_tweets, 1):
        m = item["metrics"]
        print(f"  #{idx}: {item['link']}  score={item['score']:.2f}  "
              f"(likes={m['likes']} retweets={m['retweets']} replies={m['replies']} "
              f"bookmarks={m['bookmarks']} views={m['views']})")

    # 5. Prepare download directory
    download_dir = Path("downloads_videos")
    if download_dir.exists():
        shutil.rmtree(download_dir)
    download_dir.mkdir(exist_ok=True)

    downloaded_paths = []
    successful_urls = []

    for i, item in enumerate(new_tweets):
        tweet_url = item["link"]
        rnd = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=4))
        out_file = download_dir / f"video_{i+1}_{rnd}.mp4"
        print(f"Downloading from {tweet_url} (score {item['score']:.2f}) ...")
        success = download_tweet_video(tweet_url, out_file, cookies_path)
        if success and out_file.exists() and out_file.stat().st_size > 0:
            downloaded_paths.append(out_file)
            successful_urls.append(tweet_url)
            print(f"  -> Downloaded: {out_file}")
        else:
            print(f"  -> No video found or download failed for {tweet_url}")

    if not downloaded_paths:
        print("No videos were successfully downloaded. Exiting.")
        sys.exit(0)

    # 6. Concatenate videos
    print(f"Concatenating {len(downloaded_paths)} videos in score order...")
    clips = [VideoFileClip(str(path)) for path in downloaded_paths]
    final_clip = concatenate_videoclips(clips, method="compose")
    output_video = download_dir / "final_compilation.mp4"
    final_clip.write_videofile(str(output_video), codec="libx264", audio_codec="aac")
    for c in clips:
        c.close()
    final_clip.close()
    print(f"Compilation saved to {output_video}")

    # 7. Upload to Facebook
    upload_video(output_video, fb_token, fb_id)

    # 8. Mark these URLs as used
    append_used_urls(used_file, successful_urls)
    print(f"Added {len(successful_urls)} new URLs to '{used_file}'.")


if __name__ == "__main__":
    main()
