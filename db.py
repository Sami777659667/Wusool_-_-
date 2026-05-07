import os
import re
import csv
import json
import time
import shutil
import hashlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence, Iterable, Tuple

logger = logging.getLogger("AdPlatformDB_V8")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


@dataclass(frozen=True)
class DBConfig:
    db_dir: str = "data"
    db_name: str = "system_database.db"
    backup_dir_name: str = "backups"
    temp_dir_name: str = "tmp"
    db_version: str = "8"
    ad_counter_start: int = 1000
    retry_count: int = 4
    retry_delay: float = 0.18


class DatabaseManager:
    """
    قاعدة بيانات SQLite شاملة ومهيأة للتوسع.

    مميزات أساسية:
    - ترحيل تلقائي غير مدمّر للبيانات
    - حماية من التزامن عبر RLock + retries
    - WAL + busy timeout + foreign keys
    - جداول للمدفوعات، الإشعارات، المحفظة، الطوابير، النصوص، الكاش، الملفات، سجل الإدارة
    - بحث نصي كامل FTS5 مع fallback
    - تصدير CSV / JSON / XLSX
    - دعم اختياري للتصدير إلى MongoDB
    """

    def __init__(self, db_dir: str = "data", db_name: str = "system_database.db"):
        self.cfg = DBConfig(db_dir=db_dir, db_name=db_name)
        self.db_dir = self.cfg.db_dir
        self.db_path = os.path.join(self.db_dir, self.cfg.db_name)
        self.backup_dir = os.path.join(self.db_dir, self.cfg.backup_dir_name)
        self.temp_dir = os.path.join(self.db_dir, self.cfg.temp_dir_name)

        os.makedirs(self.db_dir, exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

        self._write_lock = threading.RLock()
        self._initialize_db()

    # ------------------------------------------------------------------
    # اتصال / معاملات / إعادة المحاولة
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -64000")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        return conn

    def _retryable(self, func, *args, **kwargs):
        last_exc = None
        for attempt in range(self.cfg.retry_count):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as e:
                last_exc = e
                msg = str(e).lower()
                if "locked" in msg or "busy" in msg or "malformed" in msg:
                    time.sleep(self.cfg.retry_delay * (attempt + 1))
                    continue
                raise
        if last_exc:
            raise last_exc

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _fetchone(self, query: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        def _op():
            conn = self._connect()
            try:
                cur = conn.execute(query, params)
                return cur.fetchone()
            finally:
                conn.close()

        return self._retryable(_op)

    def _fetchall(self, query: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        def _op():
            conn = self._connect()
            try:
                cur = conn.execute(query, params)
                return cur.fetchall()
            finally:
                conn.close()

        return self._retryable(_op)

    def _execute(self, query: str, params: Sequence[Any] = (), *, commit: bool = False) -> int:
        def _op():
            conn = self._connect()
            try:
                cur = conn.execute(query, params)
                if commit:
                    conn.commit()
                return cur.rowcount
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        return self._retryable(_op)

    # ------------------------------------------------------------------
    # أدوات نصية / JSON / Hash
    # ------------------------------------------------------------------
    def _normalize(self, text: Any) -> str:
        if text is None:
            return ""
        text = str(text).strip().lower()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^\w\s\u0600-\u06FF]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _safe_text(self, value: Any, max_len: int = 4000) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if len(text) > max_len:
            text = text[:max_len]
        return text

    def _safe_int(self, value: Any, default: Optional[int] = 0) -> Optional[int]:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except Exception:
            return default

    def _safe_float(self, value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    def _json_dumps(self, value: Any) -> str:
        try:
            return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return "{}"

    def _json_loads(self, value: Any, default: Any = None) -> Any:
        if default is None:
            default = {}
        if not value:
            return default
        try:
            return json.loads(value)
        except Exception:
            return default

    def _extract_keywords(self, *parts: Any) -> str:
        combined = " ".join(self._safe_text(p, 1000) for p in parts if p)
        combined = self._normalize(combined)
        words: List[str] = []
        for word in combined.split():
            if len(word) >= 2 and word not in words:
                words.append(word)
        return " ".join(words[:80])

    def _make_hash(self, *parts: Any) -> str:
        raw = "||".join(self._safe_text(p, 3000) for p in parts if p is not None)
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    # ------------------------------------------------------------------
    # التهيئة / الترحيل
    # ------------------------------------------------------------------
    def _initialize_db(self) -> None:
        try:
            with self._write_lock:
                with self._transaction() as conn:
                    cur = conn.cursor()

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS users (
                            user_id INTEGER PRIMARY KEY,
                            full_name TEXT,
                            username TEXT,
                            referred_by INTEGER,
                            referrals_count INTEGER DEFAULT 0,
                            balance INTEGER DEFAULT 0,
                            points INTEGER DEFAULT 0,
                            stars INTEGER DEFAULT 0,
                            frozen_balance INTEGER DEFAULT 0,
                            is_blocked INTEGER DEFAULT 0,
                            is_vip INTEGER DEFAULT 0,
                            is_premium INTEGER DEFAULT 0,
                            premium_until DATETIME,
                            total_ads_posted INTEGER DEFAULT 0,
                            total_ads_published INTEGER DEFAULT 0,
                            total_ads_found INTEGER DEFAULT 0,
                            total_searches INTEGER DEFAULT 0,
                            total_messages INTEGER DEFAULT 0,
                            last_active DATETIME DEFAULT CURRENT_TIMESTAMP,
                            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            extra_data TEXT DEFAULT '{}'
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ads (
                            ad_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            serial_id TEXT UNIQUE NOT NULL,
                            owner_id INTEGER,
                            ad_type TEXT,
                            deal_side TEXT,
                            name TEXT,
                            field TEXT,
                            category TEXT,
                            city TEXT,
                            price_text TEXT,
                            price_value REAL,
                            description TEXT,
                            keywords TEXT,
                            search_text TEXT,
                            photo_id TEXT,
                            source_chat_id INTEGER,
                            source_message_id INTEGER,
                            source_caption TEXT,
                            source_media_group_id TEXT,
                            channel_chat_id INTEGER,
                            channel_message_id INTEGER,
                            published_at DATETIME,
                            status_tag TEXT,
                            is_active INTEGER DEFAULT 1,
                            views_count INTEGER DEFAULT 0,
                            saved_count INTEGER DEFAULT 0,
                            extra_data TEXT DEFAULT '{}',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE SET NULL
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ad_events (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            serial_id TEXT,
                            event_type TEXT NOT NULL,
                            details TEXT,
                            error_message TEXT,
                            source_chat_id INTEGER,
                            source_message_id INTEGER,
                            channel_chat_id INTEGER,
                            channel_message_id INTEGER,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS stats (
                            stat_key TEXT PRIMARY KEY,
                            stat_value INTEGER DEFAULT 0
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS settings (
                            key TEXT PRIMARY KEY,
                            value TEXT
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS duplicates (
                            msg_hash TEXT PRIMARY KEY,
                            chat_id INTEGER,
                            message_id INTEGER,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    # مدفوعات / فواتير / معاملات (Stars / crypto / PayPal / local / Binance)
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS payments (
                            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            invoice_no TEXT UNIQUE,
                            user_id INTEGER,
                            purpose TEXT,
                            provider TEXT,
                            currency TEXT,
                            amount REAL,
                            amount_credits REAL,
                            external_ref TEXT,
                            status TEXT DEFAULT 'pending',
                            details TEXT DEFAULT '{}',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            confirmed_at DATETIME,
                            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS wallet_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            direction TEXT NOT NULL,
                            amount REAL NOT NULL,
                            balance_before REAL,
                            balance_after REAL,
                            reason TEXT,
                            ref_type TEXT,
                            ref_id TEXT,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            extra_data TEXT DEFAULT '{}',
                            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE SET NULL
                        )
                        """
                    )

                    # إشعارات داخلية
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS notifications (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER,
                            notif_type TEXT,
                            title TEXT,
                            body TEXT,
                            payload TEXT DEFAULT '{}',
                            is_read INTEGER DEFAULT 0,
                            priority INTEGER DEFAULT 0,
                            scheduled_at DATETIME,
                            sent_at DATETIME,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                        )
                        """
                    )

                    # نصوص الواجهة القابلة للإدارة من القاعدة
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ui_strings (
                            string_key TEXT PRIMARY KEY,
                            value TEXT,
                            lang TEXT DEFAULT 'ar',
                            category TEXT DEFAULT 'general',
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    # سجل الإدارة والتحكم
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS admin_actions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            admin_id INTEGER,
                            action TEXT NOT NULL,
                            target_user_id INTEGER,
                            target_table TEXT,
                            target_key TEXT,
                            before_value TEXT,
                            after_value TEXT,
                            notes TEXT,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    # طوابير الدردشة / الرسائل / الإرسال المتدرج لتقليل الحظر
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS message_queue (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            queue_name TEXT DEFAULT 'default',
                            sender_id INTEGER,
                            recipient_id INTEGER,
                            message_type TEXT,
                            payload TEXT,
                            status TEXT DEFAULT 'pending',
                            attempts INTEGER DEFAULT 0,
                            next_attempt_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    # محادثات / طلبات / وساطة / تشغيل المراسلات
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS chat_threads (
                            thread_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            thread_type TEXT,
                            created_by INTEGER,
                            owner_id INTEGER,
                            target_id INTEGER,
                            status TEXT DEFAULT 'open',
                            meta TEXT DEFAULT '{}',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS chat_messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            thread_id INTEGER,
                            sender_id INTEGER,
                            recipient_id INTEGER,
                            message_id INTEGER,
                            message_type TEXT,
                            content TEXT,
                            file_id TEXT,
                            reply_to_message_id INTEGER,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY (thread_id) REFERENCES chat_threads(thread_id) ON DELETE CASCADE
                        )
                        """
                    )

                    # كاش مؤقت ومرن
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS temp_cache (
                            cache_key TEXT PRIMARY KEY,
                            cache_value TEXT,
                            expires_at DATETIME,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    # ملفات / صور / روابط / file_id
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS media_store (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            media_hash TEXT UNIQUE,
                            file_id TEXT,
                            file_unique_id TEXT,
                            media_type TEXT,
                            owner_id INTEGER,
                            source_chat_id INTEGER,
                            source_message_id INTEGER,
                            url TEXT,
                            caption TEXT,
                            extra_data TEXT DEFAULT '{}',
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    # ميزات قابلة للتفعيل/الإيقاف
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS feature_flags (
                            flag_key TEXT PRIMARY KEY,
                            enabled INTEGER DEFAULT 1,
                            value TEXT,
                            scope TEXT DEFAULT 'global',
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )

                    # سجل نسخ احتياطية
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS backups (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            path TEXT NOT NULL,
                            file_size INTEGER,
                            sha256 TEXT,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            notes TEXT
                        )
                        """
                    )

                    # تهيئة إعدادات أساسية
                    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('ad_counter', ?)", (str(self.cfg.ad_counter_start),))
                    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('db_version', ?)", (self.cfg.db_version,))
                    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('default_queue_delay', '1.0')")
                    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('backup_retention_days', '14')")
                    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('language', 'ar')")

                    for key in (
                        "total_global_ads",
                        "total_users",
                        "total_searches",
                        "total_published_ads",
                        "total_errors",
                        "total_payments",
                        "total_notifications",
                        "total_queue_items",
                        "total_threads",
                    ):
                        cur.execute("INSERT OR IGNORE INTO stats (stat_key, stat_value) VALUES (?, 0)", (key,))

                    self._ensure_indexes(cur)
                    self._migrate_schema(cur)

                self._try_enable_fts()
                self._sync_fts_rebuild()

            logger.info("📡 [DB] قاعدة البيانات جاهزة بالكامل.")
        except Exception as e:
            logger.exception(f"❌ فشل تهيئة قاعدة البيانات: {e}")

    def _table_columns(self, cur: sqlite3.Cursor, table_name: str) -> set:
        cur.execute(f"PRAGMA table_info({table_name})")
        return {row["name"] for row in cur.fetchall()}

    def _add_missing_columns(self, cur: sqlite3.Cursor, table_name: str, needed: Dict[str, str]) -> None:
        try:
            existing = self._table_columns(cur, table_name)
            for col, col_type in needed.items():
                if col not in existing:
                    cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {col_type}")
        except Exception as e:
            logger.warning(f"⚠️ ترحيل الجدول {table_name} فشل جزئياً: {e}")

    def _migrate_schema(self, cur: sqlite3.Cursor) -> None:
        # users
        self._add_missing_columns(
            cur,
            "users",
            {
                "referred_by": "INTEGER",
                "referrals_count": "INTEGER DEFAULT 0",
                "balance": "INTEGER DEFAULT 0",
                "points": "INTEGER DEFAULT 0",
                "stars": "INTEGER DEFAULT 0",
                "frozen_balance": "INTEGER DEFAULT 0",
                "is_blocked": "INTEGER DEFAULT 0",
                "is_vip": "INTEGER DEFAULT 0",
                "is_premium": "INTEGER DEFAULT 0",
                "premium_until": "DATETIME",
                "total_ads_posted": "INTEGER DEFAULT 0",
                "total_ads_published": "INTEGER DEFAULT 0",
                "total_ads_found": "INTEGER DEFAULT 0",
                "total_searches": "INTEGER DEFAULT 0",
                "total_messages": "INTEGER DEFAULT 0",
                "last_active": "DATETIME DEFAULT CURRENT_TIMESTAMP",
                "joined_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
                "extra_data": "TEXT DEFAULT '{}'",
            },
        )

        # ads
        self._add_missing_columns(
            cur,
            "ads",
            {
                "deal_side": "TEXT",
                "name": "TEXT",
                "field": "TEXT",
                "category": "TEXT",
                "city": "TEXT",
                "price_text": "TEXT",
                "price_value": "REAL",
                "description": "TEXT",
                "keywords": "TEXT",
                "search_text": "TEXT",
                "photo_id": "TEXT",
                "source_chat_id": "INTEGER",
                "source_message_id": "INTEGER",
                "source_caption": "TEXT",
                "source_media_group_id": "TEXT",
                "channel_chat_id": "INTEGER",
                "channel_message_id": "INTEGER",
                "published_at": "DATETIME",
                "status_tag": "TEXT",
                "is_active": "INTEGER DEFAULT 1",
                "views_count": "INTEGER DEFAULT 0",
                "saved_count": "INTEGER DEFAULT 0",
                "extra_data": "TEXT DEFAULT '{}'",
                "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
            },
        )

        # generic future-friendly tables (safe no-op if already exists)
        self._add_missing_columns(cur, "payments", {"details": "TEXT DEFAULT '{}'", "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP"})
        self._add_missing_columns(cur, "wallet_logs", {"extra_data": "TEXT DEFAULT '{}'"})
        self._add_missing_columns(cur, "notifications", {"payload": "TEXT DEFAULT '{}'", "priority": "INTEGER DEFAULT 0", "scheduled_at": "DATETIME", "sent_at": "DATETIME"})
        self._add_missing_columns(cur, "ui_strings", {"lang": "TEXT DEFAULT 'ar'", "category": "TEXT DEFAULT 'general'", "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP"})
        self._add_missing_columns(cur, "admin_actions", {"notes": "TEXT"})
        self._add_missing_columns(cur, "message_queue", {"queue_name": "TEXT DEFAULT 'default'", "attempts": "INTEGER DEFAULT 0", "next_attempt_at": "DATETIME DEFAULT CURRENT_TIMESTAMP", "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP"})
        self._add_missing_columns(cur, "chat_threads", {"meta": "TEXT DEFAULT '{}'", "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP"})
        self._add_missing_columns(cur, "chat_messages", {"file_id": "TEXT", "reply_to_message_id": "INTEGER"})
        self._add_missing_columns(cur, "temp_cache", {"expires_at": "DATETIME"})
        self._add_missing_columns(cur, "media_store", {"media_hash": "TEXT", "file_unique_id": "TEXT", "url": "TEXT", "caption": "TEXT", "extra_data": "TEXT DEFAULT '{}'"})
        self._add_missing_columns(cur, "feature_flags", {"value": "TEXT", "scope": "TEXT DEFAULT 'global'", "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP"})
        self._add_missing_columns(cur, "backups", {"file_size": "INTEGER", "sha256": "TEXT", "notes": "TEXT"})

    def _ensure_indexes(self, cur: sqlite3.Cursor) -> None:
        # users
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_joined_at ON users(joined_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(is_blocked)")

        # ads
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_owner_id ON ads(owner_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_serial_id ON ads(serial_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_type ON ads(ad_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_field ON ads(field)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_category ON ads(category)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_city ON ads(city)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_active_created ON ads(is_active, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_channel_msg ON ads(channel_message_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_source_msg ON ads(source_message_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ads_published_at ON ads(published_at)")

        # events / duplicates
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_serial ON ad_events(serial_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON ad_events(event_type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_duplicates_hash ON duplicates(msg_hash)")

        # payments / notifications / queue / threads
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_user_id ON wallet_logs(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wallet_ref ON wallet_logs(ref_type, ref_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON message_queue(status, next_attempt_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_queue_sender ON message_queue(sender_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_threads_status ON chat_threads(status, updated_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(thread_id, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_temp_cache_exp ON temp_cache(expires_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_media_hash ON media_store(media_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feature_flags_scope ON feature_flags(scope, enabled)")

    def _try_enable_fts(self) -> None:
        try:
            with self._write_lock:
                with self._transaction() as conn:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS ads_fts
                        USING fts5(
                            serial_id, name, field, category, city, description, keywords, search_text,
                            content='ads',
                            content_rowid='ad_id'
                        )
                        """
                    )
                    conn.execute(
                        """
                        CREATE TRIGGER IF NOT EXISTS ads_ai AFTER INSERT ON ads BEGIN
                            INSERT INTO ads_fts(rowid, serial_id, name, field, category, city, description, keywords, search_text)
                            VALUES (new.ad_id, new.serial_id, new.name, new.field, new.category, new.city, new.description, new.keywords, new.search_text);
                        END;
                        """
                    )
                    conn.execute(
                        """
                        CREATE TRIGGER IF NOT EXISTS ads_ad AFTER DELETE ON ads BEGIN
                            INSERT INTO ads_fts(ads_fts, rowid, serial_id, name, field, category, city, description, keywords, search_text)
                            VALUES('delete', old.ad_id, old.serial_id, old.name, old.field, old.category, old.city, old.description, old.keywords, old.search_text);
                        END;
                        """
                    )
                    conn.execute(
                        """
                        CREATE TRIGGER IF NOT EXISTS ads_au AFTER UPDATE ON ads BEGIN
                            INSERT INTO ads_fts(ads_fts, rowid, serial_id, name, field, category, city, description, keywords, search_text)
                            VALUES('delete', old.ad_id, old.serial_id, old.name, old.field, old.category, old.city, old.description, old.keywords, old.search_text);
                            INSERT INTO ads_fts(rowid, serial_id, name, field, category, city, description, keywords, search_text)
                            VALUES (new.ad_id, new.serial_id, new.name, new.field, new.category, new.city, new.description, new.keywords, new.search_text);
                        END;
                        """
                    )
        except Exception as e:
            logger.warning(f"⚠️ FTS5 غير متاح أو فشل تفعيله: {e}")

    def _sync_fts_rebuild(self) -> None:
        try:
            self._execute("INSERT INTO ads_fts(ads_fts) VALUES('rebuild')", commit=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # سجل الأحداث والأخطاء
    # ------------------------------------------------------------------
    def record_event(
        self,
        serial_id: Optional[str],
        event_type: str,
        details: str = "",
        error_message: str = "",
        source_chat_id: Optional[int] = None,
        source_message_id: Optional[int] = None,
        channel_chat_id: Optional[int] = None,
        channel_message_id: Optional[int] = None,
    ) -> bool:
        try:
            with self._write_lock:
                self._execute(
                    """
                    INSERT INTO ad_events (
                        serial_id, event_type, details, error_message,
                        source_chat_id, source_message_id, channel_chat_id, channel_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        serial_id,
                        event_type,
                        self._safe_text(details, 4000),
                        self._safe_text(error_message, 2000),
                        source_chat_id,
                        source_message_id,
                        channel_chat_id,
                        channel_message_id,
                    ),
                    commit=True,
                )
            return True
        except Exception as e:
            logger.exception(f"❌ فشل record_event: {e}")
            return False

    def record_error(self, context: str, error: Exception, serial_id: Optional[str] = None, details: str = "") -> bool:
        logger.exception(f"❌ {context}: {error}")
        self._update_stat("total_errors", 1)
        return self.record_event(serial_id, "error", details=details, error_message=f"{context}: {error}")

    def _record_admin_action(
        self,
        admin_id: Optional[int],
        action: str,
        target_user_id: Optional[int] = None,
        target_table: Optional[str] = None,
        target_key: Optional[str] = None,
        before_value: Optional[Any] = None,
        after_value: Optional[Any] = None,
        notes: Optional[str] = None,
    ) -> None:
        try:
            self._execute(
                """
                INSERT INTO admin_actions (
                    admin_id, action, target_user_id, target_table, target_key,
                    before_value, after_value, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admin_id,
                    action,
                    target_user_id,
                    target_table,
                    target_key,
                    self._safe_text(before_value, 2000),
                    self._safe_text(after_value, 2000),
                    self._safe_text(notes, 4000),
                ),
                commit=True,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # المستخدمون
    # ------------------------------------------------------------------
    def add_user(self, user_id: int, full_name: str = "", username: str = "", referred_by: Optional[int] = None) -> bool:
        try:
            with self._write_lock:
                row = self._fetchone("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
                if row:
                    self.touch_user(user_id, full_name, username)
                    return True

                self._execute(
                    """
                    INSERT INTO users (
                        user_id, full_name, username, referred_by, joined_at, last_active, extra_data
                    ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '{}')
                    """,
                    (user_id, self._safe_text(full_name, 255), self._safe_text(username, 128), referred_by),
                    commit=True,
                )
                self._update_stat("total_users", 1)

                if referred_by:
                    self.adjust_referrals(referred_by, 1)
                    self._execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referred_by, user_id), commit=True)

            logger.info(f"✅ [DB] إضافة مستخدم: {user_id} | {full_name} | @{username or ''}")
            return True
        except Exception as e:
            self.record_error("add_user", e)
            return False

    def touch_user(self, user_id: int, full_name: Optional[str] = None, username: Optional[str] = None) -> bool:
        try:
            self._execute(
                """
                UPDATE users
                SET full_name = COALESCE(?, full_name),
                    username = COALESCE(?, username),
                    last_active = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (
                    self._safe_text(full_name, 255) if full_name is not None else None,
                    self._safe_text(username, 128) if username is not None else None,
                    user_id,
                ),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("touch_user", e)
            return False

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            row = self._fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
            return dict(row) if row else None
        except Exception as e:
            self.record_error("get_user", e)
            return None

    def get_user_balance(self, user_id: int) -> Dict[str, int]:
        try:
            row = self._fetchone(
                "SELECT balance, points, stars, is_vip, is_premium, frozen_balance FROM users WHERE user_id = ?",
                (user_id,),
            )
            if not row:
                return {"balance": 0, "points": 0, "stars": 0, "frozen_balance": 0, "is_vip": 0, "is_premium": 0}
            return {
                "balance": int(row["balance"] or 0),
                "points": int(row["points"] or 0),
                "stars": int(row["stars"] or 0),
                "frozen_balance": int(row["frozen_balance"] or 0),
                "is_vip": int(row["is_vip"] or 0),
                "is_premium": int(row["is_premium"] or 0),
            }
        except Exception as e:
            self.record_error("get_user_balance", e)
            return {"balance": 0, "points": 0, "stars": 0, "frozen_balance": 0, "is_vip": 0, "is_premium": 0}

    def set_user_block(self, user_id: int, blocked: bool = True) -> bool:
        try:
            before = self.get_user(user_id)
            self._execute("UPDATE users SET is_blocked = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (1 if blocked else 0, user_id), commit=True)
            self._record_admin_action(None, "set_user_block", user_id, "users", "is_blocked", before.get("is_blocked") if before else None, 1 if blocked else 0)
            return True
        except Exception as e:
            self.record_error("set_user_block", e)
            return False

    def adjust_balance(self, user_id: int, amount: int, allow_negative: bool = False) -> bool:
        try:
            with self._write_lock:
                with self._transaction() as conn:
                    cur = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
                    row = cur.fetchone()
                    if not row:
                        return False
                    current = int(row["balance"] or 0)
                    new_balance = current + int(amount)
                    if new_balance < 0 and not allow_negative:
                        return False
                    conn.execute("UPDATE users SET balance = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (new_balance, user_id))
                    conn.execute(
                        """
                        INSERT INTO wallet_logs (user_id, direction, amount, balance_before, balance_after, reason, ref_type, ref_id, extra_data)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}')
                        """,
                        (user_id, "credit" if amount >= 0 else "debit", abs(int(amount)), current, new_balance, "balance_adjust", "manual", None),
                    )
            logger.info(f"💰 [DB] balance user={user_id} delta={amount}")
            return True
        except Exception as e:
            self.record_error("adjust_balance", e)
            return False

    def freeze_balance(self, user_id: int, amount: int) -> bool:
        try:
            amount = int(amount)
            with self._write_lock:
                with self._transaction() as conn:
                    row = conn.execute("SELECT balance, frozen_balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
                    if not row:
                        return False
                    balance = int(row["balance"] or 0)
                    frozen = int(row["frozen_balance"] or 0)
                    if balance < amount:
                        return False
                    conn.execute(
                        "UPDATE users SET balance = balance - ?, frozen_balance = frozen_balance + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (amount, amount, user_id),
                    )
                    conn.execute(
                        "INSERT INTO wallet_logs (user_id, direction, amount, balance_before, balance_after, reason, ref_type, ref_id, extra_data) VALUES (?, 'freeze', ?, ?, ?, ?, ?, ?, '{}')",
                        (user_id, amount, balance, balance - amount, "freeze_balance", "balance", None, {}),
                    )
            return True
        except Exception as e:
            self.record_error("freeze_balance", e)
            return False

    def unfreeze_balance(self, user_id: int, amount: int) -> bool:
        try:
            amount = int(amount)
            with self._write_lock:
                with self._transaction() as conn:
                    row = conn.execute("SELECT balance, frozen_balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
                    if not row:
                        return False
                    balance = int(row["balance"] or 0)
                    frozen = int(row["frozen_balance"] or 0)
                    if frozen < amount:
                        return False
                    conn.execute(
                        "UPDATE users SET balance = balance + ?, frozen_balance = frozen_balance - ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (amount, amount, user_id),
                    )
                    conn.execute(
                        "INSERT INTO wallet_logs (user_id, direction, amount, balance_before, balance_after, reason, ref_type, ref_id, extra_data) VALUES (?, 'unfreeze', ?, ?, ?, ?, ?, ?, '{}')",
                        (user_id, amount, balance, balance + amount, "unfreeze_balance", "balance", None, {}),
                    )
            return True
        except Exception as e:
            self.record_error("unfreeze_balance", e)
            return False

    def add_points(self, user_id: int, points: int) -> bool:
        try:
            self._execute("UPDATE users SET points = COALESCE(points,0) + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(points), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("add_points", e)
            return False

    def add_stars(self, user_id: int, stars: int) -> bool:
        try:
            self._execute("UPDATE users SET stars = COALESCE(stars,0) + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(stars), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("add_stars", e)
            return False

    def set_vip(self, user_id: int, value: bool = True) -> bool:
        try:
            self._execute("UPDATE users SET is_vip = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (1 if value else 0, user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("set_vip", e)
            return False

    def set_premium(self, user_id: int, value: bool = True, days: Optional[int] = None) -> bool:
        try:
            premium_until = None
            if value and days:
                premium_until = (datetime.utcnow() + timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            self._execute(
                """
                UPDATE users
                SET is_premium = ?,
                    premium_until = ?,
                    last_active = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (1 if value else 0, premium_until, user_id),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("set_premium", e)
            return False

    def extend_premium(self, user_id: int, days: int = 30) -> bool:
        try:
            row = self._fetchone("SELECT premium_until FROM users WHERE user_id = ?", (user_id,))
            if not row:
                return False
            base = datetime.utcnow()
            if row["premium_until"]:
                try:
                    base = datetime.strptime(row["premium_until"], "%Y-%m-%d %H:%M:%S")
                    if base < datetime.utcnow():
                        base = datetime.utcnow()
                except Exception:
                    base = datetime.utcnow()
            until = (base + timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
            self._execute("UPDATE users SET is_premium = 1, premium_until = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (until, user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("extend_premium", e)
            return False

    def adjust_referrals(self, user_id: int, delta: int = 1) -> bool:
        try:
            self._execute("UPDATE users SET referrals_count = COALESCE(referrals_count,0) + ? WHERE user_id = ?", (int(delta), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("adjust_referrals", e)
            return False

    def increment_user_ads_posted(self, user_id: int, delta: int = 1) -> bool:
        try:
            self._execute("UPDATE users SET total_ads_posted = COALESCE(total_ads_posted,0) + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(delta), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("increment_user_ads_posted", e)
            return False

    def increment_user_ads_published(self, user_id: int, delta: int = 1) -> bool:
        try:
            self._execute("UPDATE users SET total_ads_published = COALESCE(total_ads_published,0) + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(delta), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("increment_user_ads_published", e)
            return False

    def increment_user_searches(self, user_id: int, delta: int = 1) -> bool:
        try:
            self._execute("UPDATE users SET total_searches = COALESCE(total_searches,0) + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(delta), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("increment_user_searches", e)
            return False

    def increment_user_messages(self, user_id: int, delta: int = 1) -> bool:
        try:
            self._execute("UPDATE users SET total_messages = COALESCE(total_messages,0) + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(delta), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("increment_user_messages", e)
            return False

    def update_user_extra_data(self, user_id: int, data: Dict[str, Any]) -> bool:
        try:
            row = self._fetchone("SELECT extra_data FROM users WHERE user_id = ?", (user_id,))
            if not row:
                return False
            current = self._json_loads(row["extra_data"], {})
            current.update(data or {})
            self._execute("UPDATE users SET extra_data = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (self._json_dumps(current), user_id), commit=True)
            return True
        except Exception as e:
            self.record_error("update_user_extra_data", e)
            return False

    # ------------------------------------------------------------------
    # الإعلانات
    # ------------------------------------------------------------------
    def _next_serial(self, conn: sqlite3.Connection) -> str:
        cur = conn.execute("SELECT value FROM settings WHERE key = 'ad_counter'")
        row = cur.fetchone()
        current = int(row["value"]) if row and row["value"] is not None else self.cfg.ad_counter_start
        new_value = current + 1
        conn.execute("UPDATE settings SET value = ? WHERE key = 'ad_counter'", (str(new_value),))
        return f"#{new_value}"

    def save_ad_full(
        self,
        owner_id: int,
        ad_type: str,
        deal_side: str,
        name: str,
        field: str,
        category: str = "",
        city: str = "",
        price_text: str = "",
        price_value: Optional[float] = None,
        description: str = "",
        photo_id: str = "",
        status_tag: str = "",
        source_chat_id: Optional[int] = None,
        source_message_id: Optional[int] = None,
        source_caption: str = "",
        source_media_group_id: str = "",
        search_text: Optional[str] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        حفظ إعلان كامل + توليد رقم تسلسلي.
        يرجع serial_id مثل #1001
        """
        try:
            ad_type = self._safe_text(ad_type, 64)
            deal_side = self._safe_text(deal_side, 32)
            name = self._safe_text(name, 255)
            field = self._safe_text(field, 128)
            category = self._safe_text(category, 128)
            city = self._safe_text(city, 128)
            price_text = self._safe_text(price_text, 128)
            description = self._safe_text(description, 12000)
            photo_id = self._safe_text(photo_id, 256)
            status_tag = self._safe_text(status_tag, 64)
            source_caption = self._safe_text(source_caption, 4000)
            source_media_group_id = self._safe_text(source_media_group_id, 128)
            price_value = self._safe_float(price_value, None)

            payload = self._json_dumps(extra_data or {})
            if search_text is None:
                search_text = " ".join([
                    ad_type, deal_side, name, field, category, city, price_text,
                    description, status_tag, source_caption
                ])
            search_text = self._normalize(search_text)
            keywords = self._extract_keywords(ad_type, deal_side, name, field, category, city, description, status_tag, source_caption)

            with self._write_lock:
                with self._transaction() as conn:
                    serial_id = self._next_serial(conn)
                    conn.execute(
                        """
                        INSERT INTO ads (
                            serial_id, owner_id, ad_type, deal_side, name, field, category, city,
                            price_text, price_value, description, keywords, search_text, photo_id,
                            source_chat_id, source_message_id, source_caption, source_media_group_id,
                            status_tag, is_active, views_count, saved_count, extra_data, created_at, updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?,
                            ?, 1, 0, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """,
                        (
                            serial_id, owner_id, ad_type, deal_side, name, field, category, city,
                            price_text, price_value, description, keywords, search_text, photo_id,
                            source_chat_id, source_message_id, source_caption, source_media_group_id,
                            status_tag, payload
                        ),
                    )
                    conn.execute("UPDATE users SET total_ads_posted = COALESCE(total_ads_posted,0) + 1, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (owner_id,))
                    conn.execute("UPDATE stats SET stat_value = stat_value + 1 WHERE stat_key = 'total_global_ads'")

            logger.info(f"✅ [DB] حفظ إعلان serial={serial_id} owner={owner_id} type={ad_type} field={field} city={city} price={price_text}")
            self.record_event(
                serial_id,
                "saved",
                details=self._safe_text(f"owner={owner_id}, ad_type={ad_type}, deal_side={deal_side}, field={field}, category={category}", 2000),
                source_chat_id=source_chat_id,
                source_message_id=source_message_id,
            )
            return serial_id
        except Exception as e:
            self.record_error("save_ad_full", e)
            return None

    def attach_publication(self, serial_id: str, channel_chat_id: int, channel_message_id: int) -> bool:
        try:
            self._execute(
                """
                UPDATE ads
                SET channel_chat_id = ?,
                    channel_message_id = ?,
                    published_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE serial_id = ?
                """,
                (channel_chat_id, channel_message_id, serial_id),
                commit=True,
            )
            owner_id = self.get_ad_owner_id(serial_id)
            if owner_id:
                self.increment_user_ads_published(owner_id, 1)
            self._update_stat("total_published_ads", 1)
            self.record_event(serial_id, "published", details="published to channel", channel_chat_id=channel_chat_id, channel_message_id=channel_message_id)
            return True
        except Exception as e:
            self.record_error("attach_publication", e, serial_id=serial_id)
            return False

    def get_ad_owner_id(self, serial_id: str) -> Optional[int]:
        try:
            row = self._fetchone("SELECT owner_id FROM ads WHERE serial_id = ?", (serial_id,))
            return int(row["owner_id"]) if row and row["owner_id"] is not None else None
        except Exception as e:
            self.record_error("get_ad_owner_id", e, serial_id=serial_id)
            return None

    def get_ad_by_serial(self, serial_id: str) -> Optional[Dict[str, Any]]:
        try:
            row = self._fetchone("SELECT * FROM ads WHERE serial_id = ?", (serial_id,))
            return dict(row) if row else None
        except Exception as e:
            self.record_error("get_ad_by_serial", e, serial_id=serial_id)
            return None

    def get_ad_by_channel_message(self, channel_message_id: int) -> Optional[Dict[str, Any]]:
        try:
            row = self._fetchone("SELECT * FROM ads WHERE channel_message_id = ?", (channel_message_id,))
            return dict(row) if row else None
        except Exception as e:
            self.record_error("get_ad_by_channel_message", e)
            return None

    def get_recent_ads(self, limit: int = 10, only_active: bool = True) -> List[Dict[str, Any]]:
        try:
            query = "SELECT * FROM ads"
            params: List[Any] = []
            if only_active:
                query += " WHERE is_active = 1"
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(int(limit))
            rows = self._fetchall(query, params)
            return [dict(r) for r in rows]
        except Exception as e:
            self.record_error("get_recent_ads", e)
            return []

    def get_ads_by_owner(self, owner_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            rows = self._fetchall("SELECT * FROM ads WHERE owner_id = ? ORDER BY created_at DESC LIMIT ?", (owner_id, int(limit)))
            return [dict(r) for r in rows]
        except Exception as e:
            self.record_error("get_ads_by_owner", e)
            return []

    def increment_views(self, serial_id: str) -> bool:
        try:
            self._execute("UPDATE ads SET views_count = COALESCE(views_count,0) + 1, updated_at = CURRENT_TIMESTAMP WHERE serial_id = ?", (serial_id,), commit=True)
            return True
        except Exception as e:
            self.record_error("increment_views", e, serial_id=serial_id)
            return False

    def increment_saved(self, serial_id: str) -> bool:
        try:
            self._execute("UPDATE ads SET saved_count = COALESCE(saved_count,0) + 1, updated_at = CURRENT_TIMESTAMP WHERE serial_id = ?", (serial_id,), commit=True)
            return True
        except Exception as e:
            self.record_error("increment_saved", e, serial_id=serial_id)
            return False

    def mark_ad_inactive(self, serial_id: str) -> bool:
        try:
            self._execute("UPDATE ads SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE serial_id = ?", (serial_id,), commit=True)
            self.record_event(serial_id, "inactive", details="marked inactive")
            return True
        except Exception as e:
            self.record_error("mark_ad_inactive", e, serial_id=serial_id)
            return False

    def mark_ad_active(self, serial_id: str) -> bool:
        try:
            self._execute("UPDATE ads SET is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE serial_id = ?", (serial_id,), commit=True)
            self.record_event(serial_id, "active", details="marked active")
            return True
        except Exception as e:
            self.record_error("mark_ad_active", e, serial_id=serial_id)
            return False

    def delete_ad(self, serial_id: str) -> bool:
        try:
            self._execute("DELETE FROM ads WHERE serial_id = ?", (serial_id,), commit=True)
            self.record_event(serial_id, "deleted", details="ad removed from db")
            return True
        except Exception as e:
            self.record_error("delete_ad", e, serial_id=serial_id)
            return False

    # ------------------------------------------------------------------
    # البحث
    # ------------------------------------------------------------------
    def search_ads_smart(
        self,
        text: Optional[str] = None,
        ad_type: Optional[str] = None,
        field: Optional[str] = None,
        category: Optional[str] = None,
        city: Optional[str] = None,
        limit: int = 10,
        only_active: bool = True,
        owner_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        try:
            self._update_stat("total_searches", 1)
            if owner_id:
                self.increment_user_searches(owner_id, 1)

            normalized_text = self._normalize(text) if text else ""
            params: List[Any] = []

            if normalized_text:
                try:
                    fts_query = normalized_text.replace('"', ' ').strip()
                    sql = """
                        SELECT ads.*
                        FROM ads_fts
                        JOIN ads ON ads_fts.rowid = ads.ad_id
                        WHERE ads_fts MATCH ?
                    """
                    params.append(fts_query)
                    conditions = []
                    if only_active:
                        conditions.append("ads.is_active = 1")
                    if ad_type and ad_type != "ALL":
                        conditions.append("ads.ad_type LIKE ?")
                        params.append(f"%{ad_type}%")
                    if field and field != "ALL":
                        conditions.append("ads.field LIKE ?")
                        params.append(f"%{field}%")
                    if category and category != "ALL":
                        conditions.append("ads.category LIKE ?")
                        params.append(f"%{category}%")
                    if city and city != "ALL":
                        conditions.append("ads.city LIKE ?")
                        params.append(f"%{city}%")
                    if owner_id:
                        conditions.append("ads.owner_id = ?")
                        params.append(owner_id)
                    if conditions:
                        sql += " AND " + " AND ".join(conditions)
                    sql += """
                        ORDER BY
                            CASE WHEN ads.status_tag LIKE '%🔥%' THEN 0 ELSE 1 END,
                            ads.created_at DESC
                        LIMIT ?
                    """
                    params.append(int(limit))
                    rows = self._fetchall(sql, params)
                    return [dict(r) for r in rows]
                except Exception:
                    pass

            query = "SELECT * FROM ads WHERE 1=1"
            if only_active:
                query += " AND is_active = 1"
            if ad_type and ad_type != "ALL":
                query += " AND ad_type LIKE ?"
                params.append(f"%{ad_type}%")
            if field and field != "ALL":
                query += " AND field LIKE ?"
                params.append(f"%{field}%")
            if category and category != "ALL":
                query += " AND category LIKE ?"
                params.append(f"%{category}%")
            if city and city != "ALL":
                query += " AND city LIKE ?"
                params.append(f"%{city}%")
            if owner_id:
                query += " AND owner_id = ?"
                params.append(owner_id)
            if normalized_text:
                for token in [w for w in normalized_text.split() if len(w) >= 2][:8]:
                    like = f"%{token}%"
                    query += """
                        AND (
                            name LIKE ?
                            OR field LIKE ?
                            OR category LIKE ?
                            OR city LIKE ?
                            OR description LIKE ?
                            OR keywords LIKE ?
                            OR search_text LIKE ?
                            OR serial_id LIKE ?
                        )
                    """
                    params.extend([like, like, like, like, like, like, like, like])

            query += """
                ORDER BY
                    CASE WHEN status_tag LIKE '%🔥%' THEN 0 ELSE 1 END,
                    created_at DESC
                LIMIT ?
            """
            params.append(int(limit))
            rows = self._fetchall(query, params)
            return [dict(r) for r in rows]
        except Exception as e:
            self.record_error("search_ads_smart", e)
            return []

    # ------------------------------------------------------------------
    # الإحصائيات / التتبع
    # ------------------------------------------------------------------
    def _update_stat(self, key: str, delta: int) -> bool:
        try:
            self._execute(
                """
                INSERT INTO stats (stat_key, stat_value)
                VALUES (?, ?)
                ON CONFLICT(stat_key)
                DO UPDATE SET stat_value = stat_value + excluded.stat_value
                """,
                (key, int(delta)),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error(f"_update_stat:{key}", e)
            return False

    def get_stat(self, key: str) -> int:
        try:
            row = self._fetchone("SELECT stat_value FROM stats WHERE stat_key = ?", (key,))
            return int(row["stat_value"]) if row else 0
        except Exception as e:
            self.record_error("get_stat", e)
            return 0

    def get_dashboard_stats(self) -> Dict[str, int]:
        try:
            active_row = self._fetchone("SELECT COUNT(*) AS c FROM ads WHERE is_active = 1")
            all_row = self._fetchone("SELECT COUNT(*) AS c FROM ads")
            return {
                "total_users": self.get_stat("total_users"),
                "total_global_ads": self.get_stat("total_global_ads"),
                "total_searches": self.get_stat("total_searches"),
                "total_published_ads": self.get_stat("total_published_ads"),
                "total_errors": self.get_stat("total_errors"),
                "total_payments": self.get_stat("total_payments"),
                "total_notifications": self.get_stat("total_notifications"),
                "active_ads": int(active_row["c"]) if active_row else 0,
                "all_ads": int(all_row["c"]) if all_row else 0,
            }
        except Exception as e:
            self.record_error("get_dashboard_stats", e)
            return {}

    # ------------------------------------------------------------------
    # التكرار والحماية
    # ------------------------------------------------------------------
    def is_duplicate_message(self, msg_hash: str) -> bool:
        try:
            row = self._fetchone("SELECT 1 FROM duplicates WHERE msg_hash = ?", (msg_hash,))
            return bool(row)
        except Exception as e:
            self.record_error("is_duplicate_message", e)
            return False

    def register_duplicate_message(self, msg_hash: str, chat_id: Optional[int] = None, message_id: Optional[int] = None) -> bool:
        try:
            self._execute("INSERT OR IGNORE INTO duplicates (msg_hash, chat_id, message_id) VALUES (?, ?, ?)", (msg_hash, chat_id, message_id), commit=True)
            return True
        except Exception as e:
            self.record_error("register_duplicate_message", e)
            return False

    # ------------------------------------------------------------------
    # المدفوعات / النجوم / العملات / التحويلات
    # ------------------------------------------------------------------
    def generate_invoice_no(self, prefix: str = "INV") -> str:
        stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        rnd = hashlib.sha1(os.urandom(12)).hexdigest()[:8].upper()
        return f"{prefix}-{stamp}-{rnd}"

    def create_payment(
        self,
        user_id: int,
        purpose: str,
        provider: str,
        currency: str,
        amount: float,
        amount_credits: float = 0,
        external_ref: str = "",
        details: Optional[Dict[str, Any]] = None,
        invoice_no: Optional[str] = None,
        status: str = "pending",
    ) -> Optional[str]:
        try:
            invoice_no = invoice_no or self.generate_invoice_no()
            self._execute(
                """
                INSERT INTO payments (
                    invoice_no, user_id, purpose, provider, currency,
                    amount, amount_credits, external_ref, status, details,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    invoice_no,
                    user_id,
                    self._safe_text(purpose, 128),
                    self._safe_text(provider, 64),
                    self._safe_text(currency, 16),
                    float(amount),
                    float(amount_credits),
                    self._safe_text(external_ref, 256),
                    self._safe_text(status, 32),
                    self._json_dumps(details or {}),
                ),
                commit=True,
            )
            self._update_stat("total_payments", 1)
            return invoice_no
        except Exception as e:
            self.record_error("create_payment", e)
            return None

    def get_payment(self, invoice_no: str) -> Optional[Dict[str, Any]]:
        try:
            row = self._fetchone("SELECT * FROM payments WHERE invoice_no = ?", (invoice_no,))
            return dict(row) if row else None
        except Exception as e:
            self.record_error("get_payment", e)
            return None

    def update_payment_status(
        self,
        invoice_no: str,
        status: str,
        admin_id: Optional[int] = None,
        notes: str = "",
        confirmed: bool = False,
    ) -> bool:
        try:
            before = self.get_payment(invoice_no)
            self._execute(
                """
                UPDATE payments
                SET status = ?, updated_at = CURRENT_TIMESTAMP,
                    confirmed_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE confirmed_at END
                WHERE invoice_no = ?
                """,
                (self._safe_text(status, 32), 1 if confirmed else 0, invoice_no),
                commit=True,
            )
            self._record_admin_action(admin_id, "update_payment_status", before.get("user_id") if before else None, "payments", invoice_no, before.get("status") if before else None, status, notes)
            return True
        except Exception as e:
            self.record_error("update_payment_status", e)
            return False

    def confirm_payment_and_credit(
        self,
        invoice_no: str,
        admin_id: Optional[int] = None,
        notes: str = "",
        grant_balance: int = 0,
        grant_points: int = 0,
        grant_stars: int = 0,
        grant_premium_days: int = 0,
    ) -> bool:
        try:
            payment = self.get_payment(invoice_no)
            if not payment:
                return False
            user_id = int(payment["user_id"])
            with self._write_lock:
                with self._transaction() as conn:
                    row = conn.execute("SELECT balance, points, stars FROM users WHERE user_id = ?", (user_id,)).fetchone()
                    if not row:
                        return False
                    before_balance = int(row["balance"] or 0)
                    before_points = int(row["points"] or 0)
                    before_stars = int(row["stars"] or 0)
                    if grant_balance:
                        conn.execute("UPDATE users SET balance = balance + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(grant_balance), user_id))
                    if grant_points:
                        conn.execute("UPDATE users SET points = points + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(grant_points), user_id))
                    if grant_stars:
                        conn.execute("UPDATE users SET stars = stars + ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (int(grant_stars), user_id))
                    if grant_premium_days:
                        current = conn.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
                        base = datetime.utcnow()
                        if current and current["premium_until"]:
                            try:
                                dt = datetime.strptime(current["premium_until"], "%Y-%m-%d %H:%M:%S")
                                if dt > base:
                                    base = dt
                            except Exception:
                                pass
                        premium_until = (base + timedelta(days=int(grant_premium_days))).strftime("%Y-%m-%d %H:%M:%S")
                        conn.execute("UPDATE users SET is_premium = 1, premium_until = ?, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (premium_until, user_id))
                    conn.execute(
                        "UPDATE payments SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE invoice_no = ?",
                        (invoice_no,),
                    )
                    conn.execute(
                        "INSERT INTO wallet_logs (user_id, direction, amount, balance_before, balance_after, reason, ref_type, ref_id, extra_data) VALUES (?, 'credit', ?, ?, ?, ?, ?, ?, ?)",
                        (
                            user_id,
                            float(grant_balance),
                            before_balance,
                            before_balance + int(grant_balance),
                            "payment_confirmed",
                            payment.get("provider", "payment"),
                            invoice_no,
                            self._json_dumps({"grant_points": grant_points, "grant_stars": grant_stars, "grant_premium_days": grant_premium_days}),
                        ),
                    )
            self._record_admin_action(admin_id, "confirm_payment_and_credit", user_id, "payments", invoice_no, payment.get("status"), "confirmed", notes)
            return True
        except Exception as e:
            self.record_error("confirm_payment_and_credit", e)
            return False

    def add_wallet_log(
        self,
        user_id: int,
        direction: str,
        amount: float,
        balance_before: Optional[float] = None,
        balance_after: Optional[float] = None,
        reason: str = "",
        ref_type: str = "",
        ref_id: str = "",
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> bool:
        try:
            self._execute(
                """
                INSERT INTO wallet_logs (
                    user_id, direction, amount, balance_before, balance_after,
                    reason, ref_type, ref_id, extra_data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    user_id,
                    self._safe_text(direction, 16),
                    float(amount),
                    balance_before,
                    balance_after,
                    self._safe_text(reason, 255),
                    self._safe_text(ref_type, 64),
                    self._safe_text(ref_id, 128),
                    self._json_dumps(extra_data or {}),
                ),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("add_wallet_log", e)
            return False

    def get_wallet_logs(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            rows = self._fetchall("SELECT * FROM wallet_logs WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, int(limit)))
            return [dict(r) for r in rows]
        except Exception as e:
            self.record_error("get_wallet_logs", e)
            return []

    def create_star_payment(self, user_id: int, purpose: str, stars: int, details: Optional[Dict[str, Any]] = None) -> Optional[str]:
        return self.create_payment(
            user_id=user_id,
            purpose=purpose,
            provider="telegram_stars",
            currency="STARS",
            amount=float(stars),
            amount_credits=0,
            external_ref="",
            details=details or {},
        )

    def create_crypto_payment(
        self,
        user_id: int,
        purpose: str,
        provider: str,
        currency: str,
        amount: float,
        external_ref: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        return self.create_payment(
            user_id=user_id,
            purpose=purpose,
            provider=provider,  # binance / wallet_bot / usdt / btc / eth ...
            currency=currency,
            amount=amount,
            amount_credits=amount,
            external_ref=external_ref,
            details=details or {},
        )

    # ------------------------------------------------------------------
    # الإشعارات
    # ------------------------------------------------------------------
    def add_notification(
        self,
        user_id: int,
        notif_type: str,
        title: str,
        body: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: int = 0,
        scheduled_at: Optional[str] = None,
    ) -> Optional[int]:
        try:
            self._execute(
                """
                INSERT INTO notifications (
                    user_id, notif_type, title, body, payload,
                    priority, scheduled_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    user_id,
                    self._safe_text(notif_type, 64),
                    self._safe_text(title, 255),
                    self._safe_text(body, 4000),
                    self._json_dumps(payload or {}),
                    int(priority),
                    scheduled_at,
                ),
                commit=True,
            )
            self._update_stat("total_notifications", 1)
            row = self._fetchone("SELECT last_insert_rowid() AS id")
            return int(row["id"]) if row and row["id"] is not None else None
        except Exception as e:
            self.record_error("add_notification", e)
            return None

    def get_pending_notifications(self, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            rows = self._fetchall(
                """
                SELECT * FROM notifications
                WHERE is_read = 0
                  AND (scheduled_at IS NULL OR scheduled_at <= CURRENT_TIMESTAMP)
                ORDER BY priority DESC, id ASC
                LIMIT ?
                """,
                (int(limit),),
            )
            return [dict(r) for r in rows]
        except Exception as e:
            self.record_error("get_pending_notifications", e)
            return []

    def mark_notification_read(self, notif_id: int) -> bool:
        try:
            self._execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (notif_id,), commit=True)
            return True
        except Exception as e:
            self.record_error("mark_notification_read", e)
            return False

    # ------------------------------------------------------------------
    # نصوص الواجهة
    # ------------------------------------------------------------------
    def set_ui_string(self, key: str, value: str, lang: str = "ar", category: str = "general") -> bool:
        try:
            self._execute(
                """
                INSERT INTO ui_strings (string_key, value, lang, category, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(string_key)
                DO UPDATE SET value = excluded.value, lang = excluded.lang, category = excluded.category, updated_at = CURRENT_TIMESTAMP
                """,
                (self._safe_text(key, 128), self._safe_text(value, 12000), self._safe_text(lang, 16), self._safe_text(category, 64)),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("set_ui_string", e)
            return False

    def get_ui_string(self, key: str, default: str = "", lang: str = "ar") -> str:
        try:
            row = self._fetchone("SELECT value FROM ui_strings WHERE string_key = ? AND lang = ?", (key, lang))
            if row and row["value"] is not None:
                return str(row["value"])
            row = self._fetchone("SELECT value FROM ui_strings WHERE string_key = ? ORDER BY CASE WHEN lang = ? THEN 0 ELSE 1 END LIMIT 1", (key, lang))
            return str(row["value"]) if row and row["value"] is not None else default
        except Exception as e:
            self.record_error("get_ui_string", e)
            return default

    # ------------------------------------------------------------------
    # الطوابير / المحادثات / الطلبات
    # ------------------------------------------------------------------
    def enqueue_message(
        self,
        sender_id: int,
        recipient_id: Optional[int],
        message_type: str,
        payload: Any,
        queue_name: str = "default",
        delay_seconds: float = 0,
    ) -> Optional[int]:
        try:
            next_time = (datetime.utcnow() + timedelta(seconds=float(delay_seconds))).strftime("%Y-%m-%d %H:%M:%S")
            self._execute(
                """
                INSERT INTO message_queue (
                    queue_name, sender_id, recipient_id, message_type, payload,
                    status, attempts, next_attempt_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    self._safe_text(queue_name, 64),
                    sender_id,
                    recipient_id,
                    self._safe_text(message_type, 64),
                    self._json_dumps(payload),
                    next_time,
                ),
                commit=True,
            )
            self._update_stat("total_queue_items", 1)
            row = self._fetchone("SELECT last_insert_rowid() AS id")
            return int(row["id"]) if row and row["id"] is not None else None
        except Exception as e:
            self.record_error("enqueue_message", e)
            return None

    def fetch_due_queue_items(self, queue_name: str = "default", limit: int = 20) -> List[Dict[str, Any]]:
        try:
            rows = self._fetchall(
                """
                SELECT * FROM message_queue
                WHERE queue_name = ?
                  AND status IN ('pending', 'retry')
                  AND next_attempt_at <= CURRENT_TIMESTAMP
                ORDER BY id ASC
                LIMIT ?
                """,
                (self._safe_text(queue_name, 64), int(limit)),
            )
            return [dict(r) for r in rows]
        except Exception as e:
            self.record_error("fetch_due_queue_items", e)
            return []

    def mark_queue_done(self, queue_id: int) -> bool:
        try:
            self._execute("UPDATE message_queue SET status = 'done', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (queue_id,), commit=True)
            return True
        except Exception as e:
            self.record_error("mark_queue_done", e)
            return False

    def mark_queue_retry(self, queue_id: int, delay_seconds: int = 10) -> bool:
        try:
            next_time = (datetime.utcnow() + timedelta(seconds=int(delay_seconds))).strftime("%Y-%m-%d %H:%M:%S")
            self._execute(
                "UPDATE message_queue SET status = 'retry', attempts = attempts + 1, next_attempt_at = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (next_time, queue_id),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("mark_queue_retry", e)
            return False

    def create_thread(self, thread_type: str, created_by: int, owner_id: Optional[int] = None, target_id: Optional[int] = None, meta: Optional[Dict[str, Any]] = None) -> Optional[int]:
        try:
            self._execute(
                """
                INSERT INTO chat_threads (thread_type, created_by, owner_id, target_id, status, meta, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'open', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (self._safe_text(thread_type, 64), created_by, owner_id, target_id, self._json_dumps(meta or {})),
                commit=True,
            )
            self._update_stat("total_threads", 1)
            row = self._fetchone("SELECT last_insert_rowid() AS id")
            return int(row["id"]) if row and row["id"] is not None else None
        except Exception as e:
            self.record_error("create_thread", e)
            return None

    def add_thread_message(
        self,
        thread_id: int,
        sender_id: int,
        recipient_id: Optional[int],
        message_type: str,
        content: str = "",
        file_id: str = "",
        reply_to_message_id: Optional[int] = None,
        message_id: Optional[int] = None,
    ) -> Optional[int]:
        try:
            self._execute(
                """
                INSERT INTO chat_messages (
                    thread_id, sender_id, recipient_id, message_id, message_type,
                    content, file_id, reply_to_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    thread_id,
                    sender_id,
                    recipient_id,
                    message_id,
                    self._safe_text(message_type, 64),
                    self._safe_text(content, 12000),
                    self._safe_text(file_id, 256),
                    reply_to_message_id,
                ),
                commit=True,
            )
            self.increment_user_messages(sender_id, 1)
            row = self._fetchone("SELECT last_insert_rowid() AS id")
            return int(row["id"]) if row and row["id"] is not None else None
        except Exception as e:
            self.record_error("add_thread_message", e)
            return None

    def close_thread(self, thread_id: int) -> bool:
        try:
            self._execute("UPDATE chat_threads SET status = 'closed', updated_at = CURRENT_TIMESTAMP WHERE thread_id = ?", (thread_id,), commit=True)
            return True
        except Exception as e:
            self.record_error("close_thread", e)
            return False

    # ------------------------------------------------------------------
    # الكاش المؤقت
    # ------------------------------------------------------------------
    def cache_set(self, cache_key: str, cache_value: Any, ttl_seconds: int = 300) -> bool:
        try:
            expires_at = (datetime.utcnow() + timedelta(seconds=int(ttl_seconds))).strftime("%Y-%m-%d %H:%M:%S")
            self._execute(
                """
                INSERT INTO temp_cache (cache_key, cache_value, expires_at, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(cache_key)
                DO UPDATE SET cache_value = excluded.cache_value, expires_at = excluded.expires_at
                """,
                (self._safe_text(cache_key, 128), self._json_dumps(cache_value), expires_at),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("cache_set", e)
            return False

    def cache_get(self, cache_key: str, default: Any = None) -> Any:
        try:
            row = self._fetchone(
                "SELECT cache_value, expires_at FROM temp_cache WHERE cache_key = ?",
                (self._safe_text(cache_key, 128),),
            )
            if not row:
                return default
            if row["expires_at"]:
                exp = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
                if exp < datetime.utcnow():
                    self.cache_delete(cache_key)
                    return default
            try:
                return json.loads(row["cache_value"])
            except Exception:
                return row["cache_value"]
        except Exception as e:
            self.record_error("cache_get", e)
            return default

    def cache_delete(self, cache_key: str) -> bool:
        try:
            self._execute("DELETE FROM temp_cache WHERE cache_key = ?", (self._safe_text(cache_key, 128),), commit=True)
            return True
        except Exception as e:
            self.record_error("cache_delete", e)
            return False

    def purge_expired_cache(self) -> int:
        try:
            return self._execute("DELETE FROM temp_cache WHERE expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP", commit=True)
        except Exception as e:
            self.record_error("purge_expired_cache", e)
            return 0

    # ------------------------------------------------------------------
    # وسائط / file_id / روابط
    # ------------------------------------------------------------------
    def save_media(
        self,
        file_id: str,
        media_type: str,
        owner_id: Optional[int] = None,
        file_unique_id: str = "",
        source_chat_id: Optional[int] = None,
        source_message_id: Optional[int] = None,
        url: str = "",
        caption: str = "",
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        try:
            media_hash = self._make_hash(file_id, file_unique_id, media_type, url, caption)
            self._execute(
                """
                INSERT OR IGNORE INTO media_store (
                    media_hash, file_id, file_unique_id, media_type, owner_id,
                    source_chat_id, source_message_id, url, caption, extra_data, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    media_hash,
                    self._safe_text(file_id, 256),
                    self._safe_text(file_unique_id, 128),
                    self._safe_text(media_type, 32),
                    owner_id,
                    source_chat_id,
                    source_message_id,
                    self._safe_text(url, 1000),
                    self._safe_text(caption, 4000),
                    self._json_dumps(extra_data or {}),
                ),
                commit=True,
            )
            row = self._fetchone("SELECT id FROM media_store WHERE media_hash = ?", (media_hash,))
            return int(row["id"]) if row and row["id"] is not None else None
        except Exception as e:
            self.record_error("save_media", e)
            return None

    def get_media_by_file_id(self, file_id: str) -> Optional[Dict[str, Any]]:
        try:
            row = self._fetchone("SELECT * FROM media_store WHERE file_id = ? ORDER BY id DESC LIMIT 1", (self._safe_text(file_id, 256),))
            return dict(row) if row else None
        except Exception as e:
            self.record_error("get_media_by_file_id", e)
            return None

    # ------------------------------------------------------------------
    # الميزات / الصلاحيات / الإعدادات
    # ------------------------------------------------------------------
    def set_setting(self, key: str, value: str) -> bool:
        try:
            self._execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (self._safe_text(key, 128), self._safe_text(value, 12000)),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("set_setting", e)
            return False

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        try:
            row = self._fetchone("SELECT value FROM settings WHERE key = ?", (self._safe_text(key, 128),))
            return str(row["value"]) if row and row["value"] is not None else default
        except Exception as e:
            self.record_error("get_setting", e)
            return default

    def set_feature_flag(self, flag_key: str, enabled: bool = True, value: str = "", scope: str = "global") -> bool:
        try:
            self._execute(
                """
                INSERT INTO feature_flags (flag_key, enabled, value, scope, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(flag_key)
                DO UPDATE SET enabled = excluded.enabled, value = excluded.value, scope = excluded.scope, updated_at = CURRENT_TIMESTAMP
                """,
                (self._safe_text(flag_key, 128), 1 if enabled else 0, self._safe_text(value, 2000), self._safe_text(scope, 64)),
                commit=True,
            )
            return True
        except Exception as e:
            self.record_error("set_feature_flag", e)
            return False

    def get_feature_flag(self, flag_key: str, default: bool = False) -> bool:
        try:
            row = self._fetchone("SELECT enabled FROM feature_flags WHERE flag_key = ?", (self._safe_text(flag_key, 128),))
            return bool(int(row["enabled"])) if row else default
        except Exception as e:
            self.record_error("get_feature_flag", e)
            return default

    def grant_admin_power(self, admin_id: int, target_user_id: int, action: str, notes: str = "") -> bool:
        self._record_admin_action(admin_id, action, target_user_id, None, None, None, None, notes)
        return True

    # ------------------------------------------------------------------
    # المدفوعات المحلية / التحويل اليدوي / الموافقة من المشرف
    # ------------------------------------------------------------------
    def create_local_payment_request(
        self,
        user_id: int,
        purpose: str,
        amount: float,
        currency: str = "YER",
        method: str = "local",
        details: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        return self.create_payment(
            user_id=user_id,
            purpose=purpose,
            provider=method,
            currency=currency,
            amount=amount,
            amount_credits=amount,
            external_ref="",
            details=details or {},
        )

    # ------------------------------------------------------------------
    # التصدير / الاستيراد / النسخ الاحتياطي
    # ------------------------------------------------------------------
    def create_backup(self) -> Optional[str]:
        try:
            filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            backup_path = os.path.join(self.backup_dir, filename)
            shutil.copy2(self.db_path, backup_path)
            file_size = os.path.getsize(backup_path)
            sha256 = self._file_sha256(backup_path)
            self._execute(
                "INSERT INTO backups (path, file_size, sha256, notes) VALUES (?, ?, ?, ?)",
                (backup_path, file_size, sha256, "auto"),
                commit=True,
            )
            logger.info(f"✅ [DB] Backup created: {backup_path}")
            return backup_path
        except Exception as e:
            self.record_error("create_backup", e)
            return None

    def _file_sha256(self, path: str) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def restore_backup(self, backup_path: str) -> bool:
        try:
            if not os.path.exists(backup_path):
                return False
            shutil.copy2(backup_path, self.db_path)
            logger.info(f"✅ [DB] Restored backup: {backup_path}")
            return True
        except Exception as e:
            self.record_error("restore_backup", e)
            return False

    def export_table_csv(self, table_name: str, output_path: str) -> Optional[str]:
        try:
            rows = self._fetchall(f"SELECT * FROM {table_name}")
            if not rows:
                return None
            with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(rows[0].keys())
                for row in rows:
                    writer.writerow([row[k] for k in row.keys()])
            return output_path
        except Exception as e:
            self.record_error("export_table_csv", e)
            return None

    def export_all_json(self, output_path: str) -> Optional[str]:
        try:
            data = {}
            for table in (
                "users", "ads", "ad_events", "stats", "settings", "duplicates",
                "payments", "wallet_logs", "notifications", "ui_strings",
                "admin_actions", "message_queue", "chat_threads", "chat_messages",
                "temp_cache", "media_store", "feature_flags", "backups",
            ):
                data[table] = [dict(r) for r in self._fetchall(f"SELECT * FROM {table}")]
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return output_path
        except Exception as e:
            self.record_error("export_all_json", e)
            return None

    def export_table_xlsx(self, table_name: str, output_path: str) -> Optional[str]:
        try:
            try:
                from openpyxl import Workbook
            except Exception:
                return None
            rows = self._fetchall(f"SELECT * FROM {table_name}")
            wb = Workbook()
            ws = wb.active
            ws.title = table_name[:31]
            if rows:
                ws.append(list(rows[0].keys()))
                for row in rows:
                    ws.append([row[k] for k in row.keys()])
            wb.save(output_path)
            return output_path
        except Exception as e:
            self.record_error("export_table_xlsx", e)
            return None

    def export_to_mongodb(self, mongo_uri: str, db_name: str = "telegram_bot", collection_prefix: str = "") -> bool:
        try:
            try:
                from pymongo import MongoClient
            except Exception:
                return False
            client = MongoClient(mongo_uri)
            mongo_db = client[db_name]
            for table in (
                "users", "ads", "ad_events", "stats", "settings", "duplicates",
                "payments", "wallet_logs", "notifications", "ui_strings",
                "admin_actions", "message_queue", "chat_threads", "chat_messages",
                "temp_cache", "media_store", "feature_flags", "backups",
            ):
                coll = mongo_db[f"{collection_prefix}{table}"]
                rows = [dict(r) for r in self._fetchall(f"SELECT * FROM {table}")]
                if rows:
                    coll.insert_many(rows, ordered=False)
            return True
        except Exception as e:
            self.record_error("export_to_mongodb", e)
            return False

    # ------------------------------------------------------------------
    # مساعدات عامة
    # ------------------------------------------------------------------
    def cleanup_expired(self) -> Dict[str, int]:
        result = {}
        try:
            result["cache"] = self.purge_expired_cache()
        except Exception:
            result["cache"] = 0
        return result

    def vacuum(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("VACUUM")
            return True
        except Exception as e:
            self.record_error("vacuum", e)
            return False

    def get_db_path(self) -> str:
        return os.path.abspath(self.db_path)

          
# نفس الاسم حتى لا تنكسر الملفات المرتبطة
db = DatabaseManager()
