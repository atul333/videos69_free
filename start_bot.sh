#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# start_bot.sh  — Start video69_bot with proper logging to nohup.out + bot.log
# Usage:
#   chmod +x start_bot.sh
#   ./start_bot.sh
# ─────────────────────────────────────────────────────────────────────────────

# Go to the bot's directory (so .env and DB files are found regardless of CWD)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Activate virtualenv if present
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
    echo "[start_bot.sh] Activated venv: $VIRTUAL_ENV"
fi

# Kill any existing bot process before starting a fresh one
echo "[start_bot.sh] Stopping old bot instances..."
pkill -f "video69_bot.py" 2>/dev/null || true
sleep 1

# PYTHONUNBUFFERED=1  -> forces Python to flush stdout/stderr immediately
# python -u           -> same as PYTHONUNBUFFERED (belt + braces)
# nohup ... &         -> runs in background, survives SSH disconnect
# >> nohup.out 2>&1   -> appends both stdout AND stderr to nohup.out

echo "[start_bot.sh] Starting bot..."
PYTHONUNBUFFERED=1 nohup python -u "$SCRIPT_DIR/video69_bot.py" >> "$SCRIPT_DIR/nohup.out" 2>&1 &

BOT_PID=$!
echo "[start_bot.sh] Bot started! PID = $BOT_PID"
echo "[start_bot.sh] Logs: $SCRIPT_DIR/nohup.out  and  $SCRIPT_DIR/bot.log"
echo "[start_bot.sh] To watch logs live:  tail -f $SCRIPT_DIR/nohup.out"
echo "[start_bot.sh] To stop bot:         kill $BOT_PID   (or: pkill -f video69_bot.py)"

# Give it 3 seconds and show first few lines of nohup.out
sleep 3
echo ""
echo "--- First lines of nohup.out ---"
tail -20 "$SCRIPT_DIR/nohup.out"
echo "--------------------------------"
