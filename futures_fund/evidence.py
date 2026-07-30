"""Per-coin evidence packs — the deterministic market data the LLM specialists read.

Pure data assembly from the exchange: NO opinion, NO decision. Each field is fail-soft (a missing
datum defaults to a neutral value) so one broken read never crashes the cycle.

2026-07 forensic-review additions (the fields whose absence destroyed the desk):
  * LIQUIDITY (`depth_usd_bid/ask`, `spread_bps`, `est_slippage_bps_2k`) — the PM sized a $9K
    short in a name whose real one-way exit cost was 300-1000bps and could not see it.
  * FUNDING ECONOMICS (`funding_interval_h`, `expected_funding_8h_bps`) — a perp desk whose carry
    theses could not be quantified per-cycle.
  * HONEST BETA (`beta_clamped`, `beta_n_samples`) — beta was a 45-HOUR OLS (the config documents
    45 DAYS); LAB printed beta 10.42 and forced a 4x-cash hedge. `beta_btc` keeps the raw value
    for audit; `beta_clamped` (|beta| capped at 3.0) is what sizing should use.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from futures_fund.beta import beta_for_symbols
from futures_fund.slippage import estimate_slippage

BETA_CLAMP = 3.0                 # sizing beta cap: |beta| beyond this is estimation noise
_SLIP_PROBE_USD = 2_000.0        # est_slippage_bps_2k probe size (legacy field, kept for compat)
_SLIP_CURVE_USD = (2_000.0, 5_000.0, 10_000.0)   # clip sizes the desk actually trades


class EvidencePack(BaseModel):
    symbol: str
    mark: float
    momentum_pct: float = 0.0        # close-to-close % change over the OHLCV window
    # stdev of log returns, annualized-ish (raw stdev * sqrt(periods))
    realized_vol: float = 0.0
    beta_btc: float = 1.0            # raw rolling beta to BTC (audit value)
    beta_clamped: float = 1.0        # sign(beta) * min(|beta|, 3.0) — USE THIS for sizing/hedging
    beta_n_samples: int = 0          # aligned return points behind the estimate (0 = fallback 1.0)
    funding_rate: float = 0.0        # current funding rate per interval
    funding_apr: float = 0.0         # funding_rate * (8760 / interval_hours)
    funding_interval_h: float = 8.0
    expected_funding_8h_bps: float = 0.0   # funding_rate * (8/interval) * 1e4 — per-8h carry, bps
    basis_bps: float = 0.0           # (mark - index) / index * 1e4
    open_interest: float = 0.0       # open-interest notional (USD)
    oi_change_pct: float = 0.0       # first->last OI % change over the history window
    long_short_ratio: float = 0.0
    depth_usd_bid: float = 0.0       # summed top-of-book bid notional (USD)
    depth_usd_ask: float = 0.0       # summed top-of-book ask notional (USD)
    liquidity_mid: float = 0.0       # midpoint of the SAME L2 snapshot used for the cost curve
    spread_bps: float = 0.0          # (best_ask - best_bid) / mid, bps
    est_slippage_bps_2k: float = 0.0  # est. one-way slippage (bps) for a $2K clip via depth walk
    # SIZE-AWARE cost curve (bps at each clip size). Slippage is CONVEX in clip size: on cycle 11
    # a $5K WLD leg cost 4.1x what its $2K probe implied, so the break-even math cleared a trade
    # whose true payback was ~32 cycles, not 7.7. NEVER extrapolate the 2k probe linearly — read
    # the bps at (or above) the clip you actually intend to trade.
    slippage_curve_bps: dict[str, float] = Field(default_factory=dict)
    as_of_ts: datetime


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — evidence is fail-soft; a bad read defaults to neutral
        return default


def _slip_bps_at(
    sym: str,
    reference_price: float,
    levels: list[tuple[float, float]],
    notional: float,
    half_spread_bps: float = 0.0,
) -> float:
    """One-way slippage in bps for a clip of `notional` USD, via the SAME depth walk the fill path
    uses (`estimate_slippage`), floored at the half-spread.

    `reference_price` and `levels` MUST come from the same L2 snapshot. Market movement between the
    evidence snapshot and the later paper execution is recorded as decision-to-execution drift,
    not multiplied into this liquidity estimate. The floor preserves the unavoidable cost of
    crossing a deep book even when its price impact is otherwise negligible."""
    if reference_price <= 0 or not levels or notional <= 0:
        base = max(0.0, half_spread_bps)
    else:
        qty = notional / reference_price
        cost = _safe(
            lambda: estimate_slippage(
                sym,
                qty,
                reference_price,
                depth=levels,
                adv_usd=0.0,
                half_spread_bps=half_spread_bps,
            ),
            0.0,
        )
        walk_bps = (cost / notional) * 1e4 if cost else 0.0
        base = max(walk_bps, half_spread_bps)
    return base


def _depth_fields(
    exchange, sym: str
) -> tuple[float, float, float, float, float, dict[str, float]]:
    """Return depth, same-snapshot midpoint/spread, and the size-aware cost curve.

    The curve prices the clip sizes the desk actually trades ($2k/$5k/$10k), because slippage is
    convex in size. It takes the worse of buying through asks and selling through bids so the
    side-agnostic evidence remains conservative without confusing market drift with friction."""
    book = _safe(lambda: exchange.depth(sym), None)
    if not book:
        return 0.0, 0.0, 0.0, 0.0, 0.0, {}
    bids = [
        (float(p), float(q))
        for p, q in (book.get("bids") or [])
        if float(p) > 0.0 and float(q) > 0.0
    ]
    asks = [
        (float(p), float(q))
        for p, q in (book.get("asks") or [])
        if float(p) > 0.0 and float(q) > 0.0
    ]
    bid_usd = sum(p * q for p, q in bids)
    ask_usd = sum(p * q for p, q in asks)
    if not bids or not asks or bids[0][0] > asks[0][0]:
        # A one-sided/crossed book has no coherent midpoint. Preserve its visible depth for audit
        # but publish no cost curve rather than pairing it with a mark from another timestamp.
        return bid_usd, ask_usd, 0.0, 0.0, 0.0, {}

    mid = (bids[0][0] + asks[0][0]) / 2.0
    spread = (asks[0][0] - bids[0][0]) / mid * 1e4 if mid > 0 else 0.0
    # Floor every curve point at the HALF-SPREAD — crossing the book always costs at least that,
    # even when the depth-walk impact is ~0 (cycle-17 fix).
    half_spread = spread / 2.0
    curve = {
        f"{int(n // 1000)}k": round(
            max(
                _slip_bps_at(sym, mid, asks, n, half_spread),
                _slip_bps_at(sym, mid, bids, n, half_spread),
            ),
            3,
        )
        for n in _SLIP_CURVE_USD
    }
    slip_bps = curve.get("2k", 0.0)
    return bid_usd, ask_usd, mid, spread, slip_bps, curve


def build_evidence(exchange, symbols: list[str], *, now: datetime,
                   btc_symbol: str) -> list[EvidencePack]:
    """Assemble one EvidencePack per symbol from the exchange reads. Fail-soft per field.

    `btc_symbol` is ALWAYS included (even if it missed the top-N) so its mark is available for a PM
    BTC-hedge leg and so beta-to-BTC can be computed. Beta is the rolling beta of each coin's
    DAILY close series to BTC's (BTC itself = 1.0; insufficient history -> 1.0). Daily closes make
    the 45-sample lookback the ~45 DAYS the config documents — the prior 1h-bar feed made it 45
    HOURS and printed a beta of 10.42 on a crashed coin (the cycle-4 blowout input)."""
    all_syms = list(dict.fromkeys([*symbols, btc_symbol]))
    closes_by: dict[str, pd.Series] = {}
    rows: list[dict] = []
    for sym in all_syms:
        # funding carries the mark price, so fetch funding ONCE and reuse its mark — avoids a
        # redundant fetch_funding_rate per symbol (mark_price() hit the same endpoint), trimming
        # the per-cycle REST burst that was tripping Binance's rate-limit ban.
        fi = _safe(lambda: exchange.funding(sym), None)  # noqa: B023
        mark = float(getattr(fi, "mark_price", 0.0) or 0.0) if fi else 0.0
        if mark <= 0:  # funding read failed or lacked a mark — fall back to the dedicated call
            mark = _safe(lambda: float(exchange.mark_price(sym)), 0.0)  # noqa: B023
        if mark <= 0:
            continue
        closes = _safe(
            lambda: list(exchange.ohlcv(sym, timeframe="1h", limit=200)["close"]),  # noqa: B023
            [],
        )
        daily_closes = _safe(
            lambda: list(exchange.ohlcv(sym, timeframe="1d", limit=60)["close"]),  # noqa: B023
            [],
        )
        # beta from daily closes when available (>=10 points), else fall back to the hourly
        # series (better than the 1.0 default for very young listings).
        beta_src = daily_closes if len(daily_closes) >= 10 else closes
        if len(beta_src) >= 2:
            closes_by[sym] = pd.Series(beta_src)
        mom = ((closes[-1] / closes[0] - 1.0) * 100.0) if len(closes) >= 2 and closes[0] else 0.0
        rv = 0.0
        if len(closes) >= 3:
            rets = np.diff(np.log(np.clip(closes, 1e-12, None)))
            rv = float(np.std(rets) * np.sqrt(len(rets)))
        fr = float(getattr(fi, "current_rate", 0.0) or 0.0) if fi else 0.0
        interval = float(getattr(fi, "interval_hours", 8.0) or 8.0) if fi else 8.0
        apr = fr * (8760.0 / interval) if interval else 0.0
        exp_8h_bps = fr * (8.0 / interval) * 1e4 if interval else 0.0
        idx = float(getattr(fi, "index_price", mark) or mark) if fi else mark
        basis = ((mark - idx) / idx * 1e4) if idx else 0.0
        oi_hist = _safe(
            lambda: list(exchange.open_interest_history(sym)["oi_value"]),  # noqa: B023
            [],
        )
        oi = float(oi_hist[-1]) if oi_hist else 0.0
        oi_chg = (
            (oi_hist[-1] / oi_hist[0] - 1.0) * 100.0
        ) if len(oi_hist) >= 2 and oi_hist[0] else 0.0
        lsr_hist = _safe(
            lambda: list(exchange.long_short_ratio(sym)["long_short_ratio"]),  # noqa: B023
            [],
        )
        lsr = float(lsr_hist[-1]) if lsr_hist else 0.0
        d_bid, d_ask, liquidity_mid, spread, slip2k, slip_curve = _depth_fields(exchange, sym)
        rows.append({
            "symbol": sym, "mark": mark, "momentum_pct": mom, "realized_vol": rv,
            "funding_rate": fr, "funding_apr": apr, "funding_interval_h": interval,
            "expected_funding_8h_bps": exp_8h_bps, "basis_bps": basis, "open_interest": oi,
            "oi_change_pct": oi_chg, "long_short_ratio": lsr,
            "depth_usd_bid": d_bid, "depth_usd_ask": d_ask,
            "liquidity_mid": liquidity_mid, "spread_bps": spread,
            "est_slippage_bps_2k": slip2k, "slippage_curve_bps": slip_curve,
            "_n_beta": max(0, len(closes_by.get(sym, [])) - 1),
        })
    betas = beta_for_symbols(closes_by, btc_symbol=btc_symbol, lookback=45)
    packs: list[EvidencePack] = []
    for row in rows:
        n_beta = row.pop("_n_beta")
        raw = betas.get(row["symbol"], 1.0)
        clamped = float(np.sign(raw) * min(abs(raw), BETA_CLAMP)) if raw else 0.0
        packs.append(EvidencePack(**row, beta_btc=raw, beta_clamped=clamped,
                                  beta_n_samples=n_beta, as_of_ts=now))
    return packs
