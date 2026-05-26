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
import json
import requests
import anthropic
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # GitHub Actions injects secrets as env vars; dotenv only needed locally

HKT          = timezone(timedelta(hours=8))
OUTPUT_PATH  = os.path.join(os.path.dirname(__file__), "..", "docs", "sentiment.json")

REDDIT_UA    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
REDDIT_HDR   = {"User-Agent": REDDIT_UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"}
REDDIT_BASE  = "https://oauth.reddit.com"
REDDIT_AUTH  = "https://www.reddit.com/api/v1/access_token"

HOURS_WINDOW = 24  # fetch all posts from the last 24 hours


# ── Reddit ───────────────────────────────────────────────────────────────────

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
        r = requests.get(
            f"https://www.reddit.com/r/wallstreetbets/comments/{post_id}.json",
            headers=REDDIT_HDR,
            params={"limit": 5, "depth": 1, "sort": "top"},
            timeout=15,
        )
        if not r.ok:
            return []
        return [
            c["data"].get("body", "")[:200]
            for c in r.json()[1]["data"]["children"]
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
        r = requests.get(
            "https://www.reddit.com/r/wallstreetbets/new.json",
            headers=REDDIT_HDR,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()["data"]

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

SYSTEM_PROMPT = """You are a WSB (WallStreetBets) sentiment analyst.
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
  - themes: 5–7 dominant topics ordered hottest first
  - tickers: top 8 by mention count across all posts + comments
  - summary: 2–3 sentence punchy WSB narrative

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
      "heat": "hot|rising|cool",
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
  "summary": "<punchy 2-3 sentence WSB narrative>"
}

Rules:
- bullets: exactly 4 per theme, WSB voice, no fluff
- tickers: top 8 by mention count only
- Do not invent post_classifications entries — one per input post, in order
"""


def analyze_with_claude(posts_text: str, post_count: int, api_key: str) -> dict:
    client  = anthropic.Anthropic(api_key=api_key)
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

    Formula side — equal weight per post (upvotes excluded: loss porn gets
    as many upvotes as gain porn, so upvotes don't indicate sentiment direction):
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    api_key       = os.environ["ANTHROPIC_API_KEY"]
    client_id     = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")

    print(f"Fetching WSB posts from last {HOURS_WINDOW}h (/new + timestamp filter)…")
    if client_id and client_secret:
        token = get_reddit_token(client_id, client_secret)
        posts = fetch_wsb_posts_authenticated(token)
    else:
        posts = fetch_wsb_posts_unauthenticated()
    print(f"  Found {len(posts)} posts · title + body + top 5 comments each")

    posts_text = posts_to_text(posts)

    print("Analyzing with Claude…")
    claude_result = analyze_with_claude(posts_text, len(posts), api_key)

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
        "themes":        claude_result.get("themes", []),
        "tickers":       claude_result.get("tickers", []),
        "summary":       claude_result.get("summary", ""),
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "updated_hkt":   datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT"),
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    m = metrics["methodology"]
    print(f"\nSaved → {OUTPUT_PATH}")
    print(f"Overall score : {result['overall_score']} ({result['overall_label']})")
    print(f"  Formula score (bull%): {m['formula_score']}  |  AI holistic score: {m['ai_score']}  |  Blend: {result['overall_score']}")
    print(f"Bull / Bear   : {m['bull_count']}/{m['total_posts']} posts bullish · {m['bear_count']}/{m['total_posts']} bearish · {m['neutral_count']} neutral")


if __name__ == "__main__":
    main()
