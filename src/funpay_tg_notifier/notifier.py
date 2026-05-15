"""Formats and dispatches Telegram notifications for FunPay events."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from .db import Database

if TYPE_CHECKING:
    from FunPayAPI import Account
    from FunPayAPI.updater.events import (
        NewMessageEvent,
        NewOrderEvent,
        OrderStatusChangedEvent,
    )

log = logging.getLogger(__name__)

STATE_AUTOREPLY_ENABLED = "autoreply_enabled"
STATE_AUTOREPLY_TEXT = "autoreply_text"
DEFAULT_AUTOREPLY_TEXT = "Здравствуйте! Я скоро вернусь и отвечу — обычно в течение 5–10 минут."


def _esc(text: str | None) -> str:
    if text is None:
        return ""
    return html.escape(str(text))


def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class Notifier:
    """Routes FunPay events to Telegram and (optionally) sends auto-replies on FunPay."""

    def __init__(self, bot: Bot, db: Database, telegram_chat_id: int) -> None:
        self.bot = bot
        self.db = db
        self.chat_id = telegram_chat_id
        self.account: Account | None = None

    def set_account(self, account: Account) -> None:
        self.account = account

    async def _send(self, text: str) -> None:
        try:
            await self.bot.send_message(
                self.chat_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                me = None
                try:
                    me = await self.bot.get_me()
                except Exception:
                    pass
                bot_handle = f"@{me.username}" if me and me.username else "твоему боту"
                log.error(
                    "Telegram вернул 'chat not found' для chat_id=%s. "
                    "Открой %s в Telegram и нажми /start — до этого бот не может тебе писать.",
                    self.chat_id,
                    bot_handle,
                )
            else:
                log.exception("Failed to send Telegram message: %s", e)
        except TelegramAPIError as e:
            log.exception("Failed to send Telegram message: %s", e)

    # ---- event handlers ----

    async def handle_new_message(self, event: "NewMessageEvent") -> None:
        if self.account is None:
            return
        msg = event.message

        # Skip our own outgoing messages.
        if msg.author_id == self.account.id:
            return
        # Skip FunPay system messages (order confirmations, refunds, etc. — these come
        # in via NewOrder/OrderStatusChanged events anyway).
        if msg.author_id == 0:
            return

        author = msg.author or "(unknown)"
        await self.db.log_message(author, msg.chat_id, msg.text)

        if await self.db.is_blocked(author):
            log.info("Skipping notification — user %s is blocked", author)
            return

        chat_link = f"https://funpay.com/chat/?node={msg.chat_id}"
        body = msg.text or ("[изображение] " + (msg.image_link or ""))
        text = (
            "💬 <b>Новое сообщение на FunPay</b>\n"
            f"От: <b>{_esc(author)}</b>\n"
            f"Чат: <a href=\"{_esc(chat_link)}\">открыть</a>\n\n"
            f"{_esc(_truncate(body))}"
        )
        await self._send(text)

        # Auto-reply (once per chat).
        if await self._autoreply_enabled() and not await self.db.autoreply_already_sent(msg.chat_id):
            reply_text = await self._autoreply_text()
            try:
                self.account.send_message(msg.chat_id, reply_text, chat_name=author)
                await self.db.mark_autoreply_sent(msg.chat_id)
                await self._send(
                    f"🤖 Автоответ отправлен пользователю <b>{_esc(author)}</b>"
                )
            except Exception as e:
                log.exception("Auto-reply send failed: %s", e)
                await self._send(
                    f"⚠️ Не удалось отправить автоответ <b>{_esc(author)}</b>: {_esc(str(e))}"
                )

    async def handle_new_order(self, event: "NewOrderEvent") -> None:
        order = event.order
        await self.db.upsert_order(
            order.id,
            order.buyer_username,
            float(order.price),
            order.description,
            order.status.name if hasattr(order.status, "name") else str(order.status),
        )

        if await self.db.is_blocked(order.buyer_username):
            return

        order_link = f"https://funpay.com/orders/{order.id}/"
        chat_link = f"https://funpay.com/chat/?node=users-{order.buyer_id}"
        text = (
            "🛒 <b>Новый заказ на FunPay</b>\n"
            f"Покупатель: <b>{_esc(order.buyer_username)}</b>\n"
            f"Лот: {_esc(order.description)}\n"
            f"Категория: {_esc(order.subcategory_name)}\n"
            f"Сумма: <b>{order.price:.2f} ₽</b>"
            + (f" × {order.amount}" if order.amount else "")
            + "\n"
            f"Заказ: <a href=\"{_esc(order_link)}\">#{_esc(order.id)}</a>"
            f" · <a href=\"{_esc(chat_link)}\">чат с покупателем</a>"
        )
        await self._send(text)

    async def handle_order_status_changed(self, event: "OrderStatusChangedEvent") -> None:
        order = event.order
        status_name = order.status.name if hasattr(order.status, "name") else str(order.status)
        await self.db.upsert_order(
            order.id,
            order.buyer_username,
            float(order.price),
            order.description,
            status_name,
        )

        emoji = {"CLOSED": "✅", "PAID": "💰", "REFUNDED": "↩️"}.get(status_name, "🔁")
        order_link = f"https://funpay.com/orders/{order.id}/"
        text = (
            f"{emoji} <b>Статус заказа изменён: {_esc(status_name)}</b>\n"
            f"Покупатель: <b>{_esc(order.buyer_username)}</b>\n"
            f"Лот: {_esc(order.description)}\n"
            f"Сумма: <b>{order.price:.2f} ₽</b>\n"
            f"Заказ: <a href=\"{_esc(order_link)}\">#{_esc(order.id)}</a>"
        )
        await self._send(text)

    async def handle_runner_error(self, exc: BaseException) -> None:
        """Called when the FunPay runner crashes (cookie expired, network, etc.)."""
        text = (
            "⚠️ <b>FunPay недоступен или сессия истекла</b>\n"
            f"Ошибка: <code>{_esc(_truncate(str(exc), 400))}</code>\n\n"
            "Если это длится дольше пары минут — проверь, не разлогинило ли тебя "
            "на funpay.com, и обнови <code>FUNPAY_GOLDEN_KEY</code> в .env."
        )
        await self._send(text)

    async def handle_startup(self, username: str | None, balance: str | None) -> None:
        text = (
            "🟢 <b>Бот запущен</b>\n"
            f"FunPay аккаунт: <b>{_esc(username or '(неизвестно)')}</b>\n"
            + (f"Баланс: {_esc(balance)}\n" if balance else "")
            + "Команды: /help"
        )
        await self._send(text)

    # ---- autoreply state ----

    async def _autoreply_enabled(self) -> bool:
        v = await self.db.get_state(STATE_AUTOREPLY_ENABLED, "0")
        return v == "1"

    async def _autoreply_text(self) -> str:
        return await self.db.get_state(STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT) or DEFAULT_AUTOREPLY_TEXT
