"""SQLite layer: stats counters, blocked users, runtime state."""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    username TEXT NOT NULL,
    chat_id TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_username ON messages(username);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    buyer TEXT NOT NULL,
    price REAL NOT NULL,
    description TEXT,
    status TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS blocked (
    username TEXT PRIMARY KEY,
    blocked_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autoreply_done (
    chat_id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    # ---- messages ----
    async def log_message(self, username: str, chat_id: str | int, text: str | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO messages (ts, username, chat_id, text) VALUES (?, ?, ?, ?)",
                (int(time.time()), username, str(chat_id), text),
            )
            await db.commit()

    # ---- orders ----
    async def upsert_order(
        self,
        order_id: str,
        buyer: str,
        price: float,
        description: str,
        status: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO orders (id, ts, buyer, price, description, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    price=excluded.price,
                    description=excluded.description
                """,
                (order_id, int(time.time()), buyer, price, description, status),
            )
            await db.commit()

    async def count_messages_since(self, since_ts: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE ts >= ?", (since_ts,)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def count_orders_since(self, since_ts: int) -> tuple[int, float]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*), COALESCE(SUM(price), 0) FROM orders WHERE ts >= ?",
                (since_ts,),
            )
            row = await cur.fetchone()
            return (int(row[0]), float(row[1])) if row else (0, 0.0)

    # ---- block list ----
    async def block(self, username: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO blocked (username, blocked_at) VALUES (?, ?)",
                (username.lower(), int(time.time())),
            )
            await db.commit()

    async def unblock(self, username: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM blocked WHERE username = ?", (username.lower(),))
            await db.commit()

    async def is_blocked(self, username: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM blocked WHERE username = ?", (username.lower(),)
            )
            row = await cur.fetchone()
            return row is not None

    async def list_blocked(self) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT username FROM blocked ORDER BY blocked_at DESC")
            rows = await cur.fetchall()
            return [r[0] for r in rows]

    # ---- key-value state ----
    async def get_state(self, key: str, default: str | None = None) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM state WHERE key = ?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_state(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await db.commit()

    # ---- autoreply dedupe ----
    async def autoreply_already_sent(self, chat_id: str | int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM autoreply_done WHERE chat_id = ?", (str(chat_id),)
            )
            row = await cur.fetchone()
            return row is not None

    async def mark_autoreply_sent(self, chat_id: str | int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO autoreply_done (chat_id, ts) VALUES (?, ?)",
                (str(chat_id), int(time.time())),
            )
            await db.commit()
