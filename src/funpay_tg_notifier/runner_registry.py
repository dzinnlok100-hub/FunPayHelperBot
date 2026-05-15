"""Per-Telegram-user FunPay runners.

Each user gets their own daemon thread that long-polls FunPay with their own
``golden_key`` cookie and forwards events into the shared asyncio loop where
the Telegram bot lives.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Optional

from FunPayAPI import Account, Runner
from FunPayAPI.updater.events import (
    InitialChatEvent,
    InitialOrderEvent,
    NewMessageEvent,
    NewOrderEvent,
    OrderStatusChangedEvent,
)

from .db import Database
from .notifier import Notifier

log = logging.getLogger(__name__)

_RUNNER_BACKOFF_SECONDS = 30


@dataclass
class _RunnerHandle:
    thread: threading.Thread
    stop_event: threading.Event
    account_ref: list[Optional[Account]]


class RunnerRegistry:
    """Manages one FunPay runner thread per Telegram user.

    Public methods are safe to call from the asyncio loop. The actual polling
    happens in background threads (FunPayAPI is sync-only).
    """

    def __init__(
        self,
        db: Database,
        notifier: Notifier,
        loop: asyncio.AbstractEventLoop,
        poll_delay: float,
    ) -> None:
        self.db = db
        self.notifier = notifier
        self.loop = loop
        self.poll_delay = poll_delay
        self._runners: dict[int, _RunnerHandle] = {}
        self._lock = threading.Lock()

    # ---- public API ----

    def is_running(self, tg_user_id: int) -> bool:
        with self._lock:
            handle = self._runners.get(tg_user_id)
            return handle is not None and handle.thread.is_alive()

    def get_account(self, tg_user_id: int) -> Optional[Account]:
        with self._lock:
            handle = self._runners.get(tg_user_id)
            return handle.account_ref[0] if handle else None

    def start(
        self,
        tg_user_id: int,
        golden_key: str,
        user_agent: str | None,
    ) -> None:
        """Start (or restart) a runner for a user."""
        self.stop(tg_user_id)
        stop_event = threading.Event()
        account_ref: list[Optional[Account]] = [None]
        thread = threading.Thread(
            target=self._thread_main,
            args=(tg_user_id, golden_key, user_agent, account_ref, stop_event),
            name=f"funpay-runner-{tg_user_id}",
            daemon=True,
        )
        handle = _RunnerHandle(thread=thread, stop_event=stop_event, account_ref=account_ref)
        with self._lock:
            self._runners[tg_user_id] = handle
        thread.start()
        log.info("Started FunPay runner for tg_user=%s", tg_user_id)

    def stop(self, tg_user_id: int) -> bool:
        """Stop a runner if running. Returns True if a runner was stopped."""
        with self._lock:
            handle = self._runners.pop(tg_user_id, None)
        if handle is None:
            return False
        handle.stop_event.set()
        log.info("Stop requested for FunPay runner tg_user=%s", tg_user_id)
        return True

    def stop_all(self) -> None:
        with self._lock:
            handles = list(self._runners.items())
            self._runners.clear()
        for tg_user_id, handle in handles:
            handle.stop_event.set()
            log.info("Stop requested for FunPay runner tg_user=%s (shutdown)", tg_user_id)

    def active_user_ids(self) -> list[int]:
        with self._lock:
            return [uid for uid, h in self._runners.items() if h.thread.is_alive()]

    # ---- background thread ----

    def _thread_main(
        self,
        tg_user_id: int,
        golden_key: str,
        user_agent: str | None,
        account_ref: list[Optional[Account]],
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                account = Account(golden_key, user_agent=user_agent).get()
                account_ref[0] = account
                log.info(
                    "FunPay runner connected for tg_user=%s (account=%s, id=%s)",
                    tg_user_id, account.username, account.id,
                )
                runner = Runner(account)

                for event in runner.listen(requests_delay=self.poll_delay):
                    if stop_event.is_set():
                        break

                    # First-run snapshot events — not "new", just whatever already exists.
                    if isinstance(event, (InitialChatEvent, InitialOrderEvent)):
                        continue

                    try:
                        if isinstance(event, NewMessageEvent):
                            asyncio.run_coroutine_threadsafe(
                                self.notifier.handle_new_message(tg_user_id, account, event),
                                self.loop,
                            )
                        elif isinstance(event, NewOrderEvent):
                            asyncio.run_coroutine_threadsafe(
                                self.notifier.handle_new_order(tg_user_id, event),
                                self.loop,
                            )
                        elif isinstance(event, OrderStatusChangedEvent):
                            asyncio.run_coroutine_threadsafe(
                                self.notifier.handle_order_status_changed(tg_user_id, event),
                                self.loop,
                            )
                    except Exception:
                        log.exception(
                            "Failed to dispatch FunPay event for tg_user=%s: %r",
                            tg_user_id, event,
                        )
            except Exception as exc:
                log.exception(
                    "FunPay runner crashed for tg_user=%s; will retry in %ss",
                    tg_user_id, _RUNNER_BACKOFF_SECONDS,
                )
                try:
                    asyncio.run_coroutine_threadsafe(
                        self.notifier.handle_runner_error(tg_user_id, exc),
                        self.loop,
                    )
                except Exception:
                    log.exception("Failed to schedule runner-error notification")
                if stop_event.wait(_RUNNER_BACKOFF_SECONDS):
                    break

        log.info("FunPay runner stopped for tg_user=%s", tg_user_id)
