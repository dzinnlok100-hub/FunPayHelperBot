"""Application entry point: glues FunPay event runner to the aiogram Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from FunPayAPI import Account, Runner
from FunPayAPI.updater.events import (
    InitialChatEvent,
    InitialOrderEvent,
    NewMessageEvent,
    NewOrderEvent,
    OrderStatusChangedEvent,
)

from .config import Settings, load_settings
from .db import Database
from .funpay_helpers import format_balance, get_balance_safe
from .handlers import register_handlers
from .notifier import Notifier

log = logging.getLogger(__name__)

# How long to wait before retrying after a Runner-level crash.
_RUNNER_BACKOFF_SECONDS = 30


def _funpay_thread(
    settings: Settings,
    notifier: Notifier,
    account_ref: list[Optional[Account]],
    loop: asyncio.AbstractEventLoop,
    stop_event: threading.Event,
    db: Database,
) -> None:
    """Sync polling loop. Runs in its own thread because FunPayAPI is sync."""
    while not stop_event.is_set():
        try:
            account = Account(
                settings.golden_key,
                user_agent=settings.user_agent,
            ).get()
            account_ref[0] = account
            notifier.set_account(account)

            # Telegram-side startup notice (only on the very first successful login).
            async def _startup_notice() -> None:
                bal = await get_balance_safe(account, db)
                await notifier.handle_startup(
                    account.username,
                    format_balance(bal) if bal else None,
                )

            asyncio.run_coroutine_threadsafe(_startup_notice(), loop)

            runner = Runner(account)
            log.info("FunPay runner started for account %s (id=%s)", account.username, account.id)

            for event in runner.listen(requests_delay=settings.funpay_poll_delay):
                if stop_event.is_set():
                    break

                # Skip "initial" events emitted on the first runner request — those are not
                # new things, just whatever already exists.
                if isinstance(event, (InitialChatEvent, InitialOrderEvent)):
                    continue

                try:
                    if isinstance(event, NewMessageEvent):
                        asyncio.run_coroutine_threadsafe(
                            notifier.handle_new_message(event), loop
                        )
                    elif isinstance(event, NewOrderEvent):
                        asyncio.run_coroutine_threadsafe(
                            notifier.handle_new_order(event), loop
                        )
                    elif isinstance(event, OrderStatusChangedEvent):
                        asyncio.run_coroutine_threadsafe(
                            notifier.handle_order_status_changed(event), loop
                        )
                except Exception:
                    log.exception("Error while dispatching FunPay event %r", event)
        except Exception as exc:
            log.exception("FunPay runner crashed; will retry in %ss", _RUNNER_BACKOFF_SECONDS)
            try:
                asyncio.run_coroutine_threadsafe(
                    notifier.handle_runner_error(exc), loop
                )
            except Exception:
                log.exception("Failed to schedule runner-error notification")
            # Backoff before reconnecting.
            if stop_event.wait(_RUNNER_BACKOFF_SECONDS):
                break


async def _amain() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Database(settings.db_path)
    await db.init()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    notifier = Notifier(bot, db, settings.telegram_chat_id)

    account_ref: list[Optional[Account]] = [None]

    def get_account() -> Optional[Account]:
        return account_ref[0]

    register_handlers(dp, db, notifier, settings.telegram_chat_id, get_account)

    # Make the db accessible to the funpay polling thread.
    funpay_db_ref = db

    loop = asyncio.get_running_loop()
    stop_event = threading.Event()
    funpay_thread = threading.Thread(
        target=_funpay_thread,
        args=(settings, notifier, account_ref, loop, stop_event, funpay_db_ref),
        name="funpay-runner",
        daemon=True,
    )
    funpay_thread.start()

    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        stop_event.set()
        await bot.session.close()


def run() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down.")


if __name__ == "__main__":
    run()
