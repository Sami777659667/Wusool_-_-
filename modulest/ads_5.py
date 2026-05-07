
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

DB_PATH = Path(__file__).with_name("bot.db")


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = str(path)
        self._lock = threading.RLock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS users (
                    user_id     INTEGER PRIMARY KEY,
                    first_name   TEXT,
                    username     TEXT,
                    created_at   INTEGER
                );

                CREATE TABLE IF NOT EXISTS wallets (
                    user_id     INTEGER PRIMARY KEY,
                    stars       INTEGER NOT NULL DEFAULT 0,
                    updated_at  INTEGER
                );

                CREATE TABLE IF NOT EXISTS settings (
                    k TEXT PRIMARY KEY,
                    v TEXT,
                    updated_at INTEGER
                );

                CREATE TABLE IF NOT EXISTS targets (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id        INTEGER NOT NULL,
                    raw_target      TEXT NOT NULL,
                    chat_id         INTEGER,
                    username        TEXT,
                    title           TEXT,
                    category        TEXT,
                    join_url        TEXT,
                    member_count    INTEGER DEFAULT 0,
                    views_count     INTEGER DEFAULT 0,
                    clicks_count    INTEGER DEFAULT 0,
                    joins_count     INTEGER DEFAULT 0,
                    ads_count       INTEGER DEFAULT 0,
                    is_verified     INTEGER DEFAULT 0,
                    bot_is_admin    INTEGER DEFAULT 0,
                    can_post        INTEGER DEFAULT 0,
                    can_delete      INTEGER DEFAULT 0,
                    is_suspended    INTEGER DEFAULT 0,
                    created_at      INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_targets_owner ON targets(owner_id);
                CREATE INDEX IF NOT EXISTS idx_targets_category ON targets(category);
                CREATE INDEX IF NOT EXISTS idx_targets_verified ON targets(is_verified);

                CREATE TABLE IF NOT EXISTS ads (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id         INTEGER NOT NULL,
                    title            TEXT NOT NULL,
                    description      TEXT NOT NULL,
                    image_file_id    TEXT,
                    scope_type       TEXT NOT NULL,
                    target_ids_json  TEXT NOT NULL,
                    estimated_price  REAL DEFAULT 0,
                    estimated_stars  INTEGER DEFAULT 0,
                    payment_mode     TEXT DEFAULT 'balance',
                    status           TEXT DEFAULT 'draft',
                    bot_username     TEXT,
                    post_refs_json   TEXT DEFAULT '[]',
                    created_at       INTEGER,
                    published_at     INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_ads_owner ON ads(owner_id);
                CREATE INDEX IF NOT EXISTS idx_ads_status ON ads(status);

                CREATE TABLE IF NOT EXISTS ad_targets (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id           INTEGER NOT NULL,
                    target_id       INTEGER NOT NULL,
                    chat_id         INTEGER,
                    message_id      INTEGER,
                    views_cached    INTEGER DEFAULT 0,
                    clicks_cached   INTEGER DEFAULT 0,
                    joins_cached    INTEGER DEFAULT 0,
                    shares_cached   INTEGER DEFAULT 0,
                    created_at      INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_ad_targets_ad ON ad_targets(ad_id);
                CREATE INDEX IF NOT EXISTS idx_ad_targets_target ON ad_targets(target_id);

                CREATE TABLE IF NOT EXISTS ad_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id       INTEGER NOT NULL,
                    event_type  TEXT NOT NULL,
                    user_id     INTEGER,
                    target_id   INTEGER,
                    extra_json  TEXT,
                    created_at  INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_events_ad ON ad_events(ad_id);
                CREATE INDEX IF NOT EXISTS idx_events_type ON ad_events(event_type);

                CREATE TABLE IF NOT EXISTS payment_requests (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    ad_id        INTEGER,
                    amount_stars INTEGER NOT NULL DEFAULT 0,
                    note         TEXT,
                    status       TEXT DEFAULT 'pending',
                    created_at   INTEGER,
                    decided_at   INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_payment_requests_status ON payment_requests(status);
                """
            )
            conn.commit()

    def set_setting(self, key: str, value: str):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO settings (k, v, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(k) DO UPDATE SET
                    v=excluded.v,
                    updated_at=excluded.updated_at
                """,
                (key, value, int(time.time())),
            )
            conn.commit()

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
            return row["v"] if row and row["v"] is not None else default

    def add_user(self, user_id: int, first_name: str, username: Optional[str]):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, first_name, username, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    first_name=excluded.first_name,
                    username=excluded.username
                """,
                (int(user_id), first_name, username, int(time.time())),
            )
            conn.execute(
                """
                INSERT INTO wallets (user_id, stars, updated_at)
                VALUES (?, 0, ?)
                ON CONFLICT(user_id) DO NOTHING
                """,
                (int(user_id), int(time.time())),
            )
            conn.commit()

    def get_balance(self, user_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT stars FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()
            return int(row["stars"]) if row else 0

    def credit_balance(self, user_id: int, amount: int):
        amount = int(amount)
        if amount <= 0:
            return
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO wallets (user_id, stars, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    stars = stars + excluded.stars,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), amount, int(time.time())),
            )
            conn.commit()

    def debit_balance(self, user_id: int, amount: int) -> bool:
        amount = int(amount)
        if amount <= 0:
            return True
        with self._lock, self._conn() as conn:
            row = conn.execute("SELECT stars FROM wallets WHERE user_id=?", (int(user_id),)).fetchone()
            current = int(row["stars"]) if row else 0
            if current < amount:
                return False
            conn.execute(
                "UPDATE wallets SET stars=?, updated_at=? WHERE user_id=?",
                (current - amount, int(time.time()), int(user_id)),
            )
            conn.commit()
            return True

    def save_target(self, owner_id: int, raw_target: str, chat_id: Optional[int], username: Optional[str],
                    title: Optional[str], category: str, join_url: Optional[str], member_count: int,
                    bot_is_admin: bool, can_post: bool, can_delete: bool, is_verified: bool = True,
                    is_suspended: bool = False) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO targets (
                    owner_id, raw_target, chat_id, username, title, category, join_url,
                    member_count, is_verified, bot_is_admin, can_post, can_delete, is_suspended, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(owner_id), raw_target, chat_id, username, title, category, join_url,
                    int(member_count or 0), 1 if is_verified else 0,
                    1 if bot_is_admin else 0, 1 if can_post else 0, 1 if can_delete else 0,
                    1 if is_suspended else 0, int(time.time())
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_target(self, target_id: int, **fields):
        allowed = {
            "raw_target", "chat_id", "username", "title", "category", "join_url",
            "member_count", "views_count", "clicks_count", "joins_count", "ads_count",
            "is_verified", "bot_is_admin", "can_post", "can_delete", "is_suspended",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            params.append(int(v) if isinstance(v, bool) else v)
        if not sets:
            return
        params.append(int(target_id))
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id=?", params)
            conn.commit()

    def delete_target(self, target_id: int):
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM targets WHERE id=?", (int(target_id),))
            conn.execute("DELETE FROM ad_targets WHERE target_id=?", (int(target_id),))
            conn.commit()

    def get_target(self, target_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM targets WHERE id=?", (int(target_id),)).fetchone()
            return dict(row) if row else None

    def list_targets(self, owner_id: Optional[int] = None, verified_only: Optional[bool] = None,
                     category: Optional[str] = None, include_suspended: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM targets WHERE 1=1"
        params: List[Any] = []
        if owner_id is not None:
            sql += " AND owner_id=?"
            params.append(int(owner_id))
        if verified_only is not None:
            sql += " AND is_verified=?"
            params.append(1 if verified_only else 0)
        if category:
            sql += " AND category=?"
            params.append(category)
        if not include_suspended:
            sql += " AND is_suspended=0"
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def distinct_categories(self, owner_id: Optional[int] = None, verified_only: Optional[bool] = None) -> List[str]:
        sql = "SELECT DISTINCT category FROM targets WHERE category IS NOT NULL AND category<>''"
        params: List[Any] = []
        if owner_id is not None:
            sql += " AND owner_id=?"
            params.append(int(owner_id))
        if verified_only is not None:
            sql += " AND is_verified=?"
            params.append(1 if verified_only else 0)
        sql += " ORDER BY category"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [r["category"] for r in rows if r["category"]]

    def save_ad(self, owner_id: int, title: str, description: str, image_file_id: Optional[str],
                scope_type: str, target_ids: Sequence[int], estimated_price: float,
                estimated_stars: int = 0, payment_mode: str = "balance",
                bot_username: Optional[str] = None, status: str = "draft") -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO ads (
                    owner_id, title, description, image_file_id, scope_type, target_ids_json,
                    estimated_price, estimated_stars, payment_mode, status, bot_username,
                    post_refs_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(owner_id), title, description, image_file_id, scope_type,
                    json.dumps(list(target_ids), ensure_ascii=False), float(estimated_price),
                    int(estimated_stars), payment_mode, status, bot_username,
                    json.dumps([], ensure_ascii=False), int(time.time())
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_ad(self, ad_id: int, **fields):
        allowed = {
            "title", "description", "image_file_id", "scope_type", "target_ids_json",
            "estimated_price", "estimated_stars", "payment_mode", "status",
            "bot_username", "post_refs_json", "published_at",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return
        params.append(int(ad_id))
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE ads SET {', '.join(sets)} WHERE id=?", params)
            conn.commit()

    def get_ad(self, ad_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM ads WHERE id=?", (int(ad_id),)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["target_ids"] = json.loads(d.get("target_ids_json") or "[]")
            d["post_refs"] = json.loads(d.get("post_refs_json") or "[]")
            return d

    def list_ads(self, owner_id: Optional[int] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ads WHERE 1=1"
        params: List[Any] = []
        if owner_id is not None:
            sql += " AND owner_id=?"
            params.append(int(owner_id))
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                d["target_ids"] = json.loads(d.get("target_ids_json") or "[]")
                d["post_refs"] = json.loads(d.get("post_refs_json") or "[]")
                out.append(d)
            return out

    def list_pending_ads(self) -> List[Dict[str, Any]]:
        return self.list_ads(status="pending_approval")

    def list_ad_targets(self, ad_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM ad_targets WHERE ad_id=? ORDER BY id DESC", (int(ad_id),)).fetchall()
            return [dict(r) for r in rows]

    def link_ad_target(self, ad_id: int, target_id: int, chat_id: Optional[int], message_id: Optional[int],
                       views_cached: int = 0, clicks_cached: int = 0, joins_cached: int = 0, shares_cached: int = 0):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO ad_targets (ad_id, target_id, chat_id, message_id, views_cached, clicks_cached, joins_cached, shares_cached, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(ad_id), int(target_id), chat_id, message_id, int(views_cached), int(clicks_cached), int(joins_cached), int(shares_cached), int(time.time())),
            )
            conn.commit()

    def update_ad_target(self, ad_target_id: int, **fields):
        allowed = {"message_id", "views_cached", "clicks_cached", "joins_cached", "shares_cached"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return
        params.append(int(ad_target_id))
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE ad_targets SET {', '.join(sets)} WHERE id=?", params)
            conn.commit()

    def add_ad_event(self, ad_id: int, event_type: str, user_id: Optional[int] = None,
                     target_id: Optional[int] = None, extra: Optional[Dict[str, Any]] = None):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO ad_events (ad_id, event_type, user_id, target_id, extra_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(ad_id), event_type, user_id, target_id, json.dumps(extra or {}, ensure_ascii=False), int(time.time())),
            )
            conn.commit()

    def get_ad_stats(self, ad_id: int) -> Dict[str, int]:
        with self._conn() as conn:
            def count(event_type: str) -> int:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM ad_events WHERE ad_id=? AND event_type=?",
                    (int(ad_id), event_type),
                ).fetchone()
                return int(row["c"] or 0)

            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(views_cached), 0) AS views_cached,
                    COALESCE(SUM(clicks_cached), 0) AS clicks_cached,
                    COALESCE(SUM(joins_cached), 0) AS joins_cached,
                    COALESCE(SUM(shares_cached), 0) AS shares_cached
                FROM ad_targets
                WHERE ad_id=?
                """,
                (int(ad_id),),
            ).fetchone()
            return {
                "posted": count("posted"),
                "deliver": count("deliver"),
                "click": count("click"),
                "share": count("share"),
                "join": count("join"),
                "refresh": count("refresh"),
                "views_cached": int(row["views_cached"] or 0),
                "clicks_cached": int(row["clicks_cached"] or 0),
                "joins_cached": int(row["joins_cached"] or 0),
                "shares_cached": int(row["shares_cached"] or 0),
            }

    def create_payment_request(self, user_id: int, ad_id: Optional[int], amount_stars: int, note: str = "") -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO payment_requests (user_id, ad_id, amount_stars, note, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (int(user_id), ad_id, int(amount_stars), note, int(time.time())),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_payment_requests(self, status: Optional[str] = "pending") -> List[Dict[str, Any]]:
        sql = "SELECT * FROM payment_requests WHERE 1=1"
        params: List[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_payment_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM payment_requests WHERE id=?", (int(request_id),)).fetchone()
            return dict(row) if row else None

    def update_payment_request(self, request_id: int, status: str):
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE payment_requests SET status=?, decided_at=? WHERE id=?",
                (status, int(time.time()), int(request_id)),
            )
            conn.commit()


db = Database()


import html
import json
import math
import re
import time
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import Config

MAIN_BUTTON = "📣 الإعلانات في القنوات"

MIN_TARGET_MEMBERS_DEFAULT = 10
MAX_DESC = 200
MIN_DESC = 10

TARGET_CATEGORIES = [
    "تقنية", "أخبار", "تعليم", "ترفيه", "تجارة",
    "ألعاب", "طبخ", "رياضة", "عقارات", "عام"
]

(
    S_TARGET,
    S_CATEGORY,
    A_SCOPE,
    A_SELECT,
    A_TITLE,
    A_DESC,
    A_IMAGE,
    A_CONFIRM,
    ADMIN_CREDIT,
    ADMIN_DEBIT,
) = range(10)


def is_admin(user_id: int) -> bool:
    return int(user_id) == int(Config.ADMIN_ID)


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def setting_float(key: str, default: float) -> float:
    try:
        return float(db.get_setting(key, str(default)) or default)
    except Exception:
        return default


def min_members() -> int:
    try:
        return int(db.get_setting("min_members", str(MIN_TARGET_MEMBERS_DEFAULT)) or MIN_TARGET_MEMBERS_DEFAULT)
    except Exception:
        return MIN_TARGET_MEMBERS_DEFAULT


def star_rate_sar() -> float:
    return setting_float("star_sar_rate", 1.0)


def view_price_sar_per_1000() -> float:
    return setting_float("view_price_sar_per_1000", 1.0)


def join_price_sar() -> float:
    return setting_float("join_price_sar", 0.08)


def money_sar(v: float) -> str:
    return f"{v:.2f} ر.س"


def stars_from_sar(v: float) -> int:
    rate = max(0.01, star_rate_sar())
    return max(1, int(math.ceil(v / rate)))


def sar_from_stars(stars: int) -> float:
    return round(int(stars) * star_rate_sar(), 2)


def two_col(buttons):
    rows = []
    row = []
    for b in buttons:
        row.append(b)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def main_kb(user_id: int) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("➕ إضافة قناة/مجموعة", callback_data="ui:target:add"),
        InlineKeyboardButton("📣 نشر إعلان", callback_data="ui:ad:new"),
        InlineKeyboardButton("📂 قنواتي ومجموعاتي", callback_data="ui:targets:list"),
        InlineKeyboardButton("📊 إعلاناتي", callback_data="ui:ads:list"),
        InlineKeyboardButton("💳 الرصيد", callback_data="ui:wallet"),
        InlineKeyboardButton("💰 التسعير", callback_data="ui:pricing"),
    ]
    if is_admin(user_id):
        buttons += [
            InlineKeyboardButton("🧑‍💼 لوحة الإدارة", callback_data="ui:admin"),
            InlineKeyboardButton("⚙️ إعدادات المنصة", callback_data="ui:settings"),
        ]
    return InlineKeyboardMarkup(two_col(buttons))


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(two_col([
        InlineKeyboardButton("📂 كل القنوات", callback_data="adm:targets"),
        InlineKeyboardButton("📣 كل الإعلانات", callback_data="adm:ads"),
        InlineKeyboardButton("💳 شحن رصيد", callback_data="adm:credit"),
        InlineKeyboardButton("➖ خصم رصيد", callback_data="adm:debit"),
        InlineKeyboardButton("🧾 طلبات الدفع", callback_data="adm:payments"),
        InlineKeyboardButton("⚙️ التسعير", callback_data="adm:pricing"),
        InlineKeyboardButton("↩️ رجوع", callback_data="ui:home"),
    ]))


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="ui:home")]])


def normalize_target(raw: str) -> dict:
    raw = (raw or "").strip()
    raw = raw.replace("http://", "https://")
    username = raw
    if username.startswith("https://t.me/"):
        username = username.split("https://t.me/", 1)[1]
    if username.startswith("t.me/"):
        username = username.split("t.me/", 1)[1]
    if username.startswith("@"):
        username = username[1:]
    username = username.split("?")[0].split("/")[0].strip()
    if re.fullmatch(r"[A-Za-z0-9_]{4,}", username):
        return {
            "raw": raw,
            "username": username,
            "chat_ref": f"@{username}",
            "join_url": f"https://t.me/{username}",
        }
    return {"raw": raw, "username": None, "chat_ref": None, "join_url": raw}


def target_card(t: dict) -> str:
    return (
        f"🧾 <b>بطاقة القناة/المجموعة</b>\n\n"
        f"الاسم: <b>{esc(t.get('title') or 'بدون اسم')}</b>\n"
        f"المعرف: <code>{esc(t.get('username') or t.get('raw_target'))}</code>\n"
        f"المجال: <b>{esc(t.get('category') or 'غير محدد')}</b>\n"
        f"الأعضاء: <b>{int(t.get('member_count') or 0)}</b>\n"
        f"المشاهدات: <b>{int(t.get('views_count') or 0)}</b>\n"
        f"الإعلانات: <b>{int(t.get('ads_count') or 0)}</b>\n"
        f"الصلاحيات: {'مشرف ✅' if t.get('bot_is_admin') else 'مشرف ❌'} | "
        f"{'نشر ✅' if t.get('can_post') else 'نشر ❌'} | "
        f"{'حذف ✅' if t.get('can_delete') else 'حذف ❌'}\n"
        f"الحالة: {'🟢 مفعلة' if t.get('is_verified') and not t.get('is_suspended') else '🔴 موقوفة/غير مفعلة'}"
    )


def target_kb(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(two_col([
        InlineKeyboardButton("🚀 ترويج قناتي", callback_data=f"tg:promo:{target_id}"),
        InlineKeyboardButton("📣 نشر إعلان جديد", callback_data=f"tg:ad:{target_id}"),
        InlineKeyboardButton("🔄 إعادة التحقق", callback_data=f"tg:recheck:{target_id}"),
        InlineKeyboardButton("🛠️ إدارة القناة", callback_data=f"tg:manage:{target_id}"),
        InlineKeyboardButton("⛔️ عزل/إيقاف", callback_data=f"tg:suspend:{target_id}"),
        InlineKeyboardButton("🗑 حذف القناة", callback_data=f"tg:delete:{target_id}"),
    ]))


def pricing_text() -> str:
    v = view_price_sar_per_1000()
    j = join_price_sar()
    s = star_rate_sar()
    return (
        "💰 <b>الباقات والتسعير</b>\n\n"
        f"• 1000 مشاهدة/أعضاء: <b>{money_sar(v)}</b> ≈ <b>{stars_from_sar(v)}</b> نجمة\n"
        f"• كل انضمام: <b>{money_sar(j)}</b> ≈ <b>{stars_from_sar(j)}</b> نجمة\n\n"
        f"قيمة النجمة الداخلية: <b>{money_sar(s)}</b>\n"
        "الدفع يتم مقدمًا من الرصيد، وللمشرف اعتماد الطلبات يدويًا عند الحاجة."
    )


def ad_stats_card(ad: dict, stats: dict) -> str:
    return (
        f"📣 <b>إحصائيات الإعلان #{ad['id']}</b>\n\n"
        f"العنوان: <b>{esc(ad.get('title'))}</b>\n"
        f"الوصف: {esc(ad.get('description'))}\n"
        f"الحالة: <b>{esc(ad.get('status'))}</b>\n\n"
        f"👁 المشاهدات: <b>{stats['views_cached']}</b>\n"
        f"💬 النقرات: <b>{stats['clicks_cached']}</b>\n"
        f"➕ الانضمامات: <b>{stats['joins_cached']}</b>\n"
        f"🔗 المشاركات: <b>{stats['shares_cached']}</b>\n"
        f"📨 الإرسال إلى: <b>{stats['posted']}</b>\n"
        f"🔄 التحديثات: <b>{stats['refresh']}</b>"
    )


def ad_caption(ad: dict, stats: dict, bot_username: str) -> str:
    return (
        f"✨ <b>إعلان رسمي</b>\n"
        f"{esc(ad.get('title'))}\n\n"
        f"{esc(ad.get('description'))}\n\n"
        f"🔥 سارع بالانضمام لهذه الفرصة\n"
        f"🤖 @{esc(bot_username)}\n\n"
        f"👁 <b>{stats['views_cached']}</b>  |  💬 <b>{stats['clicks_cached']}</b>  |  ➕ <b>{stats['joins_cached']}</b>"
    )


def ad_kb(ad_id: int, join_url: str, bot_username: str, stats: dict) -> InlineKeyboardMarkup:
    bot_start = f"https://t.me/{bot_username}?start=ad_{ad_id}"
    share_url = f"https://t.me/share/url?url={quote_plus(bot_start)}&text={quote_plus('🔥 إعلان رسمي جديد')}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("انضم الآن 🔥", url=join_url)],
        [
            InlineKeyboardButton("مشاركة الإعلان 🔗", url=share_url),
            InlineKeyboardButton("نشر إعلان جديد 🚀", callback_data="ui:ad:new"),
        ],
        [
            InlineKeyboardButton(f"👁 المشاهدات: {stats['views_cached']}", callback_data=f"ad:noop:{ad_id}"),
            InlineKeyboardButton(f"💬 النقرات: {stats['clicks_cached']}", callback_data=f"ad:noop:{ad_id}"),
        ],
        [
            InlineKeyboardButton(f"➕ الانضمامات: {stats['joins_cached']}", callback_data=f"ad:noop:{ad_id}"),
            InlineKeyboardButton("📊 تتبع الإعلان", callback_data=f"ad:stats:{ad_id}"),
        ],
        [InlineKeyboardButton("🔄 تحديث آخر البيانات", callback_data=f"ad:refresh:{ad_id}")],
    ])


def promo_ad_prefill(target: dict) -> dict:
    name = target.get("title") or target.get("raw_target") or "قناتي"
    return {
        "selected_target_ids": [target["id"]],
        "scope_type": "manual",
        "title": f"ترويج {name} 🔥",
        "description": f"متابعينا الكرام يمكنكم الانضمام إلى {name} الآن ✨",
        "image_file_id": None,
        "estimated_sar": 0.0,
        "estimated_stars": 0,
        "payment_mode": "balance",
    }


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.first_name or "", user.username)
    bal = db.get_balance(user.id)
    text = (
        "🔥 <b>منصة الإعلانات في القنوات</b>\n\n"
        "منصة احترافية لإدارة القنوات والمجموعات والإعلانات وتتبع الرصيد.\n\n"
        f"💳 رصيدك الحالي: <b>{bal}</b> نجمة"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_kb(user.id), parse_mode=ParseMode.HTML)
    else:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=main_kb(user.id), parse_mode=ParseMode.HTML)


async def begin_target_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["tg_flow"] = {}
    await q.message.reply_text(
        "أرسل رابط القناة أو المجموعة أو اسم المستخدم العام.\nمثال: @MyChannel أو https://t.me/MyChannel",
        reply_markup=back_kb(),
    )
    return S_TARGET


async def receive_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    info = normalize_target(raw)
    if not info["chat_ref"]:
        await update.message.reply_text("أرسل رابطًا عامًا صحيحًا أو اسم مستخدم يبدأ بـ @.")
        return S_TARGET
    context.user_data.setdefault("tg_flow", {}).update(info)
    kb = InlineKeyboardMarkup(two_col(
        [InlineKeyboardButton(cat, callback_data=f"tg:cat:{cat}") for cat in TARGET_CATEGORIES] +
        [InlineKeyboardButton("❌ إلغاء", callback_data="ui:home")]
    ))
    await update.message.reply_text("اختر المجال المناسب:", reply_markup=kb)
    return S_CATEGORY


async def choose_target_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    category = q.data.split("tg:cat:", 1)[1]
    flow = context.user_data.get("tg_flow", {})
    flow["category"] = category
    msg = (
        "✅ تم استلام البيانات\n\n"
        f"المجال: <b>{esc(category)}</b>\n"
        f"الرابط: <code>{esc(flow.get('raw'))}</code>\n\n"
        "الخطوة التالية:\n"
        "1) أضف البوت مشرفًا.\n"
        "2) فعّل نشر الرسائل.\n"
        "3) فعّل حذف الرسائل.\n"
        f"4) الحد الأدنى المطلوب: <b>{min_members()}</b>."
    )
    await q.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(two_col([
            InlineKeyboardButton("✅ التحقق والحفظ", callback_data="tg:verify"),
            InlineKeyboardButton("✏️ تعديل الرابط", callback_data="ui:target:add"),
            InlineKeyboardButton("❌ إلغاء", callback_data="ui:home"),
        ]))
    )
    return ConversationHandler.END


async def verify_and_save_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    flow = context.user_data.get("tg_flow", {})
    if not flow.get("chat_ref") or not flow.get("category"):
        await q.message.reply_text("البيانات غير مكتملة.")
        return ConversationHandler.END
    try:
        me = await context.bot.get_me()
        chat = await context.bot.get_chat(flow["chat_ref"])
        member = await context.bot.get_chat_member(chat.id, me.id)
        members = await context.bot.get_chat_member_count(chat.id)
    except (BadRequest, Forbidden, TelegramError) as e:
        await q.message.reply_text(f"تعذر الوصول إلى القناة/المجموعة.\nالسبب: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    if int(members) < min_members():
        await q.message.reply_text(
            f"العدد الحالي <b>{members}</b> وهو أقل من الحد الأدنى <b>{min_members()}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    status = getattr(member, "status", "")
    is_admin_bot = status in {"administrator", "creator"}
    can_post = bool(getattr(member, "can_post_messages", False))
    can_delete = bool(getattr(member, "can_delete_messages", False))
    if chat.type == "channel":
        if not (is_admin_bot and can_post and can_delete):
            await q.message.reply_text("في القنوات يجب أن يكون البوت مشرفًا مع صلاحية النشر والحذف.")
            return ConversationHandler.END
    else:
        if not is_admin_bot:
            await q.message.reply_text("البوت يجب أن يكون مشرفًا في المجموعة قبل الحفظ.")
            return ConversationHandler.END

    target_id = db.save_target(
        owner_id=q.from_user.id,
        raw_target=flow["raw"],
        chat_id=chat.id,
        username=getattr(chat, "username", None),
        title=getattr(chat, "title", None) or getattr(chat, "full_name", None),
        category=flow["category"],
        join_url=flow.get("join_url"),
        member_count=int(members),
        bot_is_admin=is_admin_bot,
        can_post=can_post,
        can_delete=can_delete,
        is_verified=True,
    )
    target = db.get_target(target_id)
    await q.message.reply_text("🎉 تم الحفظ بنجاح\n\n" + target_card(target), parse_mode=ParseMode.HTML, reply_markup=target_kb(target_id))
    context.user_data.pop("tg_flow", None)
    return ConversationHandler.END


async def list_my_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    targets = db.list_targets(owner_id=q.from_user.id)
    if not targets:
        await q.message.reply_text("لا توجد قنوات أو مجموعات بعد.", reply_markup=back_kb())
        return
    rows = []
    for t in targets[:60]:
        status = "✅" if t["is_verified"] and not t["is_suspended"] else "⛔"
        rows.append([InlineKeyboardButton(
            f"{status} {t.get('title') or t.get('raw_target')} | {t.get('category') or 'بدون'} | {int(t.get('member_count') or 0)}",
            callback_data=f"tg:manage:{t['id']}"
        )])
    rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="ui:home")])
    await q.message.reply_text("📂 قنواتك ومجموعاتك:", reply_markup=InlineKeyboardMarkup(rows))


async def manage_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    target = db.get_target(target_id)
    if not target:
        await q.message.reply_text("العنصر غير موجود.")
        return
    await q.message.reply_text(target_card(target), reply_markup=target_kb(target_id), parse_mode=ParseMode.HTML)


async def recheck_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    target = db.get_target(target_id)
    if not target:
        await q.message.reply_text("العنصر غير موجود.")
        return
    if not target.get("username"):
        await q.message.reply_text("هذا الهدف لا يملك username عام.")
        return
    try:
        me = await context.bot.get_me()
        chat = await context.bot.get_chat(f"@{target['username']}")
        member = await context.bot.get_chat_member(chat.id, me.id)
        members = await context.bot.get_chat_member_count(chat.id)
    except Exception as e:
        await q.message.reply_text(f"تعذر إعادة التحقق: <code>{esc(e)}</code>", parse_mode=ParseMode.HTML)
        return
    db.update_target(
        target_id,
        member_count=int(members),
        title=getattr(chat, "title", None) or getattr(chat, "full_name", None),
        is_verified=1,
        bot_is_admin=1 if getattr(member, "status", "") in {"administrator", "creator"} else 0,
        can_post=1 if getattr(member, "can_post_messages", False) else 0,
        can_delete=1 if getattr(member, "can_delete_messages", False) else 0,
    )
    await q.message.reply_text("🔄 تم التحقق والتحديث بنجاح.", reply_markup=target_kb(target_id))


async def suspend_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    target = db.get_target(target_id)
    if not target:
        await q.message.reply_text("العنصر غير موجود.")
        return
    db.update_target(target_id, is_suspended=0 if target.get("is_suspended") else 1)
    await q.message.reply_text("تم تبديل حالة الإيقاف/التفعيل.", reply_markup=target_kb(target_id))


async def delete_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    db.delete_target(target_id)
    await q.message.reply_text("🗑 تم حذف القناة/المجموعة من المنصة.")


def ad_scope_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(two_col([
        InlineKeyboardButton("الكل المفعّل", callback_data="ad:scope:all"),
        InlineKeyboardButton("حسب المجال", callback_data="ad:scope:cat"),
        InlineKeyboardButton("اختيار يدوي", callback_data="ad:scope:manual"),
        InlineKeyboardButton("❌ إلغاء", callback_data="ui:home"),
    ]))


async def begin_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["ad_flow"] = {
        "selected_target_ids": [],
        "title": None,
        "description": None,
        "image_file_id": None,
        "scope_type": None,
        "payment_mode": "balance",
    }
    await q.message.reply_text("📣 <b>إنشاء إعلان جديد</b>\n\nاختر نطاق النشر:", reply_markup=ad_scope_kb(), parse_mode=ParseMode.HTML)
    return A_SCOPE


async def choose_scope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    scope = q.data.split(":")[-1]
    flow = context.user_data.setdefault("ad_flow", {})
    flow["scope_type"] = scope
    owner_filter = None if is_admin(q.from_user.id) else q.from_user.id

    if scope == "all":
        targets = db.list_targets(owner_id=owner_filter, verified_only=True, include_suspended=False)
        if not targets:
            await q.message.reply_text("لا توجد قنوات/مجموعات مفعلة.")
            return ConversationHandler.END
        flow["selected_target_ids"] = [t["id"] for t in targets]
        total_members = sum(int(t.get("member_count") or 0) for t in targets)
        estimated_sar = round((total_members / 1000.0) * view_price_sar_per_1000(), 2)
        flow["estimated_sar"] = estimated_sar
        flow["estimated_stars"] = stars_from_sar(estimated_sar)
        await q.message.reply_text(
            f"تم اختيار الكل المفعّل.\nإجمالي الأعضاء: <b>{total_members}</b>\n"
            f"السعر التقديري: <b>{money_sar(estimated_sar)}</b> ≈ <b>{flow['estimated_stars']}</b> نجمة\n\n"
            "أرسل عنوانًا جذابًا للإعلان.",
            parse_mode=ParseMode.HTML,
        )
        return A_TITLE

    if scope == "cat":
        cats = db.distinct_categories(owner_id=owner_filter, verified_only=True)
        if not cats:
            await q.message.reply_text("لا توجد مجالات محفوظة.")
            return ConversationHandler.END
        rows = two_col([InlineKeyboardButton(c, callback_data=f"ad:cat:{c}") for c in cats] + [InlineKeyboardButton("❌ إلغاء", callback_data="ui:home")])
        await q.message.reply_text("اختر المجال:", reply_markup=InlineKeyboardMarkup(rows))
        return A_SELECT

    targets = db.list_targets(owner_id=owner_filter, verified_only=True, include_suspended=False)
    if not targets:
        await q.message.reply_text("لا توجد أهداف متاحة.")
        return ConversationHandler.END
    flow["available_targets"] = [t["id"] for t in targets]
    rows = []
    for t in targets[:60]:
        rows.append([InlineKeyboardButton(f"{t.get('title') or t.get('raw_target')} | {int(t.get('member_count') or 0)}", callback_data=f"ad:pick:{t['id']}")])
    rows.append([InlineKeyboardButton("✅ اعتماد المحدد", callback_data="ad:pickdone")])
    rows.append([InlineKeyboardButton("❌ إلغاء", callback_data="ui:home")])
    await q.message.reply_text("اختر قناة أو مجموعة:", reply_markup=InlineKeyboardMarkup(rows))
    return A_SELECT


async def choose_ad_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    category = q.data.split(":")[-1]
    flow = context.user_data.setdefault("ad_flow", {})
    owner_filter = None if is_admin(q.from_user.id) else q.from_user.id
    targets = db.list_targets(owner_id=owner_filter, verified_only=True, category=category, include_suspended=False)
    if not targets:
        await q.message.reply_text("لا توجد أهداف في هذا المجال.")
        return ConversationHandler.END
    flow["selected_target_ids"] = [t["id"] for t in targets]
    total_members = sum(int(t.get("member_count") or 0) for t in targets)
    estimated_sar = round((total_members / 1000.0) * view_price_sar_per_1000(), 2)
    flow["estimated_sar"] = estimated_sar
    flow["estimated_stars"] = stars_from_sar(estimated_sar)
    await q.message.reply_text(
        f"المجال المختار: <b>{esc(category)}</b>\nعدد الأهداف: <b>{len(targets)}</b>\n"
        f"السعر التقديري: <b>{money_sar(estimated_sar)}</b> ≈ <b>{flow['estimated_stars']}</b> نجمة\n\n"
        "أرسل عنوان الإعلان الآن.",
        parse_mode=ParseMode.HTML,
    )
    return A_TITLE


async def toggle_ad_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    flow = context.user_data.setdefault("ad_flow", {})
    selected = flow.setdefault("selected_target_ids", [])
    if target_id in selected:
        selected.remove(target_id)
    else:
        selected.append(target_id)
    await q.answer(f"تم تحديث الاختيار: {len(selected)}", show_alert=False)


async def done_select_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    flow = context.user_data.get("ad_flow", {})
    ids = flow.get("selected_target_ids", [])
    if not ids:
        await q.message.reply_text("لم يتم اختيار أي قناة/مجموعة.")
        return ConversationHandler.END
    targets = [db.get_target(tid) for tid in ids]
    targets = [t for t in targets if t]
    total_members = sum(int(t.get("member_count") or 0) for t in targets)
    estimated_sar = round((total_members / 1000.0) * view_price_sar_per_1000(), 2)
    flow["estimated_sar"] = estimated_sar
    flow["estimated_stars"] = stars_from_sar(estimated_sar)
    await q.message.reply_text(
        f"تم اعتماد <b>{len(ids)}</b> هدفًا.\n"
        f"التسعير: <b>{money_sar(estimated_sar)}</b> ≈ <b>{flow['estimated_stars']}</b> نجمة\n\n"
        "أرسل عنوانًا جذابًا الآن.",
        parse_mode=ParseMode.HTML,
    )
    return A_TITLE


async def receive_ad_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = (update.message.text or "").strip()
    if len(title) < 3:
        await update.message.reply_text("العنوان قصير جدًا.")
        return A_TITLE
    context.user_data.setdefault("ad_flow", {})["title"] = title
    await update.message.reply_text("أرسل وصفًا بين 10 و200 حرف.")
    return A_DESC


async def receive_ad_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = (update.message.text or "").strip()
    if len(desc) < MIN_DESC or len(desc) > MAX_DESC:
        await update.message.reply_text("الوصف يجب أن يكون بين 10 و200 حرف.")
        return A_DESC
    context.user_data.setdefault("ad_flow", {})["description"] = desc
    await update.message.reply_text("أرسل صورة ترويجية أو اكتب /skip.")
    return A_IMAGE


async def receive_ad_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        context.user_data.setdefault("ad_flow", {})["image_file_id"] = update.message.photo[-1].file_id
        return await preview_ad(update, context)
    await update.message.reply_text("أرسل صورة صحيحة أو اكتب /skip.")
    return A_IMAGE


async def skip_ad_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault("ad_flow", {})["image_file_id"] = None
    return await preview_ad(update, context)


def ad_preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(two_col([
        InlineKeyboardButton("✅ تأكيد النشر", callback_data="ad:confirm"),
        InlineKeyboardButton("💳 طلب موافقة المشرف", callback_data="ad:req:0"),
        InlineKeyboardButton("✏️ تعديل الإعلان", callback_data="ad:edit"),
        InlineKeyboardButton("❌ إلغاء", callback_data="ui:home"),
    ]))


async def preview_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("ad_flow", {})
    ids = flow.get("selected_target_ids", [])
    targets = [db.get_target(tid) for tid in ids]
    targets = [t for t in targets if t]
    total_members = sum(int(t.get("member_count") or 0) for t in targets)
    estimated_sar = float(flow.get("estimated_sar") or 0.0)
    estimated_stars = int(flow.get("estimated_stars") or stars_from_sar(estimated_sar))
    preview = (
        "🔮 <b>معاينة الإعلان</b>\n\n"
        f"العنوان: <b>{esc(flow.get('title'))}</b>\n"
        f"الوصف: {esc(flow.get('description'))}\n\n"
        f"الأهداف: <b>{len(targets)}</b>\n"
        f"الأعضاء التقديري: <b>{total_members}</b>\n"
        f"السعر التقديري: <b>{money_sar(estimated_sar)}</b> ≈ <b>{estimated_stars}</b> نجمة\n\n"
        "🔥 سارع بالانضمام لهذه الفرصة"
    )
    if update.message:
        if flow.get("image_file_id"):
            await update.message.reply_photo(photo=flow["image_file_id"], caption=preview, reply_markup=ad_preview_kb(), parse_mode=ParseMode.HTML, has_spoiler=True)
        else:
            await update.message.reply_text(preview, reply_markup=ad_preview_kb(), parse_mode=ParseMode.HTML)
    else:
        if flow.get("image_file_id"):
            await update.callback_query.message.reply_photo(photo=flow["image_file_id"], caption=preview, reply_markup=ad_preview_kb(), parse_mode=ParseMode.HTML, has_spoiler=True)
        else:
            await update.callback_query.message.reply_text(preview, reply_markup=ad_preview_kb(), parse_mode=ParseMode.HTML)
    return A_CONFIRM


async def publish_to_targets(context: ContextTypes.DEFAULT_TYPE, ad: dict, targets: list[dict], force_admin: bool = False):
    me = await context.bot.get_me()
    bot_username = me.username or "bot"
    sent = 0
    post_refs = []
    per_target_cost = float(ad.get("estimated_stars") or 0) / max(1, len(targets))
    per_target_owner = int(math.floor(per_target_cost * 0.6))
    per_target_platform = max(0, int(math.ceil(per_target_cost - per_target_owner)))

    for target in targets:
        if not target or target.get("is_suspended") or not target.get("chat_id"):
            continue

        stats = db.get_ad_stats(ad["id"])
        kb = ad_kb(ad["id"], target.get("join_url") or target.get("raw_target"), bot_username, stats)
        caption = ad_caption(ad, stats, bot_username)
        try:
            if ad.get("image_file_id"):
                msg = await context.bot.send_photo(
                    chat_id=target["chat_id"],
                    photo=ad["image_file_id"],
                    caption=caption,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                    has_spoiler=True,
                )
            else:
                msg = await context.bot.send_message(
                    chat_id=target["chat_id"],
                    text=caption,
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
        except TelegramError:
            continue

        sent += 1
        post_refs.append({"chat_id": target["chat_id"], "message_id": msg.message_id, "target_id": target["id"]})
        db.link_ad_target(ad["id"], target["id"], target["chat_id"], msg.message_id, views_cached=int(target.get("member_count") or 0))
        db.add_ad_event(ad["id"], "posted", user_id=ad["owner_id"], target_id=target["id"])
        db.add_ad_event(ad["id"], "deliver", user_id=ad["owner_id"], target_id=target["id"])
        db.update_target(target["id"], ads_count=int(target.get("ads_count") or 0) + 1, views_count=int(target.get("views_count") or 0) + int(target.get("member_count") or 0))

        if not force_admin and per_target_owner > 0:
            db.credit_balance(int(target["owner_id"]), per_target_owner)
            db.credit_balance(int(Config.ADMIN_ID), per_target_platform)

    db.update_ad(ad["id"], status="published", published_at=int(time.time()), post_refs_json=json.dumps(post_refs, ensure_ascii=False))
    return sent


async def confirm_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    flow = context.user_data.get("ad_flow", {})
    ids = flow.get("selected_target_ids", [])
    if not ids:
        await q.message.reply_text("لم يتم تحديد أي قناة/مجموعة.")
        return ConversationHandler.END

    title = flow.get("title")
    desc = flow.get("description")
    if not title or not desc:
        await q.message.reply_text("بيانات الإعلان غير مكتملة.")
        return ConversationHandler.END

    targets = [db.get_target(tid) for tid in ids]
    targets = [t for t in targets if t]
    estimated_sar = float(flow.get("estimated_sar") or 0.0)
    estimated_stars = int(flow.get("estimated_stars") or stars_from_sar(estimated_sar))
    owner = q.from_user.id
    force_admin = is_admin(owner)

    ad_id = db.save_ad(
        owner_id=owner,
        title=title,
        description=desc,
        image_file_id=flow.get("image_file_id"),
        scope_type=flow.get("scope_type") or "manual",
        target_ids=ids,
        estimated_price=estimated_sar,
        estimated_stars=estimated_stars,
        payment_mode="balance",
        bot_username=(await context.bot.get_me()).username,
        status="draft",
    )
    ad = db.get_ad(ad_id)

    if not force_admin:
        bal = db.get_balance(owner)
        if bal < estimated_stars:
            db.update_ad(ad_id, status="insufficient_balance")
            await q.message.reply_text(
                f"⚠️ رصيدك الحالي <b>{bal}</b> نجمة.\nالمطلوب <b>{estimated_stars}</b> نجمة.\n\n"
                "يمكنك طلب موافقة المشرف أو شحن الرصيد أولًا.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(two_col([
                    InlineKeyboardButton("💳 طلب موافقة المشرف", callback_data=f"ad:req:{ad_id}"),
                    InlineKeyboardButton("↩️ رجوع", callback_data="ui:home"),
                ])),
            )
            return ConversationHandler.END
        if not db.debit_balance(owner, estimated_stars):
            await q.message.reply_text("فشل خصم الرصيد.")
            return ConversationHandler.END

    sent = await publish_to_targets(context, ad, targets, force_admin=force_admin)
    context.user_data.pop("ad_flow", None)
    await q.message.reply_text(
        f"✅ تم النشر بنجاح\n\n"
        f"الجهات المنشور فيها: <b>{sent}</b>\n"
        f"الإعلان: <b>#{ad_id}</b>\n"
        f"الرصيد المستخدم: <b>{estimated_stars}</b> نجمة",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(two_col([
            InlineKeyboardButton("📊 تتبع الإعلان", callback_data=f"ad:stats:{ad_id}"),
            InlineKeyboardButton("📣 إعلان جديد", callback_data="ui:ad:new"),
        ])),
    )


async def request_ad_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ad_id = int(q.data.split(":")[-1])
    ad = db.get_ad(ad_id)
    if not ad:
        await q.message.reply_text("الإعلان غير موجود.")
        return
    db.update_ad(ad_id, status="pending_approval")
    req_id = db.create_payment_request(q.from_user.id, ad_id, int(ad.get("estimated_stars") or 0), note="طلب موافقة للنشر")
    await q.message.reply_text(
        f"تم إرسال الطلب للمشرف.\nرقم الطلب: <b>#{req_id}</b>\nالمبلغ: <b>{ad.get('estimated_stars')}</b> نجمة",
        parse_mode=ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            Config.ADMIN_ID,
            f"🧾 طلب موافقة جديد #{req_id}\nالإعلان: #{ad_id}\nالمستخدم: {q.from_user.id}\nالمبلغ: {ad.get('estimated_stars')} نجمة",
            reply_markup=InlineKeyboardMarkup(two_col([
                InlineKeyboardButton("✅ موافقة", callback_data=f"adm:approve:{ad_id}:{req_id}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"adm:reject:{ad_id}:{req_id}"),
            ])),
        )
    except Exception:
        pass
    context.user_data.pop("ad_flow", None)


async def list_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ads = db.list_ads(owner_id=q.from_user.id)
    if not ads:
        await q.message.reply_text("لا توجد إعلانات محفوظة.")
        return
    rows = []
    for a in ads[:50]:
        rows.append([InlineKeyboardButton(f"#{a['id']} | {a['title']} | {a['status']}", callback_data=f"ad:stats:{a['id']}")])
    rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="ui:home")])
    await q.message.reply_text("📊 إعلاناتك:", reply_markup=InlineKeyboardMarkup(rows))


async def ad_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ad_id = int(q.data.split(":")[-1])
    ad = db.get_ad(ad_id)
    if not ad:
        await q.message.reply_text("الإعلان غير موجود.")
        return
    stats = db.get_ad_stats(ad_id)
    await q.message.reply_text(
        ad_stats_card(ad, stats),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(two_col([
            InlineKeyboardButton("🔄 تحديث الآن", callback_data=f"ad:refresh:{ad_id}"),
            InlineKeyboardButton("📂 أهداف الإعلان", callback_data=f"ad:targets:{ad_id}"),
            InlineKeyboardButton("📣 إعلان جديد", callback_data="ui:ad:new"),
            InlineKeyboardButton("↩️ رجوع", callback_data="ui:home"),
        ])),
    )


async def refresh_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ad_id = int(q.data.split(":")[-1])
    throttle = context.user_data.setdefault("ad_refresh", {})
    now = time.time()
    if now - float(throttle.get(str(ad_id), 0)) < 300:
        await q.answer("التحديث متاح مرة كل 5 دقائق.", show_alert=False)
        return
    throttle[str(ad_id)] = now
    db.add_ad_event(ad_id, "refresh", user_id=q.from_user.id)
    ad = db.get_ad(ad_id)
    if not ad:
        return
    stats = db.get_ad_stats(ad_id)
    await q.message.reply_text(ad_stats_card(ad, stats), parse_mode=ParseMode.HTML)


async def ad_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ad_id = int(q.data.split(":")[-1])
    rows = db.list_ad_targets(ad_id)
    if not rows:
        await q.message.reply_text("لا توجد أهداف مرتبطة بهذا الإعلان.")
        return
    text = "📂 <b>الأهداف المنشور فيها الإعلان</b>\n\n"
    for item in rows[:40]:
        tg = db.get_target(int(item["target_id"]))
        text += f"• {esc((tg or {}).get('title') or item['target_id'])} — 👁 {item['views_cached']} | 💬 {item['clicks_cached']} | ➕ {item['joins_cached']}\n"
    await q.message.reply_text(text, parse_mode=ParseMode.HTML)


async def ad_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()


async def begin_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    target = db.get_target(target_id)
    if not target:
        await q.message.reply_text("العنصر غير موجود.")
        return
    context.user_data["ad_flow"] = promo_ad_prefill(target)
    await q.message.reply_text("🚀 <b>ترويج قناتي</b>\n\nأرسل صورة أو /skip.", parse_mode=ParseMode.HTML)
    return A_IMAGE


async def begin_target_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    target = db.get_target(target_id)
    if not target:
        await q.message.reply_text("العنصر غير موجود.")
        return
    context.user_data["ad_flow"] = {
        "selected_target_ids": [target_id],
        "scope_type": "manual",
        "title": f"إعلان في {target.get('title') or 'هذه القناة'} 🚀",
        "description": "اكتب وصف الإعلان هنا ✨",
        "image_file_id": None,
        "payment_mode": "balance",
    }
    await q.message.reply_text("أرسل عنوان الإعلان الآن.")
    return A_TITLE


async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    bal = db.get_balance(q.from_user.id)
    await q.message.reply_text(
        f"💳 <b>رصيدك الحالي</b>\n\n<b>{bal}</b> نجمة\n\nالقيمة التقريبية: <b>{money_sar(sar_from_stars(bal))}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(two_col([
            InlineKeyboardButton("📣 نشر إعلان", callback_data="ui:ad:new"),
            InlineKeyboardButton("💰 التسعير", callback_data="ui:pricing"),
            InlineKeyboardButton("↩️ رجوع", callback_data="ui:home"),
        ])),
    )


async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(pricing_text(), parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.message.reply_text("هذه اللوحة للمشرف فقط.")
        return
    await q.message.reply_text("🧑‍💼 <b>لوحة الإدارة</b>\n\nكل أدوات التحكم في القنوات والإعلانات والرصيد.", parse_mode=ParseMode.HTML, reply_markup=admin_kb())


async def admin_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    targets = db.list_targets(include_suspended=True)
    if not targets:
        await q.message.reply_text("لا توجد قنوات/مجموعات.")
        return
    rows = []
    for t in targets[:60]:
        rows.append([InlineKeyboardButton(f"{t.get('title') or t.get('raw_target')} | {t.get('member_count')}", callback_data=f"tg:manage:{t['id']}")])
    rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="ui:admin")])
    await q.message.reply_text("📂 جميع القنوات والمجموعات:", reply_markup=InlineKeyboardMarkup(rows))


async def admin_ads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    ads = db.list_ads()
    if not ads:
        await q.message.reply_text("لا توجد إعلانات.")
        return
    rows = []
    for a in ads[:60]:
        rows.append([InlineKeyboardButton(f"#{a['id']} | {a['title']} | {a['status']}", callback_data=f"ad:stats:{a['id']}")])
    rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="ui:admin")])
    await q.message.reply_text("📣 جميع الإعلانات:", reply_markup=InlineKeyboardMarkup(rows))


async def admin_payments(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    reqs = db.list_payment_requests("pending")
    if not reqs:
        await q.message.reply_text("لا توجد طلبات دفع معلقة.")
        return
    rows = []
    for r in reqs[:60]:
        rows.append([InlineKeyboardButton(f"#{r['id']} | {r['user_id']} | {r['amount_stars']} نجمة", callback_data=f"adm:req:{r['id']}")])
    rows.append([InlineKeyboardButton("↩️ رجوع", callback_data="ui:admin")])
    await q.message.reply_text("🧾 طلبات الدفع:", reply_markup=InlineKeyboardMarkup(rows))


async def payment_request_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    req_id = int(q.data.split(":")[-1])
    req = db.get_payment_request(req_id)
    if not req:
        await q.message.reply_text("الطلب غير موجود.")
        return
    await q.message.reply_text(
        f"🧾 <b>طلب دفع #{req_id}</b>\n\n"
        f"المستخدم: {req['user_id']}\n"
        f"الإعلان: {req.get('ad_id')}\n"
        f"المبلغ: {req['amount_stars']} نجمة\n"
        f"الحالة: {req['status']}\n"
        f"الملاحظة: {esc(req.get('note') or '-')}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(two_col([
            InlineKeyboardButton("✅ موافقة", callback_data=f"adm:approve:{req.get('ad_id') or 0}:{req_id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"adm:reject:{req.get('ad_id') or 0}:{req_id}"),
            InlineKeyboardButton("↩️ رجوع", callback_data="ui:admin"),
        ])),
    )


async def approve_pending_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    _, _, ad_id, req_id = q.data.split(":")
    ad_id = int(ad_id)
    req_id = int(req_id)
    ad = db.get_ad(ad_id)
    if not ad:
        await q.message.reply_text("الإعلان غير موجود.")
        return
    targets = [t for t in (db.get_target(tid) for tid in ad.get("target_ids", [])) if t]
    sent = await publish_to_targets(context, ad, targets, force_admin=True)
    db.update_payment_request(req_id, "approved")
    db.update_ad(ad_id, status="published")
    await q.message.reply_text(f"تمت الموافقة والنشر بنجاح. تم النشر في {sent} وجهات.")


async def reject_pending_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    _, _, ad_id, req_id = q.data.split(":")
    db.update_payment_request(int(req_id), "rejected")
    if int(ad_id) > 0:
        db.update_ad(int(ad_id), status="rejected")
    await q.message.reply_text("تم رفض الطلب.")


async def admin_credit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    await q.message.reply_text("أرسل: user_id amount\nمثال: 123456789 50")
    return ADMIN_CREDIT


async def admin_debit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    await q.message.reply_text("أرسل: user_id amount\nمثال: 123456789 10")
    return ADMIN_DEBIT


async def admin_credit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    parts = (update.message.text or "").split()
    if len(parts) != 2:
        await update.message.reply_text("الصيغة: user_id amount")
        return ADMIN_CREDIT
    try:
        user_id, amount = int(parts[0]), int(parts[1])
    except ValueError:
        await update.message.reply_text("أرقام غير صحيحة.")
        return ADMIN_CREDIT
    db.credit_balance(user_id, amount)
    await update.message.reply_text(f"تم شحن {amount} نجمة للمستخدم {user_id}.")
    return ConversationHandler.END


async def admin_debit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    parts = (update.message.text or "").split()
    if len(parts) != 2:
        await update.message.reply_text("الصيغة: user_id amount")
        return ADMIN_DEBIT
    try:
        user_id, amount = int(parts[0]), int(parts[1])
    except ValueError:
        await update.message.reply_text("أرقام غير صحيحة.")
        return ADMIN_DEBIT
    ok = db.debit_balance(user_id, amount)
    await update.message.reply_text("تم الخصم." if ok else "الرصيد غير كافٍ.")
    return ConversationHandler.END


async def admin_pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    await q.message.reply_text(pricing_text(), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(two_col([
        InlineKeyboardButton("↩️ رجوع", callback_data="ui:admin"),
        InlineKeyboardButton("💳 الرصيد", callback_data="ui:wallet"),
    ])))


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    await q.message.reply_text(
        "⚙️ <b>إعدادات المنصة</b>\n\n"
        f"الحد الأدنى للأعضاء: <b>{min_members()}</b>\n"
        f"سعر 1000 عضو: <b>{money_sar(view_price_sar_per_1000())}</b>\n"
        f"قيمة النجمة: <b>{money_sar(star_rate_sar())}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(two_col([
            InlineKeyboardButton("🧑‍💼 إدارة", callback_data="ui:admin"),
            InlineKeyboardButton("↩️ رجوع", callback_data="ui:home"),
        ])),
    )


async def payment_request_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ad_id = int(q.data.split(":")[-1])
    ad = db.get_ad(ad_id)
    if not ad:
        await q.message.reply_text("الإعلان غير موجود.")
        return
    db.update_ad(ad_id, status="pending_approval")
    req_id = db.create_payment_request(q.from_user.id, ad_id, int(ad.get("estimated_stars") or 0), note="طلب موافقة للنشر")
    await q.message.reply_text(f"تم إنشاء طلب الموافقة #{req_id}.")
    try:
        await context.bot.send_message(
            Config.ADMIN_ID,
            f"🧾 طلب موافقة جديد #{req_id}\nالإعلان: #{ad_id}\nالمستخدم: {q.from_user.id}\nالمبلغ: {ad.get('estimated_stars')} نجمة",
            reply_markup=InlineKeyboardMarkup(two_col([
                InlineKeyboardButton("✅ موافقة", callback_data=f"adm:approve:{ad_id}:{req_id}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"adm:reject:{ad_id}:{req_id}"),
            ])),
        )
    except Exception:
        pass


async def promote_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    target = db.get_target(target_id)
    if not target:
        await q.message.reply_text("العنصر غير موجود.")
        return
    context.user_data["ad_flow"] = promo_ad_prefill(target)
    await q.message.reply_text("🚀 <b>ترويج قناتي</b>\n\nأرسل صورة أو /skip.", parse_mode=ParseMode.HTML)
    return A_IMAGE


async def begin_target_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    target_id = int(q.data.split(":")[-1])
    target = db.get_target(target_id)
    if not target:
        await q.message.reply_text("العنصر غير موجود.")
        return
    context.user_data["ad_flow"] = {
        "selected_target_ids": [target_id],
        "scope_type": "manual",
        "title": f"إعلان في {target.get('title') or 'هذه القناة'} 🚀",
        "description": "اكتب وصف الإعلان هنا ✨",
        "image_file_id": None,
        "payment_mode": "balance",
    }
    await q.message.reply_text("أرسل عنوان الإعلان الآن.")
    return A_TITLE


async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    bal = db.get_balance(q.from_user.id)
    await q.message.reply_text(
        f"💳 <b>رصيدك الحالي</b>\n\n<b>{bal}</b> نجمة\n\nالقيمة التقريبية: <b>{money_sar(sar_from_stars(bal))}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(two_col([
            InlineKeyboardButton("📣 نشر إعلان", callback_data="ui:ad:new"),
            InlineKeyboardButton("💰 التسعير", callback_data="ui:pricing"),
            InlineKeyboardButton("↩️ رجوع", callback_data="ui:home"),
        ])),
    )


async def pricing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(pricing_text(), parse_mode=ParseMode.HTML, reply_markup=back_kb())


async def auto_refresh_ads(context: ContextTypes.DEFAULT_TYPE):
    try:
        me = await context.bot.get_me()
        bot_username = me.username or "bot"
    except Exception:
        return
    ads = db.list_ads(status="published")
    for ad in ads:
        stats = db.get_ad_stats(ad["id"])
        for ref in db.list_ad_targets(ad["id"]):
            chat_id = ref.get("chat_id")
            message_id = ref.get("message_id")
            if not chat_id or not message_id:
                continue
            target = db.get_target(int(ref["target_id"]))
            if not target:
                continue
            kb = ad_kb(ad["id"], target.get("join_url") or target.get("raw_target"), bot_username, stats)
            caption = ad_caption(ad, stats, bot_username)
            try:
                if ad.get("image_file_id"):
                    await context.bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=message_id,
                        caption=caption,
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=caption,
                        reply_markup=kb,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False,
                    )
            except Exception:
                continue


def setup(application: Application):
    conv_target = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_target_add, pattern=r"^ui:target:add$")],
        states={
            S_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_target)],
            S_CATEGORY: [CallbackQueryHandler(choose_target_category, pattern=r"^tg:cat:")],
        },
        fallbacks=[CallbackQueryHandler(show_main, pattern=r"^ui:home$")],
        allow_reentry=True,
    )

    conv_ad = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_ad, pattern=r"^ui:ad:new$")],
        states={
            A_SCOPE: [CallbackQueryHandler(choose_scope, pattern=r"^ad:scope:")],
            A_SELECT: [
                CallbackQueryHandler(choose_ad_category, pattern=r"^ad:cat:"),
                CallbackQueryHandler(toggle_ad_target, pattern=r"^ad:pick:\d+$"),
                CallbackQueryHandler(done_select_targets, pattern=r"^ad:pickdone$"),
            ],
            A_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ad_title)],
            A_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ad_desc)],
            A_IMAGE: [
                MessageHandler(filters.PHOTO, receive_ad_image),
                CommandHandler("skip", skip_ad_image),
            ],
            A_CONFIRM: [
                CallbackQueryHandler(confirm_ad, pattern=r"^ad:confirm$"),
                CallbackQueryHandler(payment_request_shortcut, pattern=r"^ad:req:\d+$"),
                CallbackQueryHandler(show_main, pattern=r"^ui:home$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(show_main, pattern=r"^ui:home$"), CommandHandler("cancel", show_main)],
        allow_reentry=True,
    )

    conv_admin_credit = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_credit, pattern=r"^adm:credit$")],
        states={ADMIN_CREDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_credit_input)]},
        fallbacks=[CallbackQueryHandler(show_main, pattern=r"^ui:home$")],
        allow_reentry=True,
    )

    conv_admin_debit = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_debit, pattern=r"^adm:debit$")],
        states={ADMIN_DEBIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_debit_input)]},
        fallbacks=[CallbackQueryHandler(show_main, pattern=r"^ui:home$")],
        allow_reentry=True,
    )

    application.add_handler(conv_target, group=-1)
    application.add_handler(conv_ad, group=-1)
    application.add_handler(conv_admin_credit, group=-1)
    application.add_handler(conv_admin_debit, group=-1)

    application.add_handler(CallbackQueryHandler(show_main, pattern=r"^ui:home$"), group=-1)
    application.add_handler(CallbackQueryHandler(list_my_targets, pattern=r"^ui:targets:list$"), group=-1)
    application.add_handler(CallbackQueryHandler(list_ads, pattern=r"^ui:ads:list$"), group=-1)
    application.add_handler(CallbackQueryHandler(wallet, pattern=r"^ui:wallet$"), group=-1)
    application.add_handler(CallbackQueryHandler(pricing, pattern=r"^ui:pricing$"), group=-1)
    application.add_handler(CallbackQueryHandler(admin_menu, pattern=r"^ui:admin$"), group=-1)
    application.add_handler(CallbackQueryHandler(settings_menu, pattern=r"^ui:settings$"), group=-1)

    application.add_handler(CallbackQueryHandler(verify_and_save_target, pattern=r"^tg:verify$"), group=-1)
    application.add_handler(CallbackQueryHandler(manage_target, pattern=r"^tg:manage:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(recheck_target, pattern=r"^tg:recheck:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(suspend_target, pattern=r"^tg:suspend:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(delete_target, pattern=r"^tg:delete:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(promote_target, pattern=r"^tg:promo:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(begin_target_ad, pattern=r"^tg:ad:\d+$"), group=-1)

    application.add_handler(CallbackQueryHandler(ad_stats, pattern=r"^ad:stats:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(refresh_ad, pattern=r"^ad:refresh:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(ad_targets, pattern=r"^ad:targets:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(ad_noop, pattern=r"^ad:noop:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(payment_request_shortcut, pattern=r"^ad:req:\d+$"), group=-1)

    application.add_handler(CallbackQueryHandler(admin_targets, pattern=r"^adm:targets$"), group=-1)
    application.add_handler(CallbackQueryHandler(admin_ads, pattern=r"^adm:ads$"), group=-1)
    application.add_handler(CallbackQueryHandler(admin_payments, pattern=r"^adm:payments$"), group=-1)
    application.add_handler(CallbackQueryHandler(admin_pricing, pattern=r"^adm:pricing$"), group=-1)
    application.add_handler(CallbackQueryHandler(payment_request_view, pattern=r"^adm:req:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(approve_pending_ad, pattern=r"^adm:approve:\d+:\d+$"), group=-1)
    application.add_handler(CallbackQueryHandler(reject_pending_ad, pattern=r"^adm:reject:\d+:\d+$"), group=-1)

    if application.job_queue:
        application.job_queue.run_repeating(auto_refresh_ads, interval=300, first=300)
