# WSB Sentiment Monitor

Daily r/wallstreetbets sentiment dashboard, auto-updated at **08:00 HKT** via GitHub Actions → deployed on GitHub Pages.

## Architecture

```
GitHub Actions (cron 00:00 UTC = 08:00 HKT)
  → scripts/fetch_and_analyze.py
      → Reddit API: fetch top 40 hot posts + top comments
      → Claude API: structured sentiment JSON
      → write docs/sentiment.json
  → git commit + push
      → GitHub Pages serves docs/
```

## Setup (one-time)

### 1. Create GitHub repo & enable Pages

1. Create a new repo (public or private)
2. Push this entire folder
3. Go to **Settings → Pages → Source → Deploy from branch → `main` / `docs`**
4. Your dashboard is live at `https://<you>.github.io/<repo>/`

### 2. Add Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `REDDIT_CLIENT_ID` | Reddit app client ID (optional but recommended) |
| `REDDIT_CLIENT_SECRET` | Reddit app client secret (optional) |

> **Without Reddit secrets**: the script falls back to the unauthenticated `reddit.com/r/wallstreetbets/hot.json` endpoint (posts only, no comments, lower rate limit — fine for daily use).

### 3. Reddit API setup (optional but recommended)

1. Go to https://www.reddit.com/prefs/apps
2. Click **Create App** → type: **script**
3. Name: `wsb-sentiment-bot`, redirect URI: `http://localhost`
4. Copy **client ID** (below app name) and **client secret**
5. Add both as GitHub Secrets above

### 4. Manual trigger

Go to **Actions → WSB Daily Sentiment → Run workflow** to test immediately.

## File structure

```
wsb-sentiment/
├── .github/workflows/
│   └── daily-sentiment.yml   ← GitHub Actions cron job
├── scripts/
│   └── fetch_and_analyze.py  ← Reddit fetch + Claude analysis
├── docs/
│   ├── index.html            ← Dashboard frontend
│   └── sentiment.json        ← Auto-updated data (also serves as initial mock)
└── README.md
```

## Customization

- **Cron schedule**: edit `cron: '0 0 * * *'` in the workflow file. `0 0 * * *` = 00:00 UTC = 08:00 HKT.
- **Number of posts**: edit `limit` in `fetch_wsb_posts()` (API max: 100)
- **Themes / tone**: edit `SYSTEM_PROMPT` in `fetch_and_analyze.py`
- **Dashboard style**: edit `docs/index.html`

## Cost estimate

~40 posts × ~500 tokens/post input + ~1000 tokens output ≈ **~21k tokens/day**

Claude Sonnet 4: ~$0.003/day → **< $1/month**
