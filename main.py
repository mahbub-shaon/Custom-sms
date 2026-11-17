"""
CustomSMS / CarryBee Merchant Bot
=================================
Secure, config-driven Telegram bot with:
  - Numeric-ID-only admin authentication (no username-based auth)
  - Configurable Force Join (multiple channels, enable/disable from config.json)
  - Persistent state + broadcast jobs in SQLite (safe, additive migrations)
  - Full broadcast management with progress, FloodWait handling and admin cancel
  - HTML-safe message rendering (no Markdown parse failures)
  - Zero changes to the existing `users` table data (119 users preserved)

Run:  python main.py
Config: config.json (next to this file, or BOT_CONFIG=/path/to/config.json)
DB:    bot_database.db (next to this file, or BOT_DB=/path/to/bot_database.db)
"""

import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
import telebot
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

try:
    from telebot.apihelper import ApiTelegramException
except Exception:  # pragma: no cover - compatibility with old pyTelegramBotAPI
    ApiTelegramException = Exception


# =====================================================================
# CONFIG (token and admin ID are loaded from config.json, never hardcoded)
# =====================================================================

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("BOT_CONFIG", str(BASE_DIR / "config.json"))).resolve()
DB_PATH = Path(os.getenv("BOT_DB", str(BASE_DIR / "bot_database.db"))).resolve()

DEFAULT_API_URL = "https://api-merchant.carrybee.com/api/v2/merchant/register"


def _channel_username(value):
    """Extract '@username' from a username, a t.me link or a bare name."""
    if not value:
        return ""
    value = str(value).strip()
    if value.startswith("http"):
        m = re.search(r"t\.me/([A-Za-z0-9_]+)", value)
        return "@" + m.group(1) if m else ""
    return value if value.startswith("@") else "@" + value


def load_config():
    if not CONFIG_PATH.is_file():
        raise SystemExit("FATAL: config.json not found at %s" % CONFIG_PATH)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except Exception as exc:
        raise SystemExit("FATAL: cannot read config.json: %s" % exc)

    if not isinstance(raw, dict):
        raise SystemExit("FATAL: config.json must contain a JSON object")

    token = raw.get("bot_token") or raw.get("token")
    if not isinstance(token, str) or not token.strip():
        raise SystemExit("FATAL: config.json is missing a valid 'bot_token'")

    admin = raw.get("admin") or {}
    admin_id = admin.get("telegram_id", raw.get("admin_id"))
    try:
        admin_id = int(admin_id)
    except (TypeError, ValueError):
        raise SystemExit("FATAL: admin.telegram_id must be a numeric Telegram ID")
    if admin_id <= 0:
        raise SystemExit("FATAL: admin.telegram_id must be positive")

    fj = raw.get("force_join", {})
    if isinstance(fj, bool):
        fj_enabled, fj_raw_channels = fj, []
    else:
        fj_enabled = bool(fj.get("enabled", fj.get("enable", False)))
        fj_raw_channels = fj.get("channels", raw.get("force_join_channels", [])) or []

    channels = []
    for item in fj_raw_channels:
        if isinstance(item, str):
            uname = _channel_username(item)
            channels.append({
                "username": uname,
                "label": item,
                "url": "https://t.me/" + uname.lstrip("@") if uname else "",
            })
        elif isinstance(item, dict):
            src = item.get("username") or item.get("channel") or item.get("link") or ""
            uname = _channel_username(src)
            url = item.get("url") or ""
            if not url and uname:
                url = "https://t.me/" + uname.lstrip("@")
            if uname or url:
                channels.append({
                    "username": uname,
                    "label": item.get("label") or uname or url,
                    "url": url,
                })

    developer = raw.get("developer") or {}
    merchant = raw.get("merchant_api") or {}
    pricing = raw.get("pricing") or {}

    return {
        "token": token.strip(),
        "admin_id": admin_id,
        "admin_name": admin.get("name") or "Administrator",
        "admin_username": admin.get("contact_username") or "",
        "admin_url": admin.get("contact_url") or "",
        "developer_name": developer.get("name") or "Developer",
        "developer_username": developer.get("username") or "",
        "developer_url": developer.get("url") or "",
        "developer_note": developer.get("note") or "",
        "force_join_enabled": fj_enabled,
        "force_join_channels": channels,
        "api_url": merchant.get("url") or DEFAULT_API_URL,
        "api_timeout": int(merchant.get("timeout", 30)),
        "pricing": pricing,
    }


CFG = load_config()

TOKEN = CFG["token"]
ADMIN_ID = CFG["admin_id"]                 # the ONLY source of admin authority
ADMIN_NAME = CFG["admin_name"]
ADMIN_URL = CFG["admin_url"]
DEVELOPER_NAME = CFG["developer_name"]
DEVELOPER_USERNAME = CFG["developer_username"]
DEVELOPER_URL = CFG["developer_url"]
DEVELOPER_NOTE = CFG["developer_note"]
FORCE_JOIN_ENABLED = CFG["force_join_enabled"]
FORCE_JOIN_CHANNELS = CFG["force_join_channels"]
API_URL = CFG["api_url"]
API_TIMEOUT = CFG["api_timeout"]

# SECURITY: TLS verification is ALWAYS on. A config value can never switch it off.
VERIFY_TLS = True

bot = telebot.TeleBot(TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("customsms")

USER_BUTTONS = {
    "registermerchant", "profile", "mybalance", "referrallink",
    "buypoints", "sendpoints", "developerinfo",
}
ADMIN_MENU_BUTTONS = {
    "addpoints", "removepoints", "broadcast", "usersinfo", "backtousermenu",
}
ADMIN_PANEL_BUTTON = "adminpanel"
ALL_MENU_KEYS = USER_BUTTONS | ADMIN_MENU_BUTTONS | {ADMIN_PANEL_BUTTON}


# =====================================================================
# HTML-SAFE TEXT HELPERS
# =====================================================================

def esc(value):
    return html.escape("" if value is None else str(value), quote=False)


def b(value):
    return "<b>%s</b>" % esc(value)


def code(value):
    return "<code>%s</code>" % esc(value)


def _strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "")


def _is_parse_error(exc):
    s = str(exc).lower()
    return ("parse" in s and "entit" in s) or "unsupported start tag" in s or "can't parse" in s


def safe_send(chat_id, text, reply_markup=None, parse_mode="HTML", **kwargs):
    """Send a message; fall back to plain text if Telegram rejects the markup."""
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode,
                                reply_markup=reply_markup, **kwargs)
    except Exception as exc:
        if parse_mode and _is_parse_error(exc):
            log.warning("Markup rejected, retrying plain text: %s", exc)
            return bot.send_message(chat_id, _strip_tags(text),
                                    reply_markup=reply_markup, **kwargs)
        raise


def safe_edit(chat_id, message_id, text, reply_markup=None):
    try:
        return bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                     parse_mode="HTML", reply_markup=reply_markup)
    except Exception as exc:
        if _is_parse_error(exc):
            try:
                return bot.edit_message_text(_strip_tags(text), chat_id=chat_id,
                                             message_id=message_id,
                                             reply_markup=reply_markup)
            except Exception:
                return None
        return None


def normalize(text):
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# =====================================================================
# DATABASE — absolute path, additive migrations, no destructive changes
# =====================================================================

@contextmanager
def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        c = conn.cursor()
        # Exactly the historical schema — existing rows are never touched.
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                points INTEGER DEFAULT 5,
                referred_by INTEGER
            )
        """)
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "referred_by" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        if "last_seen" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")

        # New tables for restart-safe state and broadcast tracking.
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_states (
                kind TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (kind, user_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_jobs (
                job_id TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                content_json TEXT,
                status TEXT NOT NULL,
                total INTEGER DEFAULT 0,
                sent INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
    with db() as conn:
        conn.execute("UPDATE broadcast_jobs SET status='interrupted' WHERE status='running'")
    with db() as conn:
        check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if check != "ok":
        log.error("SQLite integrity check FAILED: %s", check)
    else:
        log.info("Database OK | path=%s | registered users=%s", DB_PATH, total)


init_db()


def touch_user(user_id):
    try:
        with db() as conn:
            conn.execute("UPDATE users SET last_seen = ? WHERE user_id = ?",
                         (int(time.time()), int(user_id)))
    except Exception as exc:
        log.debug("touch_user failed: %s", exc)


def count_referrals(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,)
        ).fetchone()[0]


def get_user_profile(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT username, points, referred_by FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return tuple(row) if row else None


def get_user(user_id, username, referrer_id=None):
    """Create a new user (5 pts) and award +3 to a valid referrer. Unchanged semantics."""
    now = int(time.time())
    with db() as conn:
        row = conn.execute("SELECT points FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            pts = row[0]
            if username:
                conn.execute("UPDATE users SET username = ?, last_seen = ? WHERE user_id = ?",
                             (username, now, user_id))
            else:
                conn.execute("UPDATE users SET last_seen = ? WHERE user_id = ?",
                             (now, user_id))
        else:
            pts = 5
            conn.execute(
                "INSERT INTO users (user_id, username, points, referred_by, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, username or "User", pts, referrer_id, now),
            )
            if referrer_id and referrer_id != user_id:
                exists = conn.execute("SELECT 1 FROM users WHERE user_id = ?",
                                      (referrer_id,)).fetchone()
                if exists:
                    conn.execute("UPDATE users SET points = points + 3 WHERE user_id = ?",
                                 (referrer_id,))
                    award = True
                else:
                    award = False
            else:
                award = False
    if not row:
        if award:
            try:
                safe_send(referrer_id,
                          "🎉 <b>নতুন রেফারেল!</b>\nআপনার রেফারেল থেকে নতুন ইউজার যুক্ত হয়েছে। "
                          "<b>+৩ Pts</b> বোনাস পেয়েছেন।")
            except Exception as exc:
                log.info("Could not notify referrer %s: %s", referrer_id, exc)
    return pts


MAX_POINTS_PER_OP = 1_000_000


MAX_POINTS_PER_OP = 1_000_000


def update_points(user_id, amount):
    with db() as conn:
        conn.execute("UPDATE users SET points = points + ? WHERE user_id = ?",
                     (int(amount), int(user_id)))


# ---------------------------------------------------------------------
# Persistent state store (replaces loss-prone plain dicts)
# ---------------------------------------------------------------------

user_steps = {}
admin_steps = {}
pending_referrers = {}
_STATE_MAPS = {"user": user_steps, "admin": admin_steps, "ref": pending_referrers}


def hydrate_states():
    with db() as conn:
        rows = conn.execute("SELECT kind, user_id, data_json FROM bot_states").fetchall()
    for row in rows:
        try:
            _STATE_MAPS.get(row["kind"], {})[row["user_id"]] = json.loads(row["data_json"])
        except Exception:
            continue


def state_set(kind, uid, value):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_states (kind,user_id,data_json,updated_at) "
            "VALUES (?,?,?,?)",
            (kind, int(uid), json.dumps(value), int(time.time())),
        )
    if kind in _STATE_MAPS:
        _STATE_MAPS[kind][int(uid)] = value


def state_get(kind, uid, default=None):
    return _STATE_MAPS.get(kind, {}).get(int(uid), default)


def state_pop(kind, uid, default=None):
    with db() as conn:
        conn.execute("DELETE FROM bot_states WHERE kind = ? AND user_id = ?",
                     (kind, int(uid)))
    if kind in _STATE_MAPS:
        return _STATE_MAPS[kind].pop(int(uid), default)
    return default


def clear_states(uid):
    with db() as conn:
        conn.execute("DELETE FROM bot_states WHERE user_id = ?", (int(uid),))
    for mapping in _STATE_MAPS.values():
        mapping.pop(int(uid), None)


hydrate_states()


# ---------------------------------------------------------------------
# Broadcast job helpers
# ---------------------------------------------------------------------

def job_create(job_id, admin_id, content, total):
    now = int(time.time())
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO broadcast_jobs "
            "(job_id,admin_id,content_json,status,total,sent,failed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, int(admin_id), json.dumps(content), "running", int(total), 0, 0, now, now),
        )


def job_progress(job_id, sent=None, failed=None, status=None):
    with db() as conn:
        if sent is not None and failed is not None and status:
            conn.execute(
                "UPDATE broadcast_jobs SET sent=?, failed=?, status=?, updated_at=? "
                "WHERE job_id=?",
                (sent, failed, status, int(time.time()), job_id),
            )
        elif sent is not None and failed is not None:
            conn.execute(
                "UPDATE broadcast_jobs SET sent=?, failed=?, updated_at=? WHERE job_id=?",
                (sent, failed, int(time.time()), job_id),
            )
        elif status:
            conn.execute(
                "UPDATE broadcast_jobs SET status=?, updated_at=? WHERE job_id=?",
                (status, int(time.time()), job_id),
            )


_cancel_lock = threading.Lock()
_cancel_events = {}


def cancel_event(job_id):
    with _cancel_lock:
        if job_id not in _cancel_events:
            _cancel_events[job_id] = threading.Event()
        return _cancel_events[job_id]


def _all_user_ids():
    with db() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]


# =====================================================================
# AUTHORIZATION — numeric ID only, checked server-side everywhere
# =====================================================================

def is_admin(user_or_id):
    """Accept ONLY the configured numeric Telegram ID. Never a username."""
    if user_or_id is None:
        return False
    try:
        uid = int(getattr(user_or_id, "id", user_or_id))
    except (TypeError, ValueError):
        return False
    return uid == ADMIN_ID


def _private_chat(update):
    """Return the private Chat of a Message/CallbackQuery, or None."""
    chat = getattr(update, "chat", None)
    if chat is None:
        chat = getattr(getattr(update, "message", None), "chat", None)
    return chat if getattr(chat, "type", None) == "private" else None


def require_admin(update):
    """Server-side guard for every admin command, button and callback.

    Admin actions are restricted to private chats so that an admin keyboard is
    never broadcast into a group where ordinary members could see it.
    """
    user = getattr(update, "from_user", None)
    if not is_admin(user):
        if hasattr(update, "data"):
            try:
                bot.answer_callback_query(update.id, "⛔ Admin only", show_alert=True)
            except Exception:
                pass
        else:
            chat = getattr(update, "chat", None)
            if getattr(chat, "type", None) == "private":
                try:
                    safe_send(chat.id,
                              "⛔ <b>Access denied.</b>\nএই অ্যাকশনটি শুধুমাত্র অ্যাডমিনের জন্য।")
                except Exception:
                    pass
        return False

    if _private_chat(update) is None:
        if hasattr(update, "data"):
            try:
                bot.answer_callback_query(
                    update.id,
                    "🛡️ Admin panel শুধুমাত্র private chat-এ ব্যবহার করুন।",
                    show_alert=True)
            except Exception:
                pass
        else:
            chat = getattr(update, "chat", None)
            try:
                safe_send(chat.id,
                          "🛡️ <b>Admin actions are private-only.</b>\n"
                          "অনুগ্রহ করে বটকে private chat-এ /admin দিন।")
            except Exception:
                pass
        return False
    return True

# =====================================================================
# FORCE JOIN — fully driven by config.json
# =====================================================================

def check_membership(user_id):
    """True = allowed / disabled, False = not joined, None = check failed."""
    if not FORCE_JOIN_ENABLED:
        return True
    for ch in FORCE_JOIN_CHANNELS:
        target = ch.get("username")
        if not target:
            continue
        try:
            member = bot.get_chat_member(target, user_id)
            if getattr(member, "status", None) in ("left", "kicked"):
                return False
        except Exception as exc:
            log.warning("Membership check failed | uid=%s channel=%s | %s",
                        user_id, target, exc)
            return None
    return True


def get_join_keyboard():
    mk = InlineKeyboardMarkup(row_width=1)
    for ch in FORCE_JOIN_CHANNELS:
        label = ch.get("label") or ch.get("username") or "Channel"
        if ch.get("url"):
            mk.add(InlineKeyboardButton("📢 " + str(label), url=ch["url"]))
    mk.add(InlineKeyboardButton("✅ Verify / I've Joined", callback_data="verify_join"))
    return mk


def send_join_message(chat_id):
    lines = [
        "╭━━━━━━━━━━━━━━━━━━━━━━━━━╮",
        "   🔐 <b>VERIFICATION REQUIRED</b>",
        "╰━━━━━━━━━━━━━━━━━━━━━━━━━╯",
        "",
        "বট ব্যবহার করার আগে নিচের চ্যানেলগুলোতে জয়েন করুন।",
        "জয়েন শেষে <b>✅ Verify</b> চাপুন।",
    ]
    safe_send(chat_id, "\n".join(lines), reply_markup=get_join_keyboard())


def check_verified_or_notify(message):
    user = getattr(message, "from_user", None)
    if not user:
        return False
    if is_admin(user):
        return True
    if not FORCE_JOIN_ENABLED:
        touch_user(user.id)
        return True
    status = check_membership(user.id)
    if status is None:
        safe_send(message.chat.id,
                  "⚠️ <b>Verification temporarily unavailable.</b>\n"
                  "অনুগ্রহ করে কিছুক্ষণ পর আবার চেষ্টা করুন।")
        return False
    if not status:
        send_join_message(message.chat.id)
        return False
    touch_user(user.id)
    return True


# =====================================================================
# KEYBOARDS
# =====================================================================

def get_main_keyboard(user_id, chat_type="private"):
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        KeyboardButton("🚀 Register Merchant"),
        KeyboardButton("👤 Profile"),
        KeyboardButton("🔗 Referral Link"),
        KeyboardButton("🛒 Buy Points"),
        KeyboardButton("🔄 Send Points"),
        KeyboardButton("ℹ️ Developer Info"),
    )
    # Admin button is added ONLY for the configured numeric admin ID,
    # and ONLY in a private chat (never leaked into a group).
    if is_admin(user_id) and chat_type == "private":
        mk.add(KeyboardButton("🛡️ Admin Panel"))
    return mk


def get_admin_keyboard():
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        KeyboardButton("👥 Users Info"),
        KeyboardButton("📢 Broadcast"),
        KeyboardButton("➕ Add Points"),
        KeyboardButton("➖ Remove Points"),
        KeyboardButton("🔙 Back to User Menu"),
    )
    return mk


def get_buy_inline():
    mk = InlineKeyboardMarkup(row_width=1)
    if ADMIN_URL:
        mk.add(InlineKeyboardButton("💬 Contact Admin to Buy Credits", url=ADMIN_URL))
    mk.add(InlineKeyboardButton("ℹ️ Developer Info", callback_data="developer_info"))
    return mk


def get_developer_inline():
    mk = InlineKeyboardMarkup(row_width=1)
    if DEVELOPER_URL:
        mk.add(InlineKeyboardButton("💬 Message Developer", url=DEVELOPER_URL))
    mk.add(InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu"))
    return mk


# =====================================================================
# MESSAGE DESIGN (premium, mobile-friendly, HTML-safe)
# =====================================================================

LINE = "━━━━━━━━━━━━━━━━━━━━━━━━━"


def header(title):
    return "╭" + LINE + "╮\n   ✦ " + b(title) + "\n╰" + LINE + "╯"


def welcome_text(user, points):
    return "\n".join([
        header("CARRYBEE MERCHANT"),
        "",
        "👋 স্বাগতম, %s!" % b(getattr(user, "first_name", "there")),
        "💎 ব্যালেন্স: %s" % b("%s Pts" % points),
        "",
        "🧭 নিচের মেনু থেকে পছন্দের অপশন বেছে নিন।",
    ])


def verified_text(user, points):
    return "\n".join([
        header("VERIFIED SUCCESSFULLY"),
        "",
        "✅ স্বাগতম, %s!" % b(getattr(user, "first_name", "there")),
        "💎 ব্যালেন্স: %s" % b("%s Pts" % points),
        "",
        "আপনার মেনু প্রস্তুত 👇",
    ])


def profile_text(user_id, username, points, refs):
    shown = ("@" + username.lstrip("@")) if username else "N/A"
    return "\n".join([
        header("MY PROFILE"),
        "",
        "🆔 User ID   : %s" % code(user_id),
        "📛 Username  : %s" % b(shown),
        "💎 Balance   : %s" % b("%s Pts" % points),
        "👥 Referrals : %s" % b(refs),
        "",
        "<i>রেফারেল প্রতি আপনি পান ৩ Pts বোনাস।</i>",
    ])


def referral_text(link):
    return "\n".join([
        header("YOUR REFERRAL LINK"),
        "",
        "🔗 %s" % code(link),
        "",
        "💡 প্রতি নতুন রেফারেলে আপনি পাবেন %s বোনাস।" % b("৩ Pts"),
        "",
        "<i>লিংকটি বন্ধুদের দিন, তারা জয়েন করলেই পয়েন্ট যোগ হবে।</i>",
    ])


def pricing_text():
    rows = CFG["pricing"] or [
        {"points": 10, "price": 30}, {"points": 30, "price": 60}, {"points": 50, "price": 100},
    ]
    lines = [header("BUY POINTS"), "", "💎 <b>AVAILABLE PACKAGES</b>", ""]
    for row in rows:
        try:
            lines.append("▫️ %s Pts  —  <b>%s Tk</b>" %
                         (int(row["points"]), int(row["price"])))
        except Exception:
            continue
    lines += ["", "💳 ক্রেডিট কিনতে নিচের বাটনে ক্লিক করুন 👇"]
    return "\n".join(lines)


def developer_text():
    lines = [header("DEVELOPER INFO"), "", "👨‍💻 <b>Developer</b> : %s" % esc(DEVELOPER_NAME)]
    if DEVELOPER_USERNAME:
        lines.append("🔖 Username   : %s" % esc(DEVELOPER_USERNAME))
    if ADMIN_NAME:
        lines.append("🛡️ Admin      : %s" % esc(ADMIN_NAME))
    if DEVELOPER_NOTE:
        lines += ["", "<i>%s</i>" % esc(DEVELOPER_NOTE)]
    lines += ["", "📩 যোগাযোগের জন্য নিচের বাটন ব্যবহার করুন।"]
    return "\n".join(lines)


def admin_panel_text(total, active):
    return "\n".join([
        header("ADMIN CONTROL PANEL"),
        "",
        "🛡️ Admin : %s" % b(ADMIN_NAME),
        "👥 Total Registered : %s" % b(total),
        "🟢 Active (7 days)  : %s" % b(active),
        "",
        "নিচের অপশন থেকে কাজটি বেছে নিন 👇",
    ])


def users_page_content(page, per_page=10):
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen >= ?",
            (int(time.time()) - 7 * 86400,),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT user_id, username, points FROM users ORDER BY user_id LIMIT ? OFFSET ?",
            (per_page, max(0, (page - 1) * per_page)),
        ).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    lines = [
        header("USERS INFORMATION"),
        "",
        "📊 Total Registered : %s" % b(total),
        "🟢 Active (7 days)  : %s" % b(active),
        "📄 Page             : %s" % b("%s/%s" % (page, pages)),
        "",
    ]
    if rows:
        for r in rows:
            uname = ("@" + r["username"].lstrip("@")) if r["username"] else "—"
            lines.append("• %s  %s  • %s Pts" %
                         (code(r["user_id"]), esc(uname), b(r["points"])))
    else:
        lines.append("<i>কোনো ইউজার পাওয়া যায়নি।</i>")
    mk = InlineKeyboardMarkup(row_width=2)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="users_page:%s" % (page - 1)))
    if page < pages:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="users_page:%s" % (page + 1)))
    if nav:
        mk.add(*nav)
    mk.add(InlineKeyboardButton("🔙 Admin Panel", callback_data="back_to_admin"))
    return "\n".join(lines), mk


def broadcast_progress_text(sent, failed, total, cancelled=False):
    remaining = max(0, total - sent - failed)
    state = "CANCELLED" if cancelled else "IN PROGRESS"
    return "\n".join([
        header("BROADCAST " + state),
        "",
        "📤 Sent      : %s" % b(sent),
        "❌ Failed    : %s" % b(failed),
        "⏳ Remaining : %s" % b(remaining),
        "📊 Total     : %s" % b(total),
    ])


def broadcast_result_text(total, sent, failed, cancelled=False):
    title = "BROADCAST CANCELLED" if cancelled else "BROADCAST COMPLETE"
    return "\n".join([
        header(title),
        "",
        "👥 Audience : %s" % b(total),
        "✅ Success  : %s" % b(sent),
        "❌ Failed   : %s" % b(failed),
        "",
        "<i>ব্যর্থ ইউজারদের জন্য ব্রডকাস্ট থামানো হয়নি।</i>",
    ])


# =====================================================================
# HANDLERS — commands first, then callbacks, then states, then fallback
# =====================================================================

@bot.message_handler(commands=["start"])
def start(message):
    user = message.from_user
    if not user:
        return
    uid = user.id

    ref = None
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref = int(parts[1][4:])
            if ref <= 0:
                ref = None
        except ValueError:
            ref = None
    if ref and ref != uid:
        state_set("ref", uid, ref)

    if not is_admin(user):
        status = check_membership(uid)
        if status is None:
            safe_send(message.chat.id,
                      "⚠️ <b>Verification temporarily unavailable.</b>\n"
                      "অনুগ্রহ করে কিছুক্ষণ পর আবার চেষ্টা করুন।")
            return
        if not status:
            send_join_message(message.chat.id)
            return

    referrer = state_pop("ref", uid)
    points = get_user(uid, user.username, referrer)
    clear_states(uid)
    safe_send(message.chat.id, welcome_text(user, points),
              reply_markup=get_main_keyboard(uid, message.chat.type))


@bot.message_handler(commands=["cancel"])
def cancel_reg(message):
    if not check_verified_or_notify(message):
        return
    uid = message.from_user.id
    if state_get("user", uid) or state_get("admin", uid) or state_get("ref", uid):
        clear_states(uid)
        safe_send(message.chat.id, "❌ <b>Cancelled.</b>\nচলমান কাজ বাতিল হয়েছে।",
                  reply_markup=get_main_keyboard(uid, message.chat.type))
    else:
        safe_send(message.chat.id, "ℹ️ <b>Nothing to cancel.</b>\nকোনো চলমান কাজ নেই।")


@bot.message_handler(commands=["myid"])
def myid(message):
    bot.reply_to(message,
                 "🆔 <b>Your ID</b> : %s\n📛 <b>Username</b> : %s" %
                 (code(message.from_user.id),
                  esc("@" + message.from_user.username if message.from_user.username else "N/A")),
                 parse_mode="HTML")


# ------------------------- ADMIN COMMANDS -------------------------

@bot.message_handler(commands=["admin", "adminpanel", "panel"])
def cmd_admin_panel(message):
    if not require_admin(message):
        return
    show_admin_panel(message)


@bot.message_handler(commands=["addpoints"])
def cmd_add_points(message):
    if not require_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        safe_send(message.chat.id, "⚠️ Usage: <code>/addpoints &lt;User_ID&gt; &lt;Amount&gt;</code>")
        return
    try:
        target, amount = int(parts[1]), int(parts[2])
    except ValueError:
        safe_send(message.chat.id, "⚠️ ID ও Amount অবশ্যই সংখ্যা হতে হবে।")
        return
    apply_point_change(message, target, amount, add=True)


@bot.message_handler(commands=["removepoints"])
def cmd_remove_points(message):
    if not require_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        safe_send(message.chat.id, "⚠️ Usage: <code>/removepoints &lt;User_ID&gt; &lt;Amount&gt;</code>")
        return
    try:
        target, amount = int(parts[1]), int(parts[2])
    except ValueError:
        safe_send(message.chat.id, "⚠️ ID ও Amount অবশ্যই সংখ্যা হতে হবে।")
        return
    apply_point_change(message, target, amount, add=False)


@bot.message_handler(commands=["users", "userlist"])
def cmd_users(message):
    if not require_admin(message):
        return
    text, markup = users_page_content(1)
    safe_send(message.chat.id, text, reply_markup=markup)


def apply_point_change(message, target, amount, add=True):
    """Shared, validated implementation for both buttons and commands."""
    if amount > MAX_POINTS_PER_OP:
        safe_send(message.chat.id,
                  "Amount is too large (max %s)." % MAX_POINTS_PER_OP)
        return
    if amount <= 0:
        safe_send(message.chat.id, "⚠️ <b>Amount must be positive.</b>")
        return
    row = get_user_profile(target)
    if not row:
        safe_send(message.chat.id, "❌ এই ID-তে কোনো ইউজার নেই।")
        return
    if not add and row[1] < amount:
        safe_send(message.chat.id,
                  "⚠️ ইউজারের আছে মাত্র %s Pts।" % b(row[1]))
        return
    update_points(target, amount if add else -amount)
    safe_send(message.chat.id,
              "✅ <b>%s Pts %s</b>\n👤 User: %s" %
              (amount, "যোগ হয়েছে" if add else "কাটা হয়েছে", code(target)),
              reply_markup=get_admin_keyboard())
    try:
        safe_send(target,
                  "💎 <b>Points updated</b>\nআপনার ব্যালেন্সে %s %s Pts।" %
                  ("+" if add else "-", amount))
    except Exception as exc:
        log.info("Point notification failed for %s: %s", target, exc)


# ------------------------- /send -------------------------

@bot.message_handler(commands=["send"])
def send_points(message):
    if not check_verified_or_notify(message):
        return
    try:
        parts = (message.text or "").split()
        if len(parts) < 3:
            safe_send(message.chat.id, "⚠️ Usage: <code>/send &lt;User_ID&gt; &lt;Amount&gt;</code>")
            return
        target, amount = int(parts[1]), int(parts[2])
        sender = message.from_user.id
        if amount > MAX_POINTS_PER_OP:
            safe_send(message.chat.id,
                      "Amount is too large (max %s)." % MAX_POINTS_PER_OP)
            return
        if amount <= 0:
            safe_send(message.chat.id, "⚠️ <b>Amount must be positive.</b>")
            return
        if target == sender:
            safe_send(message.chat.id, "⚠️ নিজেকে পয়েন্ট পাঠানো যাবে না।")
            return
        with db() as conn:
            srow = conn.execute("SELECT points FROM users WHERE user_id=?", (sender,)).fetchone()
            if not srow or srow[0] < amount:
                safe_send(message.chat.id, "❌ <b>Insufficient balance.</b>")
                return
            if not conn.execute("SELECT 1 FROM users WHERE user_id=?", (target,)).fetchone():
                safe_send(message.chat.id, "❌ প্রাপক রেজিস্টার্ড নন।")
                return
            conn.execute("UPDATE users SET points = points - ? WHERE user_id = ?", (amount, sender))
            conn.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (amount, target))
        safe_send(message.chat.id,
                  "✅ <b>%s Pts transferred</b> → %s" % (amount, code(target)))
        try:
            safe_send(target, "🎁 <b>Gift received!</b>\n%s থেকে %s Pts পেয়েছেন।" %
                      (code(sender), amount))
        except Exception:
            pass
    except Exception as exc:
        log.exception("send_points failed")
        safe_send(message.chat.id, "⚠️ <b>Transfer failed.</b>\nঅনুগ্রহ করে আবার চেষ্টা করুন।")


# ------------------------- DEVELOPER INFO (dedicated handler) -------------------------

@bot.message_handler(func=lambda m: normalize(getattr(m, "text", None)) == "developerinfo")
def developer_info_message(message):
    if not check_verified_or_notify(message):
        return
    clear_states(message.from_user.id)
    safe_send(message.chat.id, developer_text(), reply_markup=get_developer_inline())


@bot.callback_query_handler(func=lambda c: c.data == "developer_info")
def developer_info_callback(call):
    bot.answer_callback_query(call.id)
    try:
        safe_edit(call.message.chat.id, call.message.message_id,
                  developer_text(), reply_markup=get_developer_inline())
    except Exception:
        safe_send(call.message.chat.id, developer_text(),
                  reply_markup=get_developer_inline())


@bot.callback_query_handler(func=lambda c: c.data == "back_to_menu")
def back_to_menu_callback(call):
    bot.answer_callback_query(call.id)
    clear_states(call.from_user.id)
    safe_send(call.message.chat.id, "🏠 <b>Main Menu</b>",
              reply_markup=get_main_keyboard(call.from_user.id,
                                             call.message.chat.type))


# ------------------------- VERIFY CALLBACK -------------------------

@bot.callback_query_handler(func=lambda c: c.data == "verify_join")
def verify_join(call):
    user = call.from_user
    uid = user.id
    if FORCE_JOIN_ENABLED:
        status = check_membership(uid)
        if status is None:
            bot.answer_callback_query(call.id, "⚠️ চেক সমস্যা, আবার চেষ্টা করুন।", show_alert=True)
            return
        if not status:
            bot.answer_callback_query(call.id, "❌ আপনি এখনো চ্যানেলে জয়েন করেননি!", show_alert=True)
            return
    bot.answer_callback_query(call.id, "✅ Verification successful")
    referrer = state_pop("ref", uid)
    points = get_user(uid, user.username, referrer)
    clear_states(uid)
    safe_edit(call.message.chat.id, call.message.message_id,
              verified_text(user, points))
    safe_send(call.message.chat.id, "🧭 <b>Menu loaded</b> 👇",
              reply_markup=get_main_keyboard(uid, call.message.chat.type))


# ------------------------- ADMIN PANEL BUTTONS -------------------------

@bot.message_handler(func=lambda m: normalize(getattr(m, "text", None)) == ADMIN_PANEL_BUTTON)
def show_admin_panel(message):
    if not require_admin(message):
        return
    clear_states(message.from_user.id)
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen >= ?",
            (int(time.time()) - 7 * 86400,),
        ).fetchone()[0]
    safe_send(message.chat.id, admin_panel_text(total, active),
              reply_markup=get_admin_keyboard())


@bot.message_handler(func=lambda m: normalize(getattr(m, "text", None)) in ADMIN_MENU_BUTTONS)
def handle_admin_menu(message):
    if not require_admin(message):
        return
    uid = message.from_user.id
    key = normalize(message.text)

    if key == "backtousermenu":
        clear_states(uid)
        safe_send(message.chat.id, "🏠 <b>Back to user menu</b>",
                  reply_markup=get_main_keyboard(uid, message.chat.type))
        return
    if key == "usersinfo":
        text, markup = users_page_content(1)
        safe_send(message.chat.id, text, reply_markup=markup)
        return
    if key in ("addpoints", "removepoints"):
        state_clear_admin(uid)
        state_set("admin", uid, {
            "action": "add" if key == "addpoints" else "remove",
            "step": "waiting_id",
        })
        safe_send(message.chat.id,
                  "🆔 <b>User ID পাঠান</b>\nযার ব্যালেন্স পরিবর্তন করবেন:")
        return
    if key == "broadcast":
        state_clear_admin(uid)
        state_set("admin", uid, {"action": "broadcast", "step": "waiting_content"})
        safe_send(message.chat.id,
                  "\n".join([
                      header("NEW BROADCAST"),
                      "",
                      "📢 <b>কন্টেন্ট পাঠান</b> (একটি মেসেজে):",
                      "• Text / Photo / Video / Document",
                      "• Voice / Audio / Animation / Sticker",
                      "",
                      "<i>/cancel দিয়ে বাতিল করুন।</i>",
                  ]))


def state_clear_admin(uid):
    state_pop("admin", uid)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("users_page:"))
def users_page_callback(call):
    if not require_admin(call):
        return
    try:
        page = max(1, int(call.data.split(":", 1)[1]))
    except (ValueError, IndexError):
        page = 1
    bot.answer_callback_query(call.id)
    text, markup = users_page_content(page)
    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data == "back_to_admin")
def back_to_admin_callback(call):
    if not require_admin(call):
        return
    bot.answer_callback_query(call.id)
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM users WHERE last_seen >= ?",
            (int(time.time()) - 7 * 86400,),
        ).fetchone()[0]
    safe_edit(call.message.chat.id, call.message.message_id,
              admin_panel_text(total, active))


# ------------------------- ADMIN STATE HANDLER -------------------------

@bot.message_handler(func=lambda m: is_admin(m.from_user)
                     and state_get("admin", m.from_user.id) is not None
                     and normalize(getattr(m, "text", None)) not in ALL_MENU_KEYS
                     and not (getattr(m, "text", "") or "").startswith("/"))
def handle_admin_steps(message):
    if not require_admin(message):
        return
    uid = message.from_user.id
    state = state_get("admin", uid)
    if not state:
        return

    try:
        if state["action"] in ("add", "remove"):
            if state["step"] == "waiting_id":
                try:
                    target = int((message.text or "").strip())
                except ValueError:
                    safe_send(message.chat.id, "❌ সঠিক ID পাঠান (শুধু সংখ্যা):")
                    return
                row = get_user_profile(target)
                if not row:
                    safe_send(message.chat.id, "❌ এই ID-তে কোনো ইউজার নেই।")
                    return
                state["target_id"] = target
                state["step"] = "waiting_amount"
                state_set("admin", uid, state)
                safe_send(message.chat.id,
                          "👤 %s • ব্যালেন্স <b>%s Pts</b>\n\nকত Pts %s করবেন?" %
                          (esc(row[0] or "N/A"), row[1],
                           "যোগ" if state["action"] == "add" else "কাটা"))
                return
            if state["step"] == "waiting_amount":
                try:
                    amount = int((message.text or "").strip())
                except ValueError:
                    safe_send(message.chat.id, "❌ সংখ্যা পাঠান:")
                    return
                apply_point_change(message, state["target_id"], amount,
                                   add=(state["action"] == "add"))
                state_pop("admin", uid)
                return

        if state["action"] == "broadcast" and state["step"] == "waiting_content":
            if (message.text or "").startswith("/"):
                safe_send(message.chat.id, "❌ কমান্ড নয়, ব্রডকাস্ট কন্টেন্ট পাঠান।")
                return
            content = extract_broadcast_content(message)
            if not content:
                safe_send(message.chat.id,
                          "❌ এই মেসেজটি ব্রডকাস্টের জন্য সমর্থিত নয়।")
                return
            state_pop("admin", uid)
            start_broadcast(message.chat.id, content)
    except Exception:
        log.exception("handle_admin_steps failed")
        safe_send(message.chat.id, "⚠️ <b>Action failed.</b>\nসার্ভারে বিস্তারিত লগ করা হয়েছে।")


def extract_broadcast_content(message):
    """Map an incoming Telegram message to a broadcast payload."""
    if message.text:
        return {"type": "text", "text": message.text}
    caption = message.caption or ""
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id, "caption": caption}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id, "caption": caption}
    if message.document:
        return {"type": "document", "file_id": message.document.file_id, "caption": caption}
    if message.voice:
        return {"type": "voice", "file_id": message.voice.file_id, "caption": caption}
    if message.audio:
        return {"type": "audio", "file_id": message.audio.file_id, "caption": caption}
    if message.animation:
        return {"type": "animation", "file_id": message.animation.file_id, "caption": caption}
    if message.sticker:
        return {"type": "sticker", "file_id": message.sticker.file_id}
    if message.video_note:
        return {"type": "video_note", "file_id": message.video_note.file_id}
    return None


# ------------------------- USER MENU -------------------------

@bot.message_handler(func=lambda m: normalize(getattr(m, "text", None)) in USER_BUTTONS)
def handle_menu(message):
    if not check_verified_or_notify(message):
        return
    uid = message.from_user.id
    key = normalize(message.text)

    clear_states(uid)

    try:
        if key in ("profile", "mybalance"):
            row = get_user_profile(uid)
            if not row:
                points = get_user(uid, message.from_user.username)
                row = (message.from_user.username or message.from_user.first_name or "User",
                       points, None)
            safe_send(message.chat.id,
                      profile_text(uid, row[0], row[1], count_referrals(uid)))

        elif key == "referrallink":
            info = bot.get_me()
            link = "https://t.me/%s?start=ref_%s" % (info.username, uid)
            safe_send(message.chat.id, referral_text(link))

        elif key == "buypoints":
            safe_send(message.chat.id, pricing_text(), reply_markup=get_buy_inline())

        elif key == "registermerchant":
            if message.chat.type != "private":
                safe_send(message.chat.id,
                          "🔒 <b>Registration is private-only.</b>\n"
                          "অনুগ্রহ করে বটকে private chat-এ মেসেজ দিন।")
                return
            points = get_user(uid, message.from_user.username)
            if points < 1:
                safe_send(message.chat.id,
                          "❌ <b>Insufficient points.</b>\nরেজিস্ট্রেশনের জন্য কমপক্ষে ১ Pts দরকার।")
                return
            state_set("user", uid, {"step": "waiting_phone"})
            safe_send(message.chat.id,
                      "\n".join([
                          header("MERCHANT REGISTRATION"),
                          "",
                          "📞 যে নাম্বারে SMS পাঠাতে চান সেটি লিখুন 🇧🇩",
                          "",
                          "<i>বাতিল: /cancel</i>",
                      ]))

        elif key == "sendpoints":
            safe_send(message.chat.id,
                      "🔄 <b>Send Points</b>\n\nUsage: <code>/send &lt;User_ID&gt; &lt;Amount&gt;</code>")
    except Exception:
        log.exception("handle_menu failed for key=%s", key)
        safe_send(message.chat.id, "⚠️ <b>Request failed.</b>\nঅনুগ্রহ করে আবার চেষ্টা করুন।")


# ------------------------- REGISTRATION STATE -------------------------

@bot.message_handler(func=lambda m: state_get("user", m.from_user.id) is not None
                     and state_get("admin", m.from_user.id) is None
                     and normalize(getattr(m, "text", None)) not in ALL_MENU_KEYS
                     and not (getattr(m, "text", "") or "").startswith("/")
                     and getattr(getattr(m, "chat", None), "type", None) == "private")
def process_registration_steps(message):
    uid = message.from_user.id
    if not check_verified_or_notify(message):
        state_pop("user", uid)
        return
    state = state_get("user", uid)
    if not state:
        return

    try:
        if state["step"] == "waiting_phone":
            raw = (message.text or "").strip()
            digits = re.sub(r"\D", "", raw)
            if not re.fullmatch(r"\+?[0-9][0-9\-\s]{7,24}", raw) or not (10 <= len(digits) <= 15):
                safe_send(message.chat.id,
                          "❌ সঠিক নাম্বার দিন (১০–১৫ ডিজিট, দেশের কোডসহ):")
                return
            state["phone"] = "+" + digits if raw.startswith("+") else digits
            state["step"] = "waiting_name"
            state_set("user", uid, state)
            safe_send(message.chat.id,
                      "🏢 <b>Company name</b> লিখুন (যেমন: বিকাশ):")
            return

        if state["step"] == "waiting_name":
            name = (message.text or "").strip()
            if not (2 <= len(name) <= 100):
                safe_send(message.chat.id, "❌ ২–১০০ অক্ষরের নাম দিন:")
                return
            state["name"] = name
            state["step"] = "waiting_business"
            state_set("user", uid, state)
            safe_send(message.chat.id,
                      "📝 <b>Business name / SMS content</b> লিখুন:")
            return

        if state["step"] == "waiting_business":
            business = (message.text or "").strip()
            if not (1 <= len(business) <= 200):
                safe_send(message.chat.id, "❌ ১–২০০ অক্ষর লিখুন:")
                return
            if len(business) < 3:
                business += " Enterprise"
            state_pop("user", uid)

            row = get_user_profile(uid)
            if not row or row[1] < 1:
                safe_send(message.chat.id,
                          "❌ <b>Insufficient points.</b>\nরেজিস্ট্রেশন বাতিল।")
                return

            payload = {
                "name": state["name"],
                "phone_number": state["phone"],
                "business_name": business,
            }
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "origin": "https://merchant.carrybee.com",
                "referer": "https://merchant.carrybee.com/",
            }
            progress = safe_send(message.chat.id,
                                 "⏳ <b>CarryBee API-তে রিকোয়েস্ট পাঠানো হচ্ছে…</b>")
            try:
                response = requests.post(
                    API_URL, json=payload, headers=headers,
                    verify=VERIFY_TLS, timeout=API_TIMEOUT,
                )
                data = response.json()
                if not data.get("error", True):
                    update_points(uid, -1)
                    d = data.get("data") or {}
                    safe_edit(
                        message.chat.id, progress.message_id,
                        "\n".join([
                            header("REGISTRATION SUCCESSFUL"),
                            "",
                            "👤 Name    : %s" % esc(d.get("name")),
                            "📞 Phone   : %s" % esc(d.get("phone_number")),
                            "🏢 Business: %s" % esc(d.get("business_name")),
                            "🔑 OTP     : %s" % code(d.get("otp_prefix")),
                            "",
                            "<i>১ Pts কাটা হয়েছে।</i>",
                        ]),
                    )
                else:
                    safe_edit(message.chat.id, progress.message_id,
                              "⚠️ <b>Registration rejected by provider.</b>\n"
                              "অনুগ্রহ করে তথ্য যাচাই করে আবার চেষ্টা করুন।")
            except Exception:
                log.exception("Merchant API call failed")
                safe_edit(message.chat.id, progress.message_id,
                          "⚠️ <b>Provider unavailable.</b>\nকোনো পয়েন্ট কাটা হয়নি।")
    except Exception:
        log.exception("Registration step failed")
        state_pop("user", uid)
        safe_send(message.chat.id, "⚠️ <b>Registration failed.</b>\nআবার /start দিন।")


# ------------------------- BROADCAST ENGINE -------------------------

def _retry_after(exc):
    try:
        result = getattr(exc, "result_json", None) or {}
        params = result.get("parameters") or {}
        if "retry_after" in params:
            return max(1, int(params["retry_after"]))
    except Exception:
        pass
    if getattr(exc, "error_code", None) == 429 or "429" in str(exc) or "retry_after" in str(exc).lower():
        m = re.search(r"retry_after[^\d]{0,20}(\d+)", str(exc), re.I)
        return max(1, int(m.group(1))) if m else 5
    return None


def send_broadcast_piece(uid, content):
    """One recipient. Never raises. Handles FloodWait, blocks, format issues."""
    kind = content.get("type")
    caption = content.get("caption") or None  # no parse_mode => no formatting errors
    for attempt in range(3):
        try:
            if kind == "text":
                bot.send_message(uid, content.get("text", ""))
            elif kind == "photo":
                bot.send_photo(uid, content["file_id"], caption=caption)
            elif kind == "video":
                bot.send_video(uid, content["file_id"], caption=caption)
            elif kind == "document":
                bot.send_document(uid, content["file_id"], caption=caption)
            elif kind == "voice":
                bot.send_voice(uid, content["file_id"], caption=caption)
            elif kind == "audio":
                bot.send_audio(uid, content["file_id"], caption=caption)
            elif kind == "animation":
                bot.send_animation(uid, content["file_id"], caption=caption)
            elif kind == "sticker":
                bot.send_sticker(uid, content["file_id"])
            elif kind == "video_note":
                bot.send_video_note(uid, content["file_id"])
            else:
                return False
            return True
        except Exception as exc:
            wait = _retry_after(exc)
            if wait and attempt < 2:
                log.info("FloodWait for %s: %ss", uid, wait)
                time.sleep(min(wait + 1, 60))
                continue
            log.info("Broadcast to %s failed (%s): %s", uid, kind, exc)
            return False
    return False


def start_broadcast(admin_chat_id, content):
    job_id = uuid.uuid4().hex[:10]
    cancel_event(job_id)  # create fresh event
    users = _all_user_ids()
    job_create(job_id, ADMIN_ID, content, len(users))

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("🛑 Cancel Broadcast",
                                    callback_data="broadcast_cancel:" + job_id))
    progress = safe_send(admin_chat_id,
                         broadcast_progress_text(0, 0, len(users)),
                         reply_markup=markup)

    thread = threading.Thread(
        target=run_broadcast,
        args=(job_id, admin_chat_id, progress.message_id, users),
        daemon=True,
    )
    thread.start()
    return job_id


def run_broadcast(job_id, admin_chat_id, progress_id, users):
    event = cancel_event(job_id)
    total = len(users)
    sent = failed = 0
    last_update = 0.0
    cancelled = False
    content = _job_content(job_id)

    for uid in users:
        if event.is_set():
            cancelled = True
            break
        if send_broadcast_piece(uid, content):
            sent += 1
        else:
            failed += 1
        now = time.time()
        if now - last_update >= 1.0 or (sent + failed) == total:
            job_progress(job_id, sent, failed, "cancelled" if event.is_set() else "running")
            safe_edit(admin_chat_id, progress_id,
                      broadcast_progress_text(sent, failed, total, event.is_set()))
            last_update = now
        time.sleep(0.05)

    status = "cancelled" if cancelled else "completed"
    job_progress(job_id, sent, failed, status)
    report = broadcast_result_text(total, sent, failed, cancelled)
    if not safe_edit(admin_chat_id, progress_id, report):
        safe_send(admin_chat_id, report, reply_markup=get_admin_keyboard())
    with _cancel_lock:
        _cancel_events.pop(job_id, None)


def _job_content(job_id):
    with db() as conn:
        row = conn.execute("SELECT content_json FROM broadcast_jobs WHERE job_id=?",
                           (job_id,)).fetchone()
    if not row:
        return {"type": "text", "text": ""}
    try:
        return json.loads(row[0])
    except Exception:
        return {"type": "text", "text": ""}


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("broadcast_cancel:"))
def broadcast_cancel_callback(call):
    if not require_admin(call):
        return
    job_id = call.data.split(":", 1)[1]
    cancel_event(job_id).set()
    job_progress(job_id, status="cancelled")
    bot.answer_callback_query(call.id, "🛑 Broadcast cancelled", show_alert=True)
    try:
        safe_edit(call.message.chat.id, call.message.message_id,
                  "🛑 <b>Broadcast cancellation requested.</b>")
    except Exception:
        pass


# ------------------------- FALLBACK -------------------------

@bot.message_handler(func=lambda m: True)
def fallback(message):
    uid = getattr(message.from_user, "id", None)
    log.info("Unhandled message | uid=%s normalized=%r", uid, normalize(getattr(message, "text", None)))
    if getattr(getattr(message, "chat", None), "type", None) == "private":
        safe_send(message.chat.id,
                  "❓ <b>Command not recognized.</b>\nমেনু ব্যবহার করুন অথবা /start দিন।",
                  reply_markup=get_main_keyboard(uid, message.chat.type))


# =====================================================================
# RUN
# =====================================================================

if __name__ == "__main__":
    log.info("Bot starting | admin_id=%s | force_join=%s | channels=%s | api=%s",
             ADMIN_ID, FORCE_JOIN_ENABLED, len(FORCE_JOIN_CHANNELS), API_URL)
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as exc:
            log.exception("Polling crashed, restarting in 5s: %s", exc)
            time.sleep(5)
