#!/bin/bash
set -e

cd "$(dirname "$0")"

# Load env vars
export $(grep -v '^#' .env | xargs)

# Run analysis
python3 scripts/fetch_and_analyze.py

# Push to GitHub
git add docs/sentiment.json
git diff --cached --quiet || git commit -m "chore: daily sentiment update $(date '+%Y-%m-%d %H:%M HKT')"
git push
