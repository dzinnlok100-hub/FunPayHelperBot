"""SQLite layer for the multi-tenant FunPay → Telegram bot."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from .crypto import SecretCipher

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_user_id INTEGER PRIMARY KEY,
    funpay_username TEXT,
    funpay_id INTEGER,
    golden_key_enc BLOB NOT NULL,
    user_agent TEXT,
    created_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    username TEXT NOT NULL,
    chat_id TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages(tg_user_id, ts);

CREATE TABLE IF NOT EXISTS orders (
    tg_user_id INTEGER NOT NULL,
    id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    buyer TEXT NOT NULL,
    price REAL NOT NULL,
    description TEXT,
    status TEXT,
    PRIMARY KEY (tg_user_id, id)
);
CREATE INDEX IF NOT EXISTS idx_orders_user_ts ON orders(tg_user_id, ts);

CREATE TABLE IF NOT EXISTS blocked (
    tg_user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    blocked_at INTEGER NOT NULL,
    PRIMARY KEY (tg_user_id, username)
);

CREATE TABLE IF NOT EXISTS state (
    tg_user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (tg_user_id, key)
);

CREATE TABLE IF NOT EXISTS autoreply_done (
    tg_user_id INTEGER NOT NULL,
    chat_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (tg_user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(tg_user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_templates_user ON templates(tg_user_id);

CREATE TABLE IF NOT EXISTS delivery_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    used_at INTEGER,
    order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_dq_user_used ON delivery_queue(tg_user_id, used);

CREATE TABLE IF NOT EXISTS delivery_log (
    tg_user_id INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    delivered_at INTEGER NOT NULL,
    queue_item_id INTEGER,
    PRIMARY KEY (tg_user_id, order_id)
);
"""


@dataclass
class UserRow:
    tg_user_id: int
    funpay_username: str | None
    funpay_id: int | None
    golden_key: str  # decrypted
    user_agent: str | None
    enabled: bool


class Database:
    def __init__(self, path: Path, cipher: SecretCipher) -> None:
        self.path = path
        self.cipher = cipher

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()
        await self._maybe_migrate_legacy_schema()

    async def _maybe_migrate_legacy_schema(self) -> None:
        """If we detect a v1 (single-tenant) schema, log a warning. We keep both around
        but new code only reads the new tables."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
            )
            row = await cur.fetchone()
            if not row:
                return
            cur = await db.execute("PRAGMA table_info(messages)")
            cols = {r[1] for r in await cur.fetchall()}
            if "tg_user_id" not in cols:
                log.warning(
                    "Detected legacy single-tenant DB schema at %s. "
                    "Drop or move this file before running multi-tenant mode "
                    "(or accept that old stats will be ignored).",
                    self.path,
                )

    # ---- users ----

    async def upsert_user(
        self,
        tg_user_id: int,
        golden_key: str,
        funpay_username: str | None,
        funpay_id: int | None,
        user_agent: str | None,
    ) -> None:
        now = int(time.time())
        enc = self.cipher.encrypt(golden_key)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users
                    (tg_user_id, funpay_username, funpay_id, golden_key_enc,
                     user_agent, created_at, last_seen, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(tg_user_id) DO UPDATE SET
                    funpay_username=excluded.funpay_username,
                    funpay_id=excluded.funpay_id,
                    golden_key_enc=excluded.golden_key_enc,
                    user_agent=excluded.user_agent,
                    last_seen=excluded.last_seen,
                    enabled=1
                """,
                (tg_user_id, funpay_username, funpay_id, enc, user_agent, now, now),
            )
            await db.commit()

    async def set_enabled(self, tg_user_id: int, enabled: bool) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET enabled=?, last_seen=? WHERE tg_user_id=?",
                (1 if enabled else 0, int(time.time()), tg_user_id),
            )
            await db.commit()

    async def touch_last_seen(self, tg_user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET last_seen=? WHERE tg_user_id=?",
                (int(time.time()), tg_user_id),
            )
            await db.commit()

    async def delete_user(self, tg_user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            for table in (
                "messages",
                "orders",
                "blocked",
                "state",
                "autoreply_done",
                "templates",
                "delivery_queue",
                "delivery_log",
                "users",
            ):
                await db.execute(f"DELETE FROM {table} WHERE tg_user_id=?", (tg_user_id,))
            await db.commit()

    async def get_user(self, tg_user_id: int) -> UserRow | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT tg_user_id, funpay_username, funpay_id, golden_key_enc, "
                "user_agent, enabled FROM users WHERE tg_user_id=?",
                (tg_user_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return UserRow(
                tg_user_id=row[0],
                funpay_username=row[1],
                funpay_id=row[2],
                golden_key=self.cipher.decrypt(row[3]),
                user_agent=row[4],
                enabled=bool(row[5]),
            )

    async def list_users(self, only_enabled: bool = True) -> list[UserRow]:
        query = (
            "SELECT tg_user_id, funpay_username, funpay_id, golden_key_enc, "
            "user_agent, enabled FROM users"
        )
        if only_enabled:
            query += " WHERE enabled=1"
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(query)
            rows = await cur.fetchall()
        out: list[UserRow] = []
        for row in rows:
            try:
                gk = self.cipher.decrypt(row[3])
            except ValueError as e:
                log.error("Skipping user %s: decryption failed (%s)", row[0], e)
                continue
            out.append(
                UserRow(
                    tg_user_id=row[0],
                    funpay_username=row[1],
                    funpay_id=row[2],
                    golden_key=gk,
                    user_agent=row[4],
                    enabled=bool(row[5]),
                )
            )
        return out

    # ---- stats ----

    async def log_message(
        self, tg_user_id: int, funpay_username: str, chat_id: str | int, text: str | None
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO messages (tg_user_id, ts, username, chat_id, text) "
                "VALUES (?, ?, ?, ?, ?)",
                (tg_user_id, int(time.time()), funpay_username, str(chat_id), text),
            )
            await db.commit()

    async def upsert_order(
        self,
        tg_user_id: int,
        order_id: str,
        buyer: str,
        price: float,
        description: str,
        status: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO orders (tg_user_id, id, ts, buyer, price, description, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tg_user_id, id) DO UPDATE SET
                    status=excluded.status,
                    price=excluded.price,
                    description=excluded.description
                """,
                (tg_user_id, order_id, int(time.time()), buyer, price, description, status),
            )
            await db.commit()

    async def count_messages_since(self, tg_user_id: int, since_ts: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE tg_user_id=? AND ts >= ?",
                (tg_user_id, since_ts),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def count_orders_since(
        self, tg_user_id: int, since_ts: int
    ) -> tuple[int, float]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(price), 0) FROM orders "
                "WHERE tg_user_id=? AND ts >= ?",
                (tg_user_id, since_ts),
            )
            row = await cur.fetchone()
            return (int(row[0]), float(row[1])) if row else (0, 0.0)

    # ---- block list ----

    async def block(self, tg_user_id: int, funpay_username: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO blocked (tg_user_id, username, blocked_at) "
                "VALUES (?, ?, ?)",
                (tg_user_id, funpay_username.lower(), int(time.time())),
            )
            await db.commit()

    async def unblock(self, tg_user_id: int, funpay_username: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM blocked WHERE tg_user_id=? AND username=?",
                (tg_user_id, funpay_username.lower()),
            )
            await db.commit()

    async def is_blocked(self, tg_user_id: int, funpay_username: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM blocked WHERE tg_user_id=? AND username=?",
                (tg_user_id, funpay_username.lower()),
            )
            return await cur.fetchone() is not None

    async def list_blocked(self, tg_user_id: int) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT username FROM blocked WHERE tg_user_id=? ORDER BY blocked_at DESC",
                (tg_user_id,),
            )
            return [r[0] for r in await cur.fetchall()]

    # ---- per-user state ----

    async def get_state(
        self, tg_user_id: int, key: str, default: str | None = None
    ) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT value FROM state WHERE tg_user_id=? AND key=?",
                (tg_user_id, key),
            )
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_state(self, tg_user_id: int, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO state (tg_user_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT(tg_user_id, key) DO UPDATE SET value=excluded.value",
                (tg_user_id, key, value),
            )
            await db.commit()

    # ---- autoreply dedupe ----

    async def autoreply_already_sent(self, tg_user_id: int, chat_id: str | int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM autoreply_done WHERE tg_user_id=? AND chat_id=?",
                (tg_user_id, str(chat_id)),
            )
            return await cur.fetchone() is not None

    async def mark_autoreply_sent(self, tg_user_id: int, chat_id: str | int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO autoreply_done (tg_user_id, chat_id, ts) "
                "VALUES (?, ?, ?)",
                (tg_user_id, str(chat_id), int(time.time())),
            )
            await db.commit()

    # ---- templates ----

    async def template_add(self, tg_user_id: int, name: str, text: str) -> int:
        """Insert/replace a template. Returns its row id."""
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO templates (tg_user_id, name, text, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(tg_user_id, name) DO UPDATE SET text=excluded.text",
                (tg_user_id, name, text, int(time.time())),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT id FROM templates WHERE tg_user_id=? AND name=?",
                (tg_user_id, name),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def template_remove(self, tg_user_id: int, name: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM templates WHERE tg_user_id=? AND name=?",
                (tg_user_id, name),
            )
            await db.commit()
            return cur.rowcount > 0

    async def template_get(
        self, tg_user_id: int, template_id: int
    ) -> tuple[str, str] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name, text FROM templates WHERE tg_user_id=? AND id=?",
                (tg_user_id, template_id),
            )
            row = await cur.fetchone()
            return (row[0], row[1]) if row else None

    async def template_get_by_name(
        self, tg_user_id: int, name: str
    ) -> tuple[int, str] | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT id, text FROM templates WHERE tg_user_id=? AND name=?",
                (tg_user_id, name),
            )
            row = await cur.fetchone()
            return (int(row[0]), row[1]) if row else None

    async def template_list(self, tg_user_id: int) -> list[tuple[int, str, str]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT id, name, text FROM templates "
                "WHERE tg_user_id=? ORDER BY created_at ASC",
                (tg_user_id,),
            )
            rows = await cur.fetchall()
        return [(int(r[0]), r[1], r[2]) for r in rows]

    # ---- delivery queue (auto-deliver on paid order) ----

    async def queue_add(self, tg_user_id: int, content: str) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO delivery_queue (tg_user_id, content, created_at) "
                "VALUES (?, ?, ?)",
                (tg_user_id, content, int(time.time())),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def queue_list(
        self, tg_user_id: int, limit: int = 50, only_unused: bool = True
    ) -> list[tuple[int, str, bool]]:
        query = "SELECT id, content, used FROM delivery_queue WHERE tg_user_id=?"
        if only_unused:
            query += " AND used=0"
        query += " ORDER BY id ASC LIMIT ?"
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(query, (tg_user_id, limit))
            rows = await cur.fetchall()
        return [(int(r[0]), r[1], bool(r[2])) for r in rows]

    async def queue_count_available(self, tg_user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM delivery_queue WHERE tg_user_id=? AND used=0",
                (tg_user_id,),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def queue_pop(
        self, tg_user_id: int, order_id: str
    ) -> tuple[int, str] | None:
        """Atomically claim the next unused queue item for ``order_id``.

        Returns (queue_id, content) on success; returns None if there is
        nothing available or if this order_id was already delivered.
        """
        now = int(time.time())
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT 1 FROM delivery_log WHERE tg_user_id=? AND order_id=?",
                (tg_user_id, order_id),
            )
            if await cur.fetchone():
                await db.execute("ROLLBACK")
                return None
            cur = await db.execute(
                "SELECT id, content FROM delivery_queue "
                "WHERE tg_user_id=? AND used=0 ORDER BY id ASC LIMIT 1",
                (tg_user_id,),
            )
            row = await cur.fetchone()
            if not row:
                await db.execute("ROLLBACK")
                return None
            qid, content = int(row[0]), row[1]
            await db.execute(
                "UPDATE delivery_queue SET used=1, used_at=?, order_id=? WHERE id=?",
                (now, order_id, qid),
            )
            await db.execute(
                "INSERT INTO delivery_log (tg_user_id, order_id, delivered_at, queue_item_id) "
                "VALUES (?, ?, ?, ?)",
                (tg_user_id, order_id, now, qid),
            )
            await db.commit()
        return qid, content

    async def queue_clear_used(self, tg_user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM delivery_queue WHERE tg_user_id=? AND used=1",
                (tg_user_id,),
            )
            await db.commit()
            return cur.rowcount or 0

    async def queue_clear_all(self, tg_user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM delivery_queue WHERE tg_user_id=?",
                (tg_user_id,),
            )
            await db.commit()
            return cur.rowcount or 0

    async def delivery_already_logged(
        self, tg_user_id: int, order_id: str
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM delivery_log WHERE tg_user_id=? AND order_id=?",
                (tg_user_id, order_id),
            )
            return await cur.fetchone() is not None
