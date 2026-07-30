from __future__ import annotations

from futures_fund.models import Direction

PER_SYMBOL_CAP_DEFAULT: float = 0.02   # alts default magnitude (+-2%)
MAJOR_CAP: float = 0.003               # BTC/ETH magnitude (+-0.30%)
_MAJORS: frozenset[str] = frozenset({"BTC/USDT:USDT", "ETH/USDT:USDT"})


def funding_interval_hours(symbol: str, exchange) -> float:
    """Per-symbol settlement interval from /fapi/v1/fundingInfo via FuturesExchange.funding().

    Reads the `interval_hours: float` field on the FundingInfo that exchange.funding(symbol)
    returns (see market_data.FundingInfo / exchange.funding in Task 7). Defaults to 8.0 hours when
    the funding info is missing or the call fails (the major default).
    """
    try:
        return float(exchange.funding(symbol).interval_hours)
    except Exception:
        return 8.0


def funding_cap(symbol: str) -> float:
    """Clamp magnitude for the realized rate: MAJOR_CAP for majors else PER_SYMBOL_CAP_DEFAULT."""
    return MAJOR_CAP if symbol in _MAJORS else PER_SYMBOL_CAP_DEFAULT


def clamp_funding_rate(symbol: str, rate: float) -> float:
    """Clamp a realized rate to [-cap, +cap], SIGN-PRESERVING (carry stays signed, never zeroed)."""
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
    attractive than them. The per-symbol realized RATE is already clamped upstream
    (clamp_funding_rate, majors +-0.003 / alts +-0.02); this is a STRATEGY-level signal cap on top.
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
    notional_signed: float, mark: float, qty: float, rate: float, direction: Direction  # noqa: ARG001
) -> float:
    """Settlement contribution to BALANCE: -side*mark*qty*rate.

    side = +1 for long, -1 for short. A short (-1) with a positive `rate` RECEIVES funding, so the
    balance contribution is positive (a credit). Signed; never clamped to >= 0 here. Per §11 /
    contract §2.3 the per-symbol cap is applied to the RATE upstream via clamp_funding_rate, and
    this function consumes that signed, clamped rate.

    `notional_signed` is accepted for call-site symmetry with WeightLeg.target_notional (Phase 1)
    but is DELIBERATELY UNUSED — the contribution is derived from mark*qty so a partial fill is
    handled by the caller's `qty`. test_realized_funding_ignores_notional_signed pins this so a
    caller cannot desync the reviewer's funding_amount re-derivation by passing a wrong notional.
    """
    side = 1.0 if direction == "long" else -1.0
    return -side * mark * qty * rate
