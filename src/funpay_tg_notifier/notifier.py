"""Formats and dispatches per-user Telegram notifications for FunPay events."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Database
from .funpay_helpers import format_money, safe_send_message

if TYPE_CHECKING:
    from FunPayAPI import Account
    from FunPayAPI.updater.events import (
        NewMessageEvent,
        NewOrderEvent,
        OrderStatusChangedEvent,
    )

log = logging.getLogger(__name__)

# state keys (centralized here so handlers can reference them)
STATE_AUTOREPLY_ENABLED = "autoreply_enabled"
STATE_AUTOREPLY_TEXT = "autoreply_text"
STATE_AUTOREPLY_QUIET_TEXT = "autoreply_quiet_text"
STATE_QUIET_ENABLED = "quiet_enabled"
STATE_QUIET_START_MIN = "quiet_start_min"
STATE_QUIET_END_MIN = "quiet_end_min"
STATE_AUTODELIVER_ENABLED = "autodeliver_enabled"
STATE_AUTOBUMP_ENABLED = "autobump_enabled"
STATE_AUTOBUMP_LAST_RUN_TS = "autobump_last_run_ts"
STATE_DIGEST_ENABLED = "digest_enabled"
STATE_DIGEST_HOUR_UTC = "digest_hour_utc"
STATE_DIGEST_LAST_SENT_DATE = "digest_last_sent_date"
STATE_REVIEW_ASK_ENABLED = "review_ask_enabled"
STATE_REVIEW_ASK_TEXT = "review_ask_text"

DEFAULT_AUTOREPLY_TEXT = (
    "Здравствуйте! Я скоро вернусь и отвечу — обычно в течение 5–10 минут."
)
DEFAULT_AUTOREPLY_QUIET_TEXT = (
    "Здравствуйте! Сейчас у меня нерабочее время, отвечу с утра."
)
DEFAULT_REVIEW_ASK_TEXT = (
    "🙏 Спасибо за покупку, {name}!\n"
    "Если всё устроило — буду очень благодарен за ⭐⭐⭐⭐⭐ отзыв "
    "к заказу #{order}. Это правда помогает 💛\n"
    "Если что-то пошло не так — напишите, постараюсь решить."
)


def _esc(text: str | None) -> str:
    if text is None:
        return ""
    return html.escape(str(text))


def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def quiet_hours_active(db: Database, tg_user_id: int) -> bool:
    """Are quiet hours currently active for this user (in UTC)?"""
    enabled = await db.get_state(tg_user_id, STATE_QUIET_ENABLED, "0") == "1"
    if not enabled:
        return False
    start = await db.get_state(tg_user_id, STATE_QUIET_START_MIN)
    end = await db.get_state(tg_user_id, STATE_QUIET_END_MIN)
    if start is None or end is None:
        return False
    try:
        start_min = int(start)
        end_min = int(end)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    now_min = now.hour * 60 + now.minute
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    # wraps midnight
    return now_min >= start_min or now_min < end_min


class Notifier:
    """Routes FunPay events to a specific Telegram user."""

    def __init__(self, bot: Bot, db: Database) -> None:
        self.bot = bot
        self.db = db

    async def send(
        self,
        tg_user_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        disable_notification: bool | None = None,
    ) -> bool:
        """Send a message to a specific Telegram user. Returns True on success."""
        if disable_notification is None:
            disable_notification = await quiet_hours_active(self.db, tg_user_id)
        try:
            await self.bot.send_message(
                tg_user_id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
            return True
        except TelegramBadRequest as e:
            msg = str(e).lower()
            if (
                "chat not found" in msg
                or "blocked by the user" in msg
                or "user is deactivated" in msg
            ):
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

    async def _build_message_keyboard(
        self, tg_user_id: int, chat_id: str | int
    ) -> InlineKeyboardMarkup:
        """Build an inline keyboard with Reply + up to 5 user templates."""
        rows: list[list[InlineKeyboardButton]] = []
        rows.append(
            [InlineKeyboardButton(text="💬 Ответить", callback_data=f"rpl:{chat_id}")]
        )
        templates = await self.db.template_list(tg_user_id)
        # Use up to 5 templates, 2 per row.
        templates = templates[:5]
        if templates:
            row: list[InlineKeyboardButton] = []
            for tpl_id, name, _text in templates:
                # Telegram callback_data limit is 64 bytes. Names are user-defined;
                # we encode by id only and look up the text server-side.
                btn_label = f"📝 {name}"[:30]
                row.append(
                    InlineKeyboardButton(
                        text=btn_label, callback_data=f"tpl:{tpl_id}:{chat_id}"
                    )
                )
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        chat_link = f"https://funpay.com/chat/?node={chat_id}"
        rows.append([InlineKeyboardButton(text="↗️ Открыть на FunPay", url=chat_link)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ---- event handlers ----

    async def handle_new_message(
        self,
        tg_user_id: int,
        account: "Account",
        event: "NewMessageEvent",
    ) -> None:
        msg = event.message

        if msg.author_id == account.id or msg.author_id == 0:
            return

        author = msg.author or "(unknown)"
        await self.db.log_message(tg_user_id, author, msg.chat_id, msg.text)

        if await self.db.is_blocked(tg_user_id, author):
            log.info("Skipping notification — %s blocked by tg_user=%s", author, tg_user_id)
            return

        body = msg.text or ("[изображение] " + (msg.image_link or ""))
        text = (
            "💬 <b>Новое сообщение на FunPay</b>\n"
            f"От: <b>{_esc(author)}</b>\n\n"
            f"{_esc(_truncate(body))}"
        )
        kb = await self._build_message_keyboard(tg_user_id, msg.chat_id)
        await self.send(tg_user_id, text, reply_markup=kb)

        autoreply_on = (
            await self.db.get_state(tg_user_id, STATE_AUTOREPLY_ENABLED, "0") == "1"
        )
        if autoreply_on and not await self.db.autoreply_already_sent(
            tg_user_id, msg.chat_id
        ):
            in_quiet = await quiet_hours_active(self.db, tg_user_id)
            if in_quiet:
                reply_text = (
                    await self.db.get_state(
                        tg_user_id,
                        STATE_AUTOREPLY_QUIET_TEXT,
                        DEFAULT_AUTOREPLY_QUIET_TEXT,
                    )
                    or DEFAULT_AUTOREPLY_QUIET_TEXT
                )
            else:
                reply_text = (
                    await self.db.get_state(
                        tg_user_id, STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT
                    )
                    or DEFAULT_AUTOREPLY_TEXT
                )
            try:
                await asyncio.to_thread(
                    safe_send_message,
                    account, int(msg.chat_id), reply_text, author,
                )
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

    async def handle_new_order(
        self, tg_user_id: int, account: "Account", event: "NewOrderEvent"
    ) -> None:
        order = event.order
        status_name = (
            order.status.name if hasattr(order.status, "name") else str(order.status)
        )
        await self.db.upsert_order(
            tg_user_id,
            order.id,
            order.buyer_username,
            float(order.price),
            order.description,
            status_name,
        )

        if not await self.db.is_blocked(tg_user_id, order.buyer_username):
            order_link = f"https://funpay.com/orders/{order.id}/"
            chat_link = f"https://funpay.com/chat/?node=users-{order.buyer_id}"
            text = (
                "🛒 <b>Новый заказ на FunPay</b>\n"
                f"Покупатель: <b>{_esc(order.buyer_username)}</b>\n"
                f"Лот: {_esc(order.description)}\n"
                f"Категория: {_esc(order.subcategory_name)}\n"
                f"Сумма: <b>{_esc(format_money(order.price))}</b>\n"
                f"Заказ: <a href=\"{_esc(order_link)}\">#{_esc(order.id)}</a>"
                f" · <a href=\"{_esc(chat_link)}\">чат с покупателем</a>"
            )
            await self.send(tg_user_id, text)

        # Auto-deliver (idempotent via delivery_log).
        autodeliver = (
            await self.db.get_state(tg_user_id, STATE_AUTODELIVER_ENABLED, "0") == "1"
        )
        if not autodeliver:
            return
        if status_name != "PAID":
            return
        await self._try_autodeliver(tg_user_id, account, order)

    async def _try_autodeliver(
        self, tg_user_id: int, account: "Account", order
    ) -> None:
        popped = await self.db.queue_pop(tg_user_id, str(order.id))
        if popped is None:
            count = await self.db.queue_count_available(tg_user_id)
            if count == 0:
                await self.send(
                    tg_user_id,
                    "⚠️ <b>Авто-выдача:</b> очередь пуста. "
                    "Пополни командой <code>/queue add &lt;текст&gt;</code> "
                    f"(заказ <code>#{_esc(order.id)}</code> покупателю "
                    f"<b>{_esc(order.buyer_username)}</b> остался без выдачи).",
                )
            return
        _qid, content = popped

        # Find the chat with the buyer.
        chat_id_to_send: int | None = None
        try:
            shortcut = await asyncio.to_thread(
                lambda: account.get_chat_by_name(order.buyer_username, make_request=True)
            )
            if shortcut is not None:
                chat_id_to_send = int(shortcut.id)
        except Exception as e:
            log.exception("get_chat_by_name failed for autodeliver: %s", e)

        if chat_id_to_send is None:
            await self.send(
                tg_user_id,
                "⚠️ <b>Авто-выдача:</b> не смог найти чат с "
                f"<b>{_esc(order.buyer_username)}</b>. Сообщение положено обратно "
                "(/queue list).",
            )
            return

        try:
            await asyncio.to_thread(
                safe_send_message,
                account, chat_id_to_send, content, order.buyer_username,
            )
        except Exception as e:
            log.exception("Autodeliver send_message failed: %s", e)
            await self.send(
                tg_user_id,
                f"❌ <b>Авто-выдача не доставлена</b> покупателю "
                f"<b>{_esc(order.buyer_username)}</b>: <code>{_esc(str(e))}</code>",
            )
            return

        await self.send(
            tg_user_id,
            "📤 <b>Авто-выдача отправлена</b>\n"
            f"Покупатель: <b>{_esc(order.buyer_username)}</b>\n"
            f"Заказ: <code>#{_esc(order.id)}</code>\n"
            f"Отправлено: <pre>{_esc(_truncate(content, 400))}</pre>\n"
            f"Осталось в очереди: <b>{await self.db.queue_count_available(tg_user_id)}</b>",
        )

    async def handle_order_status_changed(
        self,
        tg_user_id: int,
        account: "Account",
        event: "OrderStatusChangedEvent",
    ) -> None:
        order = event.order
        status_name = (
            order.status.name if hasattr(order.status, "name") else str(order.status)
        )
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
        extra = ""
        if status_name == "REFUNDED":
            extra = (
                "\n\n⚠️ <b>Внимание: возврат.</b> "
                "Если деньги уже списаны с твоего баланса — проверь, был ли отправлен "
                "товар."
            )
        text = (
            f"{emoji} <b>Статус заказа: {_esc(status_name)}</b>\n"
            f"Покупатель: <b>{_esc(order.buyer_username)}</b>\n"
            f"Лот: {_esc(order.description)}\n"
            f"Сумма: <b>{_esc(format_money(order.price))}</b>\n"
            f"Заказ: <a href=\"{_esc(order_link)}\">#{_esc(order.id)}</a>"
            f"{extra}"
        )
        await self.send(tg_user_id, text)

        # On CLOSED (buyer confirmed receipt) — ask for a review, idempotently.
        if status_name == "CLOSED":
            await self._maybe_send_review_ask(tg_user_id, account, order)

    async def _maybe_send_review_ask(
        self, tg_user_id: int, account: "Account", order
    ) -> None:
        enabled = (
            await self.db.get_state(tg_user_id, STATE_REVIEW_ASK_ENABLED, "0") == "1"
        )
        if not enabled:
            return
        if await self.db.review_ask_already_sent(tg_user_id, str(order.id)):
            return
        if await self.db.is_blocked(tg_user_id, order.buyer_username):
            return

        template = (
            await self.db.get_state(
                tg_user_id, STATE_REVIEW_ASK_TEXT, DEFAULT_REVIEW_ASK_TEXT
            )
            or DEFAULT_REVIEW_ASK_TEXT
        )
        try:
            msg_text = template.format(
                name=order.buyer_username,
                order=order.id,
                lot=order.description or "",
            )
        except (KeyError, IndexError, ValueError) as e:
            log.warning("review-ask template format failed for tg_user=%s: %s", tg_user_id, e)
            msg_text = DEFAULT_REVIEW_ASK_TEXT.format(
                name=order.buyer_username,
                order=order.id,
                lot=order.description or "",
            )

        chat_id_to_send: int | None = None
        try:
            shortcut = await asyncio.to_thread(
                lambda: account.get_chat_by_name(order.buyer_username, make_request=True)
            )
            if shortcut is not None:
                chat_id_to_send = int(shortcut.id)
        except Exception as e:
            log.exception("review-ask get_chat_by_name failed: %s", e)

        if chat_id_to_send is None:
            await self.send(
                tg_user_id,
                "⚠️ <b>Запрос отзыва не отправлен:</b> не нашёл чат с "
                f"<b>{_esc(order.buyer_username)}</b> (заказ "
                f"<code>#{_esc(order.id)}</code>).",
            )
            return

        try:
            await asyncio.to_thread(
                safe_send_message,
                account, chat_id_to_send, msg_text, order.buyer_username,
            )
        except Exception as e:
            log.exception("review-ask send_message failed: %s", e)
            await self.send(
                tg_user_id,
                "❌ <b>Не удалось отправить запрос отзыва</b> покупателю "
                f"<b>{_esc(order.buyer_username)}</b>: <code>{_esc(str(e))}</code>",
            )
            return

        await self.db.mark_review_ask_sent(tg_user_id, str(order.id))
        await self.send(
            tg_user_id,
            "⭐ <b>Запрос отзыва отправлен</b>\n"
            f"Покупатель: <b>{_esc(order.buyer_username)}</b>\n"
            f"Заказ: <code>#{_esc(order.id)}</code>",
        )

    async def handle_runner_error(self, tg_user_id: int, exc: BaseException) -> None:
        text = (
            "⚠️ <b>FunPay недоступен или сессия истекла</b>\n"
            f"Ошибка: <code>{_esc(_truncate(str(exc), 400))}</code>\n\n"
            "Скорее всего твой <code>golden_key</code> протух. Залогинься на funpay.com заново, "
            "достань новую куку и отправь её мне командой <code>/setkey НОВЫЙ_КЛЮЧ</code>."
        )
        await self.send(tg_user_id, text)
