"""
razorpay_payment.py — Multi-Plan Razorpay Payment + Auto Premium
-----------------------------------------------------------------
Flow:
  1. Video limit hit → message shows plan buttons
  2. User taps plan → bot creates Razorpay payment link → sends QR photo
  3. User pays → Razorpay fires webhook
  4. Webhook: verify sig → delete QR → grant premium (shared DB) →
              send user "Premium Activated!" → send admin notification
"""

import os, json, hmac, hashlib, base64, aiosqlite
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from typing import Optional

import aiohttp
from aiohttp import web

from dotenv import load_dotenv
load_dotenv()  # MUST be here: this module is imported before main bot calls load_dotenv()

# ── Config from .env ──────────────────────────────────────────────────────────
RZP_KEY_ID         = os.getenv("RAZORPAY_KEY_ID", "")
RZP_KEY_SECRET     = os.getenv("RAZORPAY_KEY_SECRET", "")
RZP_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
WEBHOOK_PORT       = int(os.getenv("WEBHOOK_PORT", "8080"))
SHARED_DB_PATH     = os.getenv("SHARED_PREMIUM_DB", "/home/ubuntu/premium_shared.db")

logger = logging.getLogger("razorpay")
logger.info("[RZP] KEY_ID=%s  SECRET=%s",
            'SET' if RZP_KEY_ID else 'MISSING',
            'SET' if RZP_KEY_SECRET else 'MISSING')

# In-memory dedup: prevents duplicate notifications when Razorpay fires
# multiple webhook events (payment.authorized + qr_code.credited, etc.) simultaneously
_processed_payments: set = set()

# Legacy single-plan env vars kept for /buy command fallback
RZP_AMOUNT = int(os.getenv("RZP_PREMIUM_AMOUNT", "4900"))
RZP_HOURS  = int(os.getenv("RZP_PREMIUM_HOURS",  "720"))

# ── Plans catalogue ───────────────────────────────────────────────────────────
# Unicode U+0336 = COMBINING LONG STROKE OVERLAY (strikethrough on prev char)
# key → (button_label, hours, amount_paise, plan_name, rzp_description)
PLANS: dict[str, tuple] = {
    "1d":       ("\u26a1 1 Day \U0001f512 Unlimited Videos \u2014 \u20b91",         24,    100,  "1 Day Premium",    "Videos Bot \u2014 1 Day Unlimited"),
    "3d":       ("\U0001f525 3 Days \u2014 \u20b9\u03363\u0336 \u20b92 [33% OFF]",  72,    200,  "3 Day Premium",    "Videos Bot \u2014 3 Days Unlimited"),
    "5d":       ("\U0001f4a0 5 Days \u2014 \u20b9\u03365\u0336 \u20b93 [40% OFF]",  120,   300,  "5 Day Premium",    "Videos Bot \u2014 5 Days Unlimited"),
    "10d":      ("\U0001f451 10 Days \u2014 \u20b9\u03361\u03360\u0336 \u20b95 [50% OFF]", 240, 500, "10 Day Premium", "Videos Bot \u2014 10 Days Unlimited"),
    "lifetime": ("\U0001f39f\ufe0f Lifetime Premium \u2014 \u20b999 [LIFETIME]", 87600, 9900, "Lifetime Premium", "Videos Bot \u2014 Lifetime Unlimited"),
}


RZP_PL_URL    = "https://api.razorpay.com/v1/payment_links"
RZP_QR_URL    = "https://api.razorpay.com/v1/payments/qr_codes"  # NOTE: underscore, not hyphen!

# ── Helpers ───────────────────────────────────────────────────────────────────
def _rzp_auth() -> str:
    return "Basic " + base64.b64encode(f"{RZP_KEY_ID}:{RZP_KEY_SECRET}".encode()).decode()

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

# ── DB schema ─────────────────────────────────────────────────────────────────
_DDL = """
CREATE TABLE IF NOT EXISTS rzp_payments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    chat_id         INTEGER,
    qr_message_id   INTEGER,
    payment_link_id TEXT    NOT NULL UNIQUE,
    payment_id      TEXT,
    plan_key        TEXT    NOT NULL DEFAULT '?',
    plan_name       TEXT    NOT NULL DEFAULT 'Premium',
    amount_paise    INTEGER NOT NULL,
    premium_hours   INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'created',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    paid_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_rzp_user ON rzp_payments(user_id);
"""
# New columns added after initial release — migrated safely
_NEW_COLS = [
    ("chat_id",          "INTEGER"),
    ("qr_message_id",    "INTEGER"),
    ("limit_message_id", "INTEGER"),   # message_id of 'Video Limit Reached!' plan-buttons msg
    ("plan_key",         "TEXT NOT NULL DEFAULT '?'"),
    ("plan_name",        "TEXT NOT NULL DEFAULT 'Premium'"),
]

async def _ensure_payments_table():
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        await db.execute("PRAGMA journal_mode=WAL")
        for stmt in _DDL.split(";"):
            s = stmt.strip()
            if s:
                await db.execute(s)
        for col, defn in _NEW_COLS:
            try:
                await db.execute(f"ALTER TABLE rzp_payments ADD COLUMN {col} {defn}")
            except Exception:
                pass  # already exists
        await db.commit()
        await db.close()
        print("✅ [RZP] payments table ready")
    except Exception as e:
        print(f"⚠️ [RZP] payments table init error: {e}")

async def _record_created(user_id, chat_id, pl_id, plan_key, plan_name, amount_paise, hours):
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "INSERT OR IGNORE INTO rzp_payments "
            "(user_id,chat_id,payment_link_id,plan_key,plan_name,amount_paise,premium_hours,status) "
            "VALUES (?,?,?,?,?,?,?,'created')",
            (user_id, chat_id, pl_id, plan_key, plan_name, amount_paise, hours)
        )
        await db.commit()
        await db.close()
    except Exception as e:
        print(f"⚠️ [RZP] record_created: {e}")

async def store_qr_message_id(pl_id: str, qr_msg_id: int):
    """Call after sending the QR photo to store the message_id for later deletion."""
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE rzp_payments SET qr_message_id=? WHERE payment_link_id=?",
            (qr_msg_id, pl_id)
        )
        await db.commit()
        await db.close()
    except Exception as e:
        print(f"⚠️ [RZP] store_qr_msg_id: {e}")

async def store_limit_message_id(pl_id: str, limit_msg_id: int):
    """Store the 'Video Limit Reached!' message_id so it can be deleted after payment."""
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE rzp_payments SET limit_message_id=? WHERE payment_link_id=?",
            (limit_msg_id, pl_id)
        )
        await db.commit()
        await db.close()
    except Exception as e:
        print(f"⚠️ [RZP] store_limit_msg_id: {e}")

async def _get_record(pl_id: str) -> Optional[dict]:
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id,chat_id,qr_message_id,limit_message_id,plan_key,plan_name,"
            "amount_paise,premium_hours,status FROM rzp_payments WHERE payment_link_id=?",
            (pl_id,)
        ) as cur:
            row = await cur.fetchone()
        await db.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"⚠️ [RZP] get_record: {e}")
        return None

async def _get_record_by_user(user_id: int) -> Optional[dict]:
    """Fallback: get the most recent 'created' QR record for a user (used when pl_id is empty)."""
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id,chat_id,qr_message_id,limit_message_id,plan_key,plan_name,"
            "payment_link_id,amount_paise,premium_hours,status "
            "FROM rzp_payments "
            "WHERE user_id=? AND status='created' ORDER BY id DESC LIMIT 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
        await db.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"\u26a0\ufe0f [RZP] get_record_by_user: {e}")
        return None

async def _record_paid(pl_id: str, payment_id: str):
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(
            "UPDATE rzp_payments SET status='paid',payment_id=?,paid_at=datetime('now') "
            "WHERE payment_link_id=?", (payment_id, pl_id)
        )
        await db.commit()
        await db.close()
    except Exception as e:
        print(f"⚠️ [RZP] record_paid: {e}")

# ── Create payment link ───────────────────────────────────────────────────────
async def create_qr_code_for_plan(
    user_id: int, chat_id: int, first_name: str,
    plan_key: str, session: aiohttp.ClientSession,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Create a Razorpay QR Code for the given plan using /v1/payments/qr-codes.
    Returns (qr_image_url, qr_id, pl_id_for_db) or (None, None, None) on error.
    The qr_image_url is served directly by Razorpay — no external image service needed.
    """
    # Read keys at call-time (not import-time) — ensures load_dotenv() has already run
    key_id     = os.getenv("RAZORPAY_KEY_ID", "") or RZP_KEY_ID
    key_secret = os.getenv("RAZORPAY_KEY_SECRET", "") or RZP_KEY_SECRET
    if not key_id or not key_secret:
        logger.error("[RZP] API keys not set! Check RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env")
        return None, None, None
    plan = PLANS.get(plan_key)
    if not plan:
        return None, None, None
    _, hours, amount_paise, plan_name, description = plan
    import time as _t
    close_by = int(_t.time()) + 86400  # QR expires in 24 hours (required field for single_use)
    logger.info(f"[RZP] Calling QR API: POST {RZP_QR_URL} | plan={plan_key} amount={amount_paise}p uid={user_id}")
    try:
        async with session.post(
            RZP_QR_URL,   # https://api.razorpay.com/v1/payments/qr-codes
            json={
                "type":           "upi_qr",
                "name":           plan_name,
                "usage":          "single_use",
                "fixed_amount":   True,             # bool True (Razorpay accepts both True/1)
                "payment_amount": amount_paise,
                "description":    description,
                "close_by":       close_by,          # Required for single_use
                "notes": {
                    "user_id":    str(user_id),
                    "first_name": first_name,
                    "plan_key":   plan_key,
                    "plan_name":  plan_name,
                },
            },
            headers={"Authorization": "Basic " + base64.b64encode(f"{key_id}:{key_secret}".encode()).decode(),
                     "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
        if resp.status not in (200, 201):
            logger.error(
                f"[RZP] QR code creation FAILED — HTTP {resp.status}\n"
                f"  Request: plan={plan_key} uid={user_id} amount={amount_paise}p\n"
                f"  Razorpay response: {json.dumps(data, ensure_ascii=False)}"
            )
            return None, None, None
        qr_id     = data.get("id", "")
        image_url = data.get("image_url", "")
        if not qr_id or not image_url:
            logger.error(f"[RZP] Missing id/image_url in QR response: {data}")
            return None, None, None
        await _record_created(user_id, chat_id, qr_id, plan_key, plan_name, amount_paise, hours)
        logger.info(f"[RZP] QR code created uid={user_id} plan={plan_name} id={qr_id} image={image_url}")
        # Download the QR image from Razorpay CDN
        try:
            async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as img_resp:
                if img_resp.status == 200:
                    img_bytes = await img_resp.read()
                    # Crop tightly to show only the QR code (remove Razorpay branding/merchant name)
                    try:
                        from PIL import Image as PILImage
                        import io as _io
                        img = PILImage.open(_io.BytesIO(img_bytes))
                        w, h = img.size
                        # QR code is centered, ~78% width, starts ~32% from top
                        qr_size = int(w * 0.78)
                        left    = (w - qr_size) // 2
                        top     = int(h * 0.32)
                        pad     = int(w * 0.04)
                        box     = (max(0, left - pad), max(0, top - pad),
                                   min(w, left + qr_size + pad), min(h, top + qr_size + pad))
                        cropped = img.crop(box)
                        out     = _io.BytesIO()
                        cropped.save(out, format="PNG")
                        img_bytes = out.getvalue()
                        logger.info(f"[RZP] QR cropped to {box}")
                    except Exception as crop_err:
                        logger.warning(f"[RZP] Crop failed (using original): {crop_err}")
                    return img_bytes, qr_id, qr_id
                else:
                    logger.warning(f"[RZP] Image download failed HTTP {img_resp.status} — using URL")
                    return image_url, qr_id, qr_id
        except Exception as dl_err:
            logger.warning(f"[RZP] Image download error ({dl_err}) — using URL")
            return image_url, qr_id, qr_id
    except Exception as e:
        logger.exception(f"[RZP] create_qr_code exception for uid={user_id} plan={plan_key}: {e}")
        return None, None, None


# Legacy wrapper kept for /buy command
async def create_payment_link_for_plan(
    user_id: int, chat_id: int, first_name: str,
    plan_key: str, session: aiohttp.ClientSession,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Delegates to create_qr_code_for_plan (Razorpay QR Codes API)."""
    image_url, qr_id, pl_id = await create_qr_code_for_plan(
        user_id, chat_id, first_name, plan_key, session
    )
    return image_url, image_url, pl_id   # (payment_url=image, qr_url=image, pl_id)


async def create_payment_link(user_id, first_name, session):
    """Legacy /buy — creates 1-day QR."""
    url, _, _ = await create_qr_code_for_plan(user_id, user_id, first_name, "1d", session)
    return url, url


# ── Webhook signature ──────────────────────────────────────────────────────────
def _verify_sig(body: bytes, sig: str) -> bool:
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "") or RZP_WEBHOOK_SECRET
    if not secret or secret == "your_webhook_secret_here":
        logger.warning("[RZP] Webhook secret not set \u2014 skipping sig check")
        return True
    expected = hmac.new(secret.encode(), body, __import__('hashlib').sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        logger.error(f"[RZP] Signature mismatch. Expected: {expected}, Got: {sig}")
        return False
    return True

# ── Globals injected at startup ───────────────────────────────────────────────
_tg_app       = None
_admin_uid: Optional[int] = None
_bot_username: str = "bot"

# ── Webhook handler ────────────────────────────────────────────────────────────
async def _webhook_handler(request: web.Request) -> web.Response:
    body = await request.read()
    sig_header = request.headers.get("X-Razorpay-Signature", "")
    if not sig_header:
        logger.error("[RZP] Missing X-Razorpay-Signature header")
        return web.Response(status=400, text="Missing signature")
        
    if not _verify_sig(body, sig_header):
        return web.Response(status=400, text="Bad signature")
        
    try:
        event = json.loads(body)
    except Exception as e:
        logger.error(f"[RZP] Invalid JSON body: {e}")
        return web.Response(status=400, text="Invalid JSON")

    ev = event.get("event", "")
    print(f"📩 [RZP] Webhook: {ev}")

    if ev in ("payment_link.paid", "payment.authorized", "payment.captured"):
        pl_entity  = event.get("payload", {}).get("payment_link", {}).get("entity", {})
        pay_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
        pl_id      = pl_entity.get("id", "")
        payment_id = pay_entity.get("id", "")
        amount_paid = pay_entity.get("amount", 0)
        contact     = pay_entity.get("contact")
        email_      = pay_entity.get("email")
        ts          = pay_entity.get("created_at")
        paid_at_str = _ts_to_str(ts)
        notes      = pl_entity.get("notes") or pay_entity.get("notes") or {}
        user_id_s  = str(notes.get("user_id", ""))
        plan_key   = notes.get("plan_key", "1d")
        plan_name  = notes.get("plan_name", "Premium")
        await _handle_success(pl_id, payment_id, user_id_s, ev,
                              amount_paid, contact, email_, paid_at_str, plan_key, plan_name)

    # Razorpay QR Code payment event
    elif ev == "qr_code.credited":
        qr_entity  = event.get("payload", {}).get("qr_code",  {}).get("entity", {})
        pay_entity = event.get("payload", {}).get("payment",  {}).get("entity", {})
        qr_id      = qr_entity.get("id", "")
        payment_id = pay_entity.get("id", "")
        amount_paid = pay_entity.get("amount", 0)
        contact     = pay_entity.get("contact")
        email_      = pay_entity.get("email")
        ts          = pay_entity.get("created_at")
        paid_at_str = _ts_to_str(ts)
        notes       = qr_entity.get("notes") or {}
        user_id_s   = str(notes.get("user_id", ""))
        plan_key    = notes.get("plan_key", "1d")
        plan_name   = notes.get("plan_name", "Premium")
        # qr_id is stored as pl_id in our DB
        await _handle_success(qr_id, payment_id, user_id_s, ev,
                              amount_paid, contact, email_, paid_at_str, plan_key, plan_name)

    return web.Response(status=200, text="ok")


def _ts_to_str(ts) -> str:
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            pass
    return "Unknown"


async def _handle_success(pl_id, payment_id, user_id_s, event_name,
                           amount_paid, contact, email_, paid_at_str, plan_key, plan_name):
    global _processed_payments
    # Fast in-memory dedup — catches simultaneous duplicate webhook events before DB hit
    dedup_key = payment_id or pl_id
    if dedup_key and dedup_key in _processed_payments:
        logger.info(f"[RZP] In-memory dedup skip: {dedup_key}")
        return
    if dedup_key:
        _processed_payments.add(dedup_key)

    if not user_id_s:
        logger.warning(f"[RZP] No user_id in notes pl_id={pl_id}")
        return
    try:
        user_id = int(user_id_s)
    except ValueError:
        return

    # DB dedup — catches duplicates across bot restarts
    record = await _get_record(pl_id) if pl_id else None
    # Fallback for QR payments where payment.authorized has no pl_id:
    # look up by user_id to get qr_msg_id / limit_msg_id for deletion
    if record is None and user_id_s:
        try:
            record = await _get_record_by_user(int(user_id_s))
            if record:
                # Update pl_id so _record_paid marks the correct row
                pl_id = record.get("payment_link_id") or pl_id
                logger.info(f"[RZP] Record found by user_id fallback: pl_id={pl_id}")
        except Exception:
            pass
    if record and record.get("status") == "paid":
        logger.info(f"[RZP] DB dedup skip pl_id={pl_id}")
        return

    # Resolve plan details
    if record:
        plan_key      = record.get("plan_key")      or plan_key
        plan_name     = record.get("plan_name")     or plan_name
        hours         = record.get("premium_hours") or PLANS.get(plan_key, ("",24))[1]
        chat_id       = record.get("chat_id")       or user_id
        qr_msg_id     = record.get("qr_message_id")
        limit_msg_id  = record.get("limit_message_id")
        amt_paise     = record.get("amount_paise")  or amount_paid
    else:
        hours         = PLANS.get(plan_key, ("",24))[1]
        chat_id       = user_id
        qr_msg_id     = None
        limit_msg_id  = None
        amt_paise     = amount_paid

    amt_inr  = amt_paise / 100
    is_life  = (hours >= 87600)
    days     = None if is_life else (hours // 24)
    dur_str  = "Lifetime" if is_life else f"{days} day{'s' if days != 1 else ''}"

    # Grant premium
    try:
        from premium_sync import grant_premium_synced
        premium_until = await grant_premium_synced(user_id, hours=hours)
        logger.info(f"[RZP] Premium granted uid={user_id} {plan_name} until {premium_until.isoformat()}")
    except Exception as e:
        logger.error(f"[RZP] grant_premium_synced: {e}")
        return

    # Mark paid IMMEDIATELY — before sending Telegram messages to prevent race-condition dupes
    if pl_id:
        await _record_paid(pl_id, payment_id)

    if _tg_app is None:
        return

    # Delete QR photo message
    if qr_msg_id and chat_id:
        try:
            await _tg_app.bot.delete_message(chat_id=chat_id, message_id=qr_msg_id)
            logger.info(f"[RZP] Deleted QR msg {qr_msg_id}")
        except Exception as e:
            logger.warning(f"[RZP] Delete QR msg: {e}")

    # Delete 'Video Limit Reached!' plan-buttons message
    if limit_msg_id and chat_id:
        try:
            await _tg_app.bot.delete_message(chat_id=chat_id, message_id=limit_msg_id)
            logger.info(f"[RZP] Deleted limit msg {limit_msg_id}")
        except Exception as e:
            logger.warning(f"[RZP] Delete limit msg: {e}")

    # Format expiry
    ist_off    = timedelta(hours=5, minutes=30)
    expiry_ist = premium_until + ist_off
    expiry_str = expiry_ist.strftime("%d %b %Y, %I:%M %p IST")

    # ── User message (exact format) ───────────────────────────────────────────
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    try:
        await _tg_app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎉 *Premium Activated!*\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 Plan        : {plan_name}\n"
                f"⏰ Duration    : {dur_str}\n"
                f"💰 Amount Paid : ₹{amt_inr:.0f}\n"
                f"📅 Valid Till  : {expiry_str}\n\n"
                f"🆔 Payment ID  : `{payment_id}`\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎫 You now have unlimited access!"
            ),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎫 Get Video", callback_data="next_video")]]
            ),
        )
    except Exception as e:
        print(f"⚠️ [RZP] User notify: {e}")

    # ── Admin message ──────────────────────────────────────────────────────────
    # Fallback: read from env if _admin_uid wasn't set by start_webhook_server
    admin_id = _admin_uid or int(os.getenv("ADMIN_USER_ID", "0"))
    logger.info(f"[RZP] Admin notify → admin_id={admin_id} _admin_uid={_admin_uid}")
    if admin_id:
        try:
            days_val = days if days is not None else "∞"
            await _tg_app.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"<b>💰 Payment Received via Razorpay!</b>\n\n"
                    f"🤖 Bot: @{_bot_username}\n"
                    f"👤 User: <code>{user_id}</code>\n"
                    f"⚡️ Plan: {plan_name}\n"
                    f"💰 Amount: ₹{amt_inr:.0f}\n"
                    f"🆔 Payment ID: <code>{payment_id}</code>\n"
                    f"📞 Contact: {contact or 'None'}\n"
                    f"📧 Email: {email_ or 'None'}\n"
                    f"📅 Paid: {paid_at_str}\n"
                    f"🔔 Event: <code>{event_name}</code>\n\n"
                    f"<b>✅ PREMIUM AUTO-GRANTED</b>\n"
                    f"Valid till: {expiry_str}"
                ),
                parse_mode="HTML",
            )
            logger.info(f"[RZP] Admin notified uid={user_id} plan={plan_name}")
        except Exception as e:
            logger.error(f"[RZP] Admin notify FAILED: {e}")


# ── Webhook server ─────────────────────────────────────────────────────────────
_webhook_runner: Optional[web.AppRunner] = None
_tg_app      = None   # set by start_webhook_server()
_admin_uid   = None   # set by start_webhook_server()
_bot_username = ""    # set by start_webhook_server()


async def start_webhook_server(tg_application, admin_user_id: int, bot_username: str) -> bool:
    global _tg_app, _webhook_runner, _admin_uid, _bot_username
    _tg_app       = tg_application
    _admin_uid    = admin_user_id
    _bot_username = bot_username

    await _ensure_payments_table()

    if not RZP_KEY_ID or not RZP_KEY_SECRET:
        print("⚠️ [RZP] API keys not set — webhook NOT started")
        return False

    app = web.Application()
    app.router.add_post("/razorpay-webhook", _webhook_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    try:
        await site.start()
        _webhook_runner = runner
        print(f"✅ [RZP] Webhook on :{WEBHOOK_PORT}  →  register: http://<server>:{WEBHOOK_PORT}/razorpay-webhook")
        return True
    except Exception as e:
        print(f"❌ [RZP] Webhook start failed: {e}")
        return False


async def stop_webhook_server():
    global _webhook_runner
    if _webhook_runner:
        try:
            await _webhook_runner.cleanup()
        except Exception:
            pass
        _webhook_runner = None
        print("✅ [RZP] Webhook stopped")


async def get_recent_payments(limit: int = 10) -> list[dict]:
    results = []
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id,plan_name,payment_id,amount_paise,status,premium_hours,paid_at "
            "FROM rzp_payments ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            async for row in cur:
                results.append(dict(row))
        await db.close()
    except Exception as e:
        print(f"⚠️ [RZP] get_recent_payments: {e}")
    return results
