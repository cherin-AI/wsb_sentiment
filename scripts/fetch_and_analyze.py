"""
WSB Sentiment Fetcher + Claude Analyzer
Runs daily at 08:00 HKT via GitHub Actions
Output: docs/sentiment.json (served by GitHub Pages)

HOW METRICS ARE CALCULATED
──────────────────────────
Source: Reddit /top?t=day — today's posts pre-sorted by upvotes (same as
        reddit.com/r/wallstreetbets/top/?t=day). Up to 300 posts fetched.

Claude's job: classify each post as bullish/bearish/neutral.
Python's job: calculate every metric from those classifications.

  bull_pct        = Σ upvotes(bullish posts) / Σ upvotes(all posts) × 100
  bear_pct        = Σ upvotes(bearish posts) / Σ upvotes(all posts) × 100
  neutral_pct     = 100 − bull_pct − bear_pct
  bull_bear_ratio = bull upvotes / bear upvotes  (min denominator = 1)
  overall_score   = bull_pct  (upvote-weighted bullish sentiment)
  overall_label   = Extreme Greed ≥80 | Greed ≥60 | Neutral ≥40 | Fear ≥20 | Extreme Fear <20
"""

import os
import re
import json
import subprocess
import requests
import anthropic
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # GitHub Actions injects secrets as env vars; dotenv only needed locally

HKT          = timezone(timedelta(hours=8))
OUTPUT_PATH  = os.path.join(os.path.dirname(__file__), "..", "docs", "sentiment.json")
HISTORY_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "history.json")
HISTORY_MAX  = 180  # 90 days at 2x/day

REDDIT_UA    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
REDDIT_HDR   = {"User-Agent": REDDIT_UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"}
REDDIT_BASE  = "https://oauth.reddit.com"
REDDIT_AUTH  = "https://www.reddit.com/api/v1/access_token"

HOURS_WINDOW = 24  # fetch all posts from the last 24 hours
RSS_PAGE_LIMIT = 100
RSS_MAX_POSTS = 200
COMMENT_FETCH_BUDGET_S = 180  # max wall-clock seconds for the per-post comment loop


# Matches individual P&L brags: "2100% gains", "up 400%", "$1M+", "printing 30x", etc.
_PNL_RE = re.compile(
    r'\b\d[\d,]*[xX%]\+?\s*(gain|gains|return|returns|profit|up|pump|print|printing)s?\b'
    r'|\b(up|down|gained|lost|printing|printed)\s+\d[\d,]*[xX%]'
    r'|\$\s*\d[\d,.]+\s*[KkMmBb]\+?\s*(gain|loss|profit|post|trade|return)s?'
    r'|\bMultiple\s+\$\d'
    r'|\b\d[\d,]*%\+?\s*(gain|gains|return|returns|profit)s?',
    re.IGNORECASE,
)

def _clean_pnl(text: str) -> str:
    return re.sub(_PNL_RE, lambda m: '[market momentum]', text)


_PROFESSIONAL_REPLACEMENTS = (
    (re.compile(r"\bfugazi\b", re.IGNORECASE), "questionable"),
    (re.compile(r"\bYOLO\b", re.IGNORECASE), "high-conviction"),
    (re.compile(r"\bcowboys?\b", re.IGNORECASE), "speculative traders"),
    (re.compile(r"\bscreams?\b", re.IGNORECASE), "warns"),
    (re.compile(r"\bprints money\b", re.IGNORECASE), "generates strong cash flow"),
    (re.compile(r"\brocket ship\b", re.IGNORECASE), "upside momentum"),
)


def _professionalize_text(text: str) -> str:
    for pattern, replacement in _PROFESSIONAL_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text

def sanitize_claude_result(result: dict) -> dict:
    """Strip individual P&L figures from bullets, signals, and summary."""
    for theme in result.get("themes", []):
        theme["title"] = _professionalize_text(theme.get("title", ""))
        theme["bullets"] = [_professionalize_text(_clean_pnl(b)) for b in theme.get("bullets", [])]
        normalize_theme_heat(theme)
    for ticker in result.get("tickers", []):
        ticker["signal"] = _professionalize_text(_clean_pnl(ticker.get("signal", "")))
    if "summary" in result:
        if isinstance(result["summary"], list):
            for item in result["summary"]:
                if isinstance(item, dict) and "text" in item:
                    item["text"] = _professionalize_text(_clean_pnl(item["text"]))
        else:
            result["summary"] = _professionalize_text(_clean_pnl(result["summary"]))
    return result


def normalize_theme_heat(theme: dict) -> None:
    """Validate the AI's heat label and fold legacy synonyms onto the current
    vocabulary (hot / building / normal). "cooling" is retired: quiet themes
    carry no badge, so cooling and its legacy synonyms fold to "normal"."""
    heat = (theme.get("heat") or "").lower()
    synonyms = {
        "hot":      "hot",
        "building": "building",
        "rising":   "building",   # legacy label
    }
    theme["heat"] = synonyms.get(heat, "normal")


# ── Reddit ───────────────────────────────────────────────────────────────────

def curl_get_json(url: str, params: dict | None = None, retries: int = 4) -> dict:
    """Fetch a Reddit JSON endpoint via subprocess curl (bypasses TLS fingerprint block)."""
    import time
    if params:
        url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    for attempt in range(retries):
        result = subprocess.run(
            [
                "curl", "-sL",
                "-H", f"User-Agent: {REDDIT_UA}",
                "-H", "Accept: application/json",
                "-H", "Accept-Language: en-US,en;q=0.9",
                url,
            ],
            capture_output=True, text=True, timeout=20,
        )
        result.check_returncode()
        if result.stdout.lstrip().lower().startswith(("<!doctype", "<html", "<body")):
            raise RuntimeError(f"Reddit returned HTML instead of JSON. Response: {result.stdout[:200]}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  Reddit returned non-JSON (attempt {attempt + 1}/{retries}), retrying in {wait}s…")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Reddit returned non-JSON after {retries} attempts. Response: {result.stdout[:200]}")


def curl_get_text(url: str, retries: int = 3) -> str:
    """Fetch a text endpoint via curl using the same browser-like profile."""
    import time
    for attempt in range(retries):
        result = subprocess.run(
            [
                "curl", "-sL",
                "-H", f"User-Agent: {REDDIT_UA}",
                "-H", "Accept: application/atom+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
                url,
            ],
            capture_output=True, text=True, timeout=20,
        )
        result.check_returncode()
        if result.stdout.strip() and not result.stdout.lstrip().lower().startswith(("<body", "<!doctype", "<html")):
            return result.stdout
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Empty response from {url}")


def get_reddit_token(client_id: str, client_secret: str) -> str:
    r = requests.post(
        REDDIT_AUTH,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        headers=REDDIT_HDR,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def extract_post(pd: dict) -> dict:
    return {
        "id":           pd["id"],
        "title":        pd.get("title", ""),
        "score":        max(pd.get("score", 0), 0),
        "num_comments": pd.get("num_comments", 0),
        "selftext":     pd.get("selftext", "")[:300],
        "flair":        pd.get("link_flair_text", ""),
        "top_comments": [],
    }


def fetch_wsb_posts_authenticated(token: str) -> list[dict]:
    """Paginate /new (with comments), stop when posts exceed 24h window."""
    import time
    cutoff  = time.time() - HOURS_WINDOW * 3600
    headers = {"Authorization": f"Bearer {token}", "User-Agent": REDDIT_UA}
    all_posts, after = [], None

    while True:
        params = {"limit": 100, **({"after": after} if after else {})}
        r = requests.get(f"{REDDIT_BASE}/r/wallstreetbets/new",
                         headers=headers, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()["data"]

        hit_cutoff = False
        for child in data["children"]:
            pd = child["data"]
            if pd.get("created_utc", 0) < cutoff:
                hit_cutoff = True
                break
            post = extract_post(pd)
            cr = requests.get(
                f"{REDDIT_BASE}/r/wallstreetbets/comments/{pd['id']}",
                headers=headers,
                params={"limit": 5, "depth": 1, "sort": "top"},
                timeout=15,
            )
            if cr.ok:
                try:
                    post["top_comments"] = [
                        c["data"].get("body", "")[:200]
                        for c in cr.json()[1]["data"]["children"]
                        if c["kind"] == "t1"
                    ][:5]
                except Exception:
                    pass
            all_posts.append(post)

        after = data.get("after")
        if hit_cutoff or not after:
            break

    return all_posts


def fetch_comments(post_id: str) -> list[str]:
    """Fetch top 5 comments for a post via public JSON API (no OAuth needed)."""
    try:
        data = curl_get_json(
            f"https://www.reddit.com/r/wallstreetbets/comments/{post_id}.json",
            {"limit": 5, "depth": 1, "sort": "top"},
        )
        return [
            c["data"].get("body", "")[:200]
            for c in data[1]["data"]["children"]
            if c["kind"] == "t1"
        ][:5]
    except Exception:
        return []


def fetch_wsb_posts_unauthenticated() -> list[dict]:
    """Paginate public /new.json, stop when posts exceed 24h window, then fetch comments."""
    import time
    cutoff    = time.time() - HOURS_WINDOW * 3600
    all_posts, after = [], None

    while True:
        params = {"limit": 100, **({"after": after} if after else {})}
        data = curl_get_json("https://www.reddit.com/r/wallstreetbets/new.json", params)["data"]

        hit_cutoff = False
        for child in data["children"]:
            pd = child["data"]
            if pd.get("created_utc", 0) < cutoff:
                hit_cutoff = True
                break
            all_posts.append(extract_post(pd))

        after = data.get("after")
        if hit_cutoff or not after:
            break

    # Fetch top comments for each post
    print(f"  Fetching comments for {len(all_posts)} posts…")
    for post in all_posts:
        post["top_comments"] = fetch_comments(post["id"])

    return all_posts


def _html_to_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<(br|/p|/li|/div|/tr)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_wsb_posts_rss() -> list[dict]:
    """Fallback to Reddit's Atom feed when public .json endpoints return HTML."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    posts, seen_ids, after = [], set(), None

    while len(posts) < RSS_MAX_POSTS:
        url = f"https://www.reddit.com/r/wallstreetbets/new/.rss?limit={RSS_PAGE_LIMIT}"
        if after:
            url += f"&after={after}"

        feed = curl_get_text(url)
        root = ET.fromstring(feed)
        entries = root.findall("atom:entry", ns)
        if not entries:
            break

        hit_cutoff = False
        last_full_id = None
        for entry in entries:
            full_id = entry.findtext("atom:id", default="", namespaces=ns)
            last_full_id = full_id
            if full_id in seen_ids:
                continue
            seen_ids.add(full_id)

            published_text = entry.findtext("atom:published", default="", namespaces=ns)
            try:
                published = datetime.fromisoformat(published_text.replace("Z", "+00:00"))
            except ValueError:
                published = datetime.now(timezone.utc)
            if published < cutoff:
                hit_cutoff = True
                break

            post_id = full_id.replace("t3_", "")
            content = entry.findtext("atom:content", default="", namespaces=ns)
            posts.append({
                "id": post_id,
                "title": entry.findtext("atom:title", default="", namespaces=ns),
                "score": 0,
                "num_comments": 0,
                "selftext": _html_to_text(content)[:300],
                "flair": "",
                "top_comments": [],
            })
            if len(posts) >= RSS_MAX_POSTS:
                break

        if hit_cutoff or not last_full_id or len(entries) < RSS_PAGE_LIMIT:
            break
        after = last_full_id

    print(f"  Fetching comments for {len(posts)} RSS posts...")
    import time
    deadline = time.monotonic() + COMMENT_FETCH_BUDGET_S
    fetched = 0
    for post in posts:
        if time.monotonic() > deadline:
            print(f"  Comment budget ({COMMENT_FETCH_BUDGET_S}s) exhausted after "
                  f"{fetched}/{len(posts)} posts — continuing without remaining comments")
            break
        post["top_comments"] = fetch_comments_rss(post["id"])
        fetched += 1

    return posts


def fetch_comments_rss(post_id: str) -> list[str]:
    """Fetch top comments from a post's Atom feed when JSON comments are blocked."""
    try:
        feed = curl_get_text(f"https://www.reddit.com/r/wallstreetbets/comments/{post_id}/.rss?sort=top", retries=2)
        root = ET.fromstring(feed)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        comments = []
        for entry in root.findall("atom:entry", ns):
            title = entry.findtext("atom:title", default="", namespaces=ns)
            if "AutoModerator" in title:
                continue
            if not title.startswith("/u/"):
                continue
            text = _html_to_text(entry.findtext("atom:content", default="", namespaces=ns))
            if text:
                comments.append(text[:200])
            if len(comments) >= 5:
                break
        return comments
    except Exception:
        return []




def lookup_company_name(symbol: str) -> str:
    """Look up company short name from Yahoo Finance search API."""
    try:
        result = subprocess.run([
            "curl", "-sL",
            "-H", f"User-Agent: {REDDIT_UA}",
            f"https://query1.finance.yahoo.com/v1/finance/search?q={symbol}&quotesCount=1&newsCount=0&listsCount=0",
        ], capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        quotes = data.get("quotes", [])
        if quotes:
            return quotes[0].get("shortname", "") or quotes[0].get("longname", "")
    except Exception:
        pass
    return ""


def enrich_tickers_with_names(tickers: list[dict]) -> list[dict]:
    for ticker in tickers:
        symbol = ticker.get("symbol", "")
        ticker["company_name"] = lookup_company_name(symbol)
    return tickers


def normalize_theme_tickers(themes: list[dict]) -> list[dict]:
    for theme in themes:
        theme["tickers"] = list(theme.get("tickers", []))
    return themes


def posts_to_text(posts: list[dict]) -> str:
    lines = []
    for i, p in enumerate(posts, 1):
        lines.append(
            f"[{i}] {p['title']} "
            f"(upvotes:{p['score']} comments:{p['num_comments']} flair:{p['flair'] or 'none'})"
        )
        if p["selftext"].strip():
            lines.append(f"    body: {p['selftext']}")
        for c in p.get("top_comments", []):
            lines.append(f"    > {c}")
    return "\n".join(lines)


# ── Claude ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior financial market sentiment analyst with more than 10 years of experience covering retail trading flows, equity narratives, options speculation, and market psychology.
Output ONLY a valid JSON object — no markdown, no explanation.

Your job has three parts:

PART 1 — Per-post classification (one entry per post, in the same order as input):
  - sentiment: "bullish" if the post/comments express buying enthusiasm or gains;
               "bearish" if expressing fear, shorts, or losses;
               "neutral" if mixed, unclear, or off-topic.
  Note: read titles AND comments together. WSB uses heavy irony and sarcasm —
  a loss post titled "I'm retired" is bearish. "Bears are fucked" is bullish.

PART 2 — Holistic sentiment score:
  - overall_sentiment_score: integer 0–100 representing your qualitative read of
    the ENTIRE batch's greed/fear level after reading everything.
    Consider: tone, sarcasm, which tickers dominate, are people buying or selling,
    is the energy euphoric or defeated?
    0 = Extreme Fear, 50 = Neutral, 100 = Extreme Greed.
    This is your AI judgment — not a formula. Be honest, not generous.

PART 3 — Qualitative analysis (themes, tickers, narrative):
  - themes: exactly 6 dominant topics ordered hottest first. Always return exactly 6 — combine minor themes or add a broader catch-all theme (e.g. "General Market Mood") if needed to reach 6
  - tickers: top 8 by mention count across all posts + comments
  - summary: 2–3 sentence professional market narrative

JSON schema:
{
  "post_classifications": [
    {"id": "<post index 1-N>", "sentiment": "bullish|bearish|neutral"}
  ],
  "overall_sentiment_score": <integer 0-100>,
  "themes": [
    {
      "id": "<slug>",
      "title": "<display name>",
      "icon": "<tabler icon name e.g. cpu, rocket, flame, trending-up, skull>",
      "heat": "hot|building|normal",
      "bullets": ["<bullet 1 ≤8 words>", "<bullet 2>", "<bullet 3>", "<bullet 4>"],
      "sentiment_score": <integer 0-100, your qualitative read of this theme's bullishness>,
      "tickers": ["TICKER", ...]
    }
  ],
  "tickers": [
    {
      "symbol": "<ticker>",
      "sentiment": "bullish|bearish|mixed",
      "mentions": <integer, count across all posts+comments>,
      "signal": "<one short phrase describing WSB attitude>"
    }
  ],
  "summary": [
    {"text": "<1-3 sentences, professional market commentary, vivid and specific, 30-50 words>", "tag": "bullish|bearish|notable"},
    {"text": "<1-3 sentences, professional market commentary, vivid and specific, 30-50 words>", "tag": "bullish|bearish|notable"},
    {"text": "<1-3 sentences, professional market commentary, vivid and specific, 30-50 words>", "tag": "bullish|bearish|notable"}
  ]
}

Rules:
- heat: classify each theme's energy as "hot", "building", or "normal", judging only from this snapshot's posts and comments. Never decide from keyword matching, and never infer heat from a theme's position in the list.
    "hot"      = clearly intense engagement right now: the theme is actively moving trades, drawing piled-on reactions, or driven by a live catalyst (geopolitical shock, earnings, squeeze, mass losses) that traders are positioning around. Culturally sticky topics run hot regardless of direction.
    "building" = energy gathering: fresh interest, emerging speculation, new positions being opened, a catalyst starting to draw attention, discussion spreading across posts — no matter how small the theme is.
    "normal"   = everything else: baseline discussion without unusual intensity or fresh momentum. There is no "cooling" label; quiet or fading themes are simply "normal". When unsure, choose "normal".
  Heat measures attention and energy, NOT bullishness — a painful or bearish theme can be "hot" or "building" if people are piling into it.
- writing style: write like a financial specialist, not a forum participant. Keep the analysis concise, polished, and market-literate. Avoid casual forum slang; translate it into professional language such as "questionable", "speculative traders", "warns", "strong cash flow", "upside momentum", or "high-conviction".
- bullets: exactly 4 per theme, concise professional market language, no fluff
- tickers: top 8 by mention count only
- summary: exactly 3 items. tag must be one of: "bullish" (positive momentum, buying energy), "bearish" (fear, selling, losses), "notable" (key observation, neutral but important)
- Do not invent post_classifications entries — one per input post, in order
- Avoid provocative or explicit WSB slang in all output text (theme titles, bullets, summary, signals). For large loss screenshots, use "trading losses", "loss posts", or "account blow-ups". For large profit screenshots, use "profit posts" or "big wins".
- NEVER include specific individual P&L figures anywhere — no percentage gains/losses (e.g. "up 2100%", "printing 2000% gains"), no dollar amounts tied to individual trades (e.g. "$1M+ gain posts", "$50k loss"), no "X gains", "X returns" for specific people or tickers. Describe collective market mood and themes instead (e.g. "retail euphoria spreading" not "RKLB up 2100%")
"""


def analyze_with_claude(posts_text: str, post_count: int, api_key: str) -> dict:
    client  = anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)
    now_utc = datetime.now(timezone.utc).isoformat()

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Current time: {now_utc}\n"
                f"Total posts to classify: {post_count}\n\n"
                f"Analyze these WSB posts:\n\n{posts_text}"
            ),
        }],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ── Metric calculation ────────────────────────────────────────────────────────

def score_label(score: int) -> str:
    if score >= 80: return "Extreme Greed"
    if score >= 60: return "Greed"
    if score >= 40: return "Neutral"
    if score >= 20: return "Fear"
    return "Extreme Fear"


def calculate_metrics(posts: list[dict], classifications: list[dict],
                      claude_holistic_score: int) -> dict:
    """
    Hybrid scoring: formula (equal post weight) + Claude holistic read.

    Formula side — equal weight per post (upvotes excluded: trading loss posts
    can get as many upvotes as profit posts, so upvotes don't indicate sentiment direction):
      bull_pct        = count(bullish posts) / total posts × 100
      bear_pct        = count(bearish posts) / total posts × 100
      neutral_pct     = 100 − bull_pct − bear_pct
      bull_bear_ratio = bull count / max(bear count, 1)

    Greed Index (overall_score) — blend of both signals:
      formula_score   = bull_pct  (systematic, reproducible)
      ai_score        = claude_holistic_score  (qualitative judgment)
      overall_score   = round((formula_score + ai_score) / 2)
    """
    sentiments = [clf.get("sentiment", "neutral") for clf in classifications]
    total = len(sentiments)

    bull_count    = sentiments.count("bullish")
    bear_count    = sentiments.count("bearish")
    neutral_count = sentiments.count("neutral")

    bull_pct        = round(bull_count / total * 100)
    bear_pct        = round(bear_count / total * 100)
    neutral_pct     = max(0, 100 - bull_pct - bear_pct)
    bull_bear_ratio = round(bull_count / max(bear_count, 1), 1)

    formula_score = bull_pct
    overall_score = round(formula_score * 0.3 + claude_holistic_score * 0.7)

    return {
        "bull_pct":        bull_pct,
        "bear_pct":        bear_pct,
        "neutral_pct":     neutral_pct,
        "bull_bear_ratio": bull_bear_ratio,
        "overall_score":   overall_score,
        "overall_label":   score_label(overall_score),
        "methodology": {
            "formula_score":      formula_score,
            "ai_score":           claude_holistic_score,
            "bull_count":         bull_count,
            "bear_count":         bear_count,
            "neutral_count":      neutral_count,
            "total_posts":        total,
            "formula": {
                "bull_pct":       "bullish posts / total posts × 100",
                "bear_pct":       "bearish posts / total posts × 100",
                "bull_bear_ratio":"bull count / bear count",
                "overall_score":  "bull_pct × 0.3 + ai_holistic_score × 0.7",
            },
        },
    }


# ── History ──────────────────────────────────────────────────────────────────

def load_history() -> list[dict]:
    try:
        with open(HISTORY_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_history(history: list[dict], entry: dict) -> None:
    history.append(entry)
    if len(history) > HISTORY_MAX:
        history = history[-HISTORY_MAX:]
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    api_key       = os.environ["ANTHROPIC_API_KEY"]
    client_id     = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")

    history = load_history()

    print(f"Fetching WSB posts from last {HOURS_WINDOW}h (/new + timestamp filter)…")
    if client_id and client_secret:
        token = get_reddit_token(client_id, client_secret)
        posts = fetch_wsb_posts_authenticated(token)
    else:
        try:
            posts = fetch_wsb_posts_unauthenticated()
        except RuntimeError as exc:
            print(f"  Public JSON fetch failed: {exc}")
            print("  Falling back to Reddit RSS feed...")
            posts = fetch_wsb_posts_rss()
    print(f"  Found {len(posts)} posts · title + body + top 5 comments each")

    posts_text = posts_to_text(posts)

    print("Analyzing with Claude…")
    claude_result = sanitize_claude_result(analyze_with_claude(posts_text, len(posts), api_key))

    classifications = claude_result.get("post_classifications", [])
    if len(classifications) != len(posts):
        print(f"  Warning: got {len(classifications)} classifications for {len(posts)} posts — padding")
        while len(classifications) < len(posts):
            classifications.append({"id": str(len(classifications) + 1), "sentiment": "neutral"})

    claude_holistic = claude_result.get("overall_sentiment_score", 50)
    metrics = calculate_metrics(posts, classifications, claude_holistic)

    result = {
        **metrics,
        "post_count":   len(posts),
        "hours_window": HOURS_WINDOW,
        "themes":        normalize_theme_tickers(claude_result.get("themes", [])),
        "tickers":       enrich_tickers_with_names(claude_result.get("tickers", [])),
        "summary":       claude_result.get("summary", ""),
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "updated_hkt":   datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT"),
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if datetime.now(HKT).hour in (5, 20):
        save_history(history, {
            "ts":            result["updated_hkt"],
            "score":         result["overall_score"],
            "label":         result["overall_label"],
            "formula_score": metrics["methodology"]["formula_score"],
            "ai_score":      metrics["methodology"]["ai_score"],
        })

    m = metrics["methodology"]
    print(f"\nSaved → {OUTPUT_PATH}")
    print(f"Overall score : {result['overall_score']} ({result['overall_label']})")
    print(f"  Formula score (bull%): {m['formula_score']}  |  AI holistic score: {m['ai_score']}  |  Blend: {result['overall_score']}")
    print(f"Bull / Bear   : {m['bull_count']}/{m['total_posts']} posts bullish · {m['bear_count']}/{m['total_posts']} bearish · {m['neutral_count']} neutral")


if __name__ == "__main__":
    main()
