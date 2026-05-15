"""Formats and dispatches per-user Telegram notifications for FunPay events."""

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
DEFAULT_AUTOREPLY_TEXT = (
    "Здравствуйте! Я скоро вернусь и отвечу — обычно в течение 5–10 минут."
)


def _esc(text: str | None) -> str:
    if text is None:
        return ""
    return html.escape(str(text))


def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class Notifier:
    """Routes FunPay events to a specific Telegram user."""

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def send(self, tg_user_id: int, text: str) -> bool:
        """Send a message to a specific Telegram user. Returns True on success."""
        try:
            await self.bot.send_message(
                tg_user_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if "chat not found" in msg or "blocked by the user" in msg or "user is deactivated" in msg:
                log.warning(
                    "Telegram user %s is unreachable (%s) — disabling.",
                    tg_user_id, e,
                )
                await self.db.set_enabled(tg_user_id, False)
            else:
                log.exception("Failed to send Telegram message to %s: %s", tg_user_id, e)
            return False
        except TelegramAPIError as e:
            log.exception("Failed to send Telegram message to %s: %s", tg_user_id, e)
            return False

    # ---- event handlers ----

    async def handle_new_message(
        self,
        tg_user_id: int,
        account: "Account",
        event: "NewMessageEvent",
    ) -> None:
        msg = event.message

        # Skip our own outgoing messages and FunPay system messages.
        if msg.author_id == account.id or msg.author_id == 0:
            return

        author = msg.author or "(unknown)"
        await self.db.log_message(tg_user_id, author, msg.chat_id, msg.text)

        if await self.db.is_blocked(tg_user_id, author):
            log.info("Skipping notification — %s blocked by tg_user=%s", author, tg_user_id)
            return

        chat_link = f"https://funpay.com/chat/?node={msg.chat_id}"
        body = msg.text or ("[изображение] " + (msg.image_link or ""))
        text = (
            "💬 <b>Новое сообщение на FunPay</b>\n"
            f"От: <b>{_esc(author)}</b>\n"
            f"Чат: <a href=\"{_esc(chat_link)}\">открыть</a>\n\n"
            f"{_esc(_truncate(body))}"
        )
        await self.send(tg_user_id, text)

        # Auto-reply on FunPay (once per chat).
        autoreply_on = (
            await self.db.get_state(tg_user_id, STATE_AUTOREPLY_ENABLED, "0") == "1"
        )
        if autoreply_on and not await self.db.autoreply_already_sent(tg_user_id, msg.chat_id):
            reply_text = (
                await self.db.get_state(
                    tg_user_id, STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT
                )
                or DEFAULT_AUTOREPLY_TEXT
            )
            try:
                account.send_message(msg.chat_id, reply_text, chat_name=author)
                await self.db.mark_autoreply_sent(tg_user_id, msg.chat_id)
                await self.send(
                    tg_user_id,
                    f"🤖 Автоответ отправлен пользователю <b>{_esc(author)}</b>",
                )
            except Exception as e:
                log.exception("Auto-reply send failed for tg_user=%s: %s", tg_user_id, e)
                await self.send(
                    tg_user_id,
                    f"⚠️ Не удалось отправить автоответ <b>{_esc(author)}</b>: {_esc(str(e))}",
                )

    async def handle_new_order(self, tg_user_id: int, event: "NewOrderEvent") -> None:
        order = event.order
        status_name = order.status.name if hasattr(order.status, "name") else str(order.status)
        await self.db.upsert_order(
            tg_user_id,
            order.id,
            order.buyer_username,
            float(order.price),
            order.description,
            status_name,
        )

        if await self.db.is_blocked(tg_user_id, order.buyer_username):
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
        await self.send(tg_user_id, text)

    async def handle_order_status_changed(
        self, tg_user_id: int, event: "OrderStatusChangedEvent"
    ) -> None:
        order = event.order
        status_name = order.status.name if hasattr(order.status, "name") else str(order.status)
        await self.db.upsert_order(
            tg_user_id,
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
        await self.send(tg_user_id, text)

    async def handle_runner_error(self, tg_user_id: int, exc: BaseException) -> None:
        text = (
            "⚠️ <b>FunPay недоступен или сессия истекла</b>\n"
            f"Ошибка: <code>{_esc(_truncate(str(exc), 400))}</code>\n\n"
            "Скорее всего твой <code>golden_key</code> протух. Залогинься на funpay.com заново, "
            "достань новую куку и отправь её мне командой <code>/setkey НОВЫЙ_КЛЮЧ</code>."
        )
        await self.send(tg_user_id, text)
