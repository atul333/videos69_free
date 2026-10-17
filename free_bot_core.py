"""
free_bot_core.py — Shared Handler Logic for All 9 Free Bots
=============================================================
All Telegram handlers live here. Per-bot configuration is stored in
context.bot_data['cfg'] and injected at Application build time.

Key design decisions
---------------------
• No global constants — every setting comes from cfg.
• No premium system — this is a pure free-video-delivery bot.
• No ad-watch / Razorpay — only mandatory-channel enforcement.
• 5-second delay: answer callback IMMEDIATELY (zero extra API calls during
  the wait), then asyncio.sleep(5) server-side, then deliver the video.
  This reuses the existing callback interaction and adds NO API calls.
• Duplicate click guard: per-user 'processing' set prevents parallel
  video-delivery coroutines for the same user.
• Promotion milestones: after delivering the 10th / 20th / 30th lifetime
  video the user gets a configurable promotional message with premium bot
  links. Each milestone fires exactly once (persisted in shared DB).
• All user state is read/written via free_users_db (shared across bots).
• Only benefits actually provided are advertised.
"""

import asyncio
import logging
import time as _time
import traceback
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import RetryAfter, Forbidden, TelegramError
from telegram.request import HTTPXRequest
from typing import List, Optional

import free_users_db as db

logger = logging.getLogger("free_bot_core")

_IST = timedelta(hours=5, minutes=30)

# ── Config accessor helpers ───────────────────────────────────────────────────

def _cfg(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.bot_data["cfg"]

def _mc(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    """Membership cache: {user_id: (r1, r2, expires_monotonic)}"""
    return ctx.bot_data.setdefault("mc", {})

def _proc(ctx: ContextTypes.DEFAULT_TYPE) -> set:
    """Set of user_ids currently inside the 3-second delivery coroutine."""
    return ctx.bot_data.setdefault("proc", set())

def _used_buttons(ctx: ContextTypes.DEFAULT_TYPE) -> set:
    """Set of (chat_id, message_id) tuples whose 'next_video' button has been consumed."""
    return ctx.bot_data.setdefault("used_next_buttons", set())


# ── Mandatory channel membership ──────────────────────────────────────────────

def _join_keyboard(cfg: dict, r1: bool, r2: bool) -> InlineKeyboardMarkup:
    """Build keyboard showing ONLY the channels the user hasn't joined yet."""
    buttons = []
    ch1_link = cfg.get("mandatory_channel_1_link", "")
    if not r1 and ch1_link:
        buttons.append([InlineKeyboardButton(
            "📢 Join Channel 1", url=ch1_link
        )])
    ch2_id   = cfg.get("mandatory_channel_2_id", 0)
    ch2_link = cfg.get("mandatory_channel_2_link", "")
    if not r2 and ch2_id and ch2_id != cfg["mandatory_channel_1_id"] and ch2_link:
        buttons.append([InlineKeyboardButton(
            "📢 Join Channel 2", url=ch2_link
        )])
    buttons.append([InlineKeyboardButton(
        "🔄 I've Joined — Verify", callback_data="check_membership"
    )])
    return InlineKeyboardMarkup(buttons)


async def check_membership(ctx: ContextTypes.DEFAULT_TYPE,
                           user_id: int) -> tuple[bool, bool, bool]:
    """
    Return (all_joined, r1, r2).
    Result cached for membership_cache_ttl seconds per bot.
    Proactively invalidated when user leaves via ChatMemberHandler.
    Fail-closed: any API error → treat as not joined.
    """
    cfg = _cfg(ctx)
    mc  = _mc(ctx)
    now = _time.monotonic()

    cached = mc.get(user_id)
    if cached:
        r1, r2, exp = cached
        if now < exp:
            return r1 and r2, r1, r2

    # Evict stale entries so the dict doesn't grow unbounded
    if len(mc) > 50_000:
        cutoff = now - cfg["membership_cache_ttl"]
        stale = [k for k, (_, __, e) in mc.items() if e < cutoff]
        for k in stale:
            del mc[k]

    async def _one(ch_id: int) -> bool:
        try:
            m = await ctx.bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            return m.status not in ("left", "kicked", "banned")
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in
                   ("forbidden", "not enough rights", "chat not found", "bot was kicked")):
                logger.warning("[%s] Bot not admin in channel %s: %s",
                               cfg["name"], ch_id, e)
            else:
                logger.debug("[%s] Membership check error ch=%s: %s",
                             cfg["name"], ch_id, e)
            return False  # fail-closed

    ch2_id = cfg.get("mandatory_channel_2_id", 0)
    if ch2_id and ch2_id != cfg["mandatory_channel_1_id"]:
        r1, r2 = await asyncio.gather(
            _one(cfg["mandatory_channel_1_id"]),
            _one(ch2_id),
        )
    else:
        r1 = await _one(cfg["mandatory_channel_1_id"])
        r2 = True  # only one channel configured

    mc[user_id] = (r1, r2, now + cfg["membership_cache_ttl"])
    return r1 and r2, r1, r2


# ── Video delivery ────────────────────────────────────────────────────────────

async def _delete_video_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Job callback: delete a video message after 10 minutes."""
    data = ctx.job.data
    try:
        await ctx.bot.delete_message(
            chat_id=data["chat_id"], message_id=data["message_id"]
        )
    except Exception:
        pass  # already deleted or expired — silently ignore


DEFAULT_PREMIUM_BOTS = [
    {"text": "🤖 @Videos69_A5_bot", "url": "https://t.me/Videos69_A5_bot"},
    {"text": "🤖 @Videos69_B7_bot", "url": "https://t.me/Videos69_B7_bot"},
    {"text": "🤖 @Videos69_D3_bot", "url": "https://t.me/Videos69_D3_bot"},
    {"text": "🤖 @Videos69_B6_bot", "url": "https://t.me/Videos69_B6_bot"},
    {"text": "🤖 @Videos69_B8_bot", "url": "https://t.me/Videos69_B8_bot"},
    {"text": "🤖 @Videos69_B9_bot", "url": "https://t.me/Videos69_B9_bot"},
    {"text": "🤖 @Videos69_A4_bot", "url": "https://t.me/Videos69_A4_bot"},
    {"text": "🤖 @Videos69_A1_bot", "url": "https://t.me/Videos69_A1_bot"},
]


async def _send_milestone_promo(ctx: ContextTypes.DEFAULT_TYPE, cfg: dict,
                                chat_id: int, user_id: int, milestone: int):
    """
    Send a promotional message when user hits a multiple-of-10 milestone.
    Advertises exclusive premium bot benefits and provides the list of premium bots.
    """
    promo_msg   = cfg.get("promo_message", "")
    promo_links = cfg.get("promo_links") or DEFAULT_PREMIUM_BOTS

    # Build 2-column inline keyboard for premium bots
    keyboard = []
    row = []
    for link in promo_links:
        row.append(InlineKeyboardButton(link["text"], url=link["url"]))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("▶️ Continue Watching", callback_data="next_video")])

    # Format list of premium bots safely for Markdown
    bots_list_lines = []
    for link in promo_links:
        raw_text = link.get("text", "")
        safe_text = raw_text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        bots_list_lines.append(f"{safe_text}")
    bots_list_str = "\n".join(bots_list_lines)

    custom_promo = f"\n\n📢 {promo_msg}" if promo_msg else ""

    text = (
        f"🎉 *You've watched {milestone} videos!* 🎉\n\n"
        f"💎 *Upgrade to Premium for the Ultimate Experience!*{custom_promo}\n\n"
        f"✨ *Exclusive Premium Benefits:*\n"
        f"⚡ *Ultra-Fast Delivery* — Instant loading, much faster than free bots!\n"
        f"📥 *Download Button* — Save every video directly to your gallery!\n"
        f"🎬 *5 Lakh+ Videos* — Access over 500,000+ exclusive videos!\n"
        f"🚫 *Advertisement-Free Clean UI* — 100% clean interface with zero ads!\n"
        f"⏳ *20 Min Watch Time* — Video will delete after 20 min instead of 10 min!\n"
        f"🍿 *Unlimited 24/7* — Watch non-stop without limits or delays!\n"
        f"🔥 *HD Video Quality* — High-definition crystal-clear playback!\n\n"
        f"🤖 *Available Premium Bots:*\n"
        f"{bots_list_str}\n\n"
        f"👇 *Tap any bot below to get started:*"
    )
    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        logger.info("[%s] Milestone %d promo sent to uid=%s", cfg["name"], milestone, user_id)
    except Exception as e:
        logger.warning("[%s] Milestone promo failed uid=%s: %s", cfg["name"], user_id, e)


async def _deliver_video(ctx: ContextTypes.DEFAULT_TYPE, cfg: dict,
                         chat_id: int, user_id: int) -> bool:
    """
    Attempt to deliver a random video to the user (up to 10 tries).
    Records delivery ONLY after a successful copy_message — no duplicate counts.
    Schedules 20-minute deletion.
    Returns True on success, False if all attempts exhausted.
    """
    daily_limit = cfg.get("daily_video_limit", 0)

    for _ in range(10):
        video_id = db.get_random_unseen_video(
            user_id, cfg["min_message_id"], cfg["max_message_id"]
        )
        try:
            # Discover video length & size and retrieve cross-bot reaction counts
            dur, sz, vkey = await db.get_or_fetch_video_meta(
                cfg["video_channel_id"], video_id, ctx.bot
            )
            likes, dislikes = db.get_video_reactions(vkey)

            if daily_limit > 0:
                u = db.get_user(user_id)
                remaining_after = max(0, daily_limit - (u["daily_count"] if u else 0) - 1)
                access_line = f"⚡ *Access:* Free Daily Videos: `{remaining_after}/{daily_limit}` 🍿"
            else:
                access_line = "⚡ *Access:* Unlimited Free Videos 🔥"

            caption = (
                f"🎬 *Enjoy Your Video!* ✨\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{access_line}\n"
                f"⏳ *Auto-Delete:* Removed in 10 minutes ⚠️\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👇 *Rate below & tap Next Video to continue!* 🍿"
            )
            keyboard = [
                [
                    InlineKeyboardButton(f"👍 {likes}", callback_data=f"like:{vkey}"),
                    InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislike:{vkey}"),
                ],
                [InlineKeyboardButton("▶️ Next Video", callback_data="next_video")],
            ]

            # ── SINGLE API CALL: video + caption + buttons in one request ─────
            video_msg = await ctx.bot.copy_message(
                chat_id=chat_id,
                from_chat_id=cfg["video_channel_id"],
                message_id=video_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
                protect_content=True,
            )

            # Record AFTER successful send — prevents duplicate increments
            new_daily, new_total = db.record_video_delivered(user_id, video_id)

            logger.info("[%s] Video %s delivered to uid=%s (daily=%d total=%d)",
                        cfg["name"], video_id, user_id, new_daily, new_total)

            # Schedule 10-minute deletion (no persistent registry — simpler)
            ctx.job_queue.run_once(
                _delete_video_job,
                when=600,
                data={"chat_id": chat_id, "message_id": video_msg.message_id},
                name=f"del_{chat_id}_{video_msg.message_id}",
            )

            # Fire promotion milestones (non-blocking task — never delays delivery)
            milestones = db.check_and_claim_milestones(user_id)
            for m in milestones:
                asyncio.create_task(
                    _send_milestone_promo(ctx, cfg, chat_id, user_id, m),
                    name=f"promo_{user_id}_{m}",
                )

            return True

        except Exception as e:
            err = str(e).lower()
            # If message doesn't exist / isn't a video → mark seen so we skip it
            if any(kw in err for kw in
                   ("message_id_invalid", "can't be used", "not found",
                    "no message", "bad request")):
                u = db.get_user(user_id)
                if u:
                    u["seen_videos"].add(video_id)
                    db.mark_user_dirty(user_id)
            else:
                logger.debug("[%s] copy_message failed video=%s: %s",
                             cfg["name"], video_id, e)

    return False  # all 10 attempts exhausted


# ── Command handlers ──────────────────────────────────────────────────────────

async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /start."""
    cfg      = _cfg(ctx)
    user     = update.effective_user
    user_id  = user.id
    first_name = user.first_name or "there"

    db.get_or_create_user(user_id, first_name, user.username)
    db.register_user_with_bot(user_id, cfg["name"])

    all_joined, r1, r2 = await check_membership(ctx, user_id)
    if not all_joined:
        missing = ([] if r1 else ["Channel 1"]) + ([] if r2 else ["Channel 2"])
        await update.message.reply_text(
            f"👋 Welcome {first_name}!\n\n"
            f"🔒 To use this bot, please join "
            f"**{' and '.join(missing)}** first.\n\n"
            "Click the button(s) below, then tap Verify. 🎬",
            parse_mode="Markdown",
            reply_markup=_join_keyboard(cfg, r1, r2),
        )
        return

    daily_limit = cfg.get("daily_video_limit", 0)
    if daily_limit > 0:
        daily_count = db.get_daily_count(user_id)
        remaining   = max(0, daily_limit - daily_count)
        limit_desc  = (
            f"🎥 Free videos per day: *{daily_limit}*\n"
            f"📺 Remaining today: *{remaining}/{daily_limit}*\n"
            f"⏰ Limit resets at *midnight IST* every day\n\n"
        )
    else:
        limit_desc = (
            "🎥 Free videos: *Unlimited* 🔥\n"
            "🍿 Watch as many videos as you want anytime!\n\n"
        )
    keyboard = [[InlineKeyboardButton("🎬 Get First Video", callback_data="next_video")]]

    await update.message.reply_text(
        f"🎬 *Welcome to {cfg['name']}!* 🎬\n\n"
        f"Hello {first_name}! 👋\n\n"
        f"I share amazing videos with you every day!\n\n"
        f"{limit_desc}"
        f"Enjoy! 🍿",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /help."""
    cfg        = _cfg(ctx)
    first_name = update.effective_user.first_name or "there"
    keyboard   = []
    if cfg.get("support_url"):
        keyboard.append([InlineKeyboardButton("💬 Contact Support", url=cfg["support_url"])])
    keyboard.append([InlineKeyboardButton("🎬 Get Video", callback_data="next_video")])

    daily_limit = cfg.get("daily_video_limit", 0)
    limit_desc  = (
        f"📺 Free videos: *{daily_limit} per day*\n⏰ Resets at *midnight IST*\n\n"
        if daily_limit > 0 else
        "📺 Free videos: *Unlimited* 🔥\n\n"
    )

    await update.message.reply_text(
        f"❓ *Need Help?*\n\n"
        f"Hello {first_name}! 👋\n\n"
        f"{limit_desc}"
        f"For support, contact us using the button below:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Callback handler ──────────────────────────────────────────────────────────

async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle all inline-button callbacks."""
    query   = update.callback_query
    user_id = query.from_user.id
    cfg     = _cfg(ctx)
    data    = query.data

    # ── check_membership ──────────────────────────────────────────────────────
    if data == "check_membership":
        try:
            await query.answer()
        except Exception:
            pass

        first_name = query.from_user.first_name or "there"
        _mc(ctx).pop(user_id, None)  # force a fresh API call
        all_joined, r1, r2 = await check_membership(ctx, user_id)

        if all_joined:
            db.get_or_create_user(user_id, first_name, query.from_user.username)
            db.register_user_with_bot(user_id, cfg["name"])
            daily_limit = cfg.get("daily_video_limit", 0)
            if daily_limit > 0:
                daily_count = db.get_daily_count(user_id)
                remaining   = max(0, daily_limit - daily_count)
                limit_info  = (
                    f"🎥 Free videos per day: *{daily_limit}*\n"
                    f"📺 Remaining today: *{remaining}/{daily_limit}*\n"
                    f"⏰ Resets at *midnight IST* every day\n\n"
                )
            else:
                limit_info = (
                    "🎥 Free videos: *Unlimited* 🔥\n"
                    "🍿 Watch as many videos as you want anytime!\n\n"
                )
            try:
                await query.message.edit_text(
                    f"✅ *Verified!*\n\n"
                    f"Welcome {first_name}! 👋\n\n"
                    f"You've successfully joined both channels!\n\n"
                    f"{limit_info}"
                    f"Click below to start watching! 🍿",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🎬 Get First Video",
                                               callback_data="next_video")]]
                    ),
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    logger.debug("[%s] edit_text: %s", cfg["name"], e)
        else:
            missing = ([] if r1 else ["Channel 1"]) + ([] if r2 else ["Channel 2"])
            try:
                await query.message.edit_text(
                    f"❌ *Still need to join {' and '.join(missing)}*\n\n"
                    "Please join the channel(s) shown below, then tap Verify again. 🔒",
                    parse_mode="Markdown",
                    reply_markup=_join_keyboard(cfg, r1, r2),
                )
            except Exception as e:
                if "Message is not modified" not in str(e):
                    logger.debug("[%s] edit_text: %s", cfg["name"], e)
        return

    # ── like / dislike ────────────────────────────────────────────────────────
    if data.startswith("like:") or data.startswith("dislike:"):
        reaction = "like" if data.startswith("like:") else "dislike"
        vkey     = data.split(":", 1)[1]
        likes, dislikes, action = await db.toggle_reaction(user_id, vkey, reaction)

        alert_text = (
            "You liked this video! 👍" if action == "liked" else (
                "You disliked this video! 👎" if action == "disliked" else "Reaction removed"
            )
        )
        try:
            await query.answer(text=alert_text)
        except Exception:
            pass

        # Update message inline keyboard with latest reaction counts
        new_keyboard = [
            [
                InlineKeyboardButton(f"👍 {likes}", callback_data=f"like:{vkey}"),
                InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislike:{vkey}"),
            ],
            [InlineKeyboardButton("▶️ Next Video", callback_data="next_video")],
        ]
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(new_keyboard)
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.debug("[%s] edit_reply_markup: %s", cfg["name"], e)
        return

    # ── next_video ────────────────────────────────────────────────────────────
    if data == "next_video":
        proc = _proc(ctx)
        used = _used_buttons(ctx)

        msg = query.message
        btn_key = (msg.chat_id, msg.message_id) if msg else None

        # 1. Single-click enforcement: this button can only be clicked 1 time.
        # Once clicked, it is disabled without making any extra Telegram API requests.
        if btn_key and btn_key in used:
            try:
                await query.answer(
                    text="⚠️ Button already used! Please use the Next button on your latest video.",
                    show_alert=False,
                )
            except Exception:
                pass
            return

        # 2. Prevent duplicate clicks during the 3-second wait
        if user_id in proc:
            try:
                await query.answer(
                    text="⏳ PLEASE WAIT! Next video is loading in 3 seconds... 🎬",
                    show_alert=False,
                )
            except Exception:
                pass
            return

        # Mark this button as consumed immediately (click only 1 time)
        if btn_key:
            used.add(btn_key)
            if len(used) > 50_000:
                used.clear()
                used.add(btn_key)

        proc.add(user_id)
        try:
            # ── 3-SECOND DELAY ───────────────────────────────────────────────
            # High-visibility toast banner (show_alert=False) — zero extra API requests!
            try:
                await query.answer(
                    text="✅ PLEASE WAIT 3 SECONDS... Fetching next video! ⏳🍿",
                    show_alert=False,
                )
            except Exception:
                pass

            delay = cfg.get("video_delay_seconds", 3)
            if delay > 0:
                await asyncio.sleep(delay)
            # ─────────────────────────────────────────────────────────────────

            # Ensure user record exists
            db.get_or_create_user(
                user_id,
                query.from_user.first_name or "",
                query.from_user.username,
            )
            db.register_user_with_bot(user_id, cfg["name"])

            # Check mandatory channels (uses cache — usually zero API calls)
            all_joined, r1, r2 = await check_membership(ctx, user_id)
            if not all_joined:
                missing = ([] if r1 else ["Channel 1"]) + ([] if r2 else ["Channel 2"])
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"🔒 *You need to join {' and '.join(missing)}!*\n\n"
                         "Please join the required channel(s) below, then click Verify. 🎬",
                    parse_mode="Markdown",
                    reply_markup=_join_keyboard(cfg, r1, r2),
                )
                return

            # Check daily limit (only if daily_limit > 0)
            daily_limit = cfg.get("daily_video_limit", 0)
            if daily_limit > 0:
                db.check_and_reset_daily(user_id)
                daily_count = db.get_daily_count(user_id)
                if daily_count >= daily_limit:
                    # Build reset-time message
                    now      = datetime.now(timezone.utc)
                    now_ist  = now + _IST
                    next_rst = (now_ist + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    time_left = (next_rst - _IST) - now
                    hrs  = int(time_left.total_seconds() / 3600)
                    mins = int((time_left.total_seconds() % 3600) / 60)

                    keyboard = []
                    for link in cfg.get("promo_links", []):
                        keyboard.append([InlineKeyboardButton(link["text"], url=link["url"])])
                    keyboard.append([InlineKeyboardButton(
                        "🔄 Try Again Tomorrow", callback_data="next_video"
                    )])

                    promo_section = ""
                    if cfg.get("promo_links"):
                        promo_msg = cfg.get("promo_message", "")
                        promo_section = (
                            "\n━━━━━━━━━━━━━━━━━━━━\n"
                            + (promo_msg + "\n\n" if promo_msg else "")
                            + "👇 Check out our premium bots for unlimited access:"
                        )

                    await ctx.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=(
                            f"🚫 *Daily Video Limit Reached!* ({daily_limit}/{daily_limit})\n\n"
                            f"You've watched all *{daily_limit} free videos* for today.\n"
                            f"⏳ Next reset in: *{hrs}h {mins}m* (midnight IST)"
                            + promo_section
                        ),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                    return

            # Deliver video
            success = await _deliver_video(ctx, cfg, query.message.chat_id, user_id)
            if not success:
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="⚠️ No videos available right now. Please try again!",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔄 Try Again", callback_data="next_video")]]
                    ),
                )

        except Exception:
            logger.error("[%s] next_video error uid=%s:\n%s",
                         cfg["name"], user_id, traceback.format_exc())
            try:
                await ctx.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="❌ Something went wrong. Please try again.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("🔄 Try Again", callback_data="next_video")]]
                    ),
                )
            except Exception:
                pass
        finally:
            proc.discard(user_id)
        return

    # ── Unknown callback — answer silently ────────────────────────────────────
    try:
        await query.answer()
    except Exception:
        pass


# ── Chat member updates ───────────────────────────────────────────────────────

async def handle_chat_member_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Evict membership cache immediately when a user leaves or is kicked from a
    mandatory channel. This ensures the next video request enforces the policy.
    """
    cfg = _cfg(ctx)
    cm  = update.chat_member
    if not cm:
        return

    mandatory_ids = {cfg["mandatory_channel_1_id"]}
    ch2 = cfg.get("mandatory_channel_2_id", 0)
    if ch2 and ch2 != cfg["mandatory_channel_1_id"]:
        mandatory_ids.add(ch2)

    if cm.chat.id not in mandatory_ids:
        return

    user_id    = cm.new_chat_member.user.id
    new_status = cm.new_chat_member.status
    if new_status in ("left", "kicked", "banned"):
        _mc(ctx).pop(user_id, None)
        logger.info("[%s] uid=%s left/kicked from ch=%s — cache evicted",
                    cfg["name"], user_id, cm.chat.id)


async def handle_text_messages(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle plain text messages — redirect to get a video."""
    if not update.effective_user:
        return
    keyboard = [[InlineKeyboardButton("🎬 Get Video", callback_data="next_video")]]
    try:
        await update.message.reply_text(
            "Tap the button below to get a video! 🎬",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        pass


# ── Admin commands ────────────────────────────────────────────────────────────

def _is_admin(user_id: int, cfg: dict) -> bool:
    return user_id == cfg.get("admin_user_id", 0)


async def stats_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /stats — show live stats across all bots."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return

    s       = db.get_stats()
    now_ist = (datetime.now(timezone.utc) + _IST).strftime("%d %b %Y  %I:%M %p")
    sep     = "━" * 28

    bot_users = db.get_bot_user_ids(cfg["name"])
    text = (
        f"📊 *Stats — {cfg['name']}*\n"
        f"⏰ `{now_ist} IST`\n{sep}\n\n"
        f"👥 *This bot's users:* `{len(bot_users)}`\n"
        f"👥 *Total users (all bots):* `{s['total_users']}`\n"
        f"📅 *Active today (all bots):* `{s['active_today']}`\n"
        f"🎬 *Videos today (all bots):* `{s['videos_today']}`\n"
        f"📺 *Total ever delivered:* `{s['total_delivered']}`\n"
        f"👍 *Total Likes (all bots):* `{s.get('total_likes', 0)}`\n"
        f"👎 *Total Dislikes (all bots):* `{s.get('total_dislikes', 0)}`\n"
        f"🎥 *Tracked Videos:* `{s.get('tracked_videos', 0)}`\n\n"
        f"💡 *More Admin Tools:*\n"
        f"• `/analytics` — Customer behaviour & cohorts\n"
        f"• `/content` — Video performance & rankings\n"
        f"• `/plan` — Data-driven future plans\n"
        f"• `/user <id>` — Inspect specific customer\n"
        f"• `/broadcast` — Send message to all users"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def analytics_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /analytics (or /behaviour) — detailed customer behaviour & funnel metrics."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return

    b = db.get_behaviour_analytics()
    total = b["total_users"]
    now_ist = (datetime.now(timezone.utc) + _IST).strftime("%d %b %Y, %I:%M %p")
    sep = "━" * 28

    if total == 0:
        await update.message.reply_text("📊 No user data collected yet.")
        return

    c = b["cohorts"]
    m = b["milestones"]
    pct = lambda count: f"{round((count / total) * 100, 1)}%"

    text = (
        f"📈 *Customer Behaviour Analytics*\n"
        f"⏰ `{now_ist} IST`\n{sep}\n\n"
        f"👥 *User Base Overview:*\n"
        f"• Total Users: `{total}`\n"
        f"• New (Last 24h): `{b['new_24h']}`\n"
        f"• Active (Last 24h): `{b['active_24h']}` ({pct(b['active_24h'])})\n"
        f"• Active (Last 7d): `{b['active_7d']}` ({pct(b['active_7d'])})\n"
        f"• Churned / Inactive (>7d): `{b['inactive_7d']}` ({pct(b['inactive_7d'])})\n"
        f"• Avg Videos / User: `{b['avg_delivered']}` videos\n\n"
        f"📊 *Audience Engagement Cohorts:*\n"
        f"• 🐣 *Bouncers (1-2 vids):* `{c['bouncers']}` ({pct(c['bouncers'])})\n"
        f"• 🍿 *Casuals (3-9 vids):* `{c['casuals']}` ({pct(c['casuals'])})\n"
        f"• 🎬 *Engaged (10-29 vids):* `{c['engaged']}` ({pct(c['engaged'])})\n"
        f"• 🔥 *Loyals (30-49 vids):* `{c['loyal']}` ({pct(c['loyal'])})\n"
        f"• 👑 *Superfans (50+ vids):* `{c['superfans']}` ({pct(c['superfans'])})\n\n"
        f"💎 *Milestone Promo Reach (Conversion Funnel):*\n"
        f"• Saw 10-Vid Promo: `{m['m10']}` users ({pct(m['m10'])})\n"
        f"• Saw 20-Vid Promo: `{m['m20']}` users ({pct(m['m20'])})\n"
        f"• Saw 30-Vid Promo: `{m['m30']}` users ({pct(m['m30'])})\n"
        f"• Saw 50-Vid Promo: `{m['m50']}` users ({pct(m['m50'])})\n\n"
        f"💡 *Tip:* Use `/plan` to see future strategic recommendations!"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def content_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /content — video performance, rankings, and duration preferences."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return

    c = db.get_content_analytics()
    now_ist = (datetime.now(timezone.utc) + _IST).strftime("%d %b %Y, %I:%M %p")
    sep = "━" * 28

    dp = c["duration_prefs"]
    total_reac = c["total_likes"] + c["total_dislikes"]

    liked_lines = []
    for i, v in enumerate(c["top_liked"], 1):
        dur_m = f"{v['dur']//60}m {v['dur']%60}s" if v['dur'] else "N/A"
        sz_mb = f"{round(v['sz'] / (1024*1024), 1)}MB" if v['sz'] else "N/A"
        liked_lines.append(f"{i}. 👍 `{v['likes']}` | ⏱ `{dur_m}` | 📦 `{sz_mb}`")

    disliked_lines = []
    for i, v in enumerate(c["top_disliked"], 1):
        dur_m = f"{v['dur']//60}m {v['dur']%60}s" if v['dur'] else "N/A"
        sz_mb = f"{round(v['sz'] / (1024*1024), 1)}MB" if v['sz'] else "N/A"
        disliked_lines.append(f"{i}. 👎 `{v['dislikes']}` | ⏱ `{dur_m}` | 📦 `{sz_mb}`")

    top_liked_str = "\n".join(liked_lines) if liked_lines else "No reaction data yet."
    top_disliked_str = "\n".join(disliked_lines) if disliked_lines else "No dislikes yet."

    text = (
        f"🎬 *Content Performance & Video Analysis*\n"
        f"⏰ `{now_ist} IST`\n{sep}\n\n"
        f"📊 *Reactions Overview:*\n"
        f"• Tracked Videos: `{c['total_tracked']}`\n"
        f"• Total Reactions: `{total_reac}`\n"
        f"• 👍 Total Likes: `{c['total_likes']}` ({c['sentiment_pct']}% positive)\n"
        f"• 👎 Total Dislikes: `{c['total_dislikes']}`\n\n"
        f"⏱ *Video Length Preferences (Likes Distribution):*\n"
        f"• Short (<3 min): `{dp['short']}` likes\n"
        f"• Medium (3-7 min): `{dp['medium']}` likes\n"
        f"• Long (>7 min): `{dp['long']}` likes\n\n"
        f"🏆 *Top 5 Most Liked Videos:*\n"
        f"{top_liked_str}\n\n"
        f"⚠️ *Top Most Disliked Videos (Avoid Similar):*\n"
        f"{top_disliked_str}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def plan_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /plan — automated business insights & future roadmap."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return

    b = db.get_behaviour_analytics()
    c = db.get_content_analytics()
    now_ist = (datetime.now(timezone.utc) + _IST).strftime("%d %b %Y, %I:%M %p")
    sep = "━" * 28

    total = b["total_users"]
    if total == 0:
        await update.message.reply_text("📊 Need more user traffic to generate future plans.")
        return

    recs = []

    # 1. Retention advice
    bouncers = b["cohorts"]["bouncers"]
    bouncer_rate = (bouncers / total) * 100
    if bouncer_rate > 50:
        recs.append(
            f"⚠️ *High Initial Drop-off ({round(bouncer_rate, 1)}% bouncers):*\n"
            f"Over half of users leave after 1-2 videos. Consider featuring your highest-rated videos early."
        )
    else:
        recs.append(
            f"✅ *Healthy Retention ({round(100 - bouncer_rate, 1)}% multi-watchers):*\n"
            f"Great user retention! High proportion of users are exploring multiple videos."
        )

    # 2. Premium Upselling plan
    power_users = b["cohorts"]["loyal"] + b["cohorts"]["superfans"]
    if power_users > 0:
        recs.append(
            f"💎 *Premium Upsell Opportunity ({power_users} Power Users):*\n"
            f"You have `{power_users}` loyal users (30+ videos). Broadcast an exclusive discount to convert them to paid premium bots!"
        )
    else:
        recs.append(
            f"💎 *Nurture Watch Volume:*\n"
            f"Promote the bot in more channels to nurture casual users into 30+ video power watchers."
        )

    # 3. Content sourcing recommendation
    dp = c["duration_prefs"]
    if dp["medium"] + dp["long"] > dp["short"]:
        recs.append(
            f"🎬 *Content Sourcing Strategy:*\n"
            f"Users give more likes to medium/longer videos (3+ minutes). Upload more full-length content."
        )
    elif dp["short"] > 0:
        recs.append(
            f"🎬 *Content Sourcing Strategy:*\n"
            f"Short punchy videos (<3 minutes) are receiving high engagement. Keep a steady feed of quick videos."
        )

    # 4. Bot scaling plan
    if total > 500:
        recs.append(
            f"🚀 *Scaling Infrastructure:*\n"
            f"With `{total}` users, consider enabling 1-2 additional free bots from `free_bots_config.yaml` to distribute traffic."
        )
    else:
        recs.append(
            f"🚀 *Infrastructure Status:*\n"
            f"Current free bot fleet is running with high headroom and instant in-memory response times."
        )

    recommendations_str = "\n\n".join(recs)

    text = (
        f"🎯 *Strategic Insights & Future Action Plan*\n"
        f"⏰ `{now_ist} IST`\n{sep}\n\n"
        f"{recommendations_str}\n\n{sep}\n"
        f"📌 *Quick Actions:*\n"
        f"• Use `/broadcast` to message all active users\n"
        f"• Use `/user <id>` to inspect individual customer journeys\n"
        f"• Use `/content` to see top performing videos"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def user_detail_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /user <id> — inspect individual user's complete history."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return

    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: `/user <user_id>`\nExample: `/user 7708376300`", parse_mode="Markdown")
        return

    target_uid = int(args[0])
    p = db.get_user_full_profile(target_uid)
    if not p:
        await update.message.reply_text(f"❌ User `{target_uid}` not found in database.", parse_mode="Markdown")
        return

    sep = "━" * 28
    fs_str = (p["first_seen"] + _IST).strftime("%d %b %Y, %I:%M %p") if p["first_seen"] else "N/A"
    la_str = (p["last_active"] + _IST).strftime("%d %b %Y, %I:%M %p") if p["last_active"] else "N/A"
    bots_str = ", ".join(p["bots"]) if p["bots"] else "None recorded"
    handle = f"@{p['username']}" if p["username"] else "None"

    text = (
        f"👤 *Customer Profile — {p['first_name']}*\n"
        f"🆔 User ID: `{p['user_id']}`\n"
        f"🏷 Handle: {handle}\n{sep}\n\n"
        f"🎬 *Watching Activity:*\n"
        f"• Lifetime Videos Delivered: `{p['total_delivered']}`\n"
        f"• Today's Videos Watched: `{p['daily_count']}`\n"
        f"• Unique Videos Seen: `{p['seen_count']}`\n"
        f"• Current Milestone Level: `{p['last_milestone']}`\n\n"
        f"👍 *Feedback & Reactions:*\n"
        f"• Likes Given: `{p['likes_given']}`\n"
        f"• Dislikes Given: `{p['dislikes_given']}`\n\n"
        f"📅 *Timestamps (IST):*\n"
        f"• First Joined: `{fs_str}`\n"
        f"• Last Active: `{la_str}`\n\n"
        f"🤖 *Bots Interacted With:*\n"
        f"`{bots_str}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def admin_help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /admin — show admin dashboard and commands."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return

    text = (
        f"🛠 *Admin Analytics & Management Menu*\n\n"
        f"Available Admin Commands:\n\n"
        f"📊 `/stats` — Real-time high-level numbers across all 9 bots\n"
        f"📈 `/analytics` — Customer behaviour, engagement cohorts & funnel\n"
        f"🎬 `/content` — Top liked/disliked videos & length preferences\n"
        f"🎯 `/plan` — Data-driven future plans & actionable advice\n"
        f"👤 `/user <id>` — Deep dive into any user's watch history\n"
        f"📣 `/broadcast` — Send broadcast to this bot's users (`/broadcast all` for all bots)\n"
        f"🛑 `/cancel_broadcast` — Stop an active running broadcast"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Broadcast (Admin only, non-blocking background worker) ────────────────────

_BROADCAST_WAITING = 1
# Pacing: ~22 msgs/sec. Telegram hard ceiling is 30 msg/sec global.
# Yielding control via asyncio.sleep(0.045) ensures live user clicks & commands
# are processed concurrently with ZERO lag or hanging.
BROADCAST_INTERVAL_SECONDS = 0.045


async def _run_broadcast_worker(
    bot,
    from_chat_id: int,
    message_id: int,
    admin_chat_id: int,
    status_message_id: int,
    user_ids: List[int],
    bot_data: dict,
    scope_name: str,
):
    """
    Background worker for broadcasting promotions.
    Runs asynchronously decoupled from the command handler to prevent bot hanging.
    Handles Telegram rate limits (429 RetryAfter) with backoff.
    Throttles status edits to keep admin informed while complying with Telegram edit limits.
    """
    bot_data["_broadcast_running"] = True
    bot_data["_broadcast_cancel"] = False

    total = len(user_ids)
    sent = 0
    blocked = 0
    failed = 0
    start_time = _time.monotonic()
    last_edit = _time.monotonic()

    logger.info("Broadcast worker started: %d target users (%s)", total, scope_name)

    try:
        for idx, uid in enumerate(user_ids, 1):
            if bot_data.get("_broadcast_cancel", False):
                logger.info("Broadcast worker cancelled by admin at %d/%d", idx, total)
                break

            retries = 0
            while retries < 2:
                try:
                    await bot.copy_message(
                        chat_id=uid,
                        from_chat_id=from_chat_id,
                        message_id=message_id,
                    )
                    sent += 1
                    break
                except RetryAfter as e:
                    # Telegram rate limit FloodWait: pause and retry this user
                    wait_sec = int(e.retry_after) + 1
                    logger.warning("Telegram FloodWait hit during broadcast: sleeping %ds", wait_sec)
                    await asyncio.sleep(wait_sec)
                    retries += 1
                except Forbidden:
                    # User blocked the bot or account deleted
                    blocked += 1
                    break
                except TelegramError as e:
                    failed += 1
                    break
                except Exception:
                    failed += 1
                    break

            # Rate limiting interval: ~22 msgs/sec.
            # Crucially: asyncio.sleep yields control to the event loop so live users
            # clicking Next or sending /start have ZERO lag!
            await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)

            # Periodically update status message (every 5 seconds)
            now = _time.monotonic()
            if now - last_edit >= 5.0 or idx == total:
                elapsed = max(1, int(now - start_time))
                pct = int((idx / total) * 100) if total else 0
                speed = round(idx / elapsed, 1)
                try:
                    await bot.edit_message_text(
                        chat_id=admin_chat_id,
                        message_id=status_message_id,
                        text=(
                            f"⏳ *Broadcast in Progress...* (`{pct}%`)\n\n"
                            f"🎯 Target ({scope_name}): `{total}` users\n"
                            f"📬 Delivered: `{sent}`\n"
                            f"🚫 Blocked/Left: `{blocked}`\n"
                            f"⚠️ Other Errors: `{failed}`\n"
                            f"⚡ Pace: `{speed}` msgs/sec (safe under 30/s)\n"
                            f"⏱ Elapsed: `{elapsed}s`\n\n"
                            f"🛡️ *Live User Traffic:* 100% active with zero lag.\n"
                            f"🛑 Send `/cancel_broadcast` to stop early."
                        ),
                        parse_mode="Markdown",
                    )
                    last_edit = now
                except Exception:
                    pass

    finally:
        elapsed = max(1, int(_time.monotonic() - start_time))
        was_cancelled = bot_data.get("_broadcast_cancel", False)
        bot_data["_broadcast_running"] = False
        bot_data["_broadcast_cancel"] = False

        title = "🛑 *Broadcast Stopped Early*" if was_cancelled else "🎉 *Broadcast Completed Successfully!*"
        avg_speed = round((sent + blocked + failed) / elapsed, 1)
        final_text = (
            f"{title}\n\n"
            f"🎯 Target Audience: `{total}` ({scope_name})\n"
            f"📬 Delivered: `{sent}`\n"
            f"🚫 Blocked / Inactive: `{blocked}`\n"
            f"⚠️ Failed: `{failed}`\n"
            f"⏱ Time Elapsed: `{elapsed}s`\n"
            f"⚡ Avg Delivery Pace: `{avg_speed}` msgs/sec"
        )
        try:
            await bot.edit_message_text(
                chat_id=admin_chat_id,
                message_id=status_message_id,
                text=final_text,
                parse_mode="Markdown",
            )
        except Exception:
            try:
                await bot.send_message(
                    chat_id=admin_chat_id,
                    text=final_text,
                    parse_mode="Markdown",
                )
            except Exception:
                pass


async def broadcast_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /broadcast [all] — enter broadcast mode."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return ConversationHandler.END

    if ctx.bot_data.get("_broadcast_running", False):
        await update.message.reply_text(
            "⚠️ A broadcast is already running on this bot!\n"
            "Use `/cancel_broadcast` to stop it before starting another."
        )
        return ConversationHandler.END

    # Check scope from argument: /broadcast all vs /broadcast
    is_all = ctx.args and ctx.args[0].lower() == "all"
    scope = "all" if is_all else "bot"
    ctx.user_data["broadcast_scope"] = scope

    if scope == "all":
        count = len(db.get_all_user_ids())
        target_desc = f"**ALL users across all 9 bots** ({count} users)"
    else:
        count = len(db.get_bot_user_ids(cfg["name"]))
        target_desc = f"**users of {cfg['name']}** ({count} users)\n_(Tip: Use `/broadcast all` to message users of all 9 bots)_"

    await update.message.reply_text(
        f"📣 *Broadcast Promotional Mode*\n\n"
        f"Target Audience: {target_desc}\n\n"
        f"Please send the message you wish to broadcast.\n"
        f"Supports text, photo, video, captions, or any media format.\n\n"
        f"⚡ *Performance Guarantee:* Broadcast operates in a rate-limited background worker (~22 msgs/sec).\n"
        f"Live users will experience zero lag or hanging.\n\n"
        f"Send `/cancel` to abort.",
        parse_mode="Markdown",
    )
    return _BROADCAST_WAITING


async def broadcast_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel broadcast setup conversation."""
    await update.message.reply_text("❌ Broadcast setup cancelled.")
    return ConversationHandler.END


async def cancel_broadcast_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /cancel_broadcast or /stop_broadcast — stops an active broadcast worker."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        await update.message.reply_text("⛔ Not authorized.")
        return

    if not ctx.bot_data.get("_broadcast_running", False):
        await update.message.reply_text("ℹ️ No active broadcast is currently running.")
        return

    ctx.bot_data["_broadcast_cancel"] = True
    await update.message.reply_text("🛑 Stopping active broadcast worker... The current delivery will finalize shortly.")


async def handle_broadcast_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin-only: receive promotional message and launch asynchronous background worker."""
    cfg = _cfg(ctx)
    if not _is_admin(update.effective_user.id, cfg):
        return ConversationHandler.END

    if ctx.bot_data.get("_broadcast_running", False):
        await update.message.reply_text("⚠️ A broadcast is already actively running on this bot.")
        return ConversationHandler.END

    message = update.message
    scope = ctx.user_data.get("broadcast_scope", "bot")

    if scope == "all":
        user_ids = db.get_all_user_ids()
        scope_name = "All 9 Bots"
    else:
        user_ids = db.get_bot_user_ids(cfg["name"])
        scope_name = cfg["name"]

    total = len(user_ids)
    if not total:
        await message.reply_text(f"⚠️ No users registered under {scope_name} yet.")
        return ConversationHandler.END

    status_msg = await message.reply_text(
        f"🚀 *Broadcast Launched in Background!*\n\n"
        f"👥 Target Audience: `{total}` users ({scope_name})\n"
        f"⚡ Pacing: ~22 msgs/sec (safe under 30 msg/s Telegram ceiling)\n"
        f"🛡️ *Live User Traffic:* Fully unaffected with zero lag\n"
        f"🛑 Send `/cancel_broadcast` at any time to halt.\n\n"
        f"Delivery starting now...",
        parse_mode="Markdown",
    )

    # Spawn background worker so handler returns immediately and update loop never hangs
    asyncio.create_task(
        _run_broadcast_worker(
            bot=ctx.bot,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
            admin_chat_id=message.chat_id,
            status_message_id=status_msg.message_id,
            user_ids=user_ids,
            bot_data=ctx.bot_data,
            scope_name=scope_name,
        )
    )

    return ConversationHandler.END


# ── Application builder ───────────────────────────────────────────────────────

def build_application(cfg: dict) -> Application:
    """
    Build and return a fully configured Application for one bot token.
    All per-bot state is stored in app.bot_data so handlers are stateless.

    cfg dict keys (set by free_bot_runner.py):
        name, token, mandatory_channel_1_id, mandatory_channel_1_link,
        mandatory_channel_2_id (optional), mandatory_channel_2_link (optional),
        video_channel_id, min_message_id, max_message_id,
        daily_video_limit, video_delay_seconds, membership_cache_ttl,
        promo_message, promo_links (list of {text, url}),
        support_url, admin_user_id, log_file
    """
    token = cfg["token"]

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=15.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=5.0,
    )

    app = (
        ApplicationBuilder()
        .token(token)
        .request(request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Inject config and initialise per-bot state containers
    app.bot_data["cfg"]  = cfg
    app.bot_data["mc"]   = {}   # membership cache
    app.bot_data["proc"] = set()  # processing user IDs

    # ── Handlers ──────────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("analytics", analytics_command))
    app.add_handler(CommandHandler("behaviour", analytics_command))
    app.add_handler(CommandHandler("content", content_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("user", user_detail_command))
    app.add_handler(CommandHandler("admin", admin_help_command))
    app.add_handler(CommandHandler("cancel_broadcast", cancel_broadcast_command))
    app.add_handler(CommandHandler("stop_broadcast", cancel_broadcast_command))

    # Broadcast conversation (admin only, group=-1 for priority)
    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_command)],
        states={
            _BROADCAST_WAITING: [
                MessageHandler(
                    filters.ALL & ~filters.COMMAND,
                    handle_broadcast_message,
                )
            ]
        },
        fallbacks=[CommandHandler("cancel", broadcast_cancel)],
        per_user=True,
    )
    app.add_handler(broadcast_conv, group=-1)

    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_text_messages
    ))
    app.add_handler(ChatMemberHandler(
        handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER
    ))

    return app


# ── Lifecycle hooks ───────────────────────────────────────────────────────────

async def _post_init(application: Application):
    """Called once per bot after the Application is built and connected."""
    cfg  = application.bot_data["cfg"]
    name = cfg["name"]
    logger.info("=== [%s] post_init ===", name)

    # Verify video channel is accessible
    try:
        chat = await application.bot.get_chat(cfg["video_channel_id"])
        logger.info("[%s] Video channel OK: %s (id=%s)", name, chat.title, chat.id)
    except Exception as e:
        logger.warning("[%s] Cannot access video channel %s: %s",
                       name, cfg["video_channel_id"], e)

    # Verify mandatory channels and auto-populate links if empty
    for ch_key, link_key, label in [
        ("mandatory_channel_1_id", "mandatory_channel_1_link", "Channel 1"),
        ("mandatory_channel_2_id", "mandatory_channel_2_link", "Channel 2"),
    ]:
        ch_id = cfg.get(ch_key)
        if ch_id:
            try:
                chat = await application.bot.get_chat(ch_id)
                logger.info("[%s] %s OK: %s (id=%s)", name, label, chat.title, chat.id)
                if not cfg.get(link_key):
                    link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else "")
                    if link:
                        cfg[link_key] = link
                        logger.info("[%s] Auto-detected %s link: %s", name, label, link)
            except Exception as e:
                logger.warning("[%s] Cannot access %s %s: %s", name, label, ch_id, e)

    # Report identity
    try:
        me = await application.bot.get_me()
        logger.info("[%s] ONLINE: @%s (ID: %s)", name, me.username, me.id)
        print(f"🚀 [{name}] Online as @{me.username} (ID: {me.id})", flush=True)
    except Exception as e:
        logger.warning("[%s] get_me failed: %s", name, e)


async def _post_shutdown(application: Application):
    """Called once per bot during graceful shutdown."""
    cfg = application.bot_data.get("cfg", {})
    logger.info("[%s] Shutdown complete", cfg.get("name", "?"))
