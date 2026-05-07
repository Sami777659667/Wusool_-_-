# -*- coding: utf-8 -*-
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import random
import sqlite3
import string
import sys
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, PreCheckoutQueryHandler, filters

try:
    from config import Config
    from db import db
except Exception:
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from config import Config
    from db import db

logger = logging.getLogger("PremiumModule")
MAIN_BUTTON = "💎 البريميوم"

PLANS: Dict[str, Dict[str, Any]] = {
    "spark": {"code": "spark", "title": "✨ سبارك", "days": 7, "stars": 1, "binance_usdt": 4.9, "bank_sar": 19, "ads_boost": 2, "daily_posts": 10, "featured_slots": 1, "opportunity_feed": "فرص مميزة يومية"},
    "pro": {"code": "pro", "title": "💠 برو", "days": 30, "stars": 499, "binance_usdt": 11.9, "bank_sar": 49, "ads_boost": 5, "daily_posts": 30, "featured_slots": 3, "opportunity_feed": "فرص غير محدودة جزئيًا"},
    "legend": {"code": "legend", "title": "👑 ليجند", "days": 90, "stars": 1199, "binance_usdt": 27.9, "bank_sar": 119, "ads_boost": 10, "daily_posts": 99, "featured_slots": 7, "opportunity_feed": "فرص غير محدودة"},
}

SAUDI_BANKS = [
    {"name": "مصرف الراجحي", "account_name": "اسم الحساب من إعداداتك", "iban": "SA00 0000 0000 0000 0000 0000", "note": "حوّل بنفس الاسم ثم أرسل إشعار التحويل."},
    {"name": "مدى", "account_name": "اسم الحساب من إعداداتك", "iban": "بطاقة/حساب مرتبط بمدى", "note": "إذا كان الدفع عبر مدى فاملأ اسم صاحب البطاقة ووقت العملية."},
    {"name": "إنجاز", "account_name": "اسم الحساب من إعداداتك", "iban": "بيانات إنجاز من إعداداتك", "note": "أرسل رقم العملية بعد التحويل أو صورة الإشعار."},
]

POLL_SECONDS = 20
POLL_ATTEMPTS = 90


def _db_path() -> str:
    return getattr(db, "db_path", os.path.join("data", "system_database.db"))


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


def _ensure_column(table: str, column: str, col_type: str) -> None:
    try:
        conn = _connect()
        try:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("ensure column failed %s.%s: %s", table, column, e)


def _safe_text(value: Any, max_len: int = 1000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def _now_utc() -> datetime:
    return datetime.utcnow()


def _plan(code: str) -> Dict[str, Any]:
    return PLANS.get(code, PLANS["spark"])


def _pretty_days(days: int) -> str:
    return "يوم واحد" if days == 1 else f"{days} يوم"


def _generate_order_no(prefix: str = "PRM") -> str:
    stamp = datetime.utcnow().strftime("%y%m%d%H%M%S")
    rand = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}{stamp}{rand}"[:32]


def _user_row(user_id: int) -> Dict[str, Any]:
    if hasattr(db, "get_user"):
        try:
            row = db.get_user(user_id)
            if row:
                return row
        except Exception:
            pass
    row = _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return dict(row) if row else {"user_id": user_id}


def _extra_data(profile: Dict[str, Any]) -> Dict[str, Any]:
    raw = profile.get("extra_data")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _update_extra(user_id: int, patch: Dict[str, Any]) -> None:
    profile = _user_row(user_id)
    current = _extra_data(profile)
    current.update(patch)
    _execute(
        "UPDATE users SET extra_data = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
        (json.dumps(current, ensure_ascii=False, separators=(",", ":")), user_id),
    )


def _display_name(profile: Dict[str, Any]) -> str:
    name = _safe_text(profile.get("full_name"))
    last = _safe_text(profile.get("last_name"))
    username = _safe_text(profile.get("username"))
    if name and last:
        return f"{name} {last}".strip()
    if name:
        return name
    if last:
        return last
    if username:
        return f"@{username}"
    return f"User {profile.get('user_id')}"


def _active_premium(profile: Dict[str, Any]) -> bool:
    if _safe_int(profile.get("is_vip", 0)) == 1:
        return True
    if _safe_int(profile.get("is_premium", 0)) != 1:
        return False
    raw = profile.get("premium_until")
    if not raw:
        return True
    try:
        dt = datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        return dt >= _now_utc()
    except Exception:
        return True


def _premium_until_text(profile: Dict[str, Any]) -> str:
    raw = profile.get("premium_until")
    if not raw:
        return "غير محدد"
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(raw)


def _badge(profile: Dict[str, Any]) -> str:
    if _safe_int(profile.get("is_vip", 0)) == 1:
        return "💎 VIP"
    if _active_premium(profile):
        return "✅ بريميوم مفعل"
    return "⏳ غير مفعل"


def _ensure_schema() -> None:
    try:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS premium_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    plan_code TEXT NOT NULL,
                    payment_method TEXT NOT NULL,
                    payment_status TEXT DEFAULT 'pending',
                    contact_name TEXT,
                    phone TEXT,
                    bank_name TEXT,
                    reference_no TEXT,
                    binance_trade_no TEXT,
                    stars_invoice_payload TEXT,
                    proof_file_id TEXT,
                    notes TEXT,
                    provider_response TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    approved_at DATETIME,
                    expires_at DATETIME,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_premium_orders_user ON premium_orders(user_id, created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_premium_orders_status ON premium_orders(payment_status, created_at DESC)")
            conn.commit()
        finally:
            conn.close()

        _ensure_column("users", "premium_plan_code", "TEXT")
        _ensure_column("users", "premium_source", "TEXT")
        _ensure_column("users", "premium_order_no", "TEXT")
        _ensure_column("users", "premium_badge", "TEXT")
        _ensure_column("users", "premium_ads_boost", "INTEGER DEFAULT 0")
        _ensure_column("users", "premium_daily_posts", "INTEGER DEFAULT 0")
        _ensure_column("users", "premium_featured_slots", "INTEGER DEFAULT 0")
        _ensure_column("users", "premium_last_renewal_at", "DATETIME")
    except Exception as e:
        logger.exception("premium schema failed: %s", e)


def _order_insert(user_id: int, plan_code: str, method: str, **kwargs) -> str:
    order_no = _generate_order_no()
    _execute(
        """
        INSERT INTO premium_orders (
            order_no, user_id, plan_code, payment_method, payment_status,
            contact_name, phone, bank_name, reference_no, binance_trade_no,
            stars_invoice_payload, proof_file_id, notes, provider_response,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            order_no,
            user_id,
            plan_code,
            method,
            _safe_text(kwargs.get("contact_name"), 255),
            _safe_text(kwargs.get("phone"), 64),
            _safe_text(kwargs.get("bank_name"), 64),
            _safe_text(kwargs.get("reference_no"), 64),
            _safe_text(kwargs.get("binance_trade_no"), 64),
            _safe_text(kwargs.get("stars_invoice_payload"), 1024),
            _safe_text(kwargs.get("proof_file_id"), 256),
            _safe_text(kwargs.get("notes"), 500),
            _safe_text(kwargs.get("provider_response"), 2000),
        ),
    )
    return order_no


def _order_get(order_no: str) -> Optional[Dict[str, Any]]:
    row = _fetchone("SELECT * FROM premium_orders WHERE order_no = ?", (order_no,))
    return dict(row) if row else None


def _order_update(order_no: str, **fields) -> None:
    if not fields:
        return
    parts = []
    values = []
    for key, value in fields.items():
        parts.append(f"{key} = ?")
        values.append(value)
    parts.append("updated_at = CURRENT_TIMESTAMP")
    values.append(order_no)
    _execute(f"UPDATE premium_orders SET {', '.join(parts)} WHERE order_no = ?", tuple(values))


def _activate_premium(user_id: int, plan_code: str, source: str, order_no: str) -> bool:
    plan = _plan(plan_code)
    days = _safe_int(plan.get("days", 30), 30)
    try:
        if hasattr(db, "extend_premium"):
            ok = bool(db.extend_premium(user_id, days=days))
        elif hasattr(db, "set_premium"):
            ok = bool(db.set_premium(user_id, True, days=days))
        else:
            until = (_now_utc() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            _execute("UPDATE users SET is_premium = 1, premium_until = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (until, user_id))
            ok = True
        if ok:
            _execute(
                """
                UPDATE users
                SET premium_plan_code = ?, premium_source = ?, premium_order_no = ?, premium_badge = ?,
                    premium_ads_boost = ?, premium_daily_posts = ?, premium_featured_slots = ?,
                    premium_last_renewal_at = CURRENT_TIMESTAMP, last_active = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (plan_code, source, order_no, "💎", _safe_int(plan.get("ads_boost", 0)), _safe_int(plan.get("daily_posts", 0)), _safe_int(plan.get("featured_slots", 0)), user_id),
            )
            _update_extra(user_id, {"premium": True, "premium_plan_code": plan_code, "premium_source": source, "premium_order_no": order_no, "premium_activated_at": datetime.utcnow().isoformat()})
            _order_update(order_no, payment_status="approved", approved_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        return ok
    except Exception as e:
        logger.exception("activate premium failed: %s", e)
        return False


class BinancePayClient:
    def __init__(self) -> None:
        self.api_key = _safe_text(getattr(Config, "BINANCE_PAY_API_KEY", ""))
        self.secret = _safe_text(getattr(Config, "BINANCE_PAY_API_SECRET", ""))
        self.cert_sn = _safe_text(getattr(Config, "BINANCE_PAY_CERTIFICATE_SN", getattr(Config, "BINANCE_PAY_CERT_SN", "")))
        self.base_url = _safe_text(getattr(Config, "BINANCE_PAY_BASE_URL", "https://bpay.binanceapi.com"))
        self.enabled = bool(self.api_key and self.secret and self.cert_sn)

    def _sign(self, timestamp: str, nonce: str, body: str) -> str:
        payload = f"{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), payload, hashlib.sha512).hexdigest().upper()

    def _request(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Binance Pay credentials are not configured.")
        body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(datetime.utcnow().timestamp() * 1000))
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
        signature = self._sign(timestamp, nonce, body_json)
        req = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=body_json.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "BinancePay-Timestamp": timestamp,
                "BinancePay-Nonce": nonce,
                "BinancePay-Certificate-SN": self.cert_sn,
                "BinancePay-Signature": signature,
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))

    def create_order(self, order_no: str, plan: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "env": {"terminalType": "APP"},
            "merchantTradeNo": order_no,
            "orderAmount": float(plan["binance_usdt"]),
            "currency": "USDT",
            "description": f"Premium subscription for {plan['title']}",
            "goodsDetails": [
                {
                    "goodsType": "01",
                    "goodsCategory": "Z000",
                    "referenceGoodsId": order_no,
                    "goodsName": plan["title"].replace("💠", "").replace("✨", "").replace("👑", "").strip() or "Premium",
                    "goodsDetail": f"Premium subscription for user {user.get('user_id')}",
                }
            ],
            "passThroughInfo": json.dumps({"user_id": user.get("user_id"), "plan_code": plan["code"], "module": "premium"}, ensure_ascii=False, separators=(",", ":")),
        }
        sub = _safe_text(getattr(Config, "BINANCE_PAY_SUBMERCHANT_ID", ""))
        if sub:
            body["merchant"] = {"subMerchantId": sub}
        return self._request("/binancepay/openapi/v3/order", body)

    def query_order(self, order_no: str) -> Dict[str, Any]:
        return self._request("/binancepay/openapi/v2/order/query", {"merchantTradeNo": order_no})


BINANCE_CLIENT = BinancePayClient()


def _home_text(user_id: int) -> str:
    profile = _user_row(user_id)
    plan = _plan(_safe_text(profile.get("premium_plan_code")) or "spark")
    return (
        "💎 <b>نادي البريميوم</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "ترقية تجعل حسابك يظهر أولًا وتمنحك مساحة أقوى داخل البوت.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الحالة: <b>{_esc(_badge(profile))}</b>\n"
        f"الخطة الحالية: <b>{_esc(plan['title'])}</b>\n"
        f"الانتهاء: <b>{_esc(_premium_until_text(profile))}</b>\n"
        f"الظهور الإضافي: <b>{_safe_int(profile.get('premium_ads_boost', 0))}x</b>\n"
        f"النشر اليومي: <b>{_safe_int(profile.get('premium_daily_posts', 0))}</b>\n"
        f"المساحات المميزة: <b>{_safe_int(profile.get('premium_featured_slots', 0))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "المزايا: نشر خدماتك بشكل مميز، ظهور بارز، إعلانات مميزة، فرص أكثر، نظام إحالات أقوى، ووصول أسرع للفرص."
    )


def _plans_text() -> str:
    lines = ["📦 <b>باقات البريميوم</b>", "━━━━━━━━━━━━━━━━━━━━"]
    for code, plan in PLANS.items():
        lines += [
            f"• <b>{_esc(plan['title'])}</b>",
            f"  المدة: {_pretty_days(_safe_int(plan['days']))}",
            f"  النجوم: <b>{_safe_int(plan['stars'])}</b>",
            f"  Binance: <b>{_safe_text(plan['binance_usdt'])} USDT</b>",
            f"  التحويل المحلي: <b>{_safe_text(plan['bank_sar'])} SAR</b>",
            f"  النشر اليومي: <b>{_safe_int(plan['daily_posts'])}</b>",
            f"  الظهور المميز: <b>{_safe_int(plan['ads_boost'])}x</b>",
            "",
        ]
    lines.append("اختر الخطة ثم طريقة الدفع المناسبة.")
    return "\n".join(lines)


def _features_text(plan_code: str = "pro") -> str:
    plan = _plan(plan_code)
    return (
        "✨ <b>المزايا الذهبية</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{_esc(plan['title'])}</b>\n"
        f"• ظهور بارز في البوت والبحث\n"
        f"• تمييز بصري ذهبي/فاخر\n"
        f"• أولوية أعلى في فرص البيع والشراء\n"
        f"• نشر إعلانات مميزة وزيادة الوصول\n"
        f"• فرص مخصصة أكثر وبدون زحام\n"
        f"• إحصائيات ومشاهدة أفضل\n"
        f"• نظام إحالات أقوى\n"
        f"• {plan['daily_posts']} نشرات يوميًا تقريبًا حسب الخطة\n"
        f"• {plan['featured_slots']} مساحة مميزة للظهور\n"
        f"• {plan['opportunity_feed']}"
    )


def _methods_text(plan_code: str) -> str:
    plan = _plan(plan_code)
    return (
        "💳 <b>طرق الدفع</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الخطة المختارة: <b>{_esc(plan['title'])}</b>\n"
        f"• ⭐ Telegram Stars: مفعّل\n"
        f"• 🤖 Binance Pay: {'مفعّل' if BINANCE_CLIENT.enabled else 'يحتاج إعداد المفاتيح'}\n"
        f"• 🏦 تحويل محلي: متاح عبر المراجعة اليدوية\n"
        f"• 🔁 الإحالات: ربط لاحق مع نظام الإحالات\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Stars مناسبة للسلع والخدمات الرقمية داخل تيليجرام، بينما Binance Pay يعتمد على المفاتيح والتوقيع والتحقق من حالة الطلب."
    )


def _bank_text(plan_code: str) -> str:
    plan = _plan(plan_code)
    bank_lines = []
    for bank in SAUDI_BANKS:
        bank_lines.append(
            f"• <b>{_esc(bank['name'])}</b>\n"
            f"  اسم الحساب: {_esc(bank['account_name'])}\n"
            f"  IBAN / رقم: {_esc(bank['iban'])}\n"
            f"  ملاحظة: {_esc(bank['note'])}"
        )
    return (
        "🏦 <b>طلب تحويل محلي</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الخطة: <b>{_esc(plan['title'])}</b>\n"
        f"المبلغ التقريبي: <b>{_safe_text(plan['bank_sar'])} SAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(bank_lines) +
        "\n━━━━━━━━━━━━━━━━━━━━\n"
        "أرسل الاسم الكامل، رقم الجوال، واسم البنك ثم أرفق صورة الإشعار أو رقم العملية."
    )


def _binance_text(plan_code: str) -> str:
    plan = _plan(plan_code)
    return (
        "🤖 <b>Binance Pay</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الخطة: <b>{_esc(plan['title'])}</b>\n"
        f"القيمة: <b>{_safe_text(plan['binance_usdt'])} USDT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "سيتم إنشاء أمر دفع موقّع ثم مراقبة حالته تلقائيًا حتى يصبح PAID."
    )


def _status_text(user_id: int) -> str:
    profile = _user_row(user_id)
    plan = _plan(_safe_text(profile.get("premium_plan_code")) or "spark")
    return (
        "📋 <b>حالة اشتراكي</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الاسم: <b>{_esc(_display_name(profile))}</b>\n"
        f"الحالة: <b>{_esc(_badge(profile))}</b>\n"
        f"الخطة: <b>{_esc(plan['title'])}</b>\n"
        f"ينتهي في: <b>{_esc(_premium_until_text(profile))}</b>\n"
        f"المصدر: <b>{_esc(_safe_text(profile.get('premium_source')) or 'غير محدد')}</b>\n"
        f"آخر طلب: <code>{_esc(_safe_text(profile.get('premium_order_no')) or 'لا يوجد')}</code>\n"
        f"الظهور الإضافي: <b>{_safe_int(profile.get('premium_ads_boost', 0))}x</b>\n"
        f"النشر اليومي: <b>{_safe_int(profile.get('premium_daily_posts', 0))}</b>\n"
        f"المساحات المميزة: <b>{_safe_int(profile.get('premium_featured_slots', 0))}</b>"
    )


def _request_text(plan_code: str) -> str:
    plan = _plan(plan_code)
    return (
        "🪪 <b>طلب تفعيل بريميوم</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"الخطة: <b>{_esc(plan['title'])}</b>\n"
        f"المدة: <b>{_pretty_days(_safe_int(plan['days']))}</b>\n"
        f"النجوم: <b>{_safe_int(plan['stars'])}</b>\n"
        f"Binance: <b>{_safe_text(plan['binance_usdt'])} USDT</b>\n"
        f"تحويل محلي: <b>{_safe_text(plan['bank_sar'])} SAR</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "أدخل الاسم الكامل وبيانات الدفع ثم أرسل الإشعار أو الصورة."
    )


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 الباقات", callback_data="premium:plans"), InlineKeyboardButton("💳 طرق الدفع", callback_data="premium:methods")],
        [InlineKeyboardButton("⚡ الاشتراك السريع", callback_data="premium:buy:spark"), InlineKeyboardButton("💠 برو", callback_data="premium:buy:pro")],
        [InlineKeyboardButton("👑 ليجند", callback_data="premium:buy:legend"), InlineKeyboardButton("📋 حالتي", callback_data="premium:status")],
        [InlineKeyboardButton("✨ المزايا", callback_data="premium:features"), InlineKeyboardButton("🔙 رجوع", callback_data="premium:back")],
    ])


def _plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ سبارك", callback_data="premium:buy:spark"), InlineKeyboardButton("💠 برو", callback_data="premium:buy:pro")],
        [InlineKeyboardButton("👑 ليجند", callback_data="premium:buy:legend"), InlineKeyboardButton("💳 طرق الدفع", callback_data="premium:methods")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="premium:main")],
    ])


def _payment_keyboard(plan_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ بالنجوم", callback_data=f"premium:pay:stars:{plan_code}"), InlineKeyboardButton("🤖 بينانس", callback_data=f"premium:pay:binance:{plan_code}")],
        [InlineKeyboardButton("🏦 تحويل محلي", callback_data=f"premium:pay:bank:{plan_code}"), InlineKeyboardButton("🔁 الإحالات", callback_data=f"premium:pay:ref:{plan_code}")],
        [InlineKeyboardButton("🔙 الباقات", callback_data="premium:plans")],
    ])


def _admin_keyboard(order_no: str, user_id: int, plan_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ قبول", callback_data=f"premium:admin:approve:{order_no}"), InlineKeyboardButton("❌ رفض", callback_data=f"premium:admin:reject:{order_no}")],
        [InlineKeyboardButton("👤 فتح الحساب", callback_data=f"premium:admin:user:{user_id}"), InlineKeyboardButton("📦 الخطة", callback_data=f"premium:buy:{plan_code}")],
    ])


async def _send_screen(update: Update, text: str, keyboard: InlineKeyboardMarkup):
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            return
        except Exception:
            pass
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def _notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
    admin_id = _safe_int(getattr(Config, "ADMIN_ID", 0))
    if not admin_id:
        return
    try:
        await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.warning("admin notify failed: %s", e)


async def _stars_checkout(context: ContextTypes.DEFAULT_TYPE, user_id: int, plan_code: str) -> str:
    plan = _plan(plan_code)
    order_no = _order_insert(user_id, plan_code, "stars")
    payload = json.dumps({"order_no": order_no, "plan_code": plan_code, "method": "stars", "user_id": user_id}, ensure_ascii=False, separators=(",", ":"))
    await context.bot.send_invoice(
        chat_id=user_id,
        title=f"Premium {plan['title']}",
        description=f"اشتراك بريميوم لمدة {_pretty_days(_safe_int(plan['days']))}",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=plan["title"], amount=_safe_int(plan["stars"]))],
    )
    _order_update(order_no, stars_invoice_payload=payload)
    return order_no


async def _poll_binance(context: ContextTypes.DEFAULT_TYPE, order_no: str, user_id: int, plan_code: str) -> None:
    for _ in range(POLL_ATTEMPTS):
        order = _order_get(order_no)
        if not order or order.get("payment_status") in {"approved", "rejected"}:
            return
        try:
            response = BINANCE_CLIENT.query_order(order_no)
            _order_update(order_no, provider_response=json.dumps(response, ensure_ascii=False))
            status = _safe_text((response.get("data") or {}).get("status")).upper()
            if status == "PAID":
                if _activate_premium(user_id, plan_code, "binance", order_no):
                    await _notify_admin(context, "✅ <b>تفعيل بريميوم Binance</b>\n━━━━━━━━━━━━━━━━━━━━\n" + f"المستخدم: <code>{user_id}</code>\nالطلب: <code>{_esc(order_no)}</code>\nالخطة: <b>{_esc(_plan(plan_code)['title'])}</b>")
                    try:
                        await context.bot.send_message(chat_id=user_id, text=f"✅ <b>تم تأكيد الدفع عبر Binance</b>\n━━━━━━━━━━━━━━━━━━━━\nتم تفعيل: <b>{_esc(_plan(plan_code)['title'])}</b>\nينتهي في: <b>{_esc(_premium_until_text(_user_row(user_id)))}</b>", parse_mode=ParseMode.HTML, reply_markup=_main_keyboard())
                    except Exception:
                        pass
                return
            if status in {"CANCELED", "EXPIRED", "ERROR"}:
                _order_update(order_no, payment_status=status.lower())
                return
        except Exception as e:
            logger.warning("binance poll failed: %s", e)
        await asyncio.sleep(POLL_SECONDS)


async def _start_bank_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_code: str) -> None:
    st = context.user_data.setdefault("premium_state", {})
    st["plan_code"] = plan_code
    st["awaiting"] = {"key": "bank_name"}
    await _send_screen(update, _bank_text(plan_code), InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="premium:methods")]]))


async def _start_binance_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_code: str) -> None:
    st = context.user_data.setdefault("premium_state", {})
    st["plan_code"] = plan_code
    user_id = update.effective_user.id
    if not BINANCE_CLIENT.enabled:
        st["awaiting"] = {"key": "binance_manual_name"}
        await _send_screen(update, _binance_text(plan_code) + "\n\n⚠️ مفاتيح Binance Pay غير مضبوطة بعد، لذلك سيتم استقبال الطلب يدويًا.", InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="premium:methods")]]))
        return

    order_no = _order_insert(user_id, plan_code, "binance")
    st["order_no"] = order_no
    try:
        response = BINANCE_CLIENT.create_order(order_no, _plan(plan_code), _user_row(user_id))
        _order_update(order_no, provider_response=json.dumps(response, ensure_ascii=False))
        data = response.get("data") or {}
        checkout = _safe_text(data.get("checkoutUrl"))
        qrcode = _safe_text(data.get("qrcodeLink"))
        universal = _safe_text(data.get("universalUrl"))
        txt = (
            "🤖 <b>تم إنشاء أمر Binance Pay</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"رقم الطلب: <code>{_esc(order_no)}</code>\n"
            f"الخطة: <b>{_esc(_plan(plan_code)['title'])}</b>\n"
            "الحالة: <b>بانتظار الدفع</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Checkout: <code>{_esc(checkout or universal or qrcode or 'غير متوفر')}</code>\n"
            f"QR: <code>{_esc(qrcode or 'غير متوفر')}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "سيتم التحقق تلقائيًا من الحالة عبر Query Order حتى تصبح PAID."
        )
        await _send_screen(update, txt, InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث الحالة", callback_data=f"premium:checkbinance:{order_no}")], [InlineKeyboardButton("↩️ رجوع", callback_data="premium:methods")]]))
        asyncio.create_task(_poll_binance(context, order_no, user_id, plan_code))
    except Exception as e:
        logger.exception("create binance order failed: %s", e)
        _order_update(order_no, payment_status="failed", provider_response=str(e))
        await _send_screen(update, _binance_text(plan_code) + "\n\n❌ تعذر إنشاء أمر Binance الآن. جرب لاحقًا أو استخدم النجوم.", InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="premium:methods")]]))


async def _handle_bank_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    st = context.user_data.setdefault("premium_state", {})
    awaiting = st.get("awaiting") or {}
    key = _safe_text(awaiting.get("key"))
    plan_code = _safe_text(st.get("plan_code")) or "spark"
    user_id = update.effective_user.id

    if key == "bank_name":
        st["awaiting"] = {"key": "bank_phone", "bank_name": text}
        await update.message.reply_text("📞 أرسل رقم الجوال أو رقم التواصل.")
        return True

    if key == "bank_phone":
        st["awaiting"] = {"key": "bank_reference", "bank_name": awaiting.get("bank_name"), "phone": text}
        await update.message.reply_text("🏦 أرسل اسم البنك أو اسم الوسيط/المحفظة.")
        return True

    if key == "bank_reference":
        st["awaiting"] = {"key": "bank_proof", "bank_name": awaiting.get("bank_name"), "phone": awaiting.get("phone"), "reference_no": text}
        await update.message.reply_text("🧾 أرسل الآن صورة الإشعار أو رقم العملية كنص.")
        return True

    if key == "bank_proof":
        order_no = st.get("order_no") or _order_insert(user_id, plan_code, "bank", contact_name=awaiting.get("bank_name"), phone=awaiting.get("phone"), bank_name=awaiting.get("bank_name"), reference_no=awaiting.get("reference_no"), proof_file_id=text)
        st["order_no"] = order_no
        _order_update(order_no, proof_file_id=text, contact_name=awaiting.get("bank_name"), phone=awaiting.get("phone"), bank_name=awaiting.get("bank_name"), reference_no=awaiting.get("reference_no"))
        await _notify_admin(context, "🏦 <b>طلب تحويل محلي جديد</b>\n━━━━━━━━━━━━━━━━━━━━\n" + f"المستخدم: <code>{user_id}</code>\nالطلب: <code>{_esc(order_no)}</code>\nالخطة: <b>{_esc(_plan(plan_code)['title'])}</b>\nالاسم: <b>{_esc(_safe_text(awaiting.get('bank_name')))}</b>\nالجوال: <b>{_esc(_safe_text(awaiting.get('phone')))}</b>\nالمرجع: <code>{_esc(_safe_text(awaiting.get('reference_no')))}</code>\nالملف: <code>{_esc(text)}</code>", _admin_keyboard(order_no, user_id, plan_code))
        await update.message.reply_text("✅ تم استلام طلبك. سيتم مراجعته وتفعيل الاشتراك بعد التأكد.", parse_mode=ParseMode.HTML, reply_markup=_main_keyboard())
        st["awaiting"] = None
        return True

    if key == "binance_manual_name":
        st["awaiting"] = {"key": "binance_manual_ref", "contact_name": text}
        await update.message.reply_text("أرسل رقم العملية أو TXID أو صورة الإشعار.")
        return True

    if key == "binance_manual_ref":
        order_no = st.get("order_no") or _order_insert(user_id, plan_code, "binance_manual", contact_name=awaiting.get("contact_name"), reference_no=text)
        st["order_no"] = order_no
        _order_update(order_no, reference_no=text, payment_status="pending_review")
        await _notify_admin(context, "🤖 <b>طلب Binance يدوي</b>\n━━━━━━━━━━━━━━━━━━━━\n" + f"المستخدم: <code>{user_id}</code>\nالطلب: <code>{_esc(order_no)}</code>\nالخطة: <b>{_esc(_plan(plan_code)['title'])}</b>\nالاسم: <b>{_esc(_safe_text(awaiting.get('contact_name')))}</b>\nالمرجع/TxID: <code>{_esc(text)}</code>", _admin_keyboard(order_no, user_id, plan_code))
        await update.message.reply_text("✅ تم حفظ طلب Binance اليدوي للمراجعة.", parse_mode=ParseMode.HTML, reply_markup=_main_keyboard())
        st["awaiting"] = None
        return True

    return False


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.message.photo:
        return False
    st = context.user_data.setdefault("premium_state", {})
    awaiting = st.get("awaiting") or {}
    key = _safe_text(awaiting.get("key"))
    if key not in {"bank_proof", "binance_manual_ref"}:
        return False
    file_id = update.message.photo[-1].file_id
    return await _handle_bank_text(update, context, file_id)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = _safe_text(update.message.text, 5000)
    if text == MAIN_BUTTON:
        return await show_main(update, context)
    if context.user_data.get("premium_state", {}).get("awaiting"):
        await _handle_bank_text(update, context, text)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _handle_photo(update, context)


async def handle_pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.pre_checkout_query.answer(ok=True)
    except Exception as e:
        logger.warning("precheckout failed: %s", e)
        try:
            await update.pre_checkout_query.answer(ok=False, error_message="تعذر تأكيد الطلب الآن")
        except Exception:
            pass


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.successful_payment:
        return
    sp = update.message.successful_payment
    try:
        payload = json.loads(sp.invoice_payload or "{}")
    except Exception:
        payload = {}
    order_no = _safe_text(payload.get("order_no"))
    plan_code = _safe_text(payload.get("plan_code")) or "spark"
    user_id = update.effective_user.id if update.effective_user else _safe_int(payload.get("user_id"), 0)
    if not order_no:
        order_no = _order_insert(user_id, plan_code, "stars")
    _order_update(order_no, payment_status="paid", provider_response=json.dumps({"telegram_payment_charge_id": sp.telegram_payment_charge_id, "provider_payment_charge_id": sp.provider_payment_charge_id, "currency": sp.currency, "total_amount": sp.total_amount}, ensure_ascii=False))
    if _activate_premium(user_id, plan_code, "stars", order_no):
        await update.message.reply_text("✅ تم استلام الدفع بالنجوم وتفعيل البريميوم بنجاح.", parse_mode=ParseMode.HTML, reply_markup=_main_keyboard())
        await _notify_admin(context, "✅ <b>اشتراك بريميوم بالنجوم</b>\n━━━━━━━━━━━━━━━━━━━━\n" + f"المستخدم: <code>{user_id}</code>\nالطلب: <code>{_esc(order_no)}</code>\nالخطة: <b>{_esc(_plan(plan_code)['title'])}</b>")
    else:
        await update.message.reply_text("✅ تم حفظ الدفع، لكن التفعيل فشل. راجع السجل.", parse_mode=ParseMode.HTML)


async def show_main(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    if context is None or not update.effective_user:
        return
    try:
        if hasattr(db, "add_user"):
            db.add_user(update.effective_user.id, update.effective_user.first_name or "", update.effective_user.username or "")
    except Exception:
        pass
    await _send_screen(update, _home_text(update.effective_user.id), _main_keyboard())


async def search_handler(update: Update, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    return await show_main(update, context)


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    user_id = query.from_user.id if query.from_user else 0
    st = context.user_data.setdefault("premium_state", {})
    try:
        if data in {"premium:main", "premium:back"}:
            st["awaiting"] = None
            await query.answer()
            return await show_main(update, context)
        if data == "premium:plans":
            await query.answer()
            return await _send_screen(update, _plans_text(), _plans_keyboard())
        if data == "premium:methods":
            plan_code = _safe_text(st.get("plan_code")) or "spark"
            await query.answer()
            return await _send_screen(update, _methods_text(plan_code), _payment_keyboard(plan_code))
        if data == "premium:features":
            plan_code = _safe_text(st.get("plan_code")) or "pro"
            await query.answer()
            return await _send_screen(update, _features_text(plan_code), _plans_keyboard())
        if data == "premium:status":
            await query.answer()
            return await _send_screen(update, _status_text(user_id), _main_keyboard())
        if data.startswith("premium:buy:"):
            plan_code = data.split(":")[-1]
            st["plan_code"] = plan_code
            await query.answer()
            return await _send_screen(update, _request_text(plan_code), _payment_keyboard(plan_code))
        if data.startswith("premium:pay:stars:"):
            plan_code = data.split(":")[-1]
            st["plan_code"] = plan_code
            await query.answer("جارٍ إصدار فاتورة النجوم...")
            order_no = await _stars_checkout(context, user_id, plan_code)
            st["order_no"] = order_no
            return await query.message.reply_text("✅ تم إرسال فاتورة النجوم إلى هذه المحادثة.", reply_markup=_main_keyboard(), parse_mode=ParseMode.HTML)
        if data.startswith("premium:pay:binance:"):
            plan_code = data.split(":")[-1]
            st["plan_code"] = plan_code
            await query.answer("جارٍ تجهيز Binance Pay...")
            return await _start_binance_flow(update, context, plan_code)
        if data.startswith("premium:pay:bank:"):
            plan_code = data.split(":")[-1]
            st["plan_code"] = plan_code
            await query.answer()
            return await _start_bank_flow(update, context, plan_code)
        if data.startswith("premium:pay:ref:"):
            await query.answer()
            return await _send_screen(update, "🔁 <b>تفعيل عبر الإحالات</b>\n━━━━━━━━━━━━━━━━━━━━\nيمكن ربط هذه الميزة لاحقًا مع جدول الإحالات لإعطاء أيام مجانية أو خصم تلقائي.", InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع", callback_data="premium:methods")]]))
        if data.startswith("premium:checkbinance:"):
            order_no = data.split(":")[-1]
            await query.answer("جارٍ التحقق...")
            order = _order_get(order_no)
            if not order:
                return await query.message.reply_text("❌ الطلب غير موجود.", reply_markup=_main_keyboard())
            if not BINANCE_CLIENT.enabled:
                return await query.message.reply_text("⚠️ Binance Pay غير مفعّل بعد.", reply_markup=_main_keyboard())
            res = BINANCE_CLIENT.query_order(order_no)
            status = _safe_text((res.get("data") or {}).get("status")).upper()
            if status == "PAID" and _activate_premium(_safe_int(order.get("user_id")), _safe_text(order.get("plan_code")) or "spark", "binance", order_no):
                return await query.message.reply_text("✅ تم تأكيد الدفع وتفعيل البريميوم.", reply_markup=_main_keyboard())
            return await query.message.reply_text(f"📡 حالة الطلب الحالية: {status or 'UNKNOWN'}", reply_markup=_main_keyboard())
        if data.startswith("premium:admin:approve:"):
            if user_id != _safe_int(getattr(Config, "ADMIN_ID", 0)):
                return await query.answer("لصاحب الإدارة فقط", show_alert=True)
            order_no = data.split(":")[-1]
            order = _order_get(order_no)
            if not order:
                return await query.answer("الطلب غير موجود", show_alert=True)
            if _activate_premium(_safe_int(order.get("user_id")), _safe_text(order.get("plan_code")) or "spark", _safe_text(order.get("payment_method")) or "manual", order_no):
                _order_update(order_no, payment_status="approved", approved_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
                await query.answer("تم القبول")
                try:
                    await context.bot.send_message(chat_id=_safe_int(order.get("user_id")), text=f"✅ <b>تم تفعيل البريميوم</b>\n━━━━━━━━━━━━━━━━━━━━\nالخطة: <b>{_esc(_plan(_safe_text(order.get('plan_code')) or 'spark')['title'])}</b>\nينتهي في: <b>{_esc(_premium_until_text(_user_row(_safe_int(order.get('user_id')))))}</b>", parse_mode=ParseMode.HTML, reply_markup=_main_keyboard())
                except Exception:
                    pass
            return
        if data.startswith("premium:admin:reject:"):
            if user_id != _safe_int(getattr(Config, "ADMIN_ID", 0)):
                return await query.answer("لصاحب الإدارة فقط", show_alert=True)
            order_no = data.split(":")[-1]
            order = _order_get(order_no)
            if not order:
                return await query.answer("الطلب غير موجود", show_alert=True)
            _order_update(order_no, payment_status="rejected")
            await query.answer("تم الرفض")
            try:
                await context.bot.send_message(chat_id=_safe_int(order.get("user_id")), text="❌ تم رفض طلب الدفع. راجع البيانات أو أرسل إشعارًا صحيحًا.", reply_markup=_main_keyboard())
            except Exception:
                pass
            return
        if data.startswith("premium:admin:user:"):
            if user_id != _safe_int(getattr(Config, "ADMIN_ID", 0)):
                return await query.answer("لصاحب الإدارة فقط", show_alert=True)
            target = _safe_int(data.split(":")[-1], 0)
            profile = _user_row(target)
            await query.answer()
            return await query.message.reply_text(
                "👤 <b>ملخص المستخدم</b>\n━━━━━━━━━━━━━━━━━━━━\n" +
                f"الاسم: <b>{_esc(_display_name(profile))}</b>\n" +
                f"البريميوم: <b>{_esc(_badge(profile))}</b>\n" +
                f"الانتهاء: <b>{_esc(_premium_until_text(profile))}</b>\n" +
                f"آخر طلب: <code>{_esc(_safe_text(profile.get('premium_order_no')) or 'لا يوجد')}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=_main_keyboard(),
            )
    except Exception as e:
        logger.exception("premium callback error: %s", e)
        try:
            await query.answer("حدث خطأ.", show_alert=True)
        except Exception:
            pass


async def setup(application):
    _ensure_schema()
    application.add_handler(CallbackQueryHandler(handle_callbacks, pattern=r"^premium:"))
    application.add_handler(PreCheckoutQueryHandler(handle_pre_checkout))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment), group=29)
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo), group=30)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text), group=31)
    logger.info("Premium module loaded")
