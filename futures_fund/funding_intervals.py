from __future__ import annotations

from futures_fund.models import Direction

PER_SYMBOL_CAP_DEFAULT: float = 0.02  # alts default magnitude (+-2%)
MAJOR_CAP: float = 0.003  # BTC/ETH magnitude (+-0.30%)
_MAJORS: frozenset[str] = frozenset({"BTC/USDT:USDT", "ETH/USDT:USDT"})


def funding_interval_hours(symbol: str, exchange) -> float:
    """Return a proven per-symbol settlement interval without an assumed fallback.

    Production exchanges expose ``funding_interval_hours`` so they can distinguish a successful
    Binance ``fundingInfo`` response that omits a standard-8h symbol from a failed request. Older
    injected exchange seams may provide only ``funding(symbol).interval_hours``. Neither path may
    turn unavailable or malformed metadata into an invented 8h schedule.
    """
    resolver = getattr(exchange, "funding_interval_hours", None)
    raw = resolver(symbol) if callable(resolver) else exchange.funding(symbol).interval_hours
    try:
        hours = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid funding interval metadata for {symbol}: {raw!r}") from exc
    if not hours.is_integer() or int(hours) not in {1, 2, 4, 8}:
        raise ValueError(f"unsupported funding interval for {symbol}: {hours}")
    return hours


def funding_cap(symbol: str) -> float:
    """Signal-rate cap: MAJOR_CAP for majors, else PER_SYMBOL_CAP_DEFAULT."""
    return MAJOR_CAP if symbol in _MAJORS else PER_SYMBOL_CAP_DEFAULT


def clamp_funding_rate(symbol: str, rate: float) -> float:
    """Clamp a rate for *forecast/signal* use, preserving its sign.

    This guard keeps an extreme print from dominating expected-carry ranking.  It must never be
    applied to an exchange-published historical settlement: realized PAPER cash uses the raw
    published rate exactly, just as a real futures account would.
    """
    cap = funding_cap(symbol)
    if rate > cap:
        return cap
    if rate < -cap:
        return -cap
    return rate


def bounded_apr(apr: float, cap: float | None) -> float:
    """Sign-preserving clamp of an annualized funding_apr to +-cap. cap=None -> unbounded.

    EXTREME FUNDING IS A REVERSAL TRAP, NOT FREE ALPHA: a blow-off rate that annualizes to a huge
    APR should be treated as CAPPED, ranking alongside other at-cap names — never as more
    attractive than them. This is a strategy-level signal cap; realized cash settlement remains
    the raw exchange-published rate.
    Lives here (not in a sleeve) so carry and factor both import it from a neutral module."""
    if cap is None:
        return apr
    if apr > cap:
        return cap
    if apr < -cap:
        return -cap
    return apr


def intervals_per_year(interval_hours: float) -> float:
    """24/interval_hours * 365 — annualization factor for funding_apr."""
    if interval_hours <= 0:
        return 0.0
    return 24.0 / interval_hours * 365.0


def funding_apr(rate: float, interval_hours: float) -> float:
    """Signed annualized carry = rate * intervals_per_year(interval_hours)."""
    return rate * intervals_per_year(interval_hours)


def realized_funding(
    notional_signed: float,
    mark: float,
    qty: float,
    rate: float,
    direction: Direction,  # noqa: ARG001
) -> float:
    """Settlement contribution to BALANCE: -side*mark*qty*rate.

    side = +1 for long, -1 for short. A short (-1) with a positive `rate` RECEIVES funding, so the
    balance contribution is positive (a credit). Signed; never clamped here.

    The caller supplies the raw exchange-published rate for realized settlement. Signal caps such
    as :func:`clamp_funding_rate` belong only in forward ranking and risk/reward estimates.

    `notional_signed` is accepted for call-site symmetry with WeightLeg.target_notional (Phase 1)
    but is DELIBERATELY UNUSED — the contribution is derived from mark*qty so a partial fill is
    handled by the caller's `qty`. test_realized_funding_ignores_notional_signed pins this so a
    caller cannot desync the reviewer's funding_amount re-derivation by passing a wrong notional.
    """
    side = 1.0 if direction == "long" else -1.0
    return -side * mark * qty * rate
