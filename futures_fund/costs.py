from __future__ import annotations

from datetime import datetime, timedelta

from futures_fund.models import Direction

TAKER_RATE = 0.0005   # 0.05%
MAKER_RATE = 0.0002   # 0.02%
BNB_DISCOUNT = 0.90    # 10% off when paying fees in BNB


def trade_fee(notional: float, *, maker: bool, pay_bnb: bool = False) -> float:
    """Fee in USDT for a single fill of `notional` USDT."""
    rate = MAKER_RATE if maker else TAKER_RATE
    fee = abs(notional) * rate
    return fee * BNB_DISCOUNT if pay_bnb else fee


def round_trip_fee(
    notional: float, *, maker_entry: bool, maker_exit: bool, pay_bnb: bool = False
) -> float:
    """Entry + exit fee assuming the same notional both legs (conservative)."""
    return (
        trade_fee(notional, maker=maker_entry, pay_bnb=pay_bnb)
        + trade_fee(notional, maker=maker_exit, pay_bnb=pay_bnb)
    )


DEFAULT_FUNDING_INTERVAL_HOURS = 8  # majors default; per-symbol sourced in funding_intervals.py


def funding_boundary_hours(interval_hours: int = DEFAULT_FUNDING_INTERVAL_HOURS) -> tuple[int, ...]:
    """UTC hours at which funding settles (8h -> 0,8,16; 4h -> 0,4,8,12,16,20)."""
    return tuple(range(0, 24, interval_hours))


def count_funding_events(
    entry_ts: datetime, exit_ts: datetime,
    interval_hours: int = DEFAULT_FUNDING_INTERVAL_HOURS,
) -> int:
    """Number of funding settlements strictly within (entry_ts, exit_ts]."""
    if exit_ts <= entry_ts:
        return 0
    hours = set(funding_boundary_hours(interval_hours))
    count = 0
    cursor = entry_ts.replace(minute=0, second=0, microsecond=0)
    if cursor <= entry_ts:
        cursor += timedelta(hours=1)
    while cursor <= exit_ts:
        if cursor.hour in hours:
            count += 1
        cursor += timedelta(hours=1)
    return count


def project_funding(
    notional: float, funding_rate: float, direction: Direction, n_events: int
) -> float:
    """Projected funding cost in USDT (positive = we pay, negative = we receive)."""
    sign = 1.0 if direction == "long" else -1.0
    return abs(notional) * funding_rate * sign * n_events


def vwap_fill(levels: list[tuple[float, float]], qty: float) -> tuple[float, float]:
    """Walk price/qty `levels` (in crossing order) to fill `qty`.

    Returns (filled_qty, vwap). If depth is insufficient, returns the partial fill.
    """
    if qty <= 0 or not levels:
        return 0.0, 0.0
    remaining = qty
    cost = 0.0
    filled = 0.0
    for price, avail in levels:
        take = min(remaining, avail)
        cost += take * price
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    vwap = cost / filled if filled > 0 else 0.0
    return filled, vwap


def slippage_cost(
    levels: list[tuple[float, float]], qty: float, reference_price: float
) -> float:
    """USDT slippage cost: filled_qty * |vwap - reference_price|."""
    filled, vwap = vwap_fill(levels, qty)
    if filled <= 0:
        return 0.0
    return filled * abs(vwap - reference_price)
