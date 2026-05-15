"""Periodic background tasks: auto-bump lots, daily digest.

Runs as a single asyncio task that wakes every ``TICK_SECONDS`` and decides
per-user whether to fire any scheduled action.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from .db import Database
from .funpay_helpers import bump_user_lots
from .notifier import (
    STATE_AUTOBUMP_ENABLED,
    STATE_AUTOBUMP_LAST_RUN_TS,
    STATE_DIGEST_ENABLED,
    STATE_DIGEST_HOUR_UTC,
    STATE_DIGEST_LAST_SENT_DATE,
    Notifier,
)
from .runner_registry import RunnerRegistry

log = logging.getLogger(__name__)

# FunPay enforces a ~4-hour cooldown between bumps. We try every 4h+30s.
AUTOBUMP_INTERVAL_SECONDS = 4 * 3600 + 30
TICK_SECONDS = 60  # background task tick


async def _maybe_autobump(
    tg_user_id: int,
    db: Database,
    registry: RunnerRegistry,
    notifier: Notifier,
) -> None:
    if await db.get_state(tg_user_id, STATE_AUTOBUMP_ENABLED, "0") != "1":
        return
    acc = registry.get_account(tg_user_id)
    if acc is None:
        return
    last_ts_raw = await db.get_state(tg_user_id, STATE_AUTOBUMP_LAST_RUN_TS, "0")
    try:
        last_ts = int(last_ts_raw or "0")
    except ValueError:
        last_ts = 0
    if time.time() - last_ts < AUTOBUMP_INTERVAL_SECONDS:
        return

    log.info("autobump: firing for tg_user=%s", tg_user_id)
    try:
        bumped, failed = await asyncio.to_thread(bump_user_lots, acc)
    except Exception:
        log.exception("autobump: bump_user_lots crashed for tg_user=%s", tg_user_id)
        return
    await db.set_state(tg_user_id, STATE_AUTOBUMP_LAST_RUN_TS, str(int(time.time())))
    if bumped:
        cats = ", ".join(sorted(bumped))
        msg = f"🔝 <b>Авто-поднятие лотов:</b> {cats}"
        if failed:
            msg += f"\nНе удалось: {', '.join(sorted(failed))}"
        await notifier.send(tg_user_id, msg, disable_notification=True)
    # If only failures, stay silent — usually it's just the 4h cooldown.


async def _maybe_send_digest(
    tg_user_id: int,
    db: Database,
    notifier: Notifier,
) -> None:
    if await db.get_state(tg_user_id, STATE_DIGEST_ENABLED, "0") != "1":
        return
    hour_raw = await db.get_state(tg_user_id, STATE_DIGEST_HOUR_UTC, "21")
    try:
        target_hour = max(0, min(23, int(hour_raw or "21")))
    except ValueError:
        target_hour = 21
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    last_sent = await db.get_state(tg_user_id, STATE_DIGEST_LAST_SENT_DATE)
    if last_sent == today_str:
        return
    if now.hour < target_hour:
        return

    # Build digest.
    now_ts = int(time.time())
    day = 86400
    msgs_today = await db.count_messages_since(tg_user_id, now_ts - day)
    msgs_week = await db.count_messages_since(tg_user_id, now_ts - 7 * day)
    orders_today_n, orders_today_sum = await db.count_orders_since(
        tg_user_id, now_ts - day
    )
    orders_week_n, orders_week_sum = await db.count_orders_since(
        tg_user_id, now_ts - 7 * day
    )
    queue_left = await db.queue_count_available(tg_user_id)
    text = (
        "📊 <b>Ежедневная сводка</b> (UTC)\n\n"
        "<b>За сутки</b>\n"
        f"  💬 сообщений: {msgs_today}\n"
        f"  🛒 заказов: {orders_today_n} ({orders_today_sum:.2f} ₽)\n\n"
        "<b>За неделю</b>\n"
        f"  💬 сообщений: {msgs_week}\n"
        f"  🛒 заказов: {orders_week_n} ({orders_week_sum:.2f} ₽)\n\n"
        f"📦 В очереди авто-выдачи: <b>{queue_left}</b>"
    )
    sent = await notifier.send(tg_user_id, text, disable_notification=True)
    if sent:
        await db.set_state(tg_user_id, STATE_DIGEST_LAST_SENT_DATE, today_str)


async def background_loop(
    db: Database,
    registry: RunnerRegistry,
    notifier: Notifier,
    stop_event: asyncio.Event,
) -> None:
    """Single long-running task that fans out per-user scheduled actions."""
    log.info("background_loop started (tick=%ss)", TICK_SECONDS)
    try:
        while not stop_event.is_set():
            try:
                users = await db.list_users(only_enabled=True)
                for u in users:
                    try:
                        await _maybe_autobump(u.tg_user_id, db, registry, notifier)
                    except Exception:
                        log.exception(
                            "autobump tick failed for tg_user=%s", u.tg_user_id
                        )
                    try:
                        await _maybe_send_digest(u.tg_user_id, db, notifier)
                    except Exception:
                        log.exception(
                            "digest tick failed for tg_user=%s", u.tg_user_id
                        )
            except Exception:
                log.exception("background_loop iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("background_loop stopped")
