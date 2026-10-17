# Free Bots — Setup Guide

## What Was Built

| File | Purpose |
|---|---|
| [`free_users_db.py`](free_users_db.py) | Shared SQLite database for all 9 free bots |
| [`free_bot_core.py`](free_bot_core.py) | All Telegram handlers and business logic |
| [`free_bot_runner.py`](free_bot_runner.py) | Entry point — starts all bots at once |
| [`free_bots_config.yaml`](free_bots_config.yaml) | Non-secret config for all 9 bots |
| [`.env.free_bots.example`](.env.free_bots.example) | Template for bot tokens |
| [`test_free_bots.py`](test_free_bots.py) | 18 automated tests |
| [`requirements_free_bots.txt`](requirements_free_bots.txt) | Dependencies |

> **Completely separate from the premium bot system.**
> Does not use `user_states.db`, `premium_shared.db`, or `premium_sync.py`.

---

## Quick Start (5 Steps)

### Step 1 — Install dependencies

```powershell
pip install -r requirements_free_bots.txt
```

### Step 2 — Add your bot tokens to `.env`

Copy the template and fill in your bot tokens from @BotFather:

```powershell
copy .env.free_bots.example .env
```

Open `.env` and set:

```env
BOT_1_TOKEN=8677608542:AAGi...   ← your first bot's token
BOT_2_TOKEN=                     ← leave blank if not ready yet
ADMIN_USER_ID=7708376300         ← your Telegram user ID
```

The runner **skips any bot whose token is blank** — no crash, no error.

### Step 3 — Configure your bots in `free_bots_config.yaml`

Open [`free_bots_config.yaml`](free_bots_config.yaml).  
For each bot you want to run, set `enabled: true` and fill in:

| Field | Description |
|---|---|
| `mandatory_channel_1_id` | Telegram channel ID (negative number from @JsonDumpBot) |
| `mandatory_channel_1_link` | Invite link to the channel |
| `mandatory_channel_2_id` | Second mandatory channel (optional — can be same as ch1) |
| `video_channel_id` | Channel containing your videos |
| `max_message_id` | Highest message ID in the video channel |
| `promo_links` | Premium bot links shown at milestones 10 / 20 / 30 videos |

### Step 4 — Start all bots

```powershell
python free_bot_runner.py
```

All enabled bots start simultaneously. Output:

```
🚀 [Free Videos Bot 1] Online as @YourBot1 (ID: 123456789)
🚀 [Free Videos Bot 2] Online as @YourBot2 (ID: 987654321)
```

### Step 5 — Run tests (optional but recommended)

```powershell
pytest test_free_bots.py -v
```

---

## Adding a New Bot (No Code Changes)

1. Create a new bot via [@BotFather](https://t.me/BotFather) → copy the token
2. Add `BOT_N_TOKEN=<token>` to `.env`
3. In `free_bots_config.yaml`, find the matching entry and set `enabled: true`
4. Restart: `python free_bot_runner.py`

That's it. No Python changes needed.

---

## Enabling / Disabling a Bot Instantly

In `free_bots_config.yaml`:

```yaml
- name: "Free Videos Bot 3"
  enabled: false    ← bot is skipped at startup (token ignored)
```

Change to `true` and restart to enable it.

---

## How the 5-Second Delay Works

When a user clicks **▶️ Next Video**:

1. Callback is **answered immediately** (button spinner stops) — zero extra API calls
2. Server sleeps **5 seconds** (`asyncio.sleep(5)`) — no messages sent, no edits
3. Video is delivered

Duplicate clicks during the 5-second wait are silently dropped (one `answer()` call only).
This minimises Telegram API usage during the delay.

---

## Shared Database (`free_users.db`)

**All 9 bots write to the same file.** Because all bots run in one process, there is no cross-process locking needed — the in-memory cache is the single source of truth.

| Column | Description |
|---|---|
| `user_id` | Telegram user ID (cross-bot identity, primary key) |
| `first_seen` | First interaction with any bot |
| `last_active` | Last activity on any bot |
| `total_delivered` | Lifetime video count across ALL bots |
| `daily_count` | Today's watch count (resets midnight IST) |
| `milestone_10/20/30` | Promotion flags — each fires exactly once |
| `seen_videos` | JSON list of seen video IDs (cross-bot, capped at 5000) |

**Promotion milestones (10 / 20 / 30 videos)** are cross-bot: a user who watches 7 videos on Bot 1 and 3 videos on Bot 2 will receive their 10-video promotion from whichever bot delivers their 10th video.

---

## Admin Commands

| Command | Description |
|---|---|
| `/stats` | Live stats: users, active today, videos delivered |
| `/broadcast` | Send a message to all users of THIS bot |
| `/cancel` | Cancel a pending broadcast |

Only the `ADMIN_USER_ID` from `.env` can use these.

---

## Database Write Strategy (Write-Behind Cache)

```
User action → update in-memory dict → mark dirty
                                           ↓
                              every 30 seconds (background task)
                                           ↓
                              flush dirty users to free_users.db
```

This means:
- **Zero I/O on the hot path** (every video delivery, membership check)
- **Guaranteed writes on shutdown** (final flush triggered by CancelledError)
- **Crash-safe**: worst case = 30 seconds of recent activity lost

---

## How Different Configs Work on the Same Video Channel

All 9 bots can share the same `video_channel_id` and `max_message_id`.  
Because `seen_videos` is cross-bot (shared DB), a user won't see the same video twice whether they're using Bot 1 or Bot 9.

Each bot can also have **different** video channels — just change `video_channel_id` and `max_message_id` per bot in the YAML.

---

## Stopping the Bots

Press **Ctrl+C** (or send SIGTERM on Linux).  
The runner catches the signal, stops all bots, does a final DB flush, and exits cleanly.

---

## Deployment (Ubuntu Server)

```bash
# Install
pip install -r requirements_free_bots.txt

# Run as a background service
nohup python free_bot_runner.py > free_bots.log 2>&1 &

# Or with systemd (recommended for production)
# Copy free_bots.service → /etc/systemd/system/
# systemctl enable --now free_bots
```

Example `free_bots.service`:

```ini
[Unit]
Description=Free Video Bots
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/Videos69-bot-Free
ExecStart=/usr/bin/python3 free_bot_runner.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Differences from `video69_bot.py`

| Feature | `video69_bot.py` (premium bot) | Free bots (`free_bot_runner.py`) |
|---|---|---|
| Database | `user_states.db` + `premium_shared.db` | `free_users.db` only |
| Premium | Full Razorpay + ad-watch system | None |
| Download button | ✅ 1 free / 100 premium | ❌ |
| Ad-watch | ✅ AroLinks + VPLink | ❌ |
| Promotions | At daily limit | At 10 / 20 / 30 lifetime videos |
| Multi-bot | Single bot | Up to 9 bots in one process |
| Config | Env vars + hardcoded constants | YAML + `.env` |
| 5s delay | 2s button cooldown | 5s server-side sleep |
