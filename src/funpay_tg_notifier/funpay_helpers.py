"""Convenience helpers around FunPayAPI."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .db import Database

if TYPE_CHECKING:
    from FunPayAPI import Account
    from FunPayAPI.types import Balance

log = logging.getLogger(__name__)

_STATE_BALANCE_LOT_ID = "balance_lot_id"


async def get_balance_safe(
    tg_user_id: int, acc: "Account", db: Database
) -> "Balance | None":
    """Get balance, picking a valid lot_id automatically.

    FunPayAPI's `get_balance` needs a lot_id of an existing lot. The hard-coded
    default often 404s. We:
      1. Try the cached working lot id for this user.
      2. Fall back to the library default.
      3. If both fail, pull the user's own lots and try the first one;
         cache that on success.
    Returns None if all attempts fail.
    """
    cached = await db.get_state(tg_user_id, _STATE_BALANCE_LOT_ID)
    candidate_ids: list[int] = []
    if cached:
        try:
            candidate_ids.append(int(cached))
        except ValueError:
            pass
    candidate_ids.append(18853876)

    for lot_id in candidate_ids:
        try:
            bal = acc.get_balance(lot_id=lot_id)
            await db.set_state(tg_user_id, _STATE_BALANCE_LOT_ID, str(lot_id))
            return bal
        except Exception as e:
            log.debug("get_balance(lot_id=%s) failed: %s", lot_id, e)

    try:
        user = acc.get_user(acc.id)
        for lot in user.get_lots():
            try:
                bal = acc.get_balance(lot_id=int(lot.id))
                await db.set_state(tg_user_id, _STATE_BALANCE_LOT_ID, str(lot.id))
                return bal
            except Exception as e:
                log.debug("get_balance(lot_id=%s from own lots) failed: %s", lot.id, e)
    except Exception as e:
        log.warning("Could not enumerate own lots for balance lookup: %s", e)

    return None


def format_balance(bal: "Balance | None") -> str:
    if bal is None:
        return "—"
    return f"{bal.total_rub:.2f} ₽ (доступно {bal.available_rub:.2f} ₽)"
