"""Telegram command handlers (/stats, /block, /autoreply, /status, /help)."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from aiogram import Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .db import Database
from .funpay_helpers import format_balance, get_balance_safe
from .notifier import (
    DEFAULT_AUTOREPLY_TEXT,
    Notifier,
    STATE_AUTOREPLY_ENABLED,
    STATE_AUTOREPLY_TEXT,
)

if TYPE_CHECKING:
    from FunPayAPI import Account

log = logging.getLogger(__name__)

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/status — баланс, активные продажи/покупки, состояние автоответчика\n"
    "/stats — статистика за сегодня / 7 / 30 дней\n"
    "/autoreply on|off — включить/выключить автоответчик\n"
    "/autoreply set &lt;текст&gt; — задать текст автоответа\n"
    "/autoreply show — показать текущий автоответ\n"
    "/block &lt;ник&gt; — приглушить уведомления от пользователя FunPay\n"
    "/unblock &lt;ник&gt; — снять приглушение\n"
    "/blocked — список заглушённых\n"
    "/help — эта справка"
)


def register_handlers(
    dp: Dispatcher,
    db: Database,
    notifier: Notifier,
    allowed_chat_id: int,
    get_account,
) -> None:
    """Wire up handlers. ``get_account`` returns the current FunPayAPI Account (or None)."""

    only_me = F.chat.id == allowed_chat_id

    @dp.message(only_me, Command("start"))
    @dp.message(only_me, Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT, parse_mode="HTML")

    @dp.message(only_me, Command("status"))
    async def cmd_status(message: Message) -> None:
        acc = get_account()
        if acc is None:
            await message.answer("⚠️ FunPay-аккаунт ещё не подключён.")
            return
        autoreply_on = await db.get_state(STATE_AUTOREPLY_ENABLED, "0") == "1"
        bal = await get_balance_safe(acc, db)
        balance_text = format_balance(bal)
        text = (
            "<b>Статус</b>\n"
            f"Аккаунт: <b>{acc.username}</b> (id <code>{acc.id}</code>)\n"
            f"Баланс: {balance_text}\n"
            f"Активные продажи: {acc.active_sales}\n"
            f"Активные покупки: {acc.active_purchases}\n"
            f"Автоответчик: <b>{'ON' if autoreply_on else 'OFF'}</b>"
        )
        await message.answer(text, parse_mode="HTML")

    @dp.message(only_me, Command("stats"))
    async def cmd_stats(message: Message) -> None:
        now = int(time.time())
        day = 86400
        msgs_today = await db.count_messages_since(now - day)
        msgs_week = await db.count_messages_since(now - 7 * day)
        msgs_month = await db.count_messages_since(now - 30 * day)
        orders_today_n, orders_today_sum = await db.count_orders_since(now - day)
        orders_week_n, orders_week_sum = await db.count_orders_since(now - 7 * day)
        orders_month_n, orders_month_sum = await db.count_orders_since(now - 30 * day)
        text = (
            "<b>Статистика</b>\n\n"
            "<b>Сообщения</b>\n"
            f"  сегодня: {msgs_today}\n"
            f"  7 дней: {msgs_week}\n"
            f"  30 дней: {msgs_month}\n\n"
            "<b>Заказы</b>\n"
            f"  сегодня: {orders_today_n} ({orders_today_sum:.2f} ₽)\n"
            f"  7 дней: {orders_week_n} ({orders_week_sum:.2f} ₽)\n"
            f"  30 дней: {orders_month_n} ({orders_month_sum:.2f} ₽)\n\n"
            "<i>Счётчики наполняются только теми событиями, которые произошли с момента "
            "первого запуска бота.</i>"
        )
        await message.answer(text, parse_mode="HTML")

    @dp.message(only_me, Command("block"))
    async def cmd_block(message: Message, command: CommandObject) -> None:
        username = (command.args or "").strip()
        if not username:
            await message.answer("Использование: <code>/block ник_на_funpay</code>", parse_mode="HTML")
            return
        await db.block(username)
        await message.answer(f"🔇 Уведомления от <b>{username}</b> приглушены.", parse_mode="HTML")

    @dp.message(only_me, Command("unblock"))
    async def cmd_unblock(message: Message, command: CommandObject) -> None:
        username = (command.args or "").strip()
        if not username:
            await message.answer("Использование: <code>/unblock ник_на_funpay</code>", parse_mode="HTML")
            return
        await db.unblock(username)
        await message.answer(f"🔔 Уведомления от <b>{username}</b> снова включены.", parse_mode="HTML")

    @dp.message(only_me, Command("blocked"))
    async def cmd_blocked(message: Message) -> None:
        users = await db.list_blocked()
        if not users:
            await message.answer("Список заглушённых пуст.")
            return
        await message.answer(
            "<b>Заглушённые пользователи:</b>\n" + "\n".join(f"• {u}" for u in users),
            parse_mode="HTML",
        )

    @dp.message(only_me, Command("autoreply"))
    async def cmd_autoreply(message: Message, command: CommandObject) -> None:
        args = (command.args or "").strip()
        if not args:
            current = await db.get_state(STATE_AUTOREPLY_ENABLED, "0") == "1"
            text_now = await db.get_state(STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT)
            await message.answer(
                f"Автоответчик: <b>{'ON' if current else 'OFF'}</b>\n"
                f"Текст: <pre>{text_now}</pre>\n\n"
                "Команды:\n"
                "  /autoreply on\n  /autoreply off\n  /autoreply set текст\n  /autoreply show",
                parse_mode="HTML",
            )
            return

        sub, _, rest = args.partition(" ")
        sub = sub.lower()
        if sub == "on":
            await db.set_state(STATE_AUTOREPLY_ENABLED, "1")
            await message.answer("🤖 Автоответчик <b>ON</b>", parse_mode="HTML")
        elif sub == "off":
            await db.set_state(STATE_AUTOREPLY_ENABLED, "0")
            await message.answer("🤖 Автоответчик <b>OFF</b>", parse_mode="HTML")
        elif sub == "set":
            new_text = rest.strip()
            if not new_text:
                await message.answer("Укажи текст: <code>/autoreply set Привет!</code>", parse_mode="HTML")
                return
            await db.set_state(STATE_AUTOREPLY_TEXT, new_text)
            await message.answer(f"Текст автоответа обновлён:\n<pre>{new_text}</pre>", parse_mode="HTML")
        elif sub == "show":
            text_now = await db.get_state(STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT)
            await message.answer(f"<pre>{text_now}</pre>", parse_mode="HTML")
        else:
            await message.answer("Неизвестная подкоманда. Используй /autoreply без аргументов для справки.")

    @dp.message(only_me)
    async def fallback(message: Message) -> None:
        await message.answer("Не понял команду. /help — список команд.")
