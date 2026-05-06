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
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from typing import Optional

import aiohttp
from aiohttp import web

# ── Config from .env ──────────────────────────────────────────────────────────
RZP_KEY_ID         = os.getenv("RAZORPAY_KEY_ID", "")
RZP_KEY_SECRET     = os.getenv("RAZORPAY_KEY_SECRET", "")
RZP_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
WEBHOOK_PORT       = int(os.getenv("WEBHOOK_PORT", "8080"))
SHARED_DB_PATH     = os.getenv("SHARED_PREMIUM_DB", "/home/ubuntu/premium_shared.db")

# Legacy single-plan env vars kept for /buy command fallback
RZP_AMOUNT = int(os.getenv("RZP_PREMIUM_AMOUNT", "4900"))
RZP_HOURS  = int(os.getenv("RZP_PREMIUM_HOURS",  "720"))

# ── Plans catalogue ───────────────────────────────────────────────────────────
# key → (button_label, hours, amount_paise, plan_name, rzp_description)
PLANS: dict[str, tuple] = {
    "1d":       ("⚡ 1 Day 🔒 Unlimited Videos — ₹1",  24,    100,  "1 Day Premium",    "Videos Bot — 1 Day Unlimited"),
    "3d":       ("🔥 3 Days — ₹3 ₹2 [33% OFF]",        72,    200,  "3 Day Premium",    "Videos Bot — 3 Days Unlimited"),
    "5d":       ("💎 5 Days — ₹5 ₹3 [40% OFF]",        120,   300,  "5 Day Premium",    "Videos Bot — 5 Days Unlimited"),
    "10d":      ("👑 10 Days — ₹10 ₹5 [50% OFF]",      240,   500,  "10 Day Premium",   "Videos Bot — 10 Days Unlimited"),
    "lifetime": ("🎫 Lifetime Premium — ₹99 [LIFETIME]",87600, 9900, "Lifetime Premium", "Videos Bot — Lifetime Unlimited"),
}

RZP_PL_URL = "https://api.razorpay.com/v1/payment_links"

# ── Helpers ───────────────────────────────────────────────────────────────────
def _qr_image_url(data: str) -> str:
    return f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={quote(data)}"

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
    ("chat_id",       "INTEGER"),
    ("qr_message_id", "INTEGER"),
    ("plan_key",      "TEXT NOT NULL DEFAULT '?'"),
    ("plan_name",     "TEXT NOT NULL DEFAULT 'Premium'"),
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

async def _get_record(pl_id: str) -> Optional[dict]:
    try:
        db = await aiosqlite.connect(SHARED_DB_PATH)
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id,chat_id,qr_message_id,plan_key,plan_name,"
            "amount_paise,premium_hours,status FROM rzp_payments WHERE payment_link_id=?",
            (pl_id,)
        ) as cur:
            row = await cur.fetchone()
        await db.close()
        return dict(row) if row else None
    except Exception as e:
        print(f"⚠️ [RZP] get_record: {e}")
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
async def create_payment_link_for_plan(
    user_id: int, chat_id: int, first_name: str,
    plan_key: str, session: aiohttp.ClientSession,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Returns (payment_url, qr_image_url, pl_id) or (None,None,None) on error."""
    if not RZP_KEY_ID or not RZP_KEY_SECRET:
        print("⚠️ [RZP] API keys not set")
        return None, None, None
    plan = PLANS.get(plan_key)
    if not plan:
        return None, None, None
    _, hours, amount_paise, plan_name, description = plan
    try:
        async with session.post(
            RZP_PL_URL,
            json={
                "amount": amount_paise, "currency": "INR",
                "accept_partial": False, "description": description,
                "notes": {"user_id": str(user_id), "first_name": first_name,
                          "plan_key": plan_key, "plan_name": plan_name},
                "notify": {"sms": False, "email": False}, "reminder_enable": False,
            },
            headers={"Authorization": _rzp_auth(), "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json(content_type=None)
        if resp.status not in (200, 201):
            print(f"❌ [RZP] link creation HTTP {resp.status}: {data}")
            return None, None, None
        pl_id  = data.get("id", "")
        pl_url = data.get("short_url") or data.get("url", "")
        if not pl_url or not pl_id:
            return None, None, None
        await _record_created(user_id, chat_id, pl_id, plan_key, plan_name, amount_paise, hours)
        print(f"✅ [RZP] {plan_name} link uid={user_id}: {pl_url}")
        return pl_url, _qr_image_url(pl_url), pl_id
    except Exception as e:
        print(f"❌ [RZP] create_link exception: {e}")
        return None, None, None

# Legacy wrapper used by /buy command
async def create_payment_link(user_id, first_name, session):
    url, qr, _ = await create_payment_link_for_plan(user_id, user_id, first_name, "1d", session)
    return url, qr

# ── Webhook signature ──────────────────────────────────────────────────────────
def _verify_sig(body: bytes, sig: str) -> bool:
    if not RZP_WEBHOOK_SECRET or RZP_WEBHOOK_SECRET == "your_webhook_secret_here":
        print("⚠️ [RZP] Webhook secret not set — skipping sig check")
        return True
    expected = hmac.new(RZP_WEBHOOK_SECRET.encode(), body, __import__('hashlib').sha256).hexdigest()
    return hmac.compare_digest(expected, sig)

# ── Globals injected at startup ───────────────────────────────────────────────
_tg_app       = None
_admin_uid: Optional[int] = None
_bot_username: str = "bot"

# ── Webhook handler ────────────────────────────────────────────────────────────
async def _webhook_handler(request: web.Request) -> web.Response:
    body = await request.read()
    if not _verify_sig(body, request.headers.get("X-Razorpay-Signature", "")):
        return web.Response(status=400, text="Bad signature")
    try:
        event = json.loads(body)
    except Exception:
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
        paid_at_str = "Unknown"
        if ts:
            try:
                paid_at_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except Exception:
                pass
        notes      = pl_entity.get("notes") or pay_entity.get("notes") or {}
        user_id_s  = str(notes.get("user_id", ""))
        plan_key   = notes.get("plan_key", "1d")
        plan_name  = notes.get("plan_name", "Premium")
        await _handle_success(pl_id, payment_id, user_id_s, ev,
                              amount_paid, contact, email_, paid_at_str, plan_key, plan_name)
    return web.Response(status=200, text="ok")


async def _handle_success(pl_id, payment_id, user_id_s, event_name,
                           amount_paid, contact, email_, paid_at_str, plan_key, plan_name):
    if not user_id_s:
        print(f"⚠️ [RZP] No user_id in notes pl_id={pl_id}")
        return
    try:
        user_id = int(user_id_s)
    except ValueError:
        return

    # Fetch record
    record = await _get_record(pl_id) if pl_id else None
    if record and record.get("status") == "paid":
        print(f"ℹ️ [RZP] Duplicate webhook pl_id={pl_id} — skip")
        return

    # Resolve plan details
    if record:
        plan_key  = record.get("plan_key") or plan_key
        plan_name = record.get("plan_name") or plan_name
        hours     = record.get("premium_hours") or PLANS.get(plan_key, ("",24))[1]
        chat_id   = record.get("chat_id") or user_id
        qr_msg_id = record.get("qr_message_id")
        amt_paise = record.get("amount_paise") or amount_paid
    else:
        hours     = PLANS.get(plan_key, ("",24))[1]
        chat_id   = user_id
        qr_msg_id = None
        amt_paise = amount_paid

    amt_inr  = amt_paise / 100
    is_life  = (hours >= 87600)
    days     = None if is_life else (hours // 24)
    dur_str  = "Lifetime" if is_life else f"{days} day{'s' if days != 1 else ''}"

    # Grant premium
    try:
        from premium_sync import grant_premium_synced
        premium_until = await grant_premium_synced(user_id, hours=hours)
        print(f"✅ [RZP] Premium uid={user_id} {plan_name} until {premium_until.isoformat()}")
    except Exception as e:
        print(f"❌ [RZP] grant_premium_synced: {e}")
        return

    if pl_id:
        await _record_paid(pl_id, payment_id)

    if _tg_app is None:
        return

    # Delete QR message
    if qr_msg_id and chat_id:
        try:
            await _tg_app.bot.delete_message(chat_id=chat_id, message_id=qr_msg_id)
            print(f"🗑️ [RZP] Deleted QR msg {qr_msg_id}")
        except Exception as e:
            print(f"⚠️ [RZP] Delete QR: {e}")

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

    # ── Admin message (exact format) ──────────────────────────────────────────
    if _admin_uid:
        try:
            days_val = days if days else "∞"
            await _tg_app.bot.send_message(
                chat_id=_admin_uid,
                text=(
                    f"💰 *Payment Received via Razorpay!*\n\n"
                    f"🤖 Bot: @{_bot_username}\n"
                    f"👤 User: ID `{user_id}` (from payment link notes)\n"
                    f"⚡️ Plan: {plan_name}\n"
                    f"💰 Amount: ₹{amt_inr:.0f}\n"
                    f"🆔 Payment ID: `{payment_id}`\n"
                    f"📞 Contact: {contact or 'None'}\n"
                    f"📧 Email: {email_ or 'None'}\n"
                    f"📅 Paid: {paid_at_str}\n"
                    f"🔔 Event: `{event_name}`\n\n"
                    f"✅ *APPROVE COMMAND:*\n"
                    f"`/approve {user_id} {days_val} {payment_id}`"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"⚠️ [RZP] Admin notify: {e}")


# ── Webhook server ─────────────────────────────────────────────────────────────
_webhook_runner: Optional[web.AppRunner] = None


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
