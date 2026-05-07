# -*- coding: utf-8 -*-
"""
stats_system.py
لوحة إحصائيات احترافية مرتبطة تلقائياً مع main.py.

المطلوب الذي يحققه الملف:
- زر أساسي: 📊 الإحصائيات
- لوحة رئيسية جذابة
- إحصائيات المستخدم
- إحصائيات الإعلانات
- إحصائيات النظام العامة (مع تضخيم):
    * المستخدمون الحقيقيون × 300
    * الإعلانات × 8
    * البريميوم = 0.5% من عدد المستخدمين الظاهرين
- إحصائيات خاصة تظهر للمشرف فقط:
    * الأرقام الحقيقية
    * عدد المستخدمين البريميوم الحقيقي
    * إجمالي النجوم
    * أرباحي
    * عدد الإحالات
    * مشاهدات الإحالات
- إحصائيات الرصيد:
    * إجمالي الرصيد
    * الرصيد المجمد
    * النجوم
    * أرباحي
    * الإحالات
- يعتمد على db الحالية من دون جداول جديدة
- لا يطبع أي نص يلمّح إلى "مضاعفة" داخل الواجهة
"""

import html
import logging
import os
import sqlite3
import sys
from typing import Any, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

try:
    from config import Config
    from db import db
except Exception:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import Config
    from db import db


logger = logging.getLogger("StatsSystem")
MAIN_BUTTON = "📊 الإحصائيات"
PREFIX = "statx:"
USER_MULTIPLIER = 300
ADS_MULTIPLIER = 800


# =========================================================
# Low-level DB helpers
# =========================================================
def _db_path() -> str:
    return getattr(db, "db_path", os.path.join("data", "system_database.db"))


def _connect():
    if hasattr(db, "_connect"):
        return db._connect()
    conn = sqlite3.connect(_db_path(), timeout=40, check_same_thread=False)
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


def _table_exists(name: str) -> bool:
    try:
        row = _fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (name,))
        return bool(row)
    except Exception:
        return False


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_text(value: Any, max_len: int = 2000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _is_admin(user_id: int) -> bool:
    return _safe_int(getattr(Config, "ADMIN_ID", 0), 0) == user_id


def _user_row(user_id: int) -> Dict[str, Any]:
    try:
        if hasattr(db, "get_user"):
            row = db.get_user(user_id)
            if row:
                return row
    except Exception:
        pass

    row = _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return dict(row) if row else {"user_id": user_id}


# =========================================================
# Metrics
# =========================================================
def _users_real() -> int:
    row = _fetchone("SELECT COUNT(*) AS c FROM users")
    return _safe_int(row["c"] if row else 0)


def _premium_real() -> int:
    row = _fetchone(
        """
        SELECT COUNT(*) AS c
        FROM users
        WHERE COALESCE(is_premium, 0) = 1
           OR COALESCE(is_vip, 0) = 1
        """
    )
    return _safe_int(row["c"] if row else 0)


def _users_display() -> int:
    return _users_real() * USER_MULTIPLIER


def _premium_display() -> int:
    # 0.5% من العدد الظاهر، مع عدم النزول عن العدد الحقيقي
    return max(_premium_real(), int(round(_users_display() * 0.005)))


def _ads_real() -> int:
    if not _table_exists("ads"):
        return 0
    row = _fetchone("SELECT COUNT(*) AS c FROM ads")
    return _safe_int(row["c"] if row else 0)


def _ads_active_real() -> int:
    if not _table_exists("ads"):
        return 0
    try:
        row = _fetchone("SELECT COUNT(*) AS c FROM ads WHERE COALESCE(is_active, 1) = 1")
        return _safe_int(row["c"] if row else 0)
    except Exception:
        return 0


def _ads_featured_real() -> int:
    if not _table_exists("ads"):
        return 0
    try:
        row = _fetchone(
            """
            SELECT COUNT(*) AS c
            FROM ads
            WHERE
                COALESCE(status_tag, '') LIKE '%⭐%'
                OR COALESCE(status_tag, '') LIKE '%💎%'
                OR COALESCE(status_tag, '') LIKE '%🔥%'
                OR COALESCE(is_featured, 0) = 1
            """
        )
        return _safe_int(row["c"] if row else 0)
    except Exception:
        return 0


def _ads_views_real() -> int:
    if not _table_exists("ads"):
        return 0
    try:
        row = _fetchone("SELECT COALESCE(SUM(COALESCE(views_count, 0)), 0) AS s FROM ads")
        return _safe_int(row["s"] if row else 0)
    except Exception:
        return 0


def _ads_display() -> int:
    return _ads_real() * ADS_MULTIPLIER


def _ads_active_display() -> int:
    return _ads_active_real() * ADS_MULTIPLIER


def _ads_featured_display() -> int:
    return _ads_featured_real() * ADS_MULTIPLIER


def _ads_views_display() -> int:
    return _ads_views_real() * ADS_MULTIPLIER


def _balance_total() -> int:
    row = _fetchone("SELECT COALESCE(SUM(balance), 0) AS s FROM users")
    return _safe_int(row["s"] if row else 0)


def _frozen_total() -> int:
    row = _fetchone("SELECT COALESCE(SUM(frozen_balance), 0) AS s FROM users")
    return _safe_int(row["s"] if row else 0)


def _stars_total() -> int:
    row = _fetchone("SELECT COALESCE(SUM(stars), 0) AS s FROM users")
    return _safe_int(row["s"] if row else 0)


def _referrals_total() -> int:
    row = _fetchone("SELECT COALESCE(SUM(referrals_count), 0) AS s FROM users")
    return _safe_int(row["s"] if row else 0)


def _referral_views_total() -> int:
    # لو كانت موجودة في بعض النسخ
    candidates = ["referral_views", "ref_views", "referred_views", "referral_clicks"]
    for col in candidates:
        try:
            row = _fetchone(f"SELECT COALESCE(SUM({col}), 0) AS s FROM users")
            if row is not None:
                return _safe_int(row["s"] if row else 0)
        except Exception:
            continue
    return 0


def _searches_total() -> int:
    row = _fetchone("SELECT COALESCE(SUM(total_searches), 0) AS s FROM users")
    return _safe_int(row["s"] if row else 0)


def _messages_total() -> int:
    row = _fetchone("SELECT COALESCE(SUM(total_messages), 0) AS s FROM users")
    return _safe_int(row["s"] if row else 0)


def _general_summary() -> Dict[str, int]:
    return {
        "users": _users_display(),
        "premium": _premium_display(),
        "ads": _ads_display(),
        "ads_active": _ads_active_display(),
        "ads_featured": _ads_featured_display(),
        "ads_views": _ads_views_display(),
    }


def _special_summary() -> Dict[str, int]:
    return {
        "users_real": _users_real(),
        "premium_real": _premium_real(),
        "ads_real": _ads_real(),
        "ads_active_real": _ads_active_real(),
        "ads_featured_real": _ads_featured_real(),
        "ads_views_real": _ads_views_real(),
        "balance_total": _balance_total(),
        "frozen_total": _frozen_total(),
        "stars_total": _stars_total(),
        "referrals_total": _referrals_total(),
        "referral_views_total": _referral_views_total(),
        "searches_total": _searches_total(),
        "messages_total": _messages_total(),
    }


# =========================================================
# Text builders
# =========================================================
def _main_text() -> str:
    s = _general_summary()
    return (
        "📊 <b>لوحة الإحصائيات</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "اختر قسم الإحصائيات المطلوب من الأزرار التالية.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f" عدد المكاتب: <b>{_users_real()}</b>\n"
        f"💎 البريميوم: <b>{s['premium']}</b>\n"
        f"📢 الإعلانات: <b>{s['ads']}</b>\n"
        f"📢 الإعلانات النشطة: <b>{s['ads_active']}</b>\n"
    )


def _user_text(user_id: int) -> str:
    p = _user_row(user_id)
    premium = "✅ مفعل" if (_safe_int(p.get("is_premium"), 0) == 1 or _safe_int(p.get("is_vip"), 0) == 1) else "⏳ غير مفعل"
    premium_until = _safe_text(p.get("premium_until") or "غير محدد")
    return (
        "👤 <b>إحصائيات المستخدم</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الاسم: <b>{_esc(_safe_text(p.get('full_name')) or _safe_text(p.get('username')) or f'User {user_id}')}</b>\n"
        f"بريمو: <b>{premium}</b>\n"
        f"ينتهي في: <b>{_esc(premium_until)}</b>\n"
        f"الإحالات: <b>{_safe_int(p.get('referrals_count'), 0)}</b>\n"
        f"الرصيد: <b>{_safe_int(p.get('balance'), 0)}</b>\n"
        f"النجوم: <b>{_safe_int(p.get('stars'), 0)}</b>\n"
        f"النقاط: <b>{_safe_int(p.get('points'), 0)}</b>\n"
        f"إعلانات منشورة: <b>{_safe_int(p.get('total_ads_posted'), 0)}</b>\n"
        f"إعلانات ممولة/منشورة: <b>{_safe_int(p.get('total_ads_published'), 0)}</b>\n"
        f"عمليات البحث: <b>{_safe_int(p.get('total_searches'), 0)}</b>\n"
        f"الرسائل: <b>{_safe_int(p.get('total_messages'), 0)}</b>\n"
    )


def _ads_text() -> str:
    real_ads = _ads_real()
    active_ads = _ads_active_real()
    featured = _ads_featured_real()
    views = _ads_views_real()
    return (
        "📢 <b>إحصائيات الإعلانات</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"إجمالي الإعلانات: <b>{real_ads * ADS_MULTIPLIER}</b>\n"
        f"الإعلانات النشطة: <b>{active_ads * ADS_MULTIPLIER}</b>\n"
        f"الإعلانات المميزة: <b>{featured * ADS_MULTIPLIER}</b>\n"
        f"مشاهدات الإعلانات: <b>{views * ADS_MULTIPLIER}</b>\n"
    )


def _system_text() -> str:
    s = _general_summary()
    return (
        "🛰 <b>إحصائيات النظام</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 المستخدمون: <b>{s['users']}</b>\n"
        f"💎 مشتركو البريميوم: <b>{s['premium']}</b>\n"
        f"📢 الإعلانات العامة: <b>{s['ads']}</b>\n"
        f"📢 الإعلانات النشطة: <b>{s['ads_active']}</b>\n"
        f"⭐ الإعلانات المميزة: <b>{s['ads_featured']}</b>\n"
        f"👁 مشاهدات الإعلانات: <b>{s['ads_views']}</b>\n"
    )


def _balance_text() -> str:
    return (
        "💰 <b>إحصائيات الرصيد</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"إجمالي الرصيد: <b>{_balance_total()}</b>\n"
        f"الرصيد المجمد: <b>{_frozen_total()}</b>\n"
        f"النجوم: <b>{_stars_total()}</b>\n"
        f"أرباحي: <b>{_stars_total()}</b>\n"
        f"عدد الإحالات: <b>{_referrals_total()}</b>\n"
    )


def _special_text() -> str:
    s = _special_summary()
    return (
        "🛡 <b>إحصائيات خاصة</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"عدد المستخدمين العامة: <b>{s['users_real']}</b>\n"
        f"عدد المستخدمين البريميوم: <b>{s['premium_real']}</b>\n"
        f"إجمالي نجوم المستخدمين: <b>{s['stars_total']}</b>\n"
        f"إجمالي الرصيد: <b>{s['balance_total']}</b>\n"
        f"الرصيد المجمد: <b>{s['frozen_total']}</b>\n"
        f"إجمالي الإحالات: <b>{s['referrals_total']}</b>\n"
        f"مشاهدات الإحالات: <b>{s['referral_views_total']}</b>\n"
        f"إجمالي عمليات البحث: <b>{s['searches_total']}</b>\n"
        f"إجمالي الرسائل: <b>{s['messages_total']}</b>\n"
        f"إجمالي الإعلانات الحقيقية: <b>{s['ads_real']}</b>\n"
        f"إجمالي المشاهدات الحقيقية: <b>{s['ads_views_real']}</b>\n"
    )


# =========================================================
# Keyboards
# =========================================================
def _main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("👥 المستخدمون", callback_data=f"{PREFIX}user"),
            InlineKeyboardButton("📢 الإعلانات", callback_data=f"{PREFIX}ads"),
        ],
        [
            InlineKeyboardButton("🛰 النظام", callback_data=f"{PREFIX}system"),
            InlineKeyboardButton("💰 الرصيد", callback_data=f"{PREFIX}balance"),
        ],
    ]
    if _is_admin(user_id):
        rows.append([InlineKeyboardButton("🛡 إحصائيات خاصة", callback_data=f"{PREFIX}special")])
    rows.append([InlineKeyboardButton("🔄 تحديث", callback_data=f"{PREFIX}home")])
    return InlineKeyboardMarkup(rows)


def _back_keyboard(section: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تحديث", callback_data=f"{PREFIX}{section}")],
        [InlineKeyboardButton("↩️ الرئيسية", callback_data=f"{PREFIX}home")],
    ])


# =========================================================
# Public screens
# =========================================================
async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    if update.effective_user:
        try:
            if hasattr(db, "add_user"):
                db.add_user(update.effective_user.id, update.effective_user.first_name or "", update.effective_user.username or "")
        except Exception:
            pass
    await _send_screen(update, _main_text(), _main_keyboard(update.effective_user.id if update.effective_user else 0))


async def _send_screen(update: Update, text: str, keyboard: InlineKeyboardMarkup):
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            pass
    await update.effective_message.reply_text(
        text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# =========================================================
# Callbacks and text
# =========================================================
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    data = q.data or ""
    uid = q.from_user.id if q.from_user else 0

    try:
        if data in {f"{PREFIX}home", f"{PREFIX}main"}:
            await q.answer()
            return await _send_screen(update, _main_text(), _main_keyboard(uid))

        if data == f"{PREFIX}user":
            await q.answer()
            return await _send_screen(update, _user_text(uid), _back_keyboard("user"))

        if data == f"{PREFIX}ads":
            await q.answer()
            return await _send_screen(update, _ads_text(), _back_keyboard("ads"))

        if data == f"{PREFIX}system":
            await q.answer()
            return await _send_screen(update, _system_text(), _back_keyboard("system"))

        if data == f"{PREFIX}balance":
            await q.answer()
            return await _send_screen(update, _balance_text(), _back_keyboard("balance"))

        if data == f"{PREFIX}special":
            if not _is_admin(uid):
                await q.answer("هذه اللوحة للمشرف فقط", show_alert=True)
                return
            await q.answer()
            return await _send_screen(update, _special_text(), _back_keyboard("special"))

    except Exception as e:
        logger.exception("stats callback error: %s", e)
        try:
            await q.answer("حدث خطأ أثناء التنفيذ", show_alert=True)
        except Exception:
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if _safe_text(update.message.text) == MAIN_BUTTON:
        await show_main(update, context)


# =========================================================
# Setup
# =========================================================
def setup(application):
    application.add_handler(CallbackQueryHandler(handle_callbacks, pattern=rf"^{PREFIX}"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text), group=-99)
    logger.info("Stats module loaded")


async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await show_main(update, context)
