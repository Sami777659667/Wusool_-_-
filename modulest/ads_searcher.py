import asyncio
import html
import logging
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters

try:
    from config import Config
    from db import db
except ImportError:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import Config
    from db import db


logger = logging.getLogger("SearchAds_Hydrogen")

MAIN_BUTTON = "🔍 البحث عن فرصة"
FREE_CONTACT_LIMIT = 15


# =========================================================
# DB fallback helpers
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
    return conn


def _fetchone(query: str, params: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
    conn = _connect()
    try:
        cur = conn.execute(query, params)
        return cur.fetchone()
    finally:
        conn.close()


def _fetchall(query: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
    conn = _connect()
    try:
        cur = conn.execute(query, params)
        return cur.fetchall()
    finally:
        conn.close()


def _execute(query: str, params: Tuple[Any, ...] = ()) -> None:
    conn = _connect()
    try:
        conn.execute(query, params)
        conn.commit()
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


# =========================================================
# Schema
# =========================================================
def ensure_schema():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hydro_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                ad_serial TEXT,
                ad_owner_id INTEGER,
                message_text TEXT NOT NULL,
                bot_message_id INTEGER,
                status TEXT DEFAULT 'sent',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                replied_at DATETIME
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS hydro_seen_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                action_key TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_hydro_messages_sender ON hydro_messages(sender_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hydro_messages_receiver ON hydro_messages(receiver_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hydro_messages_ad ON hydro_messages(ad_serial)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hydro_seen_actions_user ON hydro_seen_actions(user_id, created_at)")
        conn.commit()
    finally:
        conn.close()


# =========================================================
# State
# =========================================================
def get_state(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    state = context.user_data.setdefault("search_state", {})
    state.setdefault("mode", "menu")
    state.setdefault("page", 0)
    state.setdefault("ads", [])
    state.setdefault("field", None)
    state.setdefault("city", None)
    state.setdefault("options", [])
    state.setdefault("options_kind", None)
    state.setdefault("awaiting_contact", None)
    state.setdefault("awaiting_reply", None)
    return state


def reset_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["search_state"] = {
        "mode": "menu",
        "page": 0,
        "ads": [],
        "field": None,
        "city": None,
        "options": [],
        "options_kind": None,
        "awaiting_contact": None,
        "awaiting_reply": None,
    }


# =========================================================
# Permissions / limits
# =========================================================
def get_user_info(user_id: int) -> Dict[str, int]:
    try:
        row = db.get_user_balance(user_id)
        return {
            "balance": safe_int(row.get("balance", 0)),
            "is_vip": safe_int(row.get("is_vip", 0)),
        }
    except Exception:
        return {"balance": 0, "is_vip": 0}


def contact_count_today(user_id: int) -> int:
    row = _fetchone(
        """
        SELECT COUNT(*) AS c
        FROM hydro_messages
        WHERE sender_id = ?
          AND date(created_at) = date('now')
        """,
        (user_id,),
    )
    return safe_int(row["c"] if row else 0)


def can_contact_today(user_id: int) -> bool:
    info = get_user_info(user_id)
    if info["is_vip"] == 1:
        return True
    return contact_count_today(user_id) < FREE_CONTACT_LIMIT


def is_vip(user_id: int) -> bool:
    return get_user_info(user_id).get("is_vip", 0) == 1


# =========================================================
# Data loaders
# =========================================================
def get_distinct_values(column: str) -> List[str]:
    if column not in {"field", "city"}:
        return []

    rows = _fetchall(
        f"""
        SELECT DISTINCT {column} AS value
        FROM ads
        WHERE is_active = 1
          AND {column} IS NOT NULL
          AND TRIM({column}) != ''
        ORDER BY value COLLATE NOCASE
        """
    )

    values: List[str] = []
    for row in rows:
        value = safe_text(row["value"], 500).strip()
        if value and value not in values:
            values.append(value)
    return values


def load_ads(field: Optional[str] = None, city: Optional[str] = None) -> List[Dict[str, Any]]:
    query = """
        SELECT *
        FROM ads
        WHERE is_active = 1
    """
    params: List[Any] = []

    if field:
        query += " AND field = ?"
        params.append(field)

    if city:
        query += " AND city = ?"
        params.append(city)

    query += """
        ORDER BY
            CASE
                WHEN status_tag LIKE '%💎%' THEN 0
                WHEN status_tag LIKE '%🔥%' THEN 1
                ELSE 2
            END,
            CASE
                WHEN published_at IS NOT NULL THEN published_at
                ELSE created_at
            END DESC,
            ad_id DESC
    """

    rows = _fetchall(query, tuple(params))
    return [dict(r) for r in rows]


# =========================================================
# UI
# =========================================================
def menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("🎯 بحث عام", callback_data="search:all"),
            InlineKeyboardButton("📍 حسب المدينة", callback_data="search:cities"),
        ],
        [
            InlineKeyboardButton("🏷 بحث مميز (VIP)", callback_data="search:fields"),
            InlineKeyboardButton("📬 رسائلي", callback_data="search:inbox"),
        ],
    ]

    return InlineKeyboardMarkup(rows)
    if user_id == getattr(Config, "ADMIN_ID", 0):
        rows.insert(0, [InlineKeyboardButton("🛠 لوحة الإدارة", callback_data="search:admin")])
    return InlineKeyboardMarkup(rows)


def browse_keyboard(has_contact: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("⬅️ السابق", callback_data="search:prev"),
            InlineKeyboardButton("التالي ➡️", callback_data="search:next"),
        ],
    ]
    if has_contact:
        rows.append([InlineKeyboardButton("💬 مراسلة المعلن", callback_data="search:contact")])
    rows.append([
        InlineKeyboardButton("🏠 القائمة", callback_data="search:menu"),
        InlineKeyboardButton("🔄 تحديث", callback_data="search:refresh"),
    ])
    rows.append([InlineKeyboardButton("❌ إغلاق", callback_data="search:close")])
    return InlineKeyboardMarkup(rows)


def pick_list_keyboard(items: List[str], kind: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []

    for idx, label in enumerate(items):
        cb = f"search:pick:{kind}:{idx}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("⬅️ رجوع", callback_data="search:menu")])
    rows.append([InlineKeyboardButton("❌ إغلاق", callback_data="search:close")])
    return InlineKeyboardMarkup(rows)


def reply_action_keyboard(message_row_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💬 رد", callback_data=f"search:reply:{message_row_id}"),
            InlineKeyboardButton("🚫 تجاهل", callback_data=f"search:ignore:{message_row_id}"),
        ]
    ])


def status_badge(ad: Dict[str, Any]) -> str:
    tag = safe_text(ad.get("status_tag"))
    if "💎" in tag:
        return "💎 ممول"
    if "🔥" in tag:
        return "🔥 مستعجل"
    return "🔆 عادي"


def format_ad(ad: Dict[str, Any], index: int, total: int) -> str:
    serial = safe_text(ad.get("serial_id"))
    name = safe_text(ad.get("name")) or "إعلان بدون اسم"
    field = safe_text(ad.get("field"))
    category = safe_text(ad.get("category"))
    city = safe_text(ad.get("city"))
    price = safe_text(ad.get("price_text"))
    desc = safe_text(ad.get("description"))
    owner_id = ad.get("owner_id")
    views = safe_int(ad.get("views_count"))
    badge = status_badge(ad)
    photo = "📷 موجودة" if ad.get("photo_id") else "⏭ بدون صورة"

    if len(desc) > 240:
        desc = desc[:240].rstrip() + "..."

    return (
        f"📢 <b>إعلان {index} / {total}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 الرقم: <code>{esc(serial)}</code>\n"
        f"🏷 الحالة: {esc(badge)}\n"
        f"🧾 الاسم: {esc(name)}\n"
        f"📂 المجال: {esc(field)}\n"
        f"🧩 التصنيف: {esc(category)}\n"
        f"📍 المدينة: {esc(city)}\n"
        f"💰 السعر: {esc(price)}\n"
        f"👀 المشاهدات: {views}\n"
        f"📷 الصورة: {photo}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>الوصف</b>\n{esc(desc)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 المعلن: <code>{esc(owner_id)}</code>\n"
        f"💬 المراسلة داخل البوت متاحة من الزر أدناه"
    )


async def delete_message_safely(query):
    try:
        await query.message.delete()
    except Exception:
        pass


async def send_current_ad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(context)
    ads = state.get("ads", [])
    if not ads:
        return await show_menu(update, context)

    if state["page"] < 0:
        state["page"] = 0
    if state["page"] >= len(ads):
        state["page"] = 0

    ad = ads[state["page"]]

    serial = safe_text(ad.get("serial_id"))
    if serial:
        try:
            await db_async(db.increment_views, serial)
        except Exception as e:
            logger.warning(f"increment_views failed: {e}")

    text = format_ad(ad, state["page"] + 1, len(ads))
    keyboard = browse_keyboard(bool(ad.get("owner_id")))

    if update.callback_query:
        await delete_message_safely(update.callback_query)
        if ad.get("photo_id"):
            await update.callback_query.message.reply_photo(
                photo=ad["photo_id"],
                caption=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await update.callback_query.message.reply_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
    else:
        if ad.get("photo_id"):
            await update.effective_message.reply_photo(
                photo=ad["photo_id"],
                caption=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
        else:
            await update.effective_message.reply_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )


async def show_menu(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    if context:
        reset_state(context)

    user = update.effective_user
    text = (
        "🔍 <b>البحث الهيدروجيني</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "تصفح إعلانًا واحدًا في كل مرة.\n"
        "يمكنك الانتقال بين الإعلانات، أو التصفية حسب المدينة، أو حسب المجال للبريميوم.\n"
        "ويمكنك مراسلة المعلن مباشرة من داخل البوت."
    )

    if update.callback_query:
        await delete_message_safely(update.callback_query)
        await update.callback_query.message.reply_text(
            text,
            reply_markup=menu_keyboard(user.id),
            parse_mode="HTML",
        )
    else:
        await update.effective_message.reply_text(
            text,
            reply_markup=menu_keyboard(user.id),
            parse_mode="HTML",
        )


async def start_browse(update: Update, context: ContextTypes.DEFAULT_TYPE, field: Optional[str] = None, city: Optional[str] = None):
    state = get_state(context)
    state["field"] = field
    state["city"] = city
    state["ads"] = await db_async(load_ads, field, city)
    state["page"] = 0
    state["mode"] = "browse"

    if not state["ads"]:
        text = (
            "❌ لا توجد إعلانات متاحة الآن.\n"
            "جرّب فلترًا آخر أو ارجع إلى القائمة."
        )
        await delete_message_safely(update.callback_query) if update.callback_query else None
        return await update.effective_message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 القائمة", callback_data="search:menu")],
                [InlineKeyboardButton("❌ إغلاق", callback_data="search:close")],
            ]),
        )

    await send_current_ad(update, context)


def ads_page_for_index(context: ContextTypes.DEFAULT_TYPE) -> Tuple[List[Dict[str, Any]], int]:
    state = get_state(context)
    ads = state.get("ads", [])
    return ads, safe_int(state.get("page", 0))


def move_page(context: ContextTypes.DEFAULT_TYPE, step: int):
    state = get_state(context)
    ads = state.get("ads", [])
    if not ads:
        state["page"] = 0
        return
    state["page"] = (safe_int(state.get("page", 0)) + step) % len(ads)


# =========================================================
# Contact / reply system
# =========================================================
async def begin_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(context)
    ads = state.get("ads", [])
    if not ads:
        return await update.effective_message.reply_text(
            "لا يوجد إعلان حاليًا.",
            reply_markup=menu_keyboard(update.effective_user.id),
        )

    ad = ads[state["page"]]
    receiver_id = ad.get("owner_id")
    serial = safe_text(ad.get("serial_id"))

    if not receiver_id:
        return await update.effective_message.reply_text(
            "هذا الإعلان لا يملك معلنًا محفوظًا.",
            reply_markup=browse_keyboard(False),
        )

    user = update.effective_user
    if not can_contact_today(user.id):
        return await update.effective_message.reply_text(
            f"❌ وصلت الحد اليومي المجاني وهو {FREE_CONTACT_LIMIT} طلبات.\n"
            f"هذا الخيار مفتوح للبريميوم بعد ذلك.",
            reply_markup=browse_keyboard(True),
        )

    state["awaiting_contact"] = {
        "sender_id": user.id,
        "receiver_id": int(receiver_id),
        "ad_serial": serial,
        "ad_owner_id": int(receiver_id),
        "page": state.get("page", 0),
        "field": state.get("field"),
        "city": state.get("city"),
    }

    return await update.effective_message.reply_text(
        "✉️ أرسل رسالتك الآن للمعلن.\n"
        "سيتم حفظها داخل البوت وإرسالها مباشرة.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء", callback_data="search:cancel_contact")]
        ]),
    )


async def send_contact_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(context)
    payload = state.get("awaiting_contact")
    if not payload:
        return False

    user = update.effective_user
    text = safe_text(update.message.text, 6000)

    if not text:
        await update.message.reply_text("أرسل رسالة نصية واضحة.")
        return True

    try:
        if not can_contact_today(user.id):
            await update.message.reply_text(
                f"❌ وصلت الحد اليومي المجاني وهو {FREE_CONTACT_LIMIT} طلبات.\n"
                f"هذا الخيار مفتوح للبريميوم بعد ذلك.",
                reply_markup=menu_keyboard(user.id),
            )
            state["awaiting_contact"] = None
            return True

        sender_id = int(payload["sender_id"])
        receiver_id = int(payload["receiver_id"])
        ad_serial = safe_text(payload.get("ad_serial"))
        ad_owner_id = int(payload.get("ad_owner_id") or receiver_id)

        if sender_id != user.id:
            state["awaiting_contact"] = None
            return False

        sender_name = safe_text(user.first_name)
        sender_username = safe_text(user.username)

        _execute(
            """
            INSERT INTO hydro_messages (
                sender_id, receiver_id, ad_serial, ad_owner_id, message_text, status
            ) VALUES (?, ?, ?, ?, ?, 'sent')
            """,
            (sender_id, receiver_id, ad_serial, ad_owner_id, text),
        )

        row = _fetchone("SELECT last_insert_rowid() AS id")
        message_row_id = safe_int(row["id"]) if row and row["id"] is not None else 0

        try:
            sent = await context.bot.send_message(
                chat_id=receiver_id,
                text=(
                    f"📩 <b>رسالة جديدة على إعلانك</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔢 الإعلان: <code>{esc(ad_serial)}</code>\n"
                    f"👤 من: {esc(sender_name)}"
                    + (f" (@{esc(sender_username)})" if sender_username else "")
                    + f"\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💬 {esc(text)}"
                ),
                reply_markup=reply_action_keyboard(message_row_id),
                parse_mode="HTML",
            )

            if message_row_id:
                _execute(
                    "UPDATE hydro_messages SET bot_message_id = ? WHERE id = ?",
                    (sent.message_id, message_row_id),
                )
        except Exception as e:
            logger.exception(f"send to receiver failed: {e}")
            try:
                db.record_event(ad_serial, "contact_failed", f"receiver={receiver_id} err={e}")
            except Exception:
                pass
            await update.message.reply_text(
                "تم حفظ الرسالة لكن فشل إرسالها للمعلن.",
                reply_markup=menu_keyboard(user.id),
            )
            state["awaiting_contact"] = None
            return True

        try:
            if hasattr(db, "record_event"):
                db.record_event(ad_serial, "contact_sent", f"sender={sender_id}, receiver={receiver_id}")
        except Exception:
            pass

        logger.info(f"CONTACT-SAVED | ad={ad_serial} sender={sender_id} receiver={receiver_id} row_id={message_row_id}")
        await update.message.reply_text("✅ تم إرسال الرسالة للمعلن.", reply_markup=menu_keyboard(user.id))
        state["awaiting_contact"] = None
        return True

    except Exception as e:
        logger.exception(f"contact send failed: {e}")
        try:
            if hasattr(db, "record_error"):
                db.record_error("hydro_contact", e, payload.get("ad_serial"), "contact forwarding failed")
        except Exception:
            pass
        await update.message.reply_text("حدث خطأ أثناء إرسال الرسالة.", reply_markup=menu_keyboard(user.id))
        state["awaiting_contact"] = None
        return True


async def begin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, message_row_id: int):
    query = update.callback_query
    row = _fetchone("SELECT * FROM hydro_messages WHERE id = ?", (message_row_id,))
    if not row:
        return await query.answer("الرسالة غير موجودة", show_alert=True)

    receiver_id = safe_int(row["receiver_id"])
    if query.from_user.id != receiver_id:
        return await query.answer("هذا الزر للمعلن فقط", show_alert=True)

    state = get_state(context)
    state["awaiting_reply"] = {
        "message_row_id": message_row_id,
        "target_user_id": safe_int(row["sender_id"]),
        "ad_serial": safe_text(row["ad_serial"]),
    }

    await query.answer()
    await query.message.reply_text(
        "✍️ أرسل الآن الرد الذي تريد إرساله إلى صاحب الطلب.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ إلغاء الرد", callback_data="search:cancel_reply")]
        ]),
    )


async def send_reply_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(context)
    payload = state.get("awaiting_reply")
    if not payload:
        return False

    user = update.effective_user
    text = safe_text(update.message.text, 6000)

    if not text:
        await update.message.reply_text("أرسل ردًا نصيًا.")
        return True

    try:
        row = _fetchone("SELECT receiver_id, sender_id, ad_serial FROM hydro_messages WHERE id = ?", (safe_int(payload["message_row_id"]),))
        if not row:
            state["awaiting_reply"] = None
            return await update.message.reply_text("الرسالة الأصلية غير موجودة.", reply_markup=menu_keyboard(user.id))

        if safe_int(row["receiver_id"]) != user.id:
            state["awaiting_reply"] = None
            return False

        target_user_id = safe_int(payload["target_user_id"])
        ad_serial = safe_text(payload.get("ad_serial"))
        message_row_id = safe_int(payload["message_row_id"])

        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                f"📩 <b>رد من المعلن</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔢 الإعلان: <code>{esc(ad_serial)}</code>\n"
                f"💬 {esc(text)}"
            ),
            parse_mode="HTML",
        )

        _execute(
            """
            UPDATE hydro_messages
            SET status = 'replied',
                replied_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message_row_id,),
        )

        try:
            if hasattr(db, "record_event"):
                db.record_event(ad_serial, "reply_sent", f"owner={user.id}, target={target_user_id}")
        except Exception:
            pass

        logger.info(f"REPLY-SENT | ad={ad_serial} owner={user.id} target={target_user_id}")
        await update.message.reply_text("✅ تم إرسال الرد.", reply_markup=menu_keyboard(user.id))
        state["awaiting_reply"] = None
        return True

    except Exception as e:
        logger.exception(f"reply failed: {e}")
        try:
            if hasattr(db, "record_error"):
                db.record_error("hydro_reply", e, payload.get("ad_serial"), "reply forwarding failed")
        except Exception:
            pass
        await update.message.reply_text("حدث خطأ أثناء إرسال الرد.", reply_markup=menu_keyboard(user.id))
        state["awaiting_reply"] = None
        return True


# =========================================================
# Lists / panels
# =========================================================
async def show_inbox(update: Update):
    user = update.effective_user
    sent_row = _fetchone("SELECT COUNT(*) AS c FROM hydro_messages WHERE sender_id = ?", (user.id,))
    recv_row = _fetchone("SELECT COUNT(*) AS c FROM hydro_messages WHERE receiver_id = ?", (user.id,))

    sent = safe_int(sent_row["c"] if sent_row else 0)
    recv = safe_int(recv_row["c"] if recv_row else 0)

    await update.effective_message.reply_text(
        f"📬 <b>رسائلك</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"المرسلة: {sent}\n"
        f"الواردة: {recv}\n\n"
        f"هذا القسم مخصص لتتبع التواصل داخل البوت.",
        reply_markup=menu_keyboard(user.id),
        parse_mode="HTML",
    )


# =========================================================
# Main flow / callbacks
# =========================================================
async def search_handler(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    if context:
        reset_state(context)
    await show_menu(update, context)


async def show_main(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    return await search_handler(update, context)


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user
    state = get_state(context)

    try:
        if data == "search:menu":
            await query.answer()
            return await show_menu(update, context)

        if data == "search:refresh":
            await query.answer("تم التحديث")
            if state.get("mode") == "browse":
                return await send_current_ad(update, context)
            return await show_menu(update, context)

        if data == "search:close":
            reset_state(context)
            await query.answer()
            await delete_message_safely(query)
            return

        if data == "search:all":
            state["mode"] = "browse"
            state["field"] = None
            state["city"] = None
            state["ads"] = await db_async(load_ads, None, None)
            state["page"] = 0
            await query.answer()
            if not state["ads"]:
                return await query.message.reply_text(
                    "❌ لا توجد إعلانات حالياً.",
                    reply_markup=menu_keyboard(user.id),
                )
            return await send_current_ad(update, context)

        if data == "search:cities":
            state["mode"] = "options"
            state["options_kind"] = "city"
            state["options"] = await db_async(get_distinct_values, "city")
            await query.answer()
            if not state["options"]:
                return await query.message.reply_text(
                    "لا توجد مدن مسجلة داخل الإعلانات الآن.",
                    reply_markup=menu_keyboard(user.id),
                )
            return await query.message.reply_text(
                "📍 <b>اختر مدينة</b>\n━━━━━━━━━━━━━━━━━━━━\nاختر من الأزرار أدناه.",
                reply_markup=pick_list_keyboard(state["options"], "city"),
                parse_mode="HTML",
            )

        if data == "search:fields":
            if not is_vip(user.id):
                await query.answer("هذا الخيار خاص بالبريميوم فقط", show_alert=True)
                return
            state["mode"] = "options"
            state["options_kind"] = "field"
            state["options"] = await db_async(get_distinct_values, "field")
            await query.answer()
            if not state["options"]:
                return await query.message.reply_text(
                    "لا توجد مجالات مسجلة داخل الإعلانات الآن.",
                    reply_markup=menu_keyboard(user.id),
                )
            return await query.message.reply_text(
                "🏷 <b>اختر مجالًا</b>\n━━━━━━━━━━━━━━━━━━━━\nاختر من الأزرار أدناه.",
                reply_markup=pick_list_keyboard(state["options"], "field"),
                parse_mode="HTML",
            )

        if data.startswith("search:pick:"):
            parts = data.split(":")
            kind = parts[2]
            idx = safe_int(parts[3], -1)

            options = state.get("options", [])
            if idx < 0 or idx >= len(options):
                return await query.answer("خيار غير صالح", show_alert=True)

            selected = options[idx]
            await query.answer()

            if kind == "field":
                if not is_vip(user.id):
                    return await query.answer("هذا الخيار خاص بالبريميوم فقط", show_alert=True)
                return await start_browse(update, context, field=selected)

            if kind == "city":
                return await start_browse(update, context, city=selected)

        if data == "search:prev":
            await query.answer()
            move_page(context, -1)
            return await send_current_ad(update, context)

        if data == "search:next":
            await query.answer()
            move_page(context, +1)
            return await send_current_ad(update, context)

        if data == "search:contact":
            await query.answer()
            return await begin_contact(update, context)

        if data == "search:cancel_contact":
            state["awaiting_contact"] = None
            await query.answer("تم الإلغاء")
            return await query.message.reply_text("تم إلغاء المراسلة.", reply_markup=menu_keyboard(user.id))

        if data == "search:inbox":
            await query.answer()
            return await show_inbox(update)

        if data == "search:admin":
            await query.answer()
            admin_user = getattr(Config, "ADMIN_USERNAME", "")
            if admin_user:
                return await query.message.reply_text(
                    f"👨‍💼 الإدارة: @{esc(admin_user)}",
                    reply_markup=menu_keyboard(user.id),
                )
            return await query.message.reply_text(
                "بيانات الإدارة غير مضبوطة.",
                reply_markup=menu_keyboard(user.id),
            )

        if data.startswith("search:reply:"):
            msg_id = safe_int(data.split(":")[-1], 0)
            return await begin_reply(update, context, msg_id)

        if data.startswith("search:ignore:"):
            msg_id = safe_int(data.split(":")[-1], 0)
            row = _fetchone("SELECT * FROM hydro_messages WHERE id = ?", (msg_id,))
            if not row:
                return await query.answer("الرسالة غير موجودة", show_alert=True)

            if safe_int(row["receiver_id"]) != user.id:
                return await query.answer("هذا الزر للمعلن فقط", show_alert=True)

            _execute("UPDATE hydro_messages SET status = 'ignored' WHERE id = ?", (msg_id,))
            try:
                if hasattr(db, "record_event"):
                    db.record_event(safe_text(row["ad_serial"]), "message_ignored", f"owner={user.id}, sender={row['sender_id']}")
            except Exception:
                pass

            await query.answer("تم التجاهل")
            return await query.message.reply_text("تم تجاهل الرسالة.", reply_markup=menu_keyboard(user.id))

        if data == "search:cancel_reply":
            state["awaiting_reply"] = None
            await query.answer("تم الإلغاء")
            return await query.message.reply_text("تم إلغاء الرد.", reply_markup=menu_keyboard(user.id))

    except Exception as e:
        logger.exception(f"callback error: {e}")
        try:
            if hasattr(db, "record_error"):
                db.record_error("search_callbacks", e, None, "callback handling failed")
        except Exception:
            pass
        await query.answer("حدث خطأ داخل البحث.", show_alert=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = safe_text(update.message.text, 10000)
    state = get_state(context)

    if text == MAIN_BUTTON:
        return await show_menu(update, context)

    if state.get("awaiting_contact"):
        return await send_contact_message(update, context)

    if state.get("awaiting_reply"):
        return await send_reply_message(update, context)


async def setup(application):
    ensure_schema()
    application.add_handler(CallbackQueryHandler(handle_callbacks, pattern=r"^search:"))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(rf"^{re.escape(MAIN_BUTTON)}$"), handle_text), group=30)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text), group=31)