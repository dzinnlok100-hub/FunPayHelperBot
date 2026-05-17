"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    admin_tg_user_id: int | None
    encryption_key: bytes
    funpay_poll_delay: float
    db_path: Path
    log_level: str


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required env var: {name}. "
            f"Copy .env.example to .env and fill in the values. "
            f"Generate a fresh ENCRYPTION_KEY with:\n"
            f'  python -c "from cryptography.fernet import Fernet; '
            f'print(Fernet.generate_key().decode())"'
        )
    return value


def load_settings() -> Settings:
    load_dotenv()
    db_path = Path(os.environ.get("DB_PATH", "data/funpay_tg.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    admin_raw = os.environ.get("ADMIN_TG_USER_ID", "").strip()
    admin_id = int(admin_raw) if admin_raw else None

    return Settings(
        telegram_token=_required("TELEGRAM_BOT_TOKEN"),
        admin_tg_user_id=admin_id,
        encryption_key=_required("ENCRYPTION_KEY").encode(),
        funpay_poll_delay=float(os.environ.get("FUNPAY_POLL_DELAY", "2")),
        db_path=db_path,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
