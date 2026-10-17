"""
free_bot_runner.py — Multi-Bot Runner (Entry Point)
====================================================
Loads all 9 free-bot configurations from free_bots_config.yaml + .env,
then runs all enabled bots concurrently in ONE process on ONE event loop.

Usage:
    python free_bot_runner.py

To add a new bot:
    1. Add  BOT_N_TOKEN=...  to your .env file
    2. Add the bot's entry block in free_bots_config.yaml (enabled: true)
    3. Restart:  python free_bot_runner.py

To disable a bot without removing it:
    Set  enabled: false  in free_bots_config.yaml — no code changes needed.
"""

# ── Force UTF-8 output on Windows (prevents UnicodeEncodeError with emoji) ──
import sys, io
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import os
import signal
import asyncio
import logging
import traceback

from dotenv import load_dotenv

load_dotenv()   # load .env before any other import reads env vars

# ── Logging (configure before importing bot modules) ─────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),      # stdout already forced to utf-8 above
        logging.FileHandler("free_bots.log", encoding="utf-8"),
    ],
)
# Silence noisy libraries
for _lib in ("httpx", "httpcore", "aiosqlite", "telegram",
             "apscheduler", "asyncio", "hpack"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

logger = logging.getLogger("free_bot_runner")

try:
    import yaml
except ImportError:
    print("❌ PyYAML not installed. Run:  pip install pyyaml")
    sys.exit(1)

import free_users_db as db
from free_bot_core import build_application


# ── Config loader ─────────────────────────────────────────────────────────────

def load_yaml_config(path: str = "free_bots_config.yaml") -> dict:
    if not os.path.exists(path):
        logger.error("Config file not found: %s", path)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not cfg:
        logger.error("Empty config file: %s", path)
        sys.exit(1)
    return cfg


def resolve_bot_configs(raw: dict) -> list[dict]:
    """
    Resolve each bot entry from the YAML config.
    - Substitutes token from the named env var.
    - Merges global settings defaults.
    - Skips disabled bots and bots with missing tokens (warns, does not crash).
    Returns a list of fully-resolved config dicts for enabled bots only.
    """
    settings  = raw.get("settings", {})
    bots_raw  = raw.get("bots", [])
    admin_uid = int(os.getenv("ADMIN_USER_ID", "0"))

    valid = []
    for i, bot in enumerate(bots_raw, 1):
        name = bot.get("name", f"Free Bot {i}")

        if not bot.get("enabled", True):
            logger.info("⏭️  [%s] disabled — skipping", name)
            continue

        token_env = bot.get("token_env", f"BOT_{i}_TOKEN")
        token     = os.getenv(token_env, "").strip()
        if not token:
            logger.warning(
                "⚠️  [%s] token env var '%s' not set or empty — skipping",
                name, token_env,
            )
            continue

        # Safety: never log the full token
        safe_token = token[:8] + "..." + token[-4:]

        ch1_id   = int(os.getenv("MANDATORY_CHANNEL_1_ID") or bot.get("mandatory_channel_1_id", 0))
        ch1_link = os.getenv("MANDATORY_CHANNEL_1_LINK") or bot.get("mandatory_channel_1_link", "")
        ch2_id   = int(os.getenv("MANDATORY_CHANNEL_2_ID") or bot.get("mandatory_channel_2_id", ch1_id))
        ch2_link = os.getenv("MANDATORY_CHANNEL_2_LINK") or bot.get("mandatory_channel_2_link", ch1_link)

        resolved = {
            # Identity
            "name":  name,
            "token": token,

            # Channels
            "mandatory_channel_1_id":   ch1_id,
            "mandatory_channel_1_link": ch1_link,
            "mandatory_channel_2_id":   ch2_id,
            "mandatory_channel_2_link": ch2_link,

            # Video source (bot-level setting or named env var overrides global defaults)
            "video_channel_id": int(
                (os.getenv(bot["video_channel_env"]) if bot.get("video_channel_env") else None)
                or bot.get("video_channel_id")
                or os.getenv("CHANNEL_ID")
                or 0
            ),
            "min_message_id":   int(bot.get("min_message_id", 1)),
            "max_message_id":   int(
                (os.getenv(bot["max_message_env"]) if bot.get("max_message_env") else None)
                or bot.get("max_message_id")
                or os.getenv("MAX_MESSAGE_ID")
                or 0
            ),

            # Behaviour (bot-level overrides global settings)
            "daily_video_limit":    int(bot.get(
                "daily_video_limit",
                settings.get("daily_video_limit", 0)
            )),
            "video_delay_seconds":  int(bot.get(
                "video_delay_seconds",
                settings.get("video_delay_seconds", 3)
            )),
            "membership_cache_ttl": int(bot.get(
                "membership_cache_ttl",
                settings.get("membership_cache_ttl", 300)
            )),

            # Promotion
            "promo_message": bot.get(
                "promo_message",
                settings.get("default_promo_message", ""),
            ),
            "promo_links": bot.get(
                "promo_links",
                settings.get("default_promo_links", []),
            ),

            # Support
            "support_url":  bot.get("support_url", settings.get("support_url", "")),

            # Admin (from env, shared across all bots)
            "admin_user_id": admin_uid,

            # Log file (for future per-bot logging)
            "log_file": bot.get("log_file", f"bot_{i}.log"),
        }

        logger.info(
            "✅ [%s] token=%s  ch1=%s  ch2=%s  vid_ch=%s  ids=[%s–%s]  "
            "limit=%s  delay=%ss",
            name, safe_token,
            ch1_id, ch2_id,
            resolved["video_channel_id"],
            resolved["min_message_id"], resolved["max_message_id"],
            resolved["daily_video_limit"],
            resolved["video_delay_seconds"],
        )
        valid.append(resolved)

    return valid


# ── Per-bot runner coroutine ──────────────────────────────────────────────────

async def _run_one_bot(app, stop_event: asyncio.Event):
    """
    Lifecycle for one bot application.
    An exception in this coroutine is caught and logged — it does NOT crash
    the other bots running in the same process.
    """
    name = app.bot_data["cfg"]["name"]
    try:
        async with app:
            await app.start()
            await app.updater.start_polling(
                allowed_updates=["message", "callback_query", "chat_member"],
                poll_interval=1.0,
                timeout=30,
                drop_pending_updates=False,
            )
            logger.info("[%s] Polling active", name)
            await stop_event.wait()           # sleep until global shutdown
            logger.info("[%s] Stopping...", name)
            await app.updater.stop()
            await app.stop()
            logger.info("[%s] Stopped cleanly", name)
    except Exception:
        logger.error("[%s] Fatal error:\n%s", name, traceback.format_exc())
        # Do NOT re-raise — other bots keep running


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    logger.info("=" * 60)
    logger.info("🤖  Free Bot Runner starting")
    logger.info("=" * 60)

    # 1. Load YAML config
    raw_cfg    = load_yaml_config()
    bot_cfgs   = resolve_bot_configs(raw_cfg)

    if not bot_cfgs:
        logger.error(
            "❌ No valid bot configurations found. "
            "Check your .env and free_bots_config.yaml"
        )
        sys.exit(1)

    logger.info("📋 %d bot(s) will be started", len(bot_cfgs))

    # 2. Initialise shared free-user database (ONCE, before any bot starts)
    db_path = raw_cfg.get("settings", {}).get("db_path", "free_users.db")
    await db.init_db(db_path=db_path)
    db.start_flush_loop()

    # 3. Build all Application objects
    apps = []
    for cfg in bot_cfgs:
        try:
            apps.append(build_application(cfg))
        except Exception as e:
            logger.error("Failed to build [%s]: %s", cfg["name"], e)

    if not apps:
        logger.error("❌ No applications could be built — exiting")
        db.stop_flush_loop()
        await db.close_db()
        sys.exit(1)

    # 4. Shared stop event — set by signal handlers or on keyboard interrupt
    stop_event = asyncio.Event()
    loop       = asyncio.get_running_loop()

    def _on_signal():
        logger.info("⛔ Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, OSError):
            # Windows: SIGTERM not supported via add_signal_handler
            pass

    # 5. Start all bots concurrently
    logger.info("🚀 Starting %d bot(s)…", len(apps))
    tasks = [
        asyncio.create_task(
            _run_one_bot(app, stop_event),
            name=app.bot_data["cfg"]["name"],
        )
        for app in apps
    ]

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, SystemExit):
        logger.info("⛔ Keyboard interrupt — shutting down")
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    # 6. Flush database and close
    logger.info("💾 Final DB flush…")
    db.stop_flush_loop()
    await asyncio.sleep(0.6)   # let flush task complete its final write
    await db.close_db()
    logger.info("✅ All bots stopped. Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted.")
    except Exception:
        logger.error("FATAL:\n%s", traceback.format_exc())
        sys.exit(1)
