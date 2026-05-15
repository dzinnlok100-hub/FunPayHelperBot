"""Telegram command handlers (multi-tenant)."""

from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from FunPayAPI import Account

from .db import Database
from .funpay_helpers import format_balance, get_balance_safe
from .notifier import (
    DEFAULT_AUTOREPLY_TEXT,
    Notifier,
    STATE_AUTOREPLY_ENABLED,
    STATE_AUTOREPLY_TEXT,
)
from .runner_registry import RunnerRegistry

log = logging.getLogger(__name__)


WELCOME_TEXT = (
    "👋 <b>FunPay → Telegram notifier</b>\n\n"
    "Я пересылаю в Telegram события с твоего аккаунта на funpay.com:\n"
    "• новые сообщения от покупателей\n"
    "• новые заказы и смена их статусов\n"
    "• умею отвечать автоматически от твоего имени, вести статистику, "
    "приглушать спамеров.\n\n"
    "<b>Как подключиться:</b>\n"
    "1. Залогинься на funpay.com в обычном браузере.\n"
    "2. Открой DevTools (F12) → Application → Cookies → <code>https://funpay.com</code> "
    "→ найди cookie <b>golden_key</b>. Скопируй её значение.\n"
    "3. Отправь мне команду:\n"
    "   <code>/setkey ТВОЙ_GOLDEN_KEY</code>\n"
    "   Дополнительно: <code>/setua ТВОЙ_USER_AGENT</code> (рекомендуется — копируется "
    "из того же DevTools, вкладка Network → любой запрос → user-agent).\n\n"
    "⚠️ <b>golden_key — это полный доступ к твоему FunPay-аккаунту.</b> Отправляя его, "
    "ты доверяешь оператору этого бота. После /setkey <b>удали своё сообщение</b> "
    "вручную (Telegram не даёт ботам удалять чужие сообщения в личке).\n\n"
    "/help — список команд."
)

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/setkey &lt;golden_key&gt; — подключить FunPay-аккаунт\n"
    "/setua &lt;user_agent&gt; — задать User-Agent (опционально)\n"
    "/status — баланс, аккаунт, состояние\n"
    "/stats — сообщения и заказы за 1 / 7 / 30 дней\n"
    "/autoreply on|off — включить/выключить автоответчик\n"
    "/autoreply set &lt;текст&gt; — задать текст автоответа\n"
    "/autoreply show — показать текущий автоответ\n"
    "/block &lt;ник&gt; — приглушить уведомления от пользователя FunPay\n"
    "/unblock &lt;ник&gt; — снять приглушение\n"
    "/blocked — список заглушённых\n"
    "/pause — приостановить уведомления полностью\n"
    "/resume — возобновить\n"
    "/unlink — удалить мои данные и отключиться от бота\n"
    "/help — эта справка"
)


def register_handlers(
    dp: Dispatcher,
    bot: Bot,
    db: Database,
    notifier: Notifier,
    registry: RunnerRegistry,
    admin_tg_user_id: int | None,
) -> None:
    async def _ensure_user_or_hint(message: Message) -> bool:
        """Returns True if the user is registered; otherwise hints and returns False."""
        u = await db.get_user(message.from_user.id)
        if u is None:
            await message.answer(
                "Сначала подключи FunPay-аккаунт командой "
                "<code>/setkey ТВОЙ_GOLDEN_KEY</code>. "
                "Подробности — /start."
            )
            return False
        return True

    @dp.message(Command("start"))
    @dp.message(Command("help"))
    async def cmd_start(message: Message) -> None:
        u = await db.get_user(message.from_user.id)
        if u and u.funpay_username:
            await message.answer(
                f"С возвращением! Подключён аккаунт <b>{u.funpay_username}</b>.\n\n"
                + HELP_TEXT
            )
        else:
            await message.answer(WELCOME_TEXT)

    @dp.message(Command("setkey"))
    async def cmd_setkey(message: Message, command: CommandObject) -> None:
        key = (command.args or "").strip()
        if not key:
            await message.answer(
                "Использование: <code>/setkey ТВОЙ_GOLDEN_KEY</code>\n\n"
                "После отправки <b>удали своё сообщение</b> — Telegram-ограничение "
                "не даёт мне сделать это за тебя."
            )
            return

        # Try to delete the message containing the key (works for bot's own messages
        # in private chats only; for user messages we just try and ignore failure).
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            pass

        # Validate the key.
        existing = await db.get_user(message.from_user.id)
        ua = existing.user_agent if existing else None
        progress = await message.answer("Проверяю ключ на FunPay…")
        try:
            account = await asyncio.to_thread(
                lambda: Account(key, user_agent=ua).get()
            )
        except Exception as e:
            await progress.edit_text(
                "❌ Не удалось залогиниться: "
                f"<code>{type(e).__name__}: {e}</code>\n\n"
                "Перепроверь, что скопировал значение cookie <b>golden_key</b> "
                "целиком и без пробелов. Если только что менял пароль на FunPay — "
                "старая кука протухла, нужна свежая."
            )
            return

        await db.upsert_user(
            tg_user_id=message.from_user.id,
            golden_key=key,
            funpay_username=account.username,
            funpay_id=account.id,
            user_agent=ua,
        )
        registry.start(message.from_user.id, key, ua)
        await progress.edit_text(
            f"✅ Подключён аккаунт <b>{account.username}</b> (id <code>{account.id}</code>).\n"
            f"Уведомления уже включены. /help — список команд.\n\n"
            "💡 Совет: <b>удали выше своё сообщение с ключом</b>, чтобы оно не висело в чате."
        )

    @dp.message(Command("setua"))
    async def cmd_setua(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        ua = (command.args or "").strip()
        if not ua:
            u = await db.get_user(message.from_user.id)
            current = u.user_agent if u else None
            await message.answer(
                "Использование: <code>/setua Mozilla/5.0 ...</code>\n"
                f"Текущий: <code>{current or '(не задан)'}</code>"
            )
            return
        u = await db.get_user(message.from_user.id)
        if u is None:
            return
        await db.upsert_user(
            tg_user_id=message.from_user.id,
            golden_key=u.golden_key,
            funpay_username=u.funpay_username,
            funpay_id=u.funpay_id,
            user_agent=ua,
        )
        registry.start(message.from_user.id, u.golden_key, ua)
        await message.answer("✅ User-Agent сохранён, раннер перезапущен.")

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        acc = registry.get_account(tg_id)
        autoreply_on = await db.get_state(tg_id, STATE_AUTOREPLY_ENABLED, "0") == "1"
        running = registry.is_running(tg_id)
        if acc is None:
            await message.answer(
                f"⏳ Раннер {'запускается' if running else 'не работает'}, "
                "аккаунт ещё не подгружен. Попробуй через несколько секунд /status."
            )
            return
        bal = await get_balance_safe(tg_id, acc, db)
        text = (
            "<b>Статус</b>\n"
            f"Аккаунт: <b>{acc.username}</b> (id <code>{acc.id}</code>)\n"
            f"Баланс: {format_balance(bal)}\n"
            f"Активные продажи: {acc.active_sales}\n"
            f"Активные покупки: {acc.active_purchases}\n"
            f"Автоответчик: <b>{'ON' if autoreply_on else 'OFF'}</b>\n"
            f"Раннер: <b>{'работает' if running else 'остановлен'}</b>"
        )
        await message.answer(text)

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        now = int(time.time())
        day = 86400
        msgs_today = await db.count_messages_since(tg_id, now - day)
        msgs_week = await db.count_messages_since(tg_id, now - 7 * day)
        msgs_month = await db.count_messages_since(tg_id, now - 30 * day)
        orders_today_n, orders_today_sum = await db.count_orders_since(tg_id, now - day)
        orders_week_n, orders_week_sum = await db.count_orders_since(tg_id, now - 7 * day)
        orders_month_n, orders_month_sum = await db.count_orders_since(tg_id, now - 30 * day)
        await message.answer(
            "<b>Статистика</b>\n\n"
            "<b>Сообщения</b>\n"
            f"  сегодня: {msgs_today}\n"
            f"  7 дней: {msgs_week}\n"
            f"  30 дней: {msgs_month}\n\n"
            "<b>Заказы</b>\n"
            f"  сегодня: {orders_today_n} ({orders_today_sum:.2f} ₽)\n"
            f"  7 дней: {orders_week_n} ({orders_week_sum:.2f} ₽)\n"
            f"  30 дней: {orders_month_n} ({orders_month_sum:.2f} ₽)\n\n"
            "<i>Счётчики наполняются только теми событиями, которые произошли "
            "с момента подключения.</i>"
        )

    @dp.message(Command("block"))
    async def cmd_block(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        username = (command.args or "").strip()
        if not username:
            await message.answer("Использование: <code>/block ник_на_funpay</code>")
            return
        await db.block(message.from_user.id, username)
        await message.answer(f"🔇 Уведомления от <b>{username}</b> приглушены.")

    @dp.message(Command("unblock"))
    async def cmd_unblock(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        username = (command.args or "").strip()
        if not username:
            await message.answer("Использование: <code>/unblock ник_на_funpay</code>")
            return
        await db.unblock(message.from_user.id, username)
        await message.answer(f"🔔 Уведомления от <b>{username}</b> снова включены.")

    @dp.message(Command("blocked"))
    async def cmd_blocked(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        users = await db.list_blocked(message.from_user.id)
        if not users:
            await message.answer("Список заглушённых пуст.")
            return
        await message.answer(
            "<b>Заглушённые пользователи:</b>\n" + "\n".join(f"• {u}" for u in users)
        )

    @dp.message(Command("autoreply"))
    async def cmd_autoreply(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        args = (command.args or "").strip()
        if not args:
            current = await db.get_state(tg_id, STATE_AUTOREPLY_ENABLED, "0") == "1"
            text_now = await db.get_state(tg_id, STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT)
            await message.answer(
                f"Автоответчик: <b>{'ON' if current else 'OFF'}</b>\n"
                f"Текст: <pre>{text_now}</pre>\n\n"
                "Команды:\n"
                "  /autoreply on\n  /autoreply off\n  /autoreply set текст\n  /autoreply show"
            )
            return

        sub, _, rest = args.partition(" ")
        sub = sub.lower()
        if sub == "on":
            await db.set_state(tg_id, STATE_AUTOREPLY_ENABLED, "1")
            await message.answer("🤖 Автоответчик <b>ON</b>")
        elif sub == "off":
            await db.set_state(tg_id, STATE_AUTOREPLY_ENABLED, "0")
            await message.answer("🤖 Автоответчик <b>OFF</b>")
        elif sub == "set":
            new_text = rest.strip()
            if not new_text:
                await message.answer("Укажи текст: <code>/autoreply set Привет!</code>")
                return
            await db.set_state(tg_id, STATE_AUTOREPLY_TEXT, new_text)
            await message.answer(f"Текст автоответа обновлён:\n<pre>{new_text}</pre>")
        elif sub == "show":
            text_now = await db.get_state(tg_id, STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT)
            await message.answer(f"<pre>{text_now}</pre>")
        else:
            await message.answer("Неизвестная подкоманда. /autoreply без аргументов — справка.")

    @dp.message(Command("pause"))
    async def cmd_pause(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        registry.stop(message.from_user.id)
        await db.set_enabled(message.from_user.id, False)
        await message.answer("⏸ Уведомления приостановлены. /resume — возобновить.")

    @dp.message(Command("resume"))
    async def cmd_resume(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        u = await db.get_user(message.from_user.id)
        if u is None:
            return
        await db.set_enabled(message.from_user.id, True)
        registry.start(message.from_user.id, u.golden_key, u.user_agent)
        await message.answer("▶️ Уведомления возобновлены.")

    @dp.message(Command("unlink"))
    async def cmd_unlink(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        registry.stop(message.from_user.id)
        await db.delete_user(message.from_user.id)
        await message.answer(
            "🗑 Все твои данные удалены. golden_key стёрт из БД. "
            "Чтобы подключиться снова — /setkey ..."
        )

    # ---- admin (optional) ----

    if admin_tg_user_id is not None:
        @dp.message(Command("admin"))
        async def cmd_admin(message: Message) -> None:
            if message.from_user.id != admin_tg_user_id:
                await message.answer("Эта команда только для админа.")
                return
            users = await db.list_users(only_enabled=False)
            active = sum(1 for u in users if u.enabled)
            running = len(registry.active_user_ids())
            lines = [
                f"<b>Админ-панель</b>",
                f"Всего пользователей: {len(users)}",
                f"Активных (enabled): {active}",
                f"Раннеров запущено: {running}",
                "",
                "<b>Список:</b>",
            ]
            for u in users[:50]:
                status = "🟢" if u.enabled else "⏸"
                running_mark = "▶️" if registry.is_running(u.tg_user_id) else "  "
                lines.append(
                    f"{status}{running_mark} <code>{u.tg_user_id}</code> — "
                    f"{u.funpay_username or '?'}"
                )
            if len(users) > 50:
                lines.append(f"… ещё {len(users) - 50}")
            await message.answer("\n".join(lines))
