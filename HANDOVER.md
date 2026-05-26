# WSB Sentiment Monitor — Claude Code Handover

## Project overview

**What it does**: Daily r/wallstreetbets sentiment dashboard. A Python script fetches all WSB posts from the last 24 hours via `/new` + timestamp filtering (with top 5 comments per post), sends them to Claude API for structured sentiment analysis, outputs `docs/sentiment.json`, and a static HTML dashboard renders that data as a financial-style monitor page. Everything auto-runs at **08:00 HKT daily via GitHub Actions** and is served via **GitHub Pages** — zero server required.

**Intended deployment URL**: `https://<username>.github.io/<repo-name>/`

---

## Repo structure

```
wsb/
├── .github/
│   └── workflows/
│       └── daily-sentiment.yml   ← GitHub Actions cron (00:00 UTC = 08:00 HKT)
├── scripts/
│   └── fetch_and_analyze.py      ← Reddit fetch + Claude API analysis → writes docs/sentiment.json
├── docs/                         ← GitHub Pages root
│   ├── index.html                ← Dashboard frontend (pure HTML/CSS/JS, no framework)
│   └── sentiment.json            ← Auto-updated data file (also serves as initial mock)
├── .env                          ← Local secrets (gitignored — never commit)
└── HANDOVER.md
```

---

## Key design decisions

### Data pipeline

**Fetch strategy**: `/r/wallstreetbets/new.json` with pagination via `after` cursor, stopping when `created_utc < now − 24h`. This mirrors exactly what you see at reddit.com/r/wallstreetbets/new.

- WSB has aggressive moderation; typically **25–50 posts survive per day** — this is by design, not a bug
- Upvote-weighted approaches were considered and rejected: loss porn gets as many upvotes as gain porn, so upvotes don't indicate sentiment direction
- Per post: title, selftext (first 300 chars), flair, top 5 comments (200 chars each) fetched via public `reddit.com/r/wallstreetbets/comments/{id}.json` — no OAuth needed
- Reddit OAuth credentials are optional: if `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` are set, uses authenticated endpoint (higher rate limit + inline comment fetch); otherwise falls back to unauthenticated path with separate comment fetches

### Claude API call (`scripts/fetch_and_analyze.py`)

- Model: `claude-sonnet-4-20250514`
- Max tokens: 8000
- System prompt instructs Claude to return **pure JSON only** (no markdown fences), with a strip fallback for code fences
- Three-part analysis structure:

  **PART 1 — Per-post classification** (one entry per post, in order):
  - `sentiment`: `bullish | bearish | neutral` based on title + comments together
  - WSB-specific sarcasm note in prompt: "Bears are fucked" = bullish, ironic loss posts = bearish

  **PART 2 — Holistic AI sentiment score**:
  - `overall_sentiment_score`: integer 0–100, Claude's qualitative read of the entire batch
  - Considers tone, sarcasm, which tickers dominate, euphoria vs defeat
  - This is AI judgment, not a formula

  **PART 3 — Qualitative analysis**:
  - `themes[]` — 5–7 dominant topics ordered hottest first, each with: id, title, icon (Tabler icon name), heat (hot/rising/cool), bullets (exactly 4, ≤8 words, WSB voice), sentiment_score (0–100), tickers[]
  - `tickers[]` — top 8 by mention count: symbol, sentiment (bullish/bearish/mixed), mentions, signal
  - `summary` — 2–3 sentence punchy WSB narrative

### Metric calculation (`calculate_metrics()`)

All metrics are calculated in Python from Claude's per-post classifications. Claude does not calculate numbers directly.

**Equal weight per post** (upvote-weighting was rejected — see above):
```
bull_pct        = count(bullish posts) / total posts × 100
bear_pct        = count(bearish posts) / total posts × 100
neutral_pct     = 100 − bull_pct − bear_pct
bull_bear_ratio = bull count / max(bear count, 1)
```

**Greed Index (overall_score)** — hybrid blend:
```
formula_score  = bull_pct                          (systematic, reproducible)
ai_score       = claude_holistic_score             (qualitative AI judgment)
overall_score  = formula_score × 0.3 + ai_score × 0.7
```

Weighting rationale: 0.7 weight on AI holistic because WSB's heavy irony and sarcasm means raw post counts miss qualitative signals that Claude catches. Formula provides an auditable anchor.

**Score labels**: Extreme Greed ≥80 | Greed ≥60 | Neutral ≥40 | Fear ≥20 | Extreme Fear <20

**`sentiment.json` output fields**:
- `bull_pct`, `bear_pct`, `neutral_pct`, `bull_bear_ratio`
- `overall_score`, `overall_label`
- `post_count`, `hours_window`
- `themes[]`, `tickers[]`, `summary`
- `updated_at` (ISO8601 UTC), `updated_hkt` (display string)
- `methodology{}` — nested object for dashboard display: `formula_score`, `ai_score`, `bull_count`, `bear_count`, `neutral_count`, `total_posts`

### Frontend (`docs/index.html`)

- Single-file, no build step, no framework
- Fetches `sentiment.json?t={Date.now()}` on load (cache-bust)
- Design: dark navy aesthetic (`#060c18` background), teal accent (`#00c9a7`), Inter + IBM Plex Mono
- Color logic: score ≥70 = teal, 40–69 = amber, <40 = red (gauge, score number, theme bars)
- Sections:
  1. Sticky header — logo, live dot, timestamp
  2. WSB Greed Index — large score number, gauge bar, Extreme Fear → Extreme Greed labels, summary text
  3. Metric cards (3-col) — Bull/Bear ratio | Posts analyzed | Bearish %
  4. Sentiment breakdown — horizontal bars for Bullish / Neutral / Bearish %
  5. Trending themes — 3-col grid cards with heat badge, 4 bullets, mini score bar, ticker chips
  6. Hot tickers table — symbol, sentiment pill, mention bar, signal phrase
  7. Methodology section — live formula display showing actual numbers, methodology note
- Fully responsive (single-col on mobile)

### GitHub Actions (`daily-sentiment.yml`)

- Trigger: `cron: '0 0 * * *'` + `workflow_dispatch` (manual trigger)
- Permissions: `contents: write` (needed to commit `sentiment.json` back to repo)
- Steps: checkout → python 3.11 → `pip install requests anthropic python-dotenv` → run script → git commit + push
- Commit only happens if `sentiment.json` changed (`git diff --cached --quiet || git commit`)

---

## Environment variables / secrets required

| Name | Where | Required |
|---|---|---|
| `ANTHROPIC_API_KEY` | `.env` locally · GitHub Secrets for CI | ✅ Yes |
| `REDDIT_CLIENT_ID` | `.env` locally · GitHub Secrets for CI | Optional |
| `REDDIT_CLIENT_SECRET` | `.env` locally · GitHub Secrets for CI | Optional |

`.env` is gitignored. For local runs, `python-dotenv` loads it automatically.

---

## Current status

**Pipeline is working end-to-end locally.** Script successfully fetches ~25 WSB posts from the last 24h (this is the realistic moderated post count), analyzes with Claude, and writes `docs/sentiment.json`. Dashboard renders correctly at `http://localhost:8080`.

**Not yet done (deployment tasks):**
1. Create the GitHub repo and push these files
2. Enable GitHub Pages (Settings → Pages → Branch: `main`, Folder: `/docs`)
3. Add `ANTHROPIC_API_KEY` as a GitHub Secret (Reddit credentials optional)
4. Trigger the first manual run via Actions tab to verify end-to-end
5. Confirm the live dashboard URL works

---

## Common tasks

### Modify the Claude analysis prompt
Edit `SYSTEM_PROMPT` in `scripts/fetch_and_analyze.py`. The JSON schema fields must stay consistent with what `docs/index.html` renders. The methodology section in the dashboard auto-shows live formula values from `data.methodology`.

### Add a new dashboard section
1. Add the field to `SYSTEM_PROMPT` JSON schema
2. Parse and render it in the `root.innerHTML` template string inside `<script>` in `index.html`

### Change the update schedule
Edit `cron: '0 0 * * *'` in `.github/workflows/daily-sentiment.yml`. UTC offset from HKT is −8h:
- 08:00 HKT = `0 0 * * *`
- 09:00 HKT = `0 1 * * *`
- 20:00 HKT = `0 12 * * *`

### Test the script locally
```bash
# Add ANTHROPIC_API_KEY to .env first
pip install requests anthropic python-dotenv
python scripts/fetch_and_analyze.py
# Check: docs/sentiment.json
# Serve: cd docs && python3 -m http.server 8080
# Open: http://localhost:8080
```

### Debug GitHub Actions failures
- `ANTHROPIC_API_KEY` secret not set → script crashes at `os.environ["ANTHROPIC_API_KEY"]`
- Reddit 429 rate limit → add `time.sleep(1)` between comment fetches in `fetch_wsb_posts_unauthenticated()`
- JSON parse failure → Claude returned markdown fences; the strip logic handles ` ```json ` but check edge cases
- Push permission denied → confirm `permissions: contents: write` is in the workflow YAML
- `len(classifications) != len(posts)` warning → safe, script pads with "neutral" to match

### Add historical data / trend chart
Currently only latest `sentiment.json` is kept. To add history:
1. In the Python script, append to `docs/history.json` (array of daily snapshots)
2. In `index.html`, fetch `history.json` and render a Chart.js line chart of `overall_score` over time

### Adjust formula/AI weighting
In `calculate_metrics()` in `fetch_and_analyze.py`:
```python
overall_score = round(formula_score * 0.3 + claude_holistic_score * 0.7)
```
Current split: 30% formula (bull post count %) / 70% AI holistic. Adjust the coefficients (must sum to 1.0). The methodology section in the dashboard auto-reflects whatever numbers are in `sentiment.json`.

---

## Dependencies

**Python** (installed fresh in Actions each run):
- `requests` — Reddit API calls
- `anthropic` — Claude API SDK
- `python-dotenv` — local `.env` loading (graceful fallback if not installed)

**Frontend** (Google Fonts CDN, no install):
- Inter (UI/body text) + IBM Plex Mono (numbers, code, labels) via Google Fonts
- No JS framework, no bundler

---

## Cost reference

~25–50 posts/day × ~600 tokens input/post + ~3,000 tokens output ≈ 20–35k tokens/day
Claude Sonnet 4: ~$0.003–0.005/day → well under $1/month
