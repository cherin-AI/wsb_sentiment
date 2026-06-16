#!/bin/bash
# WSB daily sentiment run. Reports every run to Telegram (success + failure).
# Telegram credentials live in .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.

cd "$(dirname "$0")"

# Load env vars (ANTHROPIC_API_KEY, REDDIT_*, TELEGRAM_*)
export $(grep -v '^#' .env | xargs)

# --- logging: capture everything to a temp log, still echo to launchd.log ---
LOG=$(mktemp)
exec 3>&1            # fd 3 = original stdout (-> launchd.log)
exec >>"$LOG" 2>&1   # stdout+stderr now go to the temp log
dump() { cat "$LOG" >&3; }   # flush full log to launchd.log

# --- telegram helpers ---
notify() {
  # $1 = HTML-formatted message text. No-op if creds are absent.
  [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ] || return 0
  curl -s --max-time 20 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT_ID}" \
    -d parse_mode="HTML" \
    --data-urlencode "text=$1" >/dev/null
}

esc() { sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }

fail() {
  # $1 = which step failed. Sends the last 25 log lines (the real error).
  local tail_txt
  tail_txt=$(tail -n 25 "$LOG" | esc)
  notify "❌ <b>WSB run FAILED</b> — $1
$(date '+%Y-%m-%d %H:%M HKT')

<pre>${tail_txt}</pre>"
  dump
  rm -f "$LOG"
  exit 1
}

# --- pipeline (each step alerts on failure) ---
git pull --rebase --autostash                  || fail "git pull"
python3 scripts/fetch_and_analyze.py           || fail "analysis"
git add docs/sentiment.json docs/history.json  || fail "git add"
git diff --cached --quiet \
  || git commit -m "chore: daily sentiment update $(date '+%Y-%m-%d %H:%M HKT')" \
  || fail "git commit"
git push origin "$(git branch --show-current)" || fail "git push"

# --- success report ---
SCORE=$(python3 -c "import json; d=json.load(open('docs/sentiment.json')); print(d['overall_score'])")
LABEL=$(python3 -c "import json; d=json.load(open('docs/sentiment.json')); print(d['overall_label'])")

notify "✅ <b>WSB sentiment updated</b>
Score: <b>${SCORE}</b> (${LABEL})
$(date '+%Y-%m-%d %H:%M HKT')"

# macOS local popup (best-effort; harmless if headless)
osascript -e "display notification \"Score: ${SCORE} (${LABEL}) — dashboard updated\" with title \"WSB Fear & Greed Index\" sound name \"Default\"" 2>/dev/null || true

dump
rm -f "$LOG"
