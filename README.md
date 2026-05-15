# FunPay → Telegram Notifier

Лёгкий Python-скрипт, который:

- ловит события с FunPay (через библиотеку [`FunPayAPI`](https://pypi.org/project/FunPayAPI/) — она ходит на FunPay с твоим `golden_key` cookie, никаких официальных API у FunPay нет);
- шлёт уведомления тебе в Telegram (бот, которым управляет только твой `chat_id`);
- умеет автоответчик, статистику, приглушение конкретных пользователей и оповещения о смене статусов заказов.

Сделано так, чтобы запускаться у тебя дома: на ПК с Windows / Linux / macOS или на Raspberry Pi. RAM ~50 МБ, никаких внешних сервисов кроме Telegram.

## Возможности

| Что | Команда / источник | Комментарий |
|---|---|---|
| Уведомление о новом сообщении на FunPay | автоматически | присылается в Telegram с ником, ссылкой на чат и текстом сообщения |
| Уведомление о новом заказе | автоматически | ник покупателя, лот, категория, сумма, ссылки на заказ и чат |
| Уведомление о смене статуса заказа | автоматически | оплачен / закрыт / возврат |
| Уведомление о падении сессии FunPay | автоматически | если протух `golden_key` или FunPay вернул ошибку |
| Автоответчик | `/autoreply on\|off\|set <текст>\|show` | отвечает один раз каждому новому собеседнику |
| Статистика | `/stats` | сообщения и заказы за 1 / 7 / 30 дней |
| Приглушить пользователя | `/block <ник>`, `/unblock <ник>`, `/blocked` | уведомления от этого ника не приходят (сообщения всё ещё пишутся в БД) |
| Текущий статус | `/status` | баланс, активные продажи/покупки, состояние автоответчика |
| Справка | `/help` | список команд |

## Установка

Нужен Python **3.11+**.

```bash
git clone https://github.com/<твой-логин>/funpay-tg-notifier.git
cd funpay-tg-notifier

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

pip install -e .
```

Или, если не хочешь возиться с `pip install -e`:

```bash
pip install FunPayAPI "aiogram>=3.4,<4" aiosqlite python-dotenv
```

## Настройка

1. Скопируй `.env.example` в `.env`:
   ```bash
   cp .env.example .env
   ```

2. Заполни три обязательных поля.

### `FUNPAY_GOLDEN_KEY`

1. Залогинься на [funpay.com](https://funpay.com) в обычном браузере.
2. Открой DevTools (F12) → **Application** → **Cookies** → `https://funpay.com`.
3. Найди строку `golden_key`, скопируй значение (длинная строка вроде `a1b2c3d4...`).
4. Вставь в `.env`.

⚠️ `golden_key` — это твой ключ от FunPay-аккаунта. Никому не показывай, в Git не коммить (`.env` уже в `.gitignore`). Если кука протухнет (обычно через ~30 дней или после смены пароля) — просто достань новую тем же способом.

### `FUNPAY_USER_AGENT` (рекомендуется)

В тех же DevTools → **Network** → выбери любой запрос на funpay.com → **Headers** → `user-agent`. Скопируй полностью.

FunPay не любит, когда `golden_key` приходит с одного User-Agent, а запросы — с другого. Если оставить пусто, библиотека подставит свой, но иногда это вызывает фантомные разлогины.

### `TELEGRAM_BOT_TOKEN`

1. Открой [@BotFather](https://t.me/BotFather) в Telegram.
2. `/newbot` → название → username (должен заканчиваться на `bot`).
3. BotFather выдаст токен вида `123456:ABC-DEF...`. Это и есть `TELEGRAM_BOT_TOKEN`.

### `TELEGRAM_CHAT_ID`

1. Напиши [@userinfobot](https://t.me/userinfobot) → он пришлёт твой числовой ID.
2. Вставь его в `.env` как `TELEGRAM_CHAT_ID=...`.

Бот будет слушать команды и слать уведомления **только** этому chat_id. Любой другой пользователь, который ему напишет, будет проигнорирован.

## Запуск

```bash
python -m funpay_tg_notifier
```

Или, если делал `pip install -e .`:

```bash
funpay-tg-notifier
```

**Важно:** перед первым запуском один раз открой своего бота в Telegram (по имени, которое ты выдал ему у BotFather) и нажми **Start**. Telegram-боты физически не могут писать первыми — пока ты сам не инициировал диалог, любой `send_message` будет валиться с `Bad Request: chat not found`.

При старте бот напишет в Telegram «Бот запущен» с твоим ником на FunPay и балансом. Если этого не пришло — проверь логи в консоли. Если видишь `chat not found` — значит ты ещё не нажал `/start` у бота.

### Автозапуск

#### systemd (Linux / Raspberry Pi)

`/etc/systemd/system/funpay-tg-notifier.service`:

```ini
[Unit]
Description=FunPay → Telegram notifier
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/funpay-tg-notifier
ExecStart=/home/pi/funpay-tg-notifier/.venv/bin/python -m funpay_tg_notifier
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Затем:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now funpay-tg-notifier
journalctl -u funpay-tg-notifier -f
```

#### Windows

Самый простой путь — Планировщик задач: создай задачу «При входе в систему» → действие «Запустить программу» → `путь_к_venv\Scripts\python.exe -m funpay_tg_notifier` → рабочая папка = клон репы.

## Как это работает

1. Запускается отдельный поток с `FunPayAPI.Runner.listen()` — он каждые 6 секунд опрашивает FunPay и выдаёт события (`NewMessageEvent`, `NewOrderEvent`, `OrderStatusChangedEvent` и т.д.).
2. События пересылаются в asyncio-loop, в котором крутится `aiogram` Telegram-бот.
3. Каждое событие конвертируется в HTML-сообщение и шлётся тебе в Telegram.
4. Параллельно события и заказы пишутся в локальный SQLite (`data/funpay_tg.db`) для `/stats`.
5. Если `Runner` упал (например, сессия протухла) — поток ловит исключение, шлёт алерт в Telegram и переподключается через 30 секунд.

## Подводные камни

- **FunPay не даёт API.** Всё держится на парсинге HTML/JSON через cookie. Если FunPay поменяет вёрстку — `FunPayAPI` обычно обновляется в течение нескольких дней, но возможны короткие простои.
- **Капча.** Появляется только при логине с нуля. С `golden_key` cookie запросы её не триггерят. Если вдруг словил — залогинься в браузере заново и обнови `golden_key`.
- **Антифрод FunPay.** Слишком частые запросы (< 4 секунд) или странные User-Agent могут привести к временному блоку аккаунта. Дефолтные настройки (6 секунд, твой реальный UA) безопасны.
- **«Отключили буст / пришла блокировка».** Явного события у FunPay нет. Косвенно это видно как «Runner crashed: 403» — такой алерт ты получишь.
- **Автоответчик.** Срабатывает один раз на каждый чат (по `chat_id`). Если хочется сбросить и ответить ещё раз — удали запись в таблице `autoreply_done` в SQLite.

## Лицензия

MIT. Используй как хочешь.
