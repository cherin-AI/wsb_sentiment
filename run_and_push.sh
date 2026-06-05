#!/bin/bash
set -e

cd "$(dirname "$0")"

# Load env vars
export $(grep -v '^#' .env | xargs)

# Sync latest automation/code changes before generating a new data commit.
git pull --rebase --autostash

# Run analysis
python3 scripts/fetch_and_analyze.py

# Push to GitHub
git add docs/sentiment.json docs/history.json
git diff --cached --quiet || git commit -m "chore: daily sentiment update $(date '+%Y-%m-%d %H:%M HKT')"
git push origin "$(git branch --show-current)"

# Extract score and label for notification
SCORE=$(python3 -c "import json; d=json.load(open('docs/sentiment.json')); print(d['overall_score'])")
LABEL=$(python3 -c "import json; d=json.load(open('docs/sentiment.json')); print(d['overall_label'])")

# macOS notification
osascript -e "display notification \"Score: ${SCORE} (${LABEL}) — dashboard updated\" with title \"WSB Fear & Greed Index\" sound name \"Default\""
