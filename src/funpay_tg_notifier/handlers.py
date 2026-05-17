"""Telegram command handlers (multi-tenant + per-user feature set)."""

from __future__ import annotations

import asyncio
import logging
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from FunPayAPI import Account

from .db import Database
from .funpay_helpers import (
    bump_user_lots,
    clone_lot,
    edit_lot,
    format_balance,
    format_funpay_exc,
    get_balance_safe,
    list_user_lots,
    reprice_all,
    sum_paid_orders_via_api,
)
from .notifier import (
    DEFAULT_AUTOREPLY_QUIET_TEXT,
    DEFAULT_AUTOREPLY_TEXT,
    DEFAULT_REVIEW_ASK_TEXT,
    Notifier,
    STATE_AUTODELIVER_ENABLED,
    STATE_AUTOBUMP_ENABLED,
    STATE_AUTOBUMP_LAST_RUN_TS,
    STATE_AUTOREPLY_ENABLED,
    STATE_AUTOREPLY_QUIET_TEXT,
    STATE_AUTOREPLY_TEXT,
    STATE_DIGEST_ENABLED,
    STATE_DIGEST_HOUR_UTC,
    STATE_QUIET_ENABLED,
    STATE_QUIET_END_MIN,
    STATE_QUIET_START_MIN,
    STATE_REVIEW_ASK_ENABLED,
    STATE_REVIEW_ASK_TEXT,
)
from .runner_registry import RunnerRegistry

log = logging.getLogger(__name__)


# ---- FSM states ----


class ReplyStates(StatesGroup):
    waiting_for_text = State()


class TemplateStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_text = State()


class NewLotStates(StatesGroup):
    waiting_for_source_lot = State()
    waiting_for_new_title = State()
    waiting_for_new_price = State()


# ---- shared texts ----


WELCOME_TEXT = (
    "👋 <b>FunPay → Telegram notifier</b>\n\n"
    "Я пересылаю в Telegram события с твоего аккаунта на funpay.com и помогаю "
    "управлять лотами прямо из мессенджера:\n"
    "• новые сообщения покупателей (с кнопкой «Ответить» и шаблонами)\n"
    "• новые заказы и смена их статуса (включая возвраты)\n"
    "• авто-выдача товара по очереди (для цифровых ключей и т.п.)\n"
    "• тихие часы, автоответчик, ежедневная сводка\n"
    "• /lots, /bump, /clone, /newlot, /reprice — управление лотами\n\n"
    "<b>Как подключиться:</b>\n"
    "1. Залогинься на funpay.com в обычном браузере.\n"
    "2. F12 → Application → Cookies → <code>https://funpay.com</code> → "
    "найди <b>golden_key</b>, скопируй её значение.\n"
    "3. Отправь мне: <code>/setkey ТВОЙ_GOLDEN_KEY</code>\n"
    "4. Дополнительно: <code>/setua ТВОЙ_USER_AGENT</code>.\n\n"
    "⚠️ <b>golden_key — это полный доступ к твоему FunPay-аккаунту.</b> "
    "Отправляя его, ты доверяешь оператору этого бота. После /setkey "
    "<b>удали своё сообщение</b> вручную.\n\n"
    "/help — список команд."
)

HELP_TEXT = (
    "<b>Подключение</b>\n"
    "/setkey &lt;golden_key&gt; — подключить FunPay-аккаунт\n"
    "/setua &lt;user_agent&gt; — задать User-Agent (опционально)\n"
    "/pause /resume — приостановить / возобновить уведомления\n"
    "/unlink — стереть свои данные и отвязать аккаунт\n\n"
    "<b>Информация</b>\n"
    "/status — баланс, аккаунт, состояние\n"
    "/stats — сообщения и заказы за 1 / 7 / 30 дней\n"
    "/income — выручка по закрытым заказам (через FunPay)\n"
    "/lots — мои активные лоты\n\n"
    "<b>Ответы и шаблоны</b>\n"
    "Под каждым уведомлением о сообщении есть кнопка «Ответить» и кнопки шаблонов.\n"
    "/templates — список\n"
    "/templates add &lt;имя&gt; &lt;текст&gt; — создать шаблон\n"
    "/templates rm &lt;имя&gt; — удалить\n\n"
    "<b>Автоответчик</b>\n"
    "/autoreply on|off\n"
    "/autoreply set &lt;текст&gt;\n"
    "/autoreply quiet &lt;текст&gt; — ответ в тихие часы\n"
    "/autoreply show\n\n"
    "<b>Авто-запрос отзыва</b>\n"
    "/reviewask on|off — писать покупателю при закрытии заказа\n"
    "/reviewask set &lt;текст&gt; — свой текст (плейсхолдеры {name}, {order}, {lot})\n"
    "/reviewask show — посмотреть текущий текст\n\n"
    "<b>Лоты и поднятие</b>\n"
    "/lot_pause &lt;id&gt; · /lot_resume &lt;id&gt; · /lot_price &lt;id&gt; &lt;цена&gt;\n"
    "/bump — поднять все лоты в верх категории\n"
    "/autobump on|off — авто-поднятие каждые 4 часа\n"
    "/clone &lt;id&gt; [новая цена] — продублировать лот\n"
    "/newlot — пошаговый мастер клонирования\n"
    "/reprice &lt;+5%|-50|+50&gt; — массовое изменение цены\n\n"
    "<b>Авто-выдача</b>\n"
    "/queue add &lt;текст&gt; — добавить в очередь выдачи\n"
    "/queue list — посмотреть очередь\n"
    "/queue clear — очистить очередь\n"
    "/autodeliver on|off — выдавать автоматически при оплате\n\n"
    "<b>Тихие часы, сводки и заглушения</b>\n"
    "/quiet 23:00-08:00 — задать тихие часы (UTC)\n"
    "/quiet off — отключить\n"
    "/digest on|off — ежедневная сводка\n"
    "/digest hour &lt;0-23&gt; — час отправки (UTC)\n"
    "/block &lt;ник&gt; · /unblock &lt;ник&gt; · /blocked\n"
)


def _esc(text: str) -> str:
    import html as _html
    return _html.escape(str(text))


def _parse_quiet_range(arg: str) -> tuple[int, int] | None:
    m = re.fullmatch(
        r"\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s*", arg
    )
    if not m:
        return None
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def _format_quiet_range(start_min: int, end_min: int) -> str:
    def hm(x: int) -> str:
        return f"{x // 60:02d}:{x % 60:02d}"
    return f"{hm(start_min)}–{hm(end_min)}"


def register_handlers(
    dp: Dispatcher,
    bot: Bot,
    db: Database,
    notifier: Notifier,
    registry: RunnerRegistry,
    admin_tg_user_id: int | None,
) -> None:
    # ---- helpers ----

    async def _ensure_user_or_hint(message: Message) -> bool:
        u = await db.get_user(message.from_user.id)
        if u is None:
            await message.answer(
                "Сначала подключи FunPay-аккаунт командой "
                "<code>/setkey ТВОЙ_GOLDEN_KEY</code>. Подробности — /start."
            )
            return False
        return True

    def _account_or_warn(tg_id: int) -> Account | None:
        return registry.get_account(tg_id)

    # ---- /start, /help ----

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

    # ---- /setkey, /setua, /unlink, /pause, /resume ----

    @dp.message(Command("setkey"))
    async def cmd_setkey(message: Message, command: CommandObject) -> None:
        key = (command.args or "").strip()
        if not key:
            await message.answer(
                "Использование: <code>/setkey ТВОЙ_GOLDEN_KEY</code>\n\n"
                "После отправки <b>удали своё сообщение</b>."
            )
            return
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except TelegramAPIError:
            pass

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
                f"<code>{_esc(type(e).__name__)}: {_esc(format_funpay_exc(e))}</code>\n\n"
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
            f"✅ Подключён аккаунт <b>{_esc(account.username)}</b> "
            f"(id <code>{account.id}</code>).\n"
            "Уведомления уже включены. /help — список команд.\n\n"
            "💡 Совет: <b>удали выше своё сообщение с ключом</b>."
        )

    @dp.message(Command("setua"))
    async def cmd_setua(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        ua = (command.args or "").strip()
        u = await db.get_user(message.from_user.id)
        if u is None:
            return
        if not ua:
            await message.answer(
                "Использование: <code>/setua Mozilla/5.0 ...</code>\n"
                f"Текущий: <code>{_esc(u.user_agent or '(не задан)')}</code>"
            )
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

    # ---- /status, /stats, /income ----

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        autoreply_on = await db.get_state(tg_id, STATE_AUTOREPLY_ENABLED, "0") == "1"
        autodeliver_on = await db.get_state(tg_id, STATE_AUTODELIVER_ENABLED, "0") == "1"
        autobump_on = await db.get_state(tg_id, STATE_AUTOBUMP_ENABLED, "0") == "1"
        digest_on = await db.get_state(tg_id, STATE_DIGEST_ENABLED, "0") == "1"
        quiet_on = await db.get_state(tg_id, STATE_QUIET_ENABLED, "0") == "1"
        review_ask_on = (
            await db.get_state(tg_id, STATE_REVIEW_ASK_ENABLED, "0") == "1"
        )
        running = registry.is_running(tg_id)
        queue_left = await db.queue_count_available(tg_id)
        if acc is None:
            await message.answer(
                f"⏳ Раннер {'запускается' if running else 'не работает'}. "
                "Попробуй /status через несколько секунд."
            )
            return
        bal = await get_balance_safe(tg_id, acc, db)
        text = (
            "<b>Статус</b>\n"
            f"Аккаунт: <b>{_esc(acc.username)}</b> (id <code>{acc.id}</code>)\n"
            f"Баланс: {format_balance(bal)}\n"
            f"Активные продажи: {acc.active_sales}\n"
            f"Активные покупки: {acc.active_purchases}\n"
            f"Раннер: <b>{'работает' if running else 'остановлен'}</b>\n\n"
            "<b>Режимы</b>\n"
            f"  🤖 Автоответчик: <b>{'ON' if autoreply_on else 'OFF'}</b>\n"
            f"  📤 Авто-выдача: <b>{'ON' if autodeliver_on else 'OFF'}</b> "
            f"(в очереди: {queue_left})\n"
            f"  🔝 Авто-поднятие: <b>{'ON' if autobump_on else 'OFF'}</b>\n"
            f"  🌙 Тихие часы: <b>{'ON' if quiet_on else 'OFF'}</b>\n"
            f"  📊 Ежедневная сводка: <b>{'ON' if digest_on else 'OFF'}</b>\n"
            f"  ⭐ Авто-запрос отзыва: <b>{'ON' if review_ask_on else 'OFF'}</b>"
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
            "<b>Статистика (по событиям бота)</b>\n\n"
            "<b>Сообщения</b>\n"
            f"  сегодня: {msgs_today}\n"
            f"  7 дней: {msgs_week}\n"
            f"  30 дней: {msgs_month}\n\n"
            "<b>Заказы</b>\n"
            f"  сегодня: {orders_today_n} ({orders_today_sum:.2f} ₽)\n"
            f"  7 дней: {orders_week_n} ({orders_week_sum:.2f} ₽)\n"
            f"  30 дней: {orders_month_n} ({orders_month_sum:.2f} ₽)\n\n"
            "<i>Счётчики наполняются с момента подключения. "
            "Для исторических данных используй /income.</i>"
        )

    @dp.message(Command("income"))
    async def cmd_income(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен. Подожди и попробуй снова.")
            return
        progress = await message.answer("Считаю выручку по заказам с FunPay…")
        try:
            n7, sum7 = await asyncio.to_thread(sum_paid_orders_via_api, acc, 7)
            n30, sum30 = await asyncio.to_thread(sum_paid_orders_via_api, acc, 30)
        except Exception as e:
            await progress.edit_text(f"❌ Не получилось: <code>{_esc(format_funpay_exc(e))}</code>")
            return
        await progress.edit_text(
            "<b>Выручка (PAID + CLOSED)</b>\n"
            f"  7 дней: <b>{n7}</b> заказов, <b>{sum7:.2f} ₽</b>\n"
            f"  30 дней: <b>{n30}</b> заказов, <b>{sum30:.2f} ₽</b>"
        )

    # ---- block/unblock ----

    @dp.message(Command("block"))
    async def cmd_block(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        username = (command.args or "").strip()
        if not username:
            await message.answer("Использование: <code>/block ник_на_funpay</code>")
            return
        await db.block(message.from_user.id, username)
        await message.answer(f"🔇 Уведомления от <b>{_esc(username)}</b> приглушены.")

    @dp.message(Command("unblock"))
    async def cmd_unblock(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        username = (command.args or "").strip()
        if not username:
            await message.answer("Использование: <code>/unblock ник_на_funpay</code>")
            return
        await db.unblock(message.from_user.id, username)
        await message.answer(f"🔔 Уведомления от <b>{_esc(username)}</b> снова включены.")

    @dp.message(Command("blocked"))
    async def cmd_blocked(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        users = await db.list_blocked(message.from_user.id)
        if not users:
            await message.answer("Список заглушённых пуст.")
            return
        await message.answer(
            "<b>Заглушённые пользователи:</b>\n" + "\n".join(f"• {_esc(u)}" for u in users)
        )

    # ---- /autoreply ----

    @dp.message(Command("autoreply"))
    async def cmd_autoreply(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        args = (command.args or "").strip()
        if not args:
            current = await db.get_state(tg_id, STATE_AUTOREPLY_ENABLED, "0") == "1"
            text_now = await db.get_state(tg_id, STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT)
            quiet_text = await db.get_state(
                tg_id, STATE_AUTOREPLY_QUIET_TEXT, DEFAULT_AUTOREPLY_QUIET_TEXT
            )
            await message.answer(
                f"Автоответчик: <b>{'ON' if current else 'OFF'}</b>\n\n"
                f"Дневной текст:\n<pre>{_esc(text_now or '')}</pre>\n\n"
                f"Ночной (тихие часы):\n<pre>{_esc(quiet_text or '')}</pre>\n\n"
                "Команды:\n"
                "  /autoreply on  /autoreply off\n"
                "  /autoreply set &lt;текст&gt;\n"
                "  /autoreply quiet &lt;текст&gt;\n"
                "  /autoreply show"
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
            await message.answer(f"Текст автоответа обновлён:\n<pre>{_esc(new_text)}</pre>")
        elif sub == "quiet":
            new_text = rest.strip()
            if not new_text:
                await message.answer(
                    "Укажи текст: <code>/autoreply quiet Сейчас ночь, отвечу с утра</code>"
                )
                return
            await db.set_state(tg_id, STATE_AUTOREPLY_QUIET_TEXT, new_text)
            await message.answer(
                f"Ночной автоответ обновлён:\n<pre>{_esc(new_text)}</pre>"
            )
        elif sub == "show":
            text_now = await db.get_state(tg_id, STATE_AUTOREPLY_TEXT, DEFAULT_AUTOREPLY_TEXT)
            await message.answer(f"<pre>{_esc(text_now or '')}</pre>")
        else:
            await message.answer("Неизвестная подкоманда. /autoreply без аргументов — справка.")

    # ---- /reviewask (автоматический запрос отзыва после закрытия заказа) ----

    @dp.message(Command("reviewask"))
    async def cmd_reviewask(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        args = (command.args or "").strip()
        if not args:
            enabled = (
                await db.get_state(tg_id, STATE_REVIEW_ASK_ENABLED, "0") == "1"
            )
            text_now = await db.get_state(
                tg_id, STATE_REVIEW_ASK_TEXT, DEFAULT_REVIEW_ASK_TEXT
            )
            await message.answer(
                "⭐ <b>Авто-запрос отзыва</b> при закрытии заказа: "
                f"<b>{'ON' if enabled else 'OFF'}</b>\n\n"
                f"Текст:\n<pre>{_esc(text_now or '')}</pre>\n\n"
                "Плейсхолдеры: <code>{name}</code> (ник покупателя), "
                "<code>{order}</code> (номер), <code>{lot}</code> (название лота).\n\n"
                "Команды:\n"
                "  /reviewask on  /reviewask off\n"
                "  /reviewask set &lt;текст&gt;\n"
                "  /reviewask reset — вернуть текст по умолчанию\n"
                "  /reviewask show"
            )
            return
        sub, _, rest = args.partition(" ")
        sub = sub.lower()
        if sub == "on":
            await db.set_state(tg_id, STATE_REVIEW_ASK_ENABLED, "1")
            await message.answer(
                "⭐ Авто-запрос отзыва <b>ON</b> — буду писать покупателям "
                "сразу как они подтверждают заказ."
            )
        elif sub == "off":
            await db.set_state(tg_id, STATE_REVIEW_ASK_ENABLED, "0")
            await message.answer("⭐ Авто-запрос отзыва <b>OFF</b>")
        elif sub == "set":
            new_text = rest.strip()
            if not new_text:
                await message.answer(
                    "Укажи текст: <code>/reviewask set Спасибо, {name}! "
                    "Оставь отзыв к заказу #{order}, если всё ок 🙏</code>"
                )
                return
            # Validate placeholders won't blow up.
            try:
                new_text.format(name="test", order="42", lot="lot")
            except (KeyError, IndexError, ValueError) as e:
                await message.answer(
                    f"❌ Текст не принят: <code>{_esc(format_funpay_exc(e))}</code>\n"
                    "Используй только плейсхолдеры <code>{name}</code>, "
                    "<code>{order}</code>, <code>{lot}</code>."
                )
                return
            await db.set_state(tg_id, STATE_REVIEW_ASK_TEXT, new_text)
            await message.answer(
                f"Текст обновлён:\n<pre>{_esc(new_text)}</pre>"
            )
        elif sub == "reset":
            await db.set_state(tg_id, STATE_REVIEW_ASK_TEXT, DEFAULT_REVIEW_ASK_TEXT)
            await message.answer(
                f"Текст сброшен на дефолтный:\n<pre>{_esc(DEFAULT_REVIEW_ASK_TEXT)}</pre>"
            )
        elif sub == "show":
            text_now = await db.get_state(
                tg_id, STATE_REVIEW_ASK_TEXT, DEFAULT_REVIEW_ASK_TEXT
            )
            await message.answer(f"<pre>{_esc(text_now or '')}</pre>")
        else:
            await message.answer(
                "Неизвестная подкоманда. /reviewask без аргументов — справка."
            )

    # ---- /templates ----

    @dp.message(Command("templates"))
    async def cmd_templates(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        args = (command.args or "").strip()
        if not args:
            tpls = await db.template_list(tg_id)
            if not tpls:
                await message.answer(
                    "Шаблонов пока нет. Создай: "
                    "<code>/templates add отказ Извини, продано</code>"
                )
                return
            lines = ["<b>Твои шаблоны</b>\n"]
            for _id, name, text in tpls:
                preview = text if len(text) < 80 else text[:79] + "…"
                lines.append(f"• <b>{_esc(name)}</b>: <i>{_esc(preview)}</i>")
            lines.append(
                "\nДобавить: <code>/templates add имя текст</code>\n"
                "Удалить: <code>/templates rm имя</code>\n"
                "Первые 5 шаблонов появятся кнопками под каждым уведомлением."
            )
            await message.answer("\n".join(lines))
            return
        sub, _, rest = args.partition(" ")
        sub = sub.lower()
        if sub == "add":
            name, _, text = rest.partition(" ")
            name = name.strip()
            text = text.strip()
            if not name or not text:
                await message.answer(
                    "Использование: <code>/templates add имя текст ответа</code>"
                )
                return
            await db.template_add(tg_id, name, text)
            await message.answer(f"✅ Шаблон <b>{_esc(name)}</b> сохранён.")
        elif sub in ("rm", "del", "remove"):
            name = rest.strip()
            if not name:
                await message.answer("Использование: <code>/templates rm имя</code>")
                return
            removed = await db.template_remove(tg_id, name)
            await message.answer(
                f"🗑 Удалён шаблон <b>{_esc(name)}</b>."
                if removed
                else f"Шаблон <b>{_esc(name)}</b> не найден."
            )
        else:
            await message.answer("Неизвестная подкоманда. /templates — справка.")

    # ---- /lots, /lot_pause, /lot_resume, /lot_price ----

    @dp.message(Command("lots"))
    async def cmd_lots(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен.")
            return
        try:
            lots = await asyncio.to_thread(list_user_lots, acc)
        except Exception as e:
            await message.answer(f"❌ Не удалось получить лоты: <code>{_esc(format_funpay_exc(e))}</code>")
            return
        if not lots:
            await message.answer("Активных лотов нет.")
            return
        lines = [f"<b>Активные лоты ({len(lots)})</b>\n"]
        for lot in lots[:30]:
            title = lot.description or "(без названия)"
            lines.append(
                f"<code>{lot.id}</code> · {lot.price:.2f} ₽ · {_esc(title[:60])}"
            )
        if len(lots) > 30:
            lines.append(f"\n…и ещё {len(lots) - 30}. (показано 30)")
        lines.append(
            "\nКоманды: /lot_pause &lt;id&gt;  /lot_resume &lt;id&gt;  "
            "/lot_price &lt;id&gt; &lt;цена&gt;\n"
            "/clone &lt;id&gt; [цена] — продублировать"
        )
        await message.answer("\n".join(lines))

    @dp.message(Command("lot_pause"))
    async def cmd_lot_pause(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        await _edit_lot_cmd(message, command, active=False, label="приостановлен")

    @dp.message(Command("lot_resume"))
    async def cmd_lot_resume(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        await _edit_lot_cmd(message, command, active=True, label="активирован")

    @dp.message(Command("lot_price"))
    async def cmd_lot_price(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        args = (command.args or "").split()
        if len(args) < 2:
            await message.answer("Использование: <code>/lot_price &lt;id&gt; &lt;цена&gt;</code>")
            return
        try:
            lot_id = int(args[0])
            new_price = float(args[1].replace(",", "."))
        except ValueError:
            await message.answer("ID и цена должны быть числами.")
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен.")
            return
        try:
            await asyncio.to_thread(edit_lot, acc, lot_id, price=new_price)
        except Exception as e:
            await message.answer(f"❌ <code>{_esc(format_funpay_exc(e))}</code>")
            return
        await message.answer(
            f"💰 Лот <code>{lot_id}</code> теперь стоит <b>{new_price:.2f} ₽</b>"
        )

    async def _edit_lot_cmd(
        message: Message, command: CommandObject, *, active: bool, label: str
    ) -> None:
        arg = (command.args or "").strip()
        if not arg:
            await message.answer(f"Использование: <code>/{command.command} &lt;lot_id&gt;</code>")
            return
        try:
            lot_id = int(arg)
        except ValueError:
            await message.answer("ID лота должен быть числом.")
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен.")
            return
        try:
            await asyncio.to_thread(edit_lot, acc, lot_id, active=active)
        except Exception as e:
            await message.answer(f"❌ <code>{_esc(format_funpay_exc(e))}</code>")
            return
        await message.answer(f"Лот <code>{lot_id}</code> {label}.")

    # ---- /bump, /autobump ----

    @dp.message(Command("bump"))
    async def cmd_bump(message: Message) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен.")
            return
        progress = await message.answer("Поднимаю лоты…")
        try:
            bumped, failed = await asyncio.to_thread(bump_user_lots, acc)
        except Exception as e:
            await progress.edit_text(f"❌ <code>{_esc(format_funpay_exc(e))}</code>")
            return
        if not bumped and not failed:
            await progress.edit_text("Нет лотов для поднятия.")
            return
        await db.set_state(tg_id, STATE_AUTOBUMP_LAST_RUN_TS, str(int(time.time())))
        msg = ""
        if bumped:
            msg += f"🔝 Подняты категории: <b>{_esc(', '.join(sorted(bumped)))}</b>\n"
        if failed:
            msg += (
                f"⌛ Не подняты (вероятно, ещё не прошёл 4-часовой кулдаун FunPay): "
                f"<b>{_esc(', '.join(sorted(failed)))}</b>"
            )
        await progress.edit_text(msg)

    @dp.message(Command("autobump"))
    async def cmd_autobump(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        arg = (command.args or "").strip().lower()
        current = await db.get_state(tg_id, STATE_AUTOBUMP_ENABLED, "0") == "1"
        if not arg:
            await message.answer(
                f"Авто-поднятие: <b>{'ON' if current else 'OFF'}</b>\n"
                "Включить: <code>/autobump on</code>\n"
                "Выключить: <code>/autobump off</code>\n"
                "Раз в ~4ч пытаюсь поднять все твои лоты в верх категории."
            )
            return
        if arg == "on":
            await db.set_state(tg_id, STATE_AUTOBUMP_ENABLED, "1")
            await message.answer("🔝 Авто-поднятие <b>ON</b>")
        elif arg == "off":
            await db.set_state(tg_id, STATE_AUTOBUMP_ENABLED, "0")
            await message.answer("🔝 Авто-поднятие <b>OFF</b>")
        else:
            await message.answer("Использование: <code>/autobump on|off</code>")

    # ---- /clone, /newlot, /reprice ----

    @dp.message(Command("clone"))
    async def cmd_clone(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        parts = (command.args or "").split(maxsplit=1)
        if not parts:
            await message.answer(
                "Использование: <code>/clone &lt;lot_id&gt; [новая цена]</code>"
            )
            return
        try:
            lot_id = int(parts[0])
        except ValueError:
            await message.answer("ID лота должен быть числом.")
            return
        new_price: float | None = None
        if len(parts) > 1:
            try:
                new_price = float(parts[1].replace(",", "."))
            except ValueError:
                await message.answer("Цена должна быть числом.")
                return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен.")
            return
        progress = await message.answer("Клонирую лот…")
        try:
            new_id = await asyncio.to_thread(
                clone_lot, acc, lot_id, price=new_price
            )
        except Exception as e:
            await progress.edit_text(f"❌ <code>{_esc(format_funpay_exc(e))}</code>")
            return
        if new_id:
            await progress.edit_text(
                f"📋 Новый лот создан: <code>{new_id}</code>"
                + (f" (цена <b>{new_price:.2f} ₽</b>)" if new_price else "")
            )
        else:
            await progress.edit_text(
                "Сохранение прошло, но я не нашёл новый лот по названию. "
                "Проверь /lots — возможно, он там."
            )

    @dp.message(Command("newlot"))
    async def cmd_newlot(message: Message, state: FSMContext) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен.")
            return
        try:
            lots = await asyncio.to_thread(list_user_lots, acc)
        except Exception as e:
            await message.answer(f"❌ Не получилось получить лоты: <code>{_esc(format_funpay_exc(e))}</code>")
            return
        if not lots:
            await message.answer(
                "У тебя пока нет ни одного лота. Создание с нуля через FunPay-сайт, "
                "потом возвращайся — здесь удобно клонировать."
            )
            return
        lines = ["<b>Мастер создания лота</b>\nКлонируем один из твоих:"]
        for lot in lots[:20]:
            lines.append(
                f"<code>{lot.id}</code> · {lot.price:.2f} ₽ · "
                f"{_esc((lot.description or '')[:60])}"
            )
        lines.append("\nОтправь ID лота, который копировать.")
        await message.answer("\n".join(lines))
        await state.set_state(NewLotStates.waiting_for_source_lot)

    @dp.message(NewLotStates.waiting_for_source_lot)
    async def newlot_source(message: Message, state: FSMContext) -> None:
        try:
            lot_id = int((message.text or "").strip())
        except ValueError:
            await message.answer("Это не число. Отправь ID лота или /cancel.")
            return
        await state.update_data(source_lot_id=lot_id)
        await message.answer(
            "Окей. Теперь пришли <b>новое название</b> для копии "
            "(или <code>=</code> чтобы оставить как у оригинала)."
        )
        await state.set_state(NewLotStates.waiting_for_new_title)

    @dp.message(NewLotStates.waiting_for_new_title)
    async def newlot_title(message: Message, state: FSMContext) -> None:
        title = (message.text or "").strip()
        if title == "=":
            title = ""
        await state.update_data(new_title=title)
        await message.answer(
            "И <b>новую цену в ₽</b> "
            "(или <code>=</code> чтобы оставить как у оригинала)."
        )
        await state.set_state(NewLotStates.waiting_for_new_price)

    @dp.message(NewLotStates.waiting_for_new_price)
    async def newlot_price(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        new_price: float | None = None
        if raw != "=":
            try:
                new_price = float(raw.replace(",", "."))
            except ValueError:
                await message.answer("Не понял цену. Введи число или <code>=</code>.")
                return
        data = await state.get_data()
        await state.clear()
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер не работает, попробуй позже.")
            return
        title = data.get("new_title") or None
        lot_id = int(data["source_lot_id"])
        progress = await message.answer("Создаю клон…")
        try:
            new_id = await asyncio.to_thread(
                clone_lot, acc, lot_id, price=new_price, title_ru=title
            )
        except Exception as e:
            await progress.edit_text(f"❌ <code>{_esc(format_funpay_exc(e))}</code>")
            return
        await progress.edit_text(
            "📋 Новый лот создан"
            + (f" (id <code>{new_id}</code>)" if new_id else "")
            + (f", цена <b>{new_price:.2f} ₽</b>" if new_price is not None else "")
        )

    @dp.message(Command("reprice"))
    async def cmd_reprice(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        arg = (command.args or "").strip().replace(" ", "")
        m_pct = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)%", arg)
        m_delta = re.fullmatch(r"([+-]\d+(?:\.\d+)?)", arg)
        if not (m_pct or m_delta):
            await message.answer(
                "Использование:\n"
                "<code>/reprice +5%</code> — поднять все цены на 5%\n"
                "<code>/reprice -10%</code> — опустить на 10%\n"
                "<code>/reprice +50</code> — добавить 50 ₽ к каждому лоту\n"
                "<code>/reprice -25</code> — вычесть 25 ₽"
            )
            return
        tg_id = message.from_user.id
        acc = _account_or_warn(tg_id)
        if acc is None:
            await message.answer("Раннер ещё не подгружен.")
            return
        progress = await message.answer("Перецениваю все лоты…")
        try:
            if m_pct:
                pct = float(m_pct.group(1))
                upd, fail = await asyncio.to_thread(reprice_all, acc, percent=pct)
                label = f"{'+' if pct >= 0 else ''}{pct}%"
            else:
                delta = float(m_delta.group(1))
                upd, fail = await asyncio.to_thread(reprice_all, acc, delta=delta)
                label = f"{'+' if delta >= 0 else ''}{delta} ₽"
        except Exception as e:
            await progress.edit_text(f"❌ <code>{_esc(format_funpay_exc(e))}</code>")
            return
        await progress.edit_text(
            f"✅ Перецененно: <b>{upd}</b> ({label})\n"
            f"Ошибки: <b>{fail}</b>"
        )

    # ---- /queue, /autodeliver ----

    @dp.message(Command("queue"))
    async def cmd_queue(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        args = (command.args or "").strip()
        if not args:
            await message.answer(
                "<b>Авто-выдача</b>\n"
                "Положи сюда заранее заготовленные ключи/инструкции/тексты — "
                "при оплате заказа покупателю автоматически отправится "
                "следующий элемент очереди.\n\n"
                "Команды:\n"
                "  /queue add &lt;текст&gt; — добавить (можно много раз)\n"
                "  /queue list — посмотреть, что в очереди\n"
                "  /queue clear — стереть всё\n"
                "  /autodeliver on|off — включить/выключить авто-выдачу"
            )
            return
        sub, _, rest = args.partition(" ")
        sub = sub.lower()
        if sub == "add":
            content = rest.strip()
            if not content:
                await message.answer("Использование: <code>/queue add &lt;текст&gt;</code>")
                return
            qid = await db.queue_add(tg_id, content)
            left = await db.queue_count_available(tg_id)
            await message.answer(
                f"📦 Добавлено (id <code>{qid}</code>). В очереди: <b>{left}</b>"
            )
        elif sub == "list":
            items = await db.queue_list(tg_id, limit=20, only_unused=True)
            if not items:
                await message.answer("Очередь пуста.")
                return
            lines = [f"<b>В очереди ({len(items)})</b>"]
            for qid, content, _used in items:
                preview = content if len(content) < 60 else content[:59] + "…"
                lines.append(f"<code>{qid}</code> · {_esc(preview)}")
            await message.answer("\n".join(lines))
        elif sub == "clear":
            n = await db.queue_clear_all(tg_id)
            await message.answer(f"🗑 Удалено: <b>{n}</b>")
        else:
            await message.answer("Неизвестная подкоманда. /queue — справка.")

    @dp.message(Command("autodeliver"))
    async def cmd_autodeliver(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        arg = (command.args or "").strip().lower()
        current = await db.get_state(tg_id, STATE_AUTODELIVER_ENABLED, "0") == "1"
        if not arg:
            left = await db.queue_count_available(tg_id)
            await message.answer(
                f"Авто-выдача: <b>{'ON' if current else 'OFF'}</b>\n"
                f"В очереди: <b>{left}</b>\n\n"
                "Включить: <code>/autodeliver on</code>\n"
                "Выключить: <code>/autodeliver off</code>\n"
                "При новом ОПЛАЧЕННОМ заказе бот достанет следующий "
                "элемент очереди и отправит его покупателю в чат."
            )
            return
        if arg == "on":
            await db.set_state(tg_id, STATE_AUTODELIVER_ENABLED, "1")
            await message.answer("📤 Авто-выдача <b>ON</b>")
        elif arg == "off":
            await db.set_state(tg_id, STATE_AUTODELIVER_ENABLED, "0")
            await message.answer("📤 Авто-выдача <b>OFF</b>")
        else:
            await message.answer("Использование: <code>/autodeliver on|off</code>")

    # ---- /quiet, /digest ----

    @dp.message(Command("quiet"))
    async def cmd_quiet(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        arg = (command.args or "").strip()
        if not arg:
            enabled = await db.get_state(tg_id, STATE_QUIET_ENABLED, "0") == "1"
            start = await db.get_state(tg_id, STATE_QUIET_START_MIN)
            end = await db.get_state(tg_id, STATE_QUIET_END_MIN)
            rng = (
                _format_quiet_range(int(start), int(end))
                if (start and end)
                else "(не задано)"
            )
            await message.answer(
                f"Тихие часы (UTC): <b>{'ON' if enabled else 'OFF'}</b>, диапазон: {rng}\n\n"
                "Задать: <code>/quiet 23:00-08:00</code>\n"
                "Выключить: <code>/quiet off</code>\n"
                "Во время тихих часов уведомления приходят без звука и "
                "автоответ использует ночной текст (/autoreply quiet)."
            )
            return
        if arg.lower() == "off":
            await db.set_state(tg_id, STATE_QUIET_ENABLED, "0")
            await message.answer("🌙 Тихие часы <b>OFF</b>")
            return
        rng = _parse_quiet_range(arg)
        if rng is None:
            await message.answer(
                "Не понял формат. Пример: <code>/quiet 23:00-08:00</code>"
            )
            return
        start_min, end_min = rng
        await db.set_state(tg_id, STATE_QUIET_START_MIN, str(start_min))
        await db.set_state(tg_id, STATE_QUIET_END_MIN, str(end_min))
        await db.set_state(tg_id, STATE_QUIET_ENABLED, "1")
        await message.answer(
            f"🌙 Тихие часы: <b>{_format_quiet_range(start_min, end_min)}</b> (UTC). "
            "Включено."
        )

    @dp.message(Command("digest"))
    async def cmd_digest(message: Message, command: CommandObject) -> None:
        if not await _ensure_user_or_hint(message):
            return
        tg_id = message.from_user.id
        arg = (command.args or "").strip()
        enabled = await db.get_state(tg_id, STATE_DIGEST_ENABLED, "0") == "1"
        if not arg:
            hour = await db.get_state(tg_id, STATE_DIGEST_HOUR_UTC, "21")
            await message.answer(
                f"Ежедневная сводка: <b>{'ON' if enabled else 'OFF'}</b>\n"
                f"Час отправки (UTC): <b>{hour}</b>\n\n"
                "Включить: <code>/digest on</code>\n"
                "Выключить: <code>/digest off</code>\n"
                "Сменить час: <code>/digest hour 21</code>"
            )
            return
        parts = arg.split()
        sub = parts[0].lower()
        if sub == "on":
            await db.set_state(tg_id, STATE_DIGEST_ENABLED, "1")
            await message.answer("📊 Ежедневная сводка <b>ON</b>")
        elif sub == "off":
            await db.set_state(tg_id, STATE_DIGEST_ENABLED, "0")
            await message.answer("📊 Ежедневная сводка <b>OFF</b>")
        elif sub == "hour" and len(parts) == 2:
            try:
                h = int(parts[1])
                assert 0 <= h <= 23
            except (ValueError, AssertionError):
                await message.answer("Час должен быть в диапазоне 0..23.")
                return
            await db.set_state(tg_id, STATE_DIGEST_HOUR_UTC, str(h))
            await message.answer(f"📊 Час отправки сводки: <b>{h}:00 UTC</b>")
        else:
            await message.answer("Использование: <code>/digest on|off|hour N</code>")

    # ---- inline-callbacks: reply, template ----

    @dp.callback_query(F.data.startswith("rpl:"))
    async def cb_reply(cb: CallbackQuery, state: FSMContext) -> None:
        u = await db.get_user(cb.from_user.id)
        if u is None:
            await cb.answer("Сначала /setkey")
            return
        chat_id = cb.data.split(":", 1)[1]
        await state.set_state(ReplyStates.waiting_for_text)
        await state.update_data(target_chat_id=chat_id)
        await cb.answer()
        await cb.message.reply(
            "✍️ Введи текст ответа на FunPay. Отправь /cancel чтобы отменить."
        )

    @dp.callback_query(F.data.startswith("tpl:"))
    async def cb_template(cb: CallbackQuery) -> None:
        u = await db.get_user(cb.from_user.id)
        if u is None:
            await cb.answer("Сначала /setkey", show_alert=True)
            return
        try:
            _, tpl_id_raw, chat_id_raw = cb.data.split(":", 2)
            tpl_id = int(tpl_id_raw)
        except (ValueError, IndexError):
            await cb.answer("Некорректные данные", show_alert=True)
            return
        tpl = await db.template_get(cb.from_user.id, tpl_id)
        if tpl is None:
            await cb.answer("Шаблон удалён", show_alert=True)
            return
        name, text = tpl
        acc = _account_or_warn(cb.from_user.id)
        if acc is None:
            await cb.answer("Раннер не работает", show_alert=True)
            return
        try:
            await asyncio.to_thread(
                lambda: acc.send_message(int(chat_id_raw), text)
            )
        except Exception as e:
            await cb.answer(f"Ошибка: {e}"[:200], show_alert=True)
            return
        await cb.answer(f"📝 Отправлен шаблон «{name}»")

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        if current is None:
            return
        await state.clear()
        await message.answer("Отменено.")

    @dp.message(ReplyStates.waiting_for_text)
    async def reply_text_received(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        chat_id = data.get("target_chat_id")
        if not chat_id:
            await state.clear()
            return
        text = (message.text or "").strip()
        if not text:
            await message.answer("Пустой текст — отменяю.")
            await state.clear()
            return
        acc = _account_or_warn(message.from_user.id)
        if acc is None:
            await message.answer("Раннер не работает.")
            await state.clear()
            return
        try:
            await asyncio.to_thread(
                lambda: acc.send_message(int(chat_id), text)
            )
        except Exception as e:
            await message.answer(f"❌ Не отправилось: <code>{_esc(format_funpay_exc(e))}</code>")
            await state.clear()
            return
        await message.answer("✅ Отправил.")
        await state.clear()

    # ---- admin ----

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
                "<b>Админ-панель</b>",
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
                    f"{_esc(u.funpay_username or '?')}"
                )
            if len(users) > 50:
                lines.append(f"\n…и ещё {len(users) - 50}.")
            await message.answer("\n".join(lines))
