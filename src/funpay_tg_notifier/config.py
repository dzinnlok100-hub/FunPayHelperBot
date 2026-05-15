"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    golden_key: str
    user_agent: str | None
    funpay_poll_delay: float

    telegram_token: str
    telegram_chat_id: int

    db_path: Path
    log_level: str


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required env var: {name}. "
            f"Copy .env.example to .env and fill in the values."
        )
    return value


def load_settings() -> Settings:
    load_dotenv()
    db_path = Path(os.environ.get("DB_PATH", "data/funpay_tg.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        golden_key=_required("FUNPAY_GOLDEN_KEY"),
        user_agent=os.environ.get("FUNPAY_USER_AGENT") or None,
        funpay_poll_delay=float(os.environ.get("FUNPAY_POLL_DELAY", "6")),
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=int(_required("TELEGRAM_CHAT_ID")),
        db_path=db_path,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
