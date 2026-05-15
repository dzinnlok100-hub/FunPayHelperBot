"""Application entry point: glues the FunPay runner registry to the aiogram bot."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .config import load_settings
from .crypto import SecretCipher
from .db import Database
from .handlers import register_handlers
from .notifier import Notifier
from .runner_registry import RunnerRegistry

log = logging.getLogger(__name__)


async def _amain() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cipher = SecretCipher(settings.encryption_key)
    db = Database(settings.db_path, cipher)
    await db.init()

    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    notifier = Notifier(bot, db)

    loop = asyncio.get_running_loop()
    registry = RunnerRegistry(db, notifier, loop, settings.funpay_poll_delay)

    register_handlers(dp, bot, db, notifier, registry, settings.admin_tg_user_id)

    # Auto-resume all previously enabled users on startup.
    known_users = await db.list_users(only_enabled=True)
    log.info("Resuming %d previously-active users", len(known_users))
    for u in known_users:
        try:
            registry.start(u.tg_user_id, u.golden_key, u.user_agent)
        except Exception:
            log.exception("Failed to start runner for tg_user=%s", u.tg_user_id)

    # Notify admin (if configured) that the bot is up.
    if settings.admin_tg_user_id:
        try:
            await notifier.send(
                settings.admin_tg_user_id,
                f"🟢 Бот запущен. Возобновил {len(known_users)} активных пользователей.",
            )
        except Exception:
            log.exception("Could not notify admin on startup")

    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        log.info("Shutting down, stopping all FunPay runners...")
        registry.stop_all()
        await bot.session.close()


def run() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down.")


if __name__ == "__main__":
    run()
