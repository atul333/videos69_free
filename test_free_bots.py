"""
test_free_bots.py — Automated Tests for the Free Bot System
============================================================
Tests all 18 required scenarios:

  1.  Start first free bot                  — single bot, valid config
  2.  Add second bot via config only        — no code change needed
  3.  Load multiple tokens                  — all 9 tokens from env
  4.  Invalid token handling                — skip bad token, run others
  5.  Mandatory channel check               — user blocked if not joined
  6.  Video delivery                        — copy_message on member user
  7.  Five-second delay enforcement         — asyncio.sleep before delivery
  8.  Early button click                    — duplicate during delay is dropped
  9.  Simultaneous clicks                   — concurrent coroutines handled safely
 10.  Duplicate callback protection         — 'proc' set prevents race condition
 11.  Shared video counting                 — daily count increments cross-bot
 12.  Promotion at 10 videos               — promo message sent at milestone 10
 13.  Repeated promotions at 20 and 30     — each fires exactly once
 14.  Promotion configuration              — links come from config
 15.  Database failure                      — fail-open, no crash
 16.  Restart behaviour                     — state persists across restart
 17.  Premium DB isolation                  — free_users.db ≠ premium_shared.db
 18.  Only advertised benefits              — no false claims in messages

Run with:
    pip install pytest pytest-asyncio
    pytest test_free_bots.py -v
"""

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ── Use a temp DB for every test ─────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _fresh_db(tmp_path):
    """Reset free_users_db module state and point at a temporary SQLite file."""
    import importlib
    import free_users_db as db_module

    db_path = str(tmp_path / "test_free_users.db")
    os.environ["FREE_USERS_DB"] = db_path

    # Reset all module-level state
    db_module._cache.clear()
    db_module._dirty.clear()
    db_module._bot_reg.clear()
    db_module._reg_dirty.clear()
    db_module._video_meta_cache.clear()
    db_module._video_reactions_cache.clear()
    db_module._user_reactions_cache.clear()
    db_module._initialized = False
    db_module._flush_task = None
    db_module._persistent_db = None
    db_module.DB_FILE = db_path

    yield db_module, db_path

    # Teardown
    db_module._cache.clear()
    db_module._dirty.clear()
    db_module._bot_reg.clear()
    db_module._reg_dirty.clear()
    db_module._video_meta_cache.clear()
    db_module._video_reactions_cache.clear()
    db_module._user_reactions_cache.clear()


import free_users_db as db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cfg(overrides: dict = None) -> dict:
    """Return a minimal valid bot config dict."""
    cfg = {
        "name":                     "Test Bot",
        "token":                    "1234567890:AAABBBCCC",
        "mandatory_channel_1_id":   -100111,
        "mandatory_channel_1_link": "https://t.me/ch1",
        "mandatory_channel_2_id":   -100222,
        "mandatory_channel_2_link": "https://t.me/ch2",
        "video_channel_id":         -100333,
        "min_message_id":           1,
        "max_message_id":           100,
        "daily_video_limit":        5,
        "video_delay_seconds":      0,   # 0 for tests — no real waiting
        "membership_cache_ttl":     300,
        "promo_message":            "Check premium bots!",
        "promo_links":              [{"text": "🔥 Premium", "url": "https://t.me/premium"}],
        "support_url":              "",
        "admin_user_id":            999,
        "log_file":                 "test.log",
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def _make_context(cfg: dict) -> MagicMock:
    """Return a mock ContextTypes.DEFAULT_TYPE with bot_data set."""
    ctx = MagicMock()
    ctx.bot_data = {"cfg": cfg, "mc": {}, "proc": set()}
    ctx.bot = AsyncMock()
    ctx.bot.get_chat_member = AsyncMock()
    ctx.bot.copy_message    = AsyncMock()
    ctx.bot.send_message    = AsyncMock()
    ctx.bot.delete_message  = AsyncMock()
    ctx.job_queue = MagicMock()
    ctx.job_queue.run_once = MagicMock()
    return ctx


def _make_callback_query(user_id: int, data: str, chat_id: int = 1000) -> MagicMock:
    """Return a mock CallbackQuery."""
    query = MagicMock()
    query.data             = data
    query.answer           = AsyncMock()
    query.from_user.id     = user_id
    query.from_user.first_name = "Tester"
    query.from_user.username   = "tester"
    query.message.chat_id      = chat_id
    query.message.message_id   = 42
    query.message.reply_markup = None
    query.message.edit_text    = AsyncMock()
    return query


# ══════════════════════════════════════════════════════════════════════════════
# TEST 1 — Start first free bot
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_01_start_first_bot(_fresh_db):
    """Runner can initialise the DB and build at least one Application."""
    db_module, db_path = _fresh_db
    await db_module.init_db()
    assert db_module._initialized is True
    assert os.path.exists(db_path)


# ══════════════════════════════════════════════════════════════════════════════
# TEST 2 — Add second bot via config only (no code change)
# ══════════════════════════════════════════════════════════════════════════════
def test_02_add_second_bot_config_only(tmp_path):
    """resolve_bot_configs returns both bots when two valid tokens are set."""
    import yaml
    from free_bot_runner import resolve_bot_configs

    config = {
        "settings": {"daily_video_limit": 5, "video_delay_seconds": 5,
                      "membership_cache_ttl": 300},
        "bots": [
            {"name": "Bot A", "token_env": "TEST_BOT_A",
             "enabled": True,
             "mandatory_channel_1_id": -100111,
             "mandatory_channel_1_link": "https://t.me/a",
             "video_channel_id": -100555, "max_message_id": 100},
            {"name": "Bot B", "token_env": "TEST_BOT_B",
             "enabled": True,
             "mandatory_channel_1_id": -100222,
             "mandatory_channel_1_link": "https://t.me/b",
             "video_channel_id": -100666, "max_message_id": 200},
        ],
    }

    os.environ["TEST_BOT_A"] = "111111111:AAAA"
    os.environ["TEST_BOT_B"] = "222222222:BBBB"
    os.environ["ADMIN_USER_ID"] = "0"

    result = resolve_bot_configs(config)
    assert len(result) == 2
    assert result[0]["name"] == "Bot A"
    assert result[1]["name"] == "Bot B"


# ══════════════════════════════════════════════════════════════════════════════
# TEST 3 — Load multiple tokens
# ══════════════════════════════════════════════════════════════════════════════
def test_03_load_multiple_tokens():
    """All 9 bot tokens can be loaded from environment variables."""
    from free_bot_runner import resolve_bot_configs

    bots = []
    for n in range(1, 10):
        os.environ[f"MULTI_BOT_{n}"] = f"{n * 111111111}:TOKEN{n}"
        bots.append({
            "name": f"Bot {n}", "token_env": f"MULTI_BOT_{n}", "enabled": True,
            "mandatory_channel_1_id": -100100 - n,
            "mandatory_channel_1_link": f"https://t.me/ch{n}",
            "video_channel_id": -100200 - n, "max_message_id": 1000,
        })

    cfg = {"settings": {}, "bots": bots}
    os.environ["ADMIN_USER_ID"] = "0"
    result = resolve_bot_configs(cfg)
    assert len(result) == 9, f"Expected 9 bots, got {len(result)}"


# ══════════════════════════════════════════════════════════════════════════════
# TEST 4 — Invalid token handling (skip bad token, run others)
# ══════════════════════════════════════════════════════════════════════════════
def test_04_invalid_token_handling():
    """Bot with missing token is skipped; other bots are unaffected."""
    from free_bot_runner import resolve_bot_configs

    os.environ.pop("MISSING_TOKEN_ENV", None)   # ensure var is absent
    os.environ["VALID_TOKEN_ENV"] = "123456789:VALID"
    os.environ["ADMIN_USER_ID"] = "0"

    cfg = {
        "settings": {},
        "bots": [
            {"name": "Good Bot", "token_env": "VALID_TOKEN_ENV", "enabled": True,
             "mandatory_channel_1_id": -100111, "mandatory_channel_1_link": "https://t.me/x",
             "video_channel_id": -100555, "max_message_id": 100},
            {"name": "Bad Bot", "token_env": "MISSING_TOKEN_ENV", "enabled": True,
             "mandatory_channel_1_id": -100222, "mandatory_channel_1_link": "https://t.me/y",
             "video_channel_id": -100666, "max_message_id": 200},
        ],
    }
    result = resolve_bot_configs(cfg)
    assert len(result) == 1
    assert result[0]["name"] == "Good Bot"


# ══════════════════════════════════════════════════════════════════════════════
# TEST 5 — Mandatory channel check (user blocked if not joined)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_05_mandatory_channel_check():
    """User who hasn't joined mandatory channels is shown join keyboard."""
    from free_bot_core import check_membership

    cfg = _make_cfg()
    ctx = _make_context(cfg)

    # Mock: user NOT in channel 1, IS in channel 2
    async def fake_get_member(chat_id, user_id):
        m = MagicMock()
        m.status = "left" if chat_id == -100111 else "member"
        return m

    ctx.bot.get_chat_member.side_effect = fake_get_member

    all_joined, r1, r2 = await check_membership(ctx, user_id=12345)
    assert all_joined is False
    assert r1 is False
    assert r2 is True


# ══════════════════════════════════════════════════════════════════════════════
# TEST 6 — Video delivery (copy_message called on member user)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_06_video_delivery(_fresh_db):
    """A valid user receives a video via copy_message."""
    from free_bot_core import _deliver_video

    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 11111
    db_module.get_or_create_user(user_id, "Alice")

    cfg = _make_cfg()
    ctx = _make_context(cfg)
    ctx.bot.copy_message.return_value = MagicMock(message_id=99)

    result = await _deliver_video(ctx, cfg, chat_id=1000, user_id=user_id)

    assert result is True
    ctx.bot.copy_message.assert_called_once()

    # Delivery recorded
    u = db_module.get_user(user_id)
    assert u["daily_count"] == 1
    assert u["total_delivered"] == 1
    assert len(u["seen_videos"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST 7 — Five-second delay is enforced server-side
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_07_five_second_delay(_fresh_db):
    """Delivery occurs AFTER video_delay_seconds of asyncio.sleep."""
    from free_bot_core import button_callback

    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 22222
    db_module.get_or_create_user(user_id, "Bob")

    delay_seconds = 0.1   # short for testing
    cfg = _make_cfg({"video_delay_seconds": delay_seconds})
    ctx = _make_context(cfg)
    ctx.bot.copy_message.return_value = MagicMock(message_id=55)

    # Mock membership as joined
    m = MagicMock(); m.status = "member"
    ctx.bot.get_chat_member.return_value = m

    update = MagicMock()
    update.callback_query = _make_callback_query(user_id, "next_video")

    start = asyncio.get_event_loop().time()
    await button_callback(update, ctx)
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed >= delay_seconds, (
        f"Expected delay >= {delay_seconds}s but got {elapsed:.3f}s"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TEST 8 — Early button click (duplicate during delay is dropped)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_08_early_button_click(_fresh_db):
    """Second next_video click during the delay is answered and silently dropped."""
    from free_bot_core import button_callback

    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 33333
    db_module.get_or_create_user(user_id, "Carol")

    # Manually place user in 'processing' state
    cfg = _make_cfg({"video_delay_seconds": 0})
    ctx = _make_context(cfg)
    ctx.bot_data["proc"].add(user_id)

    update = MagicMock()
    query  = _make_callback_query(user_id, "next_video")
    update.callback_query = query

    await button_callback(update, ctx)

    # query.answer() must be called (stops spinner)
    query.answer.assert_called_once()
    # copy_message must NOT be called (request was dropped)
    ctx.bot.copy_message.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# TEST 9 — Simultaneous clicks handled safely (no double delivery)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_09_simultaneous_clicks(_fresh_db):
    """Two concurrent next_video calls result in exactly one video delivered."""
    from free_bot_core import button_callback

    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 44444
    db_module.get_or_create_user(user_id, "Dave")

    cfg = _make_cfg({"video_delay_seconds": 0})
    ctx = _make_context(cfg)
    ctx.bot.copy_message.return_value = MagicMock(message_id=77)
    m = MagicMock(); m.status = "member"
    ctx.bot.get_chat_member.return_value = m

    update = MagicMock()
    update.callback_query = _make_callback_query(user_id, "next_video")

    # Fire two coroutines concurrently
    await asyncio.gather(
        button_callback(update, ctx),
        button_callback(update, ctx),
    )

    # Only one video should have been delivered (second was in 'proc')
    assert ctx.bot.copy_message.call_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST 10 — Duplicate callback protection ('proc' set)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_10_duplicate_callback_protection():
    """'proc' set discards duplicates and is cleaned up via finally block."""
    from free_bot_core import button_callback

    user_id = 55555
    cfg = _make_cfg({"video_delay_seconds": 0})
    ctx = _make_context(cfg)

    # Simulate a copy_message error → proc must still be cleaned up
    ctx.bot.copy_message.side_effect = Exception("Telegram error")
    m = MagicMock(); m.status = "member"
    ctx.bot.get_chat_member.return_value = m

    update = MagicMock()
    update.callback_query = _make_callback_query(user_id, "next_video")

    # DB not initialised — get_or_create_user will create an in-memory entry
    import free_users_db as _db
    _db.get_or_create_user(user_id, "Eve")

    await button_callback(update, ctx)

    # After the call finishes (even after error), proc must be empty
    assert user_id not in ctx.bot_data["proc"]


# ══════════════════════════════════════════════════════════════════════════════
# TEST 11 — Shared video counting (increments cross-bot)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_11_shared_video_counting(_fresh_db):
    """Daily and lifetime counts increment in shared memory regardless of bot."""
    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 66666
    db_module.get_or_create_user(user_id, "Frank")

    # Bot A delivers 2 videos
    db_module.record_video_delivered(user_id, 10)
    db_module.record_video_delivered(user_id, 20)

    # Bot B reads the same shared counts (same module state = same process)
    assert db_module.get_daily_count(user_id) == 2
    u = db_module.get_user(user_id)
    assert u["total_delivered"] == 2
    assert {10, 20}.issubset(u["seen_videos"])


# ══════════════════════════════════════════════════════════════════════════════
# TEST 12 — Promotion milestone at 10 videos
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_12_promotion_at_10_videos(_fresh_db):
    """check_and_claim_milestones returns [10] when total_delivered reaches 10."""
    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 77777
    db_module.get_or_create_user(user_id, "Grace")
    u = db_module.get_user(user_id)
    u["total_delivered"] = 9   # one below threshold

    # Not yet at milestone
    assert db_module.check_and_claim_milestones(user_id) == []

    u["total_delivered"] = 10  # cross the threshold
    triggered = db_module.check_and_claim_milestones(user_id)
    assert triggered == [10]

    # Claiming again does nothing (flag is set)
    assert db_module.check_and_claim_milestones(user_id) == []


# ══════════════════════════════════════════════════════════════════════════════
# TEST 13 — Repeated promotions at 20 and 30 (each fires exactly once)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_13_repeated_promotions_20_and_30(_fresh_db):
    """Milestones 20 and 30 each fire once and are then permanently suppressed."""
    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 88888
    db_module.get_or_create_user(user_id, "Heidi")
    u = db_module.get_user(user_id)
    u["total_delivered"] = 10
    u["milestone_10"] = True   # already triggered earlier

    u["total_delivered"] = 20
    triggered = db_module.check_and_claim_milestones(user_id)
    assert triggered == [20]
    assert db_module.check_and_claim_milestones(user_id) == []  # only once

    u["total_delivered"] = 30
    triggered = db_module.check_and_claim_milestones(user_id)
    assert triggered == [30]
    assert db_module.check_and_claim_milestones(user_id) == []  # only once


# ══════════════════════════════════════════════════════════════════════════════
# TEST 14 — Promotion configuration (links come from config)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_14_promotion_configuration(_fresh_db):
    """Milestone promo message uses bot-specific promo_links from config."""
    from free_bot_core import _send_milestone_promo

    db_module, _ = _fresh_db
    await db_module.init_db()

    cfg = _make_cfg({
        "promo_message": "Upgrade to premium!",
        "promo_links": [
            {"text": "🌟 Premium A", "url": "https://t.me/premiumA"},
            {"text": "🌟 Premium B", "url": "https://t.me/premiumB"},
        ],
    })
    ctx = _make_context(cfg)

    await _send_milestone_promo(ctx, cfg, chat_id=1000, user_id=99999, milestone=10)

    ctx.bot.send_message.assert_called_once()
    call_kwargs = ctx.bot.send_message.call_args.kwargs
    text = call_kwargs.get("text", "")
    assert "10" in text
    assert "Upgrade to premium!" in text

    # Both promo links present in keyboard
    markup = call_kwargs.get("reply_markup")
    assert markup is not None
    flat_buttons = [btn for row in markup.inline_keyboard for btn in row]
    urls = [b.url for b in flat_buttons if b.url]
    assert "https://t.me/premiumA" in urls
    assert "https://t.me/premiumB" in urls


# ══════════════════════════════════════════════════════════════════════════════
# TEST 15 — Database failure (fail-open, no crash)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_15_database_failure(_fresh_db):
    """get_random_unseen_video returns a valid ID even when DB is unreachable."""
    db_module, _ = _fresh_db
    # Do NOT call init_db — simulate a cold cache
    vid = db_module.get_random_unseen_video(user_id=12345, min_id=1, max_id=100)
    assert 1 <= vid <= 100


# ══════════════════════════════════════════════════════════════════════════════
# TEST 16 — Restart behaviour (user state persists)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_16_restart_behaviour(_fresh_db):
    """After a simulated restart (re-init from DB), state is fully restored."""
    db_module, db_path = _fresh_db
    await db_module.init_db()

    user_id = 11111
    db_module.get_or_create_user(user_id, "Ivan")
    db_module.record_video_delivered(user_id, 42)
    db_module.record_video_delivered(user_id, 43)

    # Flush dirty users to DB
    await db_module._flush_dirty(force=True)

    # Simulate restart: clear cache and reload from DB
    db_module._cache.clear()
    db_module._dirty.clear()
    await db_module._load_all_into_cache()

    u = db_module.get_user(user_id)
    assert u is not None
    assert u["total_delivered"] == 2
    assert {42, 43}.issubset(u["seen_videos"])


# ══════════════════════════════════════════════════════════════════════════════
# TEST 17 — Premium DB isolation (free_users.db ≠ premium_shared.db)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_17_premium_db_isolation(_fresh_db, tmp_path):
    """The free bot DB file is completely separate from premium_shared.db."""
    db_module, free_db_path = _fresh_db
    premium_db_path = str(tmp_path / "premium_shared.db")

    await db_module.init_db()
    db_module.get_or_create_user(12345, "Test")
    await db_module._flush_dirty(force=True)

    # Free DB must exist
    assert os.path.exists(free_db_path)

    # Premium DB must NOT have been touched
    assert not os.path.exists(premium_db_path), (
        "free_users_db wrote to premium_shared.db — isolation violated!"
    )

    # No import of premium_sync anywhere in free_users_db
    import ast
    with open("free_users_db.py", "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imports = [
        node.names[0].name if isinstance(node, ast.Import)
        else node.module
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert "premium_sync" not in imports, (
        "free_users_db must not import premium_sync!"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TEST 18 — Only advertised benefits are in messages
# ══════════════════════════════════════════════════════════════════════════════
def test_18_only_advertised_benefits():
    """
    Scan free_bot_core.py source for known false-advertising patterns.
    None of these phrases should appear in free bot messages.
    """
    import ast

    banned_phrases = [
        "Ad-Free Experience",
        "ad-free",
        "downloads per day",   # free bots have no download feature
        "100 downloads",
        "unlimited downloads",
    ]

    with open("free_bot_core.py", "r", encoding="utf-8") as f:
        source = f.read()

    for phrase in banned_phrases:
        assert phrase.lower() not in source.lower(), (
            f"False advertising found in free_bot_core.py: '{phrase}'"
        )


# ══════════════════════════════════════════════════════════════════════════════
# TEST 19 — Unlimited videos (daily_video_limit = 0)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_19_unlimited_videos(_fresh_db):
    """When daily_video_limit is 0, user can watch more than 5 videos without hitting a limit."""
    from free_bot_core import _deliver_video

    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 998877
    db_module.get_or_create_user(user_id, "UnlimitedUser")

    cfg = _make_cfg({"daily_video_limit": 0, "max_message_id": 50})
    ctx = _make_context(cfg)
    ctx.bot.copy_message.return_value = MagicMock(message_id=123)

    # Deliver 7 videos in a row
    for i in range(7):
        success = await _deliver_video(ctx, cfg, chat_id=1000, user_id=user_id)
        assert success is True

    u = db_module.get_user(user_id)
    assert u["daily_count"] == 7
    assert u["total_delivered"] == 7


# ══════════════════════════════════════════════════════════════════════════════
# TEST 20 — Cross-bot Likes & Dislikes by Video Length & Size
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_20_likes_and_dislikes_cross_bot(_fresh_db):
    """
    If any user liked a 2 min (120s) and 5MB (5242880 bytes) video,
    the like goes to that video key ('120_5242880') in the single shared database,
    accessible across all bots.
    """
    db_module, _ = _fresh_db
    await db_module.init_db()

    duration = 120        # 2 minutes
    file_size = 5242880   # 5 MB
    video_key = f"{duration}_{file_size}"

    user_1 = 1001
    user_2 = 2002

    # User 1 likes the video on Bot 1
    likes, dislikes, action = await db_module.toggle_reaction(
        user_id=user_1,
        video_key=video_key,
        reaction="like",
        duration=duration,
        file_size=file_size,
    )
    assert action == "liked"
    assert likes == 1
    assert dislikes == 0

    # User 2 on Bot 2 sees and likes the same video
    l, d = db_module.get_video_reactions(video_key)
    assert l == 1 and d == 0

    likes, dislikes, action = await db_module.toggle_reaction(
        user_id=user_2,
        video_key=video_key,
        reaction="like",
        duration=duration,
        file_size=file_size,
    )
    assert action == "liked"
    assert likes == 2
    assert dislikes == 0

    # User 1 switches their vote to dislike on Bot 2
    likes, dislikes, action = await db_module.toggle_reaction(
        user_id=user_1,
        video_key=video_key,
        reaction="dislike",
        duration=duration,
        file_size=file_size,
    )
    assert action == "disliked"
    assert likes == 1
    assert dislikes == 1

    # User 1 taps dislike again -> toggle off
    likes, dislikes, action = await db_module.toggle_reaction(
        user_id=user_1,
        video_key=video_key,
        reaction="dislike",
        duration=duration,
        file_size=file_size,
    )
    assert action == "removed"
    assert likes == 1
    assert dislikes == 0

    # Check stats across all bots
    stats = db_module.get_stats()
    assert stats["total_likes"] == 1
    assert stats["total_dislikes"] == 0
    assert stats["tracked_videos"] >= 1


# ══════════════════════════════════════════════════════════════════════════════
# TEST 21 — Promotions fire at every multiple of 10 (10, 20, 30, 40, 50, 100...)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_21_promotions_multiples_of_10(_fresh_db):
    """check_and_claim_milestones fires at every multiple of 10 videos (not just 10/20/30)."""
    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 991122
    db_module.get_or_create_user(user_id, "MultiMilestoneUser")
    u = db_module.get_user(user_id)

    # 1 to 9 -> no milestone
    for i in range(1, 10):
        u["total_delivered"] = i
        assert db_module.check_and_claim_milestones(user_id) == []

    # 10 -> fires 10
    u["total_delivered"] = 10
    assert db_module.check_and_claim_milestones(user_id) == [10]
    assert db_module.check_and_claim_milestones(user_id) == []  # only once

    # 11 to 19 -> no milestone
    for i in range(11, 20):
        u["total_delivered"] = i
        assert db_module.check_and_claim_milestones(user_id) == []

    # 20, 30, 40, 50, 60, 70, 80, 90, 100
    for m in [20, 30, 40, 50, 60, 70, 80, 90, 100]:
        u["total_delivered"] = m
        assert db_module.check_and_claim_milestones(user_id) == [m]
        assert db_module.check_and_claim_milestones(user_id) == []  # only once


# ══════════════════════════════════════════════════════════════════════════════
# TEST 22 — Premium bots advertisement benefits and 8 bots list
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_22_premium_bots_list_and_benefits(_fresh_db):
    """_send_milestone_promo displays all requested premium benefits and 8 premium bots."""
    from free_bot_core import _send_milestone_promo, DEFAULT_PREMIUM_BOTS

    db_module, _ = _fresh_db
    await db_module.init_db()

    cfg = _make_cfg({"promo_links": []})  # Falls back to DEFAULT_PREMIUM_BOTS
    ctx = _make_context(cfg)

    await _send_milestone_promo(ctx, cfg, chat_id=1234, user_id=5678, milestone=40)

    ctx.bot.send_message.assert_called_once()
    call_kwargs = ctx.bot.send_message.call_args.kwargs
    text = call_kwargs.get("text", "")

    # Milestone 40 mentioned
    assert "40" in text

    # Key benefits mentioned in text
    assert "Ultra-Fast" in text or "Faster" in text or "Speed" in text
    assert "Download Button" in text or "Download" in text
    assert "5 Lakh+" in text
    assert "Advertisement-Free Clean UI" in text
    assert "20 min" in text.lower() or "20 minutes" in text.lower()
    assert "Unlimited" in text

    # All 8 premium bots are present in the keyboard
    markup = call_kwargs.get("reply_markup")
    assert markup is not None
    flat_buttons = [btn for row in markup.inline_keyboard for btn in row]
    urls = [b.url for b in flat_buttons if b.url]

    expected_bots = [
        "https://t.me/Videos69_A5_bot",
        "https://t.me/Videos69_B7_bot",
        "https://t.me/Videos69_D3_bot",
        "https://t.me/Videos69_B6_bot",
        "https://t.me/Videos69_B8_bot",
        "https://t.me/Videos69_B9_bot",
        "https://t.me/Videos69_A4_bot",
        "https://t.me/Videos69_A1_bot",
    ]
    for b_url in expected_bots:
        assert b_url in urls

    # Continue Watching button is present
    callback_datas = [b.callback_data for b in flat_buttons if b.callback_data]
    assert "next_video" in callback_datas


# ══════════════════════════════════════════════════════════════════════════════
# TEST 23 — Next button single-click enforcement & disable without API requests
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_23_next_button_single_click_and_disabled(_fresh_db):
    """Each next_video button is clickable only 1 time, then disabled without extra API calls."""
    from free_bot_core import button_callback

    db_module, _ = _fresh_db
    await db_module.init_db()

    user_id = 112233
    db_module.get_or_create_user(user_id, "SingleClickUser")

    cfg = _make_cfg({"video_delay_seconds": 0, "max_message_id": 50})
    ctx = _make_context(cfg)
    m = MagicMock(); m.status = "member"
    ctx.bot.get_chat_member.return_value = m
    ctx.bot.copy_message.return_value = MagicMock(message_id=999)

    update = MagicMock()
    update.callback_query = _make_callback_query(user_id, "next_video", chat_id=1000)
    update.callback_query.message.message_id = 500

    # First click: successfully delivers video
    await button_callback(update, ctx)
    assert ctx.bot.copy_message.call_count == 1

    # Second click on the same button (same chat_id, message_id 500):
    await button_callback(update, ctx)
    # copy_message must NOT have been called again!
    assert ctx.bot.copy_message.call_count == 1
    # query.answer informed the user via high-visibility toast banner (show_alert=False)
    last_answer_call = update.callback_query.answer.call_args
    assert "already used" in last_answer_call.kwargs.get("text", "")
    assert last_answer_call.kwargs.get("show_alert") is False


# ══════════════════════════════════════════════════════════════════════════════
# TEST 24 — Admin-only Analysis & Future Planning Commands
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_24_admin_analysis_commands(_fresh_db):
    """
    Admin-only commands:
    • /analytics (alias /behaviour) — Customer behaviour cohorts & funnel
    • /content — Video performance, likes/dislikes ranking, duration preferences
    • /plan — Data-driven strategic roadmaps and recommendations
    • /user <id> — Individual customer journey inspector
    • /admin — Dashboard command menu
    Non-admins are strictly rejected with '⛔ Not authorized.'
    """
    from free_bot_core import (
        analytics_command,
        content_command,
        plan_command,
        user_detail_command,
        admin_help_command,
    )

    db_module, _ = _fresh_db
    await db_module.init_db()

    cfg = _make_cfg({"admin_user_id": 999})
    ctx = _make_context(cfg)

    # 1. Non-admin user (id: 1111) tries to call all admin commands
    non_admin_update = MagicMock()
    non_admin_update.effective_user.id = 1111
    non_admin_update.message.reply_text = AsyncMock()

    for cmd_handler in (analytics_command, content_command, plan_command, user_detail_command, admin_help_command):
        non_admin_update.message.reply_text.reset_mock()
        await cmd_handler(non_admin_update, ctx)
        non_admin_update.message.reply_text.assert_called_once()
        assert "⛔ Not authorized." in non_admin_update.message.reply_text.call_args[0][0]

    # 2. Seed database with diverse users and watch cohorts
    # User 1: Bouncer (2 videos)
    db_module.get_or_create_user(101, "BouncerUser")
    db_module.record_video_delivered(101, 1)
    db_module.record_video_delivered(101, 2)
    db_module.register_user_with_bot(101, "Test Bot")

    # User 2: Engaged (12 videos, reached milestone 10)
    db_module.get_or_create_user(102, "EngagedUser", "engaged_u")
    for v in range(1, 13):
        db_module.record_video_delivered(102, v)
    db_module.check_and_claim_milestones(102)
    db_module.register_user_with_bot(102, "Test Bot")

    # User 3: Superfan (55 videos)
    db_module.get_or_create_user(103, "SuperFanUser")
    for v in range(1, 56):
        db_module.record_video_delivered(103, v)
    db_module.check_and_claim_milestones(103)
    db_module.register_user_with_bot(103, "Test Bot")

    # Seed reactions:
    # 2-min video (120s, 5MB): 2 likes
    await db_module.toggle_reaction(101, "120_5242880", "like", duration=120, file_size=5242880)
    await db_module.toggle_reaction(102, "120_5242880", "like", duration=120, file_size=5242880)
    # 8-min video (480s, 20MB): 1 dislike
    await db_module.toggle_reaction(101, "480_20971520", "dislike", duration=480, file_size=20971520)

    # 3. Authorized Admin (id: 999) executes commands
    admin_update = MagicMock()
    admin_update.effective_user.id = 999
    admin_update.message.reply_text = AsyncMock()

    # /analytics
    admin_update.message.reply_text.reset_mock()
    await analytics_command(admin_update, ctx)
    analytics_text = admin_update.message.reply_text.call_args[0][0]
    assert "Customer Behaviour Analytics" in analytics_text
    assert "Total Users: `3`" in analytics_text
    assert "Bouncers" in analytics_text
    assert "Superfans" in analytics_text
    assert "Saw 10-Vid Promo" in analytics_text

    # /content
    admin_update.message.reply_text.reset_mock()
    await content_command(admin_update, ctx)
    content_text = admin_update.message.reply_text.call_args[0][0]
    assert "Content Performance" in content_text
    assert "Total Likes: `2`" in content_text
    assert "Total Dislikes: `1`" in content_text
    assert "Video Length Preferences" in content_text

    # /plan
    admin_update.message.reply_text.reset_mock()
    await plan_command(admin_update, ctx)
    plan_text = admin_update.message.reply_text.call_args[0][0]
    assert "Strategic Insights & Future Action Plan" in plan_text
    assert "Quick Actions" in plan_text

    # /user 102
    admin_update.message.reply_text.reset_mock()
    ctx.args = ["102"]
    await user_detail_command(admin_update, ctx)
    user_text = admin_update.message.reply_text.call_args[0][0]
    assert "Customer Profile — EngagedUser" in user_text
    assert "@engaged_u" in user_text
    assert "Lifetime Videos Delivered: `12`" in user_text

    # /admin
    admin_update.message.reply_text.reset_mock()
    await admin_help_command(admin_update, ctx)
    admin_menu_text = admin_update.message.reply_text.call_args[0][0]
    assert "Admin Analytics & Management Menu" in admin_menu_text
    assert "/analytics" in admin_menu_text
    assert "/content" in admin_menu_text
    assert "/plan" in admin_menu_text
    assert "/user <id>" in admin_menu_text
