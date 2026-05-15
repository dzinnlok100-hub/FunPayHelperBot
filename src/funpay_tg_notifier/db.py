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
            for table in ("messages", "orders", "blocked", "state", "autoreply_done", "users"):
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
