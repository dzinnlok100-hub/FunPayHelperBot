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


# ---- lot management helpers (synchronous: call via asyncio.to_thread) ----


def list_user_lots(acc: "Account") -> list:
    """Return the FunPay user's active (publicly visible) lots."""
    profile = acc.get_user(acc.id)
    return list(profile.get_lots())


def edit_lot(
    acc: "Account",
    lot_id: int,
    *,
    price: float | None = None,
    active: bool | None = None,
    amount: int | None = None,
    title_ru: str | None = None,
    description_ru: str | None = None,
) -> None:
    """Read a lot, modify the given fields, and save it back."""
    fields = acc.get_lot_fields(lot_id)
    if price is not None:
        fields.price = float(price)
    if active is not None:
        fields.active = bool(active)
    if amount is not None:
        fields.amount = int(amount)
    if title_ru is not None:
        fields.title_ru = title_ru
    if description_ru is not None:
        fields.description_ru = description_ru
    fields.renew_fields()
    acc.save_lot(fields)


def clone_lot(
    acc: "Account",
    lot_id: int,
    *,
    price: float | None = None,
    title_ru: str | None = None,
) -> int:
    """Duplicate an existing lot. The lot is saved with no offer_id so FunPay
    treats it as a fresh entity.

    Returns the new lot's id.
    """
    fields = acc.get_lot_fields(lot_id)
    if price is not None:
        fields.price = float(price)
    if title_ru is not None:
        fields.title_ru = title_ru
    # Drop the offer_id so save_lot treats this as a new offer.
    raw = fields.fields
    for key in ("offer_id", "offer_id[]", "node_id"):
        raw.pop(key, None)
    fields.lot_id = 0
    fields.renew_fields()
    acc.save_lot(fields)
    # FunPayAPI's save_lot does not return the new id reliably; re-list and
    # find the matching one by title.
    profile = acc.get_user(acc.id)
    target = (title_ru or fields.title_ru or "").strip()
    for lot in profile.get_lots():
        if str(lot.description or "").strip() == target:
            return int(lot.id)
    return 0


def bump_user_lots(acc: "Account") -> tuple[set[str], set[str]]:
    """Try to bump every distinct game category the user has lots in.

    Returns ``(bumped_category_names, failed_category_names)``.
    """
    bumped: set[str] = set()
    failed: set[str] = set()
    try:
        lots = list_user_lots(acc)
    except Exception as e:
        log.warning("bump_user_lots: cannot list lots: %s", e)
        return bumped, failed
    seen_categories: dict[int, str] = {}
    for lot in lots:
        try:
            sub = lot.subcategory
            cat = sub.category
            seen_categories[int(cat.id)] = str(cat.name)
        except Exception:
            continue
    for cat_id, cat_name in seen_categories.items():
        try:
            ok = acc.raise_lots(cat_id)
            if ok:
                bumped.add(cat_name)
            else:
                failed.add(cat_name)
        except Exception as e:
            log.info("raise_lots(%s) failed: %s", cat_id, e)
            failed.add(cat_name)
    return bumped, failed


def reprice_all(
    acc: "Account", *, percent: float | None = None, delta: float | None = None
) -> tuple[int, int]:
    """Adjust every active lot's price.

    ``percent``: multiply by (1 + percent/100).  ``delta``: add delta to price.
    Returns ``(updated, failed)``.
    """
    if percent is None and delta is None:
        raise ValueError("reprice_all: pass either percent or delta")
    updated = 0
    failed = 0
    for lot in list_user_lots(acc):
        try:
            fields = acc.get_lot_fields(int(lot.id))
            if fields.price is None:
                continue
            new_price = fields.price
            if percent is not None:
                new_price = new_price * (1 + percent / 100.0)
            if delta is not None:
                new_price = new_price + delta
            new_price = round(max(new_price, 0.01), 2)
            fields.price = new_price
            fields.renew_fields()
            acc.save_lot(fields)
            updated += 1
        except Exception as e:
            log.warning("reprice_all: lot %s failed: %s", getattr(lot, "id", "?"), e)
            failed += 1
    return updated, failed


def sum_paid_orders_via_api(acc: "Account", days: int) -> tuple[int, float]:
    """Fetch finished/refunded orders from FunPay and sum the closed ones for
    the last ``days`` days.

    Note: FunPay's trade page paginates; we walk up to 5 pages (≈ 100 orders),
    which is enough for a daily/weekly summary.
    """
    import datetime as _dt

    cutoff = _dt.datetime.now(tz=_dt.timezone.utc) - _dt.timedelta(days=days)
    total_n = 0
    total_sum = 0.0
    start_from: str | None = None
    for _page in range(5):
        try:
            start_from, orders = acc.get_sells(
                start_from=start_from,
                include_paid=True,
                include_closed=True,
                include_refunded=False,
            )
        except Exception as e:
            log.warning("get_sells failed: %s", e)
            break
        for o in orders:
            o_date = o.date
            if o_date.tzinfo is None:
                o_date = o_date.replace(tzinfo=_dt.timezone.utc)
            if o_date < cutoff:
                start_from = None
                break
            status_name = (
                o.status.name if hasattr(o.status, "name") else str(o.status)
            )
            if status_name in ("CLOSED", "PAID"):
                total_n += 1
                total_sum += float(o.price)
        if not start_from:
            break
    return total_n, total_sum
