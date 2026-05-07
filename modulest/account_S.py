import asyncio
import html
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

try:
    from config import Config
    from db import db
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import Config
    from db import db


logger = logging.getLogger("AccountModule")

MAIN_BUTTON = "👤حسابي"
PROFILE_CHANNEL_ID = -1003838515584
PREMIUM_REFERRAL_THRESHOLD = 5
TRADER_REFERRAL_THRESHOLD = 10
TRADER_CHANNEL_THRESHOLD = 10000


# =========================================================
# Helpers
# =========================================================
def _db_path() -> str:
    if hasattr(db, "get_db_path"):
        try:
            return db.get_db_path()
        except Exception:
            pass
    return os.path.join("data", "system_database.db")


def _connect():
    if hasattr(db, "_connect"):
        return db._connect()

    conn = sqlite3.connect(_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _fetchone(query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    conn = _connect()
    try:
        cur = conn.execute(query, params)
        return cur.fetchone()
    finally:
        conn.close()


def _execute(query: str, params: tuple = ()) -> None:
    conn = _connect()
    try:
        conn.execute(query, params)
        conn.commit()
    finally:
        conn.close()


def _insert(query: str, params: tuple = ()) -> int:
    conn = _connect()
    try:
        cur = conn.execute(query, params)
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


async def db_async(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def safe_text(value: Any, max_len: int = 5000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt)
        except Exception:
            pass
    return None


def _ensure_column(table: str, column: str, col_type: str) -> None:
    try:
        row = _fetchone(f"PRAGMA table_info({table})")
        cols = _fetchone(f"PRAGMA table_info({table})")
        existing_rows = []
        conn = _connect()
        try:
            cur = conn.execute(f"PRAGMA table_info({table})")
            existing_rows = cur.fetchall()
        finally:
            conn.close()
        existing = {r["name"] for r in existing_rows}
        if column not in existing:
            _execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except Exception as e:
        logger.warning(f"ensure column failed {table}.{column}: {e}")


def _already_handled(context: Optional[ContextTypes.DEFAULT_TYPE], update: Update) -> bool:
    if context is None:
        return False
    try:
        uid = getattr(update, "update_id", None)
        if uid is None:
            return False
        seen = context.application.bot_data.setdefault("account_seen_updates", set())
        if uid in seen:
            return True
        seen.add(uid)
        if len(seen) > 1500:
            seen.clear()
        return False
    except Exception:
        return False


# =========================================================
# Schema
# =========================================================
def ensure_schema():
    try:
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER NOT NULL,
                    receiver_id INTEGER NOT NULL,
                    message_text TEXT NOT NULL,
                    reply_text TEXT,
                    status TEXT DEFAULT 'sent',
                    bot_message_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    replied_at DATETIME
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_account_messages_sender
                ON account_messages(sender_id, created_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_account_messages_receiver
                ON account_messages(receiver_id, created_at DESC)
            """)
            conn.commit()
        finally:
            conn.close()

        _ensure_column("users", "last_name", "TEXT")
        _ensure_column("users", "contact_address", "TEXT")
        _ensure_column("users", "region", "TEXT")
        _ensure_column("users", "field", "TEXT")
        _ensure_column("users", "profile_bio", "TEXT")
        _ensure_column("users", "profile_services", "TEXT")
        _ensure_column("users", "profile_photo_id", "TEXT")
        _ensure_column("users", "profile_channel_chat_id", "INTEGER")
        _ensure_column("users", "profile_channel_message_id", "INTEGER")
        _ensure_column("users", "profile_updated_at", "DATETIME")
        _ensure_column("users", "profile_views", "INTEGER DEFAULT 0")
        _ensure_column("users", "profile_likes", "INTEGER DEFAULT 0")

        _ensure_column("users", "trader_is_published", "INTEGER DEFAULT 0")
        _ensure_column("users", "trader_channel_chat_id", "INTEGER")
        _ensure_column("users", "trader_channel_message_id", "INTEGER")
        _ensure_column("users", "trader_published_at", "DATETIME")
        _ensure_column("users", "trader_background_photo_id", "TEXT")
        _ensure_column("users", "trader_views", "INTEGER DEFAULT 0")
        _ensure_column("users", "trader_likes", "INTEGER DEFAULT 0")
        _ensure_column("users", "trader_dislikes", "INTEGER DEFAULT 0")
        _ensure_column("users", "channel_members", "INTEGER DEFAULT 0")
        _ensure_column("users", "trader_request_count", "INTEGER DEFAULT 0")
        _ensure_column("users", "ads_seen_count", "INTEGER DEFAULT 0")
        _ensure_column("users", "profile_profile_card_id", "INTEGER")
    except Exception as e:
        logger.exception(f"ensure_schema failed: {e}")


# =========================================================
# State
# =========================================================
def get_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    state = context.user_data.setdefault("account_state", {})
    state.setdefault("awaiting", None)
    state.setdefault("message_target", None)
    state.setdefault("reply_message_id", None)
    return state


def reset_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["account_state"] = {
        "awaiting": None,
        "message_target": None,
        "reply_message_id": None,
    }


# =========================================================
# Profile
# =========================================================
def get_user_profile(user_id: int) -> Dict[str, Any]:
    profile: Dict[str, Any] = {
        "user_id": user_id,
        "full_name": "",
        "last_name": "",
        "username": "",
        "referred_by": None,
        "referrals_count": 0,
        "balance": 0,
        "points": 0,
        "stars": 0,
        "is_vip": 0,
        "is_premium": 0,
        "premium_until": None,
        "total_ads_posted": 0,
        "total_ads_published": 0,
        "total_ads_found": 0,
        "total_searches": 0,
        "last_active": None,
        "joined_at": None,
        "contact_address": "",
        "region": "",
        "field": "",
        "profile_bio": "",
        "profile_services": "",
        "profile_photo_id": None,
        "profile_views": 0,
        "profile_likes": 0,
        "trader_is_published": 0,
        "trader_channel_chat_id": None,
        "trader_channel_message_id": None,
        "trader_published_at": None,
        "trader_background_photo_id": None,
        "trader_views": 0,
        "trader_likes": 0,
        "trader_dislikes": 0,
        "channel_members": 0,
        "trader_request_count": 0,
        "ads_seen_count": 0,
    }

    try:
        row = None
        if hasattr(db, "get_user"):
            try:
                row = db.get_user(user_id)
            except Exception:
                row = None

        if not row:
            conn = _connect()
            try:
                cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
            finally:
                conn.close()

        if row:
            if isinstance(row, sqlite3.Row):
                row = dict(row)
            profile.update(row)
    except Exception as e:
        logger.warning(f"get_user_profile failed: {e}")

    try:
        if hasattr(db, "get_user_balance"):
            bal = db.get_user_balance(user_id) or {}
            profile["balance"] = safe_int(bal.get("balance", profile.get("balance", 0)))
            profile["points"] = safe_int(bal.get("points", profile.get("points", 0)))
            profile["stars"] = safe_int(bal.get("stars", profile.get("stars", 0)))
            profile["is_vip"] = safe_int(bal.get("is_vip", profile.get("is_vip", 0)))
            profile["is_premium"] = safe_int(bal.get("is_premium", profile.get("is_premium", 0)))
    except Exception:
        pass

    for key in (
        "referrals_count", "total_ads_posted", "total_ads_published", "total_ads_found",
        "total_searches", "profile_views", "profile_likes", "trader_is_published",
        "trader_views", "trader_likes", "trader_dislikes", "channel_members",
        "trader_request_count", "ads_seen_count", "is_vip", "is_premium"
    ):
        profile[key] = safe_int(profile.get(key, 0))

    return profile


def display_name(profile: Dict[str, Any]) -> str:
    name = safe_text(profile.get("full_name"))
    last = safe_text(profile.get("last_name"))
    if name and last:
        return f"{name} {last}".strip()
    if name:
        return name
    if last:
        return last
    username = safe_text(profile.get("username"))
    if username:
        return f"@{username}"
    return f"User {profile.get('user_id')}"


def premium_status_label(profile: Dict[str, Any]) -> str:
    if safe_int(profile.get("is_vip", 0)) == 1:
        return "💎 VIP"
    if safe_int(profile.get("is_premium", 0)) == 1:
        until = parse_dt(profile.get("premium_until"))
        if until is None or until >= datetime.now():
            return "✅ مفعل"
    if safe_int(profile.get("referrals_count", 0)) >= PREMIUM_REFERRAL_THRESHOLD:
        return "🎁 مؤهل بالإحالات"
    return "⏳ غير مفعل"


def premium_until_text(profile: Dict[str, Any]) -> str:
    until = parse_dt(profile.get("premium_until"))
    if not until:
        return "غير محدد"
    return until.strftime("%Y-%m-%d %H:%M")


def can_publish_trader(profile: Dict[str, Any]) -> bool:
    return (
        safe_int(profile.get("is_vip", 0)) == 1
        or safe_int(profile.get("is_premium", 0)) == 1
        or safe_int(profile.get("referrals_count", 0)) >= TRADER_REFERRAL_THRESHOLD
        or safe_int(profile.get("channel_members", 0)) >= TRADER_CHANNEL_THRESHOLD
    )


# =========================================================
# Updates
# =========================================================
def update_user_field(user_id: int, field_name: str, value: Any) -> bool:
    try:
        _execute(
            f"UPDATE users SET {field_name} = ?, profile_updated_at = CURRENT_TIMESTAMP, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
            (value, user_id),
        )
        return True
    except Exception as e:
        logger.exception(f"update_user_field failed: {e}")
        return False


def set_user_profile_value(user_id: int, key: str, value: Any) -> bool:
    mapping = {
        "first_name": "full_name",
        "last_name": "last_name",
        "contact_address": "contact_address",
        "region": "region",
        "field": "field",
        "profile_bio": "profile_bio",
        "profile_services": "profile_services",
        "profile_photo_id": "profile_photo_id",
        "trader_background_photo_id": "trader_background_photo_id",
        "channel_members": "channel_members",
    }
    field = mapping.get(key)
    if not field:
        return False

    if field == "channel_members":
        return update_user_field(user_id, field, safe_int(value, 0))
    if field == "profile_bio":
        return update_user_field(user_id, field, safe_text(value, 160))
    if field == "profile_services":
        return update_user_field(user_id, field, safe_text(value, 500))
    return update_user_field(user_id, field, safe_text(value, 255))


def increment_profile_views(user_id: int) -> None:
    try:
        _execute("UPDATE users SET profile_views = COALESCE(profile_views,0) + 1 WHERE user_id = ?", (user_id,))
    except Exception:
        pass


def increment_profile_likes(user_id: int) -> None:
    try:
        _execute("UPDATE users SET profile_likes = COALESCE(profile_likes,0) + 1 WHERE user_id = ?", (user_id,))
    except Exception:
        pass


def increment_trader_views(user_id: int) -> None:
    try:
        _execute("UPDATE users SET trader_views = COALESCE(trader_views,0) + 1 WHERE user_id = ?", (user_id,))
    except Exception:
        pass


def increment_trader_likes(user_id: int) -> None:
    try:
        _execute("UPDATE users SET trader_likes = COALESCE(trader_likes,0) + 1 WHERE user_id = ?", (user_id,))
    except Exception:
        pass


def increment_trader_dislikes(user_id: int) -> None:
    try:
        _execute("UPDATE users SET trader_dislikes = COALESCE(trader_dislikes,0) + 1 WHERE user_id = ?", (user_id,))
    except Exception:
        pass


# =========================================================
# Text builders
# =========================================================
def build_main_text(profile: Dict[str, Any]) -> str:
    name = display_name(profile)
    username = safe_text(profile.get("username"))
    username_line = f"@{username}" if username else "لا يوجد"
    region = safe_text(profile.get("region")) or "غير محددة"
    field = safe_text(profile.get("field")) or "غير محدد"
    contact = safe_text(profile.get("contact_address")) or "غير محدد"
    bio = safe_text(profile.get("profile_bio")) or "لا توجد نبذة"
    services = safe_text(profile.get("profile_services")) or "لا توجد خدمات"
    photo_state = "✅ محفوظة" if safe_text(profile.get("profile_photo_id")) else "⏳ غير محفوظة"

    return (
        "👤 <b>حسابي</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الاسم: <b>{esc(name)}</b>\n"
        f"المعرف: <code>{safe_int(profile.get('user_id'))}</code>\n"
        f"المستخدم: <b>{esc(username_line)}</b>\n"
        f"المنطقة: <b>{esc(region)}</b>\n"
        f"المجال: <b>{esc(field)}</b>\n"
        f"عنوان التواصل: <b>{esc(contact)}</b>\n"
        f"النبذة: <b>{esc(bio)}</b>\n"
        f"الخدمات: <b>{esc(services)}</b>\n"
        f"الصورة/الخلفية: <b>{esc(photo_state)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 الرصيد: <b>{safe_int(profile.get('balance', 0))}</b>\n"
        f"⭐ النقاط: <b>{safe_int(profile.get('points', 0))}</b>\n"
        f"🌟 النجوم: <b>{safe_int(profile.get('stars', 0))}</b>\n"
        f"🔗 الإحالات: <b>{safe_int(profile.get('referrals_count', 0))}</b>\n"
        f"💎 حالة البريميوم: <b>{esc(premium_status_label(profile))}</b>\n"
        f"⏳ انتهاء البريميوم: <b>{esc(premium_until_text(profile))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🧾 الإعلانات المضافة: <b>{safe_int(profile.get('total_ads_posted', 0))}</b>\n"
        f"📢 الإعلانات المنشورة: <b>{safe_int(profile.get('total_ads_published', 0))}</b>\n"
        f"🔎 الإعلانات التي وجدتها: <b>{safe_int(profile.get('total_ads_found', 0))}</b>\n"
        f"🔍 عدد عمليات البحث: <b>{safe_int(profile.get('total_searches', 0))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 تمت الإحالة بواسطة: <code>{esc(profile.get('referred_by')) if profile.get('referred_by') else 'لا يوجد'}</code>\n"
        f"🗓 تاريخ الانضمام: <b>{esc(safe_text(profile.get('joined_at')) or 'غير متوفر')}</b>\n"
        f"🕒 آخر نشاط: <b>{esc(safe_text(profile.get('last_active')) or 'غير متوفر')}</b>\n"
    )


def build_info_text(profile: Dict[str, Any]) -> str:
    name = display_name(profile)
    username = safe_text(profile.get("username"))
    username_line = f"@{username}" if username else "لا يوجد"
    region = safe_text(profile.get("region")) or "غير محددة"
    field = safe_text(profile.get("field")) or "غير محدد"
    contact = safe_text(profile.get("contact_address")) or "غير محدد"
    bio = safe_text(profile.get("profile_bio")) or "لا توجد نبذة"
    services = safe_text(profile.get("profile_services")) or "لا توجد خدمات"

    return (
        "🧾 <b>المعلومات الشخصية</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الاسم: <b>{esc(name)}</b>\n"
        f"المستخدم: <b>{esc(username_line)}</b>\n"
        f"المنطقة: <b>{esc(region)}</b>\n"
        f"المجال: <b>{esc(field)}</b>\n"
        f"عنوان التواصل: <b>{esc(contact)}</b>\n"
        f"النبذة: <b>{esc(bio)}</b>\n"
        f"الخدمات: <b>{esc(services)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "يمكن تعديل هذه البيانات من زر التعديل."
    )


def build_wallet_text(profile: Dict[str, Any]) -> str:
    return (
        "💰 <b>رصيدي</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الرصيد: <b>{safe_int(profile.get('balance', 0))}</b>\n"
        f"النقاط: <b>{safe_int(profile.get('points', 0))}</b>\n"
        f"النجوم: <b>{safe_int(profile.get('stars', 0))}</b>\n"
        f"الإحالات: <b>{safe_int(profile.get('referrals_count', 0))}</b>\n"
        f"البريميوم: <b>{esc(premium_status_label(profile))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "البيانات المالية مرتبطة مباشرة بقاعدة البيانات."
    )


def build_stats_text(profile: Dict[str, Any]) -> str:
    return (
        "📊 <b>الإحصائيات</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🧾 عدد إعلاناته: <b>{safe_int(profile.get('total_ads_posted', 0))}</b>\n"
        f"👀 الإعلانات التي شاهدها: <b>{safe_int(profile.get('ads_seen_count', 0))}</b>\n"
        f"📢 الإعلانات المنشورة: <b>{safe_int(profile.get('total_ads_published', 0))}</b>\n"
        f"🔎 الإعلانات التي وجدها: <b>{safe_int(profile.get('total_ads_found', 0))}</b>\n"
        f"👤 مشاهدات الملف: <b>{safe_int(profile.get('profile_views', 0))}</b>\n"
        f"👍 إعجابات الملف: <b>{safe_int(profile.get('profile_likes', 0))}</b>\n"
        f"🏷 مشاهدات التاجر: <b>{safe_int(profile.get('trader_views', 0))}</b>\n"
        f"👍 إعجابات التاجر: <b>{safe_int(profile.get('trader_likes', 0))}</b>\n"
        f"👎 تقييمات التاجر: <b>{safe_int(profile.get('trader_dislikes', 0))}</b>\n"
        f"🔍 عدد عمليات البحث: <b>{safe_int(profile.get('total_searches', 0))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "تُحدَّث هذه الأرقام مباشرة من القاعدة."
    )


def build_request_hint_text(profile: Dict[str, Any]) -> str:
    return (
        "📢 <b>نشر الملف كتاجر</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "هذه الميزة متاحة لمن لديه:\n"
        "• حساب بريميوم\n"
        "• أو 10 إحالات أو أكثر\n"
        "• أو قناة كبيرة أكثر من 10,000 مشترك\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"وضعك الحالي: <b>{esc(premium_status_label(profile))}</b>\n"
        f"الإحالات: <b>{safe_int(profile.get('referrals_count', 0))}</b>\n"
        f"مشتركو القناة: <b>{safe_int(profile.get('channel_members', 0))}</b>\n"
    )


def build_public_trader_caption(profile: Dict[str, Any]) -> str:
    name = display_name(profile)
    username = safe_text(profile.get("username"))
    username_line = f"@{username}" if username else "لا يوجد"
    region = safe_text(profile.get("region")) or "غير محددة"
    field = safe_text(profile.get("field")) or "غير محدد"
    contact = safe_text(profile.get("contact_address")) or "غير محدد"
    bio = safe_text(profile.get("profile_bio")) or "لا توجد نبذة"
    services = safe_text(profile.get("profile_services")) or "لا توجد خدمات"
    if len(bio) > 50:
        bio = bio[:50].rstrip() + "..."

    views = safe_int(profile.get("trader_views", 0)) * 5
    likes = safe_int(profile.get("trader_likes", 0)) * 3
    dislikes = safe_int(profile.get("trader_dislikes", 0)) * 3

    return (
        "🏷 <b>ملف تاجر موثق</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 الاسم: <b>{esc(name)}</b>\n"
        f"📂 المجال: <b>{esc(field)}</b>\n"
        f"📍 الموقع: <b>{esc(region)}</b>\n"
        f"📞 التواصل: <b>{esc(contact)}</b>\n"
        f"📝 النبذة: <b>{esc(bio)}</b>\n"
        f"🛎 الخدمات: <b>{esc(services)}</b>\n"
        f"👤 المستخدم: <b>{esc(username_line)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👀 المشاهدات: <b>{views}</b> <i>(×5)</i>\n"
        f"👍 الإعجابات: <b>{likes}</b> <i>(×3)</i>\n"
        f"👎 التقييمات: <b>{dislikes}</b> <i>(×3)</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "يمكن مراسلة التاجر من الزر أدناه."
    )[:1024]


def format_account_message(row: Dict[str, Any], mode: str) -> str:
    sender_id = safe_int(row.get("sender_id"))
    receiver_id = safe_int(row.get("receiver_id"))
    text = safe_text(row.get("message_text"), 1500)
    reply = safe_text(row.get("reply_text"), 1500)
    created_at = safe_text(row.get("created_at"))
    status = safe_text(row.get("status"))

    if mode == "in":
        sender = display_name(get_user_profile(sender_id))
        return (
            "📩 <b>رسالة واردة</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 من: <b>{esc(sender)}</b>\n"
            f"🕒 {esc(created_at)}\n"
            f"📌 الحالة: <b>{esc(status)}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"💬 {esc(text)}"
            + (f"\n\n✅ الرد: {esc(reply)}" if reply else "")
        )

    receiver = display_name(get_user_profile(receiver_id))
    return (
        "📤 <b>رسالة مرسلة</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 إلى: <b>{esc(receiver)}</b>\n"
        f"🕒 {esc(created_at)}\n"
        f"📌 الحالة: <b>{esc(status)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💬 {esc(text)}"
        + (f"\n\n✅ الرد: {esc(reply)}" if reply else "\n\n⏳ لم يصل رد بعد.")
    )


# =========================================================
# Keyboards
# =========================================================
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧾 المعلومات الشخصية", callback_data="account:info"),
            InlineKeyboardButton("✏️ تعديل البيانات", callback_data="account:edit"),
        ],
        [
            InlineKeyboardButton("💰 رصيدي", callback_data="account:wallet"),
            InlineKeyboardButton("📊 الإحصائيات", callback_data="account:stats"),
        ],
        [
            InlineKeyboardButton("📢 نشر ملفي كتاجر", callback_data="account:publish_trader"),
            InlineKeyboardButton("📬 رسائلي", callback_data="account:messages"),
        ],
        [
            InlineKeyboardButton("🔄 تحديث", callback_data="account:refresh"),
            InlineKeyboardButton("↩️ رجوع", callback_data="account:back"),
        ],
    ])


def edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ الاسم", callback_data="account:set_first_name"),
            InlineKeyboardButton("✏️ اللقب", callback_data="account:set_last_name"),
        ],
        [
            InlineKeyboardButton("📞 عنوان التواصل", callback_data="account:set_contact"),
            InlineKeyboardButton("🌍 الموقع", callback_data="account:set_region"),
        ],
        [
            InlineKeyboardButton("📂 مجال النشاط", callback_data="account:set_field"),
            InlineKeyboardButton("📝 النبذة", callback_data="account:set_bio"),
        ],
        [
            InlineKeyboardButton("🛎 الخدمات", callback_data="account:set_services"),
            InlineKeyboardButton("🖼 الصورة/الخلفية", callback_data="account:set_photo"),
        ],
        [
            InlineKeyboardButton("📈 عدد مشتركين القناة", callback_data="account:set_members"),
            InlineKeyboardButton("↩️ رجوع", callback_data="account:main"),
        ],
    ])


def awaiting_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="account:main")]])


def publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📢 نشر الملف كتاجر", callback_data="account:publish_trader"),
            InlineKeyboardButton("📩 تقديم طلب حساب تجاري", callback_data="account:request_trader"),
        ],
        [InlineKeyboardButton("↩️ رجوع", callback_data="account:main")],
    ])


def trader_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 إعجاب", callback_data=f"account:trader_like:{owner_id}"),
            InlineKeyboardButton("👎", callback_data=f"account:trader_dislike:{owner_id}"),
        ],
        [
            InlineKeyboardButton("💬 مراسلة التاجر", callback_data=f"account:message:{owner_id}"),
            InlineKeyboardButton("🛎 طلب خدمة", callback_data=f"account:service:{owner_id}"),
        ],
        [InlineKeyboardButton("👤 عرض الملف", callback_data=f"account:open:{owner_id}")],
    ])


def message_keyboard(message_id: int, owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 رد", callback_data=f"account:reply:{message_id}"),
            InlineKeyboardButton("🚫 تجاهل", callback_data=f"account:ignore:{message_id}"),
        ],
        [InlineKeyboardButton("👤 عرض الملف", callback_data=f"account:open:{owner_id}")],
    ])


# =========================================================
# Screen sender
# =========================================================
async def send_screen(update: Update, text: str, keyboard: InlineKeyboardMarkup):
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass

    await update.effective_message.reply_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# =========================================================
# Publish
# =========================================================
async def publish_profile_to_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    profile = get_user_profile(user_id)
    caption = build_main_text(profile)
    photo_id = safe_text(profile.get("profile_photo_id"))
    existing_msg_id = safe_int(profile.get("profile_channel_message_id"), 0)

    try:
        sent = None
        if existing_msg_id:
            try:
                if photo_id:
                    sent = await context.bot.edit_message_caption(
                        chat_id=PROFILE_CHANNEL_ID,
                        message_id=existing_msg_id,
                        caption=caption,
                        parse_mode="HTML",
                    )
                else:
                    sent = await context.bot.edit_message_text(
                        chat_id=PROFILE_CHANNEL_ID,
                        message_id=existing_msg_id,
                        text=caption,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
            except Exception:
                sent = None

        if sent is None:
            if photo_id:
                sent = await context.bot.send_photo(
                    chat_id=PROFILE_CHANNEL_ID,
                    photo=photo_id,
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                sent = await context.bot.send_message(
                    chat_id=PROFILE_CHANNEL_ID,
                    text=caption,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

        msg_id = getattr(sent, "message_id", existing_msg_id or None)
        _execute(
            """
            UPDATE users
            SET profile_channel_chat_id = ?,
                profile_channel_message_id = ?,
                profile_updated_at = CURRENT_TIMESTAMP,
                last_active = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (PROFILE_CHANNEL_ID, msg_id, user_id),
        )

        _insert(
            """
            INSERT INTO account_messages (sender_id, receiver_id, message_text, status, created_at)
            VALUES (?, ?, ?, 'system', CURRENT_TIMESTAMP)
            """,
            (user_id, PROFILE_CHANNEL_ID, "profile published"),
        )

        if hasattr(db, "record_event"):
            try:
                db.record_event(f"user:{user_id}", "profile_published", details=f"channel={PROFILE_CHANNEL_ID}, message_id={msg_id}")
            except Exception:
                pass

        return True
    except Exception as e:
        logger.exception(f"publish_profile_to_channel failed: {e}")
        if hasattr(db, "record_error"):
            try:
                db.record_error("publish_profile_to_channel", e, f"user:{user_id}", "publish profile failed")
            except Exception:
                pass
        return False


async def publish_trader_to_channel(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    profile = get_user_profile(user_id)
    caption = build_public_trader_caption(profile)
    photo_id = safe_text(profile.get("trader_background_photo_id")) or safe_text(profile.get("profile_photo_id"))
    existing_msg_id = safe_int(profile.get("trader_channel_message_id"), 0)

    try:
        sent = None
        if existing_msg_id:
            try:
                if photo_id:
                    sent = await context.bot.edit_message_caption(
                        chat_id=PROFILE_CHANNEL_ID,
                        message_id=existing_msg_id,
                        caption=caption,
                        reply_markup=trader_keyboard(user_id),
                        parse_mode="HTML",
                    )
                else:
                    sent = await context.bot.edit_message_text(
                        chat_id=PROFILE_CHANNEL_ID,
                        message_id=existing_msg_id,
                        text=caption,
                        reply_markup=trader_keyboard(user_id),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
            except Exception:
                sent = None

        if sent is None:
            if photo_id:
                sent = await context.bot.send_photo(
                    chat_id=PROFILE_CHANNEL_ID,
                    photo=photo_id,
                    caption=caption,
                    reply_markup=trader_keyboard(user_id),
                    parse_mode="HTML",
                )
            else:
                sent = await context.bot.send_message(
                    chat_id=PROFILE_CHANNEL_ID,
                    text=caption,
                    reply_markup=trader_keyboard(user_id),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

        msg_id = getattr(sent, "message_id", existing_msg_id or None)
        _execute(
            """
            UPDATE users
            SET trader_is_published = 1,
                trader_channel_chat_id = ?,
                trader_channel_message_id = ?,
                trader_published_at = CURRENT_TIMESTAMP,
                last_active = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (PROFILE_CHANNEL_ID, msg_id, user_id),
        )

        if hasattr(db, "record_event"):
            try:
                db.record_event(f"user:{user_id}", "trader_published", details=f"channel={PROFILE_CHANNEL_ID}, message_id={msg_id}")
            except Exception:
                pass

        return True
    except Exception as e:
        logger.exception(f"publish_trader_to_channel failed: {e}")
        if hasattr(db, "record_error"):
            try:
                db.record_error("publish_trader_to_channel", e, f"user:{user_id}", "publish trader failed")
            except Exception:
                pass
        return False


# =========================================================
# Screens
# =========================================================
async def show_account(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    if _already_handled(context, update):
        return

    if not update.effective_user:
        return

    user = update.effective_user

    if hasattr(db, "add_user"):
        try:
            await db_async(db.add_user, user.id, user.first_name, user.username)
        except Exception as e:
            logger.warning(f"add_user failed: {e}")

    increment_profile_views(user.id)
    profile = get_user_profile(user.id)
    await send_screen(update, build_main_text(profile), main_keyboard())


async def show_info(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    profile = get_user_profile(update.effective_user.id)
    await send_screen(update, build_info_text(profile), main_keyboard())


async def show_wallet(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    profile = get_user_profile(update.effective_user.id)
    await send_screen(update, build_wallet_text(profile), main_keyboard())


async def show_stats(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    profile = get_user_profile(update.effective_user.id)
    await send_screen(update, build_stats_text(profile), main_keyboard())


async def show_edit_menu(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    await send_screen(
        update,
        "✏️ <b>تعديل البيانات</b>\n━━━━━━━━━━━━━━━━━━━━\nاختر الحقل الذي تريد تعديله.",
        edit_keyboard(),
    )


async def show_publish_hint(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    profile = get_user_profile(update.effective_user.id)
    await send_screen(update, build_request_hint_text(profile), publish_keyboard())


async def show_messages_panel(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    user_id = update.effective_user.id
    sent_row = _fetchone("SELECT COUNT(*) AS c FROM account_messages WHERE sender_id = ? AND status != 'system'", (user_id,))
    recv_row = _fetchone("SELECT COUNT(*) AS c FROM account_messages WHERE receiver_id = ?", (user_id,))
    sent = safe_int(sent_row["c"] if sent_row else 0)
    recv = safe_int(recv_row["c"] if recv_row else 0)

    text = (
        "📬 <b>رسائلي</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الواردة: <b>{recv}</b>\n"
        f"المرسلة: <b>{sent}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "هذا القسم مخصص لتتبع التواصل داخل الملف."
    )
    await send_screen(update, text, main_keyboard())


async def show_public_trader_profile_by_id(update: Update, owner_id: int):
    increment_trader_views(owner_id)
    profile = get_user_profile(owner_id)
    caption = build_public_trader_caption(profile)
    if update.callback_query:
        await update.callback_query.message.reply_text(caption, reply_markup=trader_keyboard(owner_id), parse_mode="HTML")
    else:
        await update.effective_message.reply_text(caption, reply_markup=trader_keyboard(owner_id), parse_mode="HTML")


# =========================================================
# Editing / Messaging
# =========================================================
async def begin_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, prompt: str, expect_photo: bool = False, limit: int = 1000):
    state = get_state(context)
    state["awaiting"] = {
        "key": key,
        "expect_photo": expect_photo,
        "limit": limit,
    }

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(prompt, reply_markup=awaiting_keyboard(), parse_mode="HTML")
    else:
        await update.effective_message.reply_text(prompt, reply_markup=awaiting_keyboard(), parse_mode="HTML")


async def save_awaiting_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = get_state(context)
    awaiting = state.get("awaiting") or {}
    if not awaiting or awaiting.get("expect_photo"):
        return False

    if not update.message:
        return False

    key = safe_text(awaiting.get("key"))
    value = safe_text(update.message.text, safe_int(awaiting.get("limit", 1000), 1000))
    if not value:
        await update.message.reply_text("أرسل قيمة صحيحة أولاً.", reply_markup=awaiting_keyboard())
        return True

    if key == "profile_bio":
        value = value[:160]
    if key == "profile_services":
        value = value[:500]

    user_id = update.effective_user.id

    if key == "request_trader":
        _insert(
            """
            INSERT INTO account_messages (sender_id, receiver_id, message_text, status, created_at)
            VALUES (?, ?, ?, 'request', CURRENT_TIMESTAMP)
            """,
            (user_id, getattr(Config, "ADMIN_ID", 0), value),
        )
        _execute(
            "UPDATE users SET trader_request_count = COALESCE(trader_request_count,0) + 1 WHERE user_id = ?",
            (user_id,),
        )
        state["awaiting"] = None
        await update.message.reply_text("✅ تم حفظ طلب الحساب التجاري.", reply_markup=main_keyboard())
        try:
            if getattr(Config, "ADMIN_ID", 0):
                await context.bot.send_message(
                    chat_id=Config.ADMIN_ID,
                    text=(
                        "📩 <b>طلب حساب تجاري جديد</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        f"المستخدم: <code>{user_id}</code>\n"
                        f"الطلب: {esc(value)}"
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            pass
        return True

    if not set_user_profile_value(user_id, key, value):
        await update.message.reply_text("تعذر حفظ التعديل.", reply_markup=main_keyboard())
        state["awaiting"] = None
        return True

    state["awaiting"] = None
    await update.message.reply_text("✅ تم الحفظ.", reply_markup=main_keyboard())

    try:
        if hasattr(db, "record_event"):
            db.record_event(f"user:{user_id}", "profile_updated", details=f"{key} updated")
    except Exception:
        pass

    try:
        await publish_profile_to_channel(context, user_id)
    except Exception:
        pass

    return True


async def save_awaiting_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = get_state(context)
    awaiting = state.get("awaiting") or {}
    if not awaiting or not awaiting.get("expect_photo"):
        return False

    if not update.message:
        return False

    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id

    if not photo_id:
        await update.message.reply_text("أرسل صورة صحيحة أولاً.", reply_markup=awaiting_keyboard())
        return True

    user_id = update.effective_user.id
    key = safe_text(awaiting.get("key"))

    if key in {"profile_photo_id", "trader_background_photo_id"}:
        ok = set_user_profile_value(user_id, key, photo_id)
    else:
        ok = set_user_profile_value(user_id, "profile_photo_id", photo_id)

    if not ok:
        await update.message.reply_text("تعذر حفظ الصورة.", reply_markup=main_keyboard())
        state["awaiting"] = None
        return True

    state["awaiting"] = None
    await update.message.reply_text("✅ تم حفظ الصورة.", reply_markup=main_keyboard())

    try:
        if hasattr(db, "record_event"):
            db.record_event(f"user:{user_id}", "profile_photo_updated", details=f"{key} updated")
    except Exception:
        pass

    try:
        await publish_profile_to_channel(context, user_id)
    except Exception:
        pass

    return True


async def begin_message(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_id: int):
    state = get_state(context)
    state["awaiting"] = {
        "key": "message",
        "expect_photo": False,
        "target_user_id": owner_id,
    }
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text("✉️ أرسل رسالتك الآن.", reply_markup=awaiting_keyboard())
    else:
        await update.effective_message.reply_text("✉️ أرسل رسالتك الآن.", reply_markup=awaiting_keyboard())


async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = get_state(context)
    awaiting = state.get("awaiting") or {}
    if safe_text(awaiting.get("key")) != "message":
        return False

    if not update.message:
        return False

    text = safe_text(update.message.text, 2000)
    if not text:
        await update.message.reply_text("أرسل رسالة نصية واضحة.")
        return True

    sender_id = update.effective_user.id
    receiver_id = safe_int(awaiting.get("target_user_id"), 0)

    msg_id = _insert(
        """
        INSERT INTO account_messages (sender_id, receiver_id, message_text, status, created_at)
        VALUES (?, ?, ?, 'sent', CURRENT_TIMESTAMP)
        """,
        (sender_id, receiver_id, text),
    )

    try:
        sent = await context.bot.send_message(
            chat_id=receiver_id,
            text=(
                "📩 <b>رسالة جديدة على ملفك</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"من: <code>{sender_id}</code>\n"
                f"الرسالة: {esc(text)}"
            ),
            reply_markup=message_keyboard(msg_id, sender_id),
            parse_mode="HTML",
        )
        _execute("UPDATE account_messages SET bot_message_id = ? WHERE id = ?", (sent.message_id, msg_id))
        await update.message.reply_text("✅ تم إرسال الرسالة.", reply_markup=main_keyboard())
    except Exception as e:
        logger.exception(f"send message failed: {e}")
        await update.message.reply_text("تم حفظ الرسالة لكن لم يتم إرسالها الآن.", reply_markup=main_keyboard())

    state["awaiting"] = None
    return True


async def begin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, message_id: int):
    row = _fetchone("SELECT * FROM account_messages WHERE id = ?", (message_id,))
    if not row:
        return await update.callback_query.answer("الرسالة غير موجودة", show_alert=True)

    if safe_int(row["receiver_id"]) != update.callback_query.from_user.id:
        return await update.callback_query.answer("هذا الزر لصاحب الملف فقط", show_alert=True)

    state = get_state(context)
    state["awaiting"] = {
        "key": "reply",
        "message_id": message_id,
        "expect_photo": False,
        "target_user_id": safe_int(row["sender_id"]),
    }
    await update.callback_query.answer()
    await update.callback_query.message.reply_text("✍️ أرسل الآن الرد.", reply_markup=awaiting_keyboard())


async def save_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = get_state(context)
    awaiting = state.get("awaiting") or {}
    if safe_text(awaiting.get("key")) != "reply":
        return False

    if not update.message:
        return False

    reply_text = safe_text(update.message.text, 2000)
    if not reply_text:
        await update.message.reply_text("أرسل ردًا نصيًا.")
        return True

    message_id = safe_int(awaiting.get("message_id"), 0)
    target_user_id = safe_int(awaiting.get("target_user_id"), 0)

    _execute(
        """
        UPDATE account_messages
        SET status = 'replied',
            reply_text = ?,
            replied_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (reply_text, message_id),
    )

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "📩 <b>رد على رسالتك</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"{esc(reply_text)}"
            ),
            parse_mode="HTML",
        )
        await update.message.reply_text("✅ تم إرسال الرد.", reply_markup=main_keyboard())
    except Exception:
        await update.message.reply_text("✅ تم حفظ الرد.", reply_markup=main_keyboard())

    state["awaiting"] = None
    return True


# =========================================================
# Callbacks
# =========================================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user
    state = get_state(context)

    try:
        if data == "account:main":
            state["awaiting"] = None
            await query.answer()
            return await show_account(update, context)

        if data == "account:refresh":
            await query.answer("تم التحديث")
            return await show_account(update, context)

        if data == "account:back":
            state["awaiting"] = None
            await query.answer()
            return await show_account(update, context)

        if data == "account:info":
            await query.answer()
            return await show_info(update, context)

        if data == "account:wallet":
            await query.answer()
            return await show_wallet(update, context)

        if data == "account:stats":
            await query.answer()
            return await show_stats(update, context)

        if data == "account:edit":
            await query.answer()
            return await show_edit_menu(update, context)

        if data == "account:publish_trader":
            await query.answer("جارٍ التحقق...")
            profile = get_user_profile(user.id)
            if not can_publish_trader(profile):
                return await query.message.reply_text(
                    "❌ ليس لديك حساب مميز أو عدد إحالات كافٍ.\n"
                    "هذه الميزة متاحة لمن لديه بريميوم أو 10 إحالات أو قناة كبيرة.",
                    reply_markup=publish_keyboard(),
                    parse_mode="HTML",
                )
            ok = await publish_trader_to_channel(context, user.id)
            if ok:
                return await query.message.reply_text("✅ تم نشر ملفك كتاجر للجميع.", reply_markup=main_keyboard())
            return await query.message.reply_text("❌ تعذر نشر الملف التجاري الآن.", reply_markup=main_keyboard())

        if data == "account:request_trader":
            await query.answer()
            profile = get_user_profile(user.id)
            return await send_screen(update, build_request_hint_text(profile), publish_keyboard())

        if data == "account:messages":
            await query.answer()
            return await show_messages_panel(update, context)

        if data == "account:like_me":
            increment_profile_likes(user.id)
            await query.answer("تم تسجيل الإعجاب")
            return await show_account(update, context)

        if data == "account:set_first_name":
            await query.answer()
            return await begin_edit(update, context, "first_name", "✏️ أرسل <b>الاسم</b> الآن:")
        if data == "account:set_last_name":
            await query.answer()
            return await begin_edit(update, context, "last_name", "✏️ أرسل <b>اللقب</b> الآن:")
        if data == "account:set_contact":
            await query.answer()
            return await begin_edit(update, context, "contact_address", "📞 أرسل <b>عنوان التواصل</b> الآن:")
        if data == "account:set_region":
            await query.answer()
            return await begin_edit(update, context, "region", "🌍 أرسل <b>الموقع</b> الآن:")
        if data == "account:set_field":
            await query.answer()
            return await begin_edit(update, context, "field", "📂 أرسل <b>مجال النشاط</b> الآن:")
        if data == "account:set_bio":
            await query.answer()
            return await begin_edit(update, context, "profile_bio", "📝 أرسل <b>النبذة</b> الآن:")
        if data == "account:set_services":
            await query.answer()
            return await begin_edit(update, context, "profile_services", "🛎 أرسل <b>الخدمات</b> التي تقدمها الآن:")
        if data == "account:set_photo":
            state["awaiting"] = {"key": "profile_photo_id", "expect_photo": True}
            await query.answer()
            return await query.message.reply_text("🖼 أرسل <b>الصورة/الخلفية</b> الآن.", reply_markup=awaiting_keyboard(), parse_mode="HTML")
        if data == "account:set_members":
            await query.answer()
            return await begin_edit(update, context, "channel_members", "📈 أرسل <b>عدد مشتركين القناة</b> الآن:")

        if data.startswith("account:message:"):
            owner_id = safe_int(data.split(":")[-1], 0)
            await query.answer()
            return await begin_message(update, context, owner_id)

        if data.startswith("account:service:"):
            owner_id = safe_int(data.split(":")[-1], 0)
            await query.answer()
            return await begin_message(update, context, owner_id)

        if data.startswith("account:open:"):
            owner_id = safe_int(data.split(":")[-1], 0)
            await query.answer()
            return await show_public_trader_profile_by_id(update, owner_id)

        if data.startswith("account:trader_like:"):
            owner_id = safe_int(data.split(":")[-1], 0)
            increment_trader_likes(owner_id)
            await query.answer("تم الإعجاب")
            return await show_public_trader_profile_by_id(update, owner_id)

        if data.startswith("account:trader_dislike:"):
            owner_id = safe_int(data.split(":")[-1], 0)
            increment_trader_dislikes(owner_id)
            await query.answer("تم التقييم")
            return await show_public_trader_profile_by_id(update, owner_id)

        if data.startswith("account:reply:"):
            message_id = safe_int(data.split(":")[-1], 0)
            return await begin_reply(update, context, message_id)

        if data.startswith("account:ignore:"):
            message_id = safe_int(data.split(":")[-1], 0)
            row = _fetchone("SELECT * FROM account_messages WHERE id = ?", (message_id,))
            if not row:
                return await query.answer("الرسالة غير موجودة", show_alert=True)
            if safe_int(row["receiver_id"]) != user.id:
                return await query.answer("هذا الزر لصاحب الملف فقط", show_alert=True)

            _execute("UPDATE account_messages SET status = 'ignored' WHERE id = ?", (message_id,))
            await query.answer("تم التجاهل")
            return await query.message.reply_text("تم تجاهل الرسالة.", reply_markup=main_keyboard())

    except Exception as e:
        logger.exception(f"account callback error: {e}")
        try:
            if hasattr(db, "record_error"):
                db.record_error("account_callbacks", e, f"user:{user.id}", "callback failed")
        except Exception:
            pass
        await query.answer("حدث خطأ.", show_alert=True)


# =========================================================
# Text / photo
# =========================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = safe_text(update.message.text, 10000)
    state = get_state(context)
    awaiting = state.get("awaiting") or {}
    key = safe_text(awaiting.get("key"))

    if text == MAIN_BUTTON:
        return await show_account(update, context)

    if key == "message":
        return await save_message(update, context)

    if key == "reply":
        return await save_reply(update, context)

    if awaiting:
        return await save_awaiting_text(update, context)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    state = get_state(context)
    awaiting = state.get("awaiting") or {}
    if awaiting and awaiting.get("expect_photo"):
        return await save_awaiting_photo(update, context)


# =========================================================
# Entry points
# =========================================================
async def search_handler(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    return await show_account(update, context)


async def show_main(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    return await show_account(update, context)


# =========================================================
# Setup
# =========================================================
async def setup(application):
    ensure_schema()
    application.add_handler(CallbackQueryHandler(handle_callbacks, pattern=r"^account:"))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(rf"^{re.escape(MAIN_BUTTON)}$"), handle_text), group=30)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text), group=31)
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo), group=31)