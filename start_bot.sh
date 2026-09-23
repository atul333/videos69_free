#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# start_bot.sh — Start video69 bot system with real-time nohup logging
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# Activate virtualenv if present
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
    echo "[start] Activated venv: $VIRTUAL_ENV"
elif [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
    echo "[start] Activated .venv: $VIRTUAL_ENV"
fi

# Detect python executable
PY="python"
if ! command -v python &>/dev/null && command -v python3 &>/dev/null; then
    PY="python3"
fi

# Kill any existing bot process before starting a fresh one
echo "[start] Stopping old bot instances..."
pkill -9 -f "video69_bot.py" 2>/dev/null || true
sleep 1

echo "[start] Starting bot with unbuffered output..."
export PYTHONUNBUFFERED=1
nohup $PY -u "$SCRIPT_DIR/video69_bot.py" >> "$SCRIPT_DIR/nohup.out" 2>&1 &

BOT_PID=$!
echo "[start] Bot started successfully! PID = $BOT_PID"
echo "[start] Logs being written to: $SCRIPT_DIR/nohup.out and $SCRIPT_DIR/bot.log"
echo "------------------------------------------------------------"
sleep 3
tail -n 30 "$SCRIPT_DIR/nohup.out"
