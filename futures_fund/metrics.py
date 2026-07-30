from __future__ import annotations

import numpy as np

# Sharpe periodicity FIX (spec §11/§18): the daily equity series annualizes x365,
# the weekly x52. The inherited 2190 (4h) factor would make every Sharpe/Sortino/DSR wrong.
# The 8h re-selection cadence (`rebal`, 3 candles/day) annualizes x1095 = 365 * 3.
PERIODS_PER_YEAR_DAILY = 365.0
PERIODS_PER_YEAR_WEEKLY = 52.0
PERIODS_PER_YEAR_8H = 1095.0


def sharpe(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR_DAILY) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sd = arr.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(arr.mean() / sd * np.sqrt(periods_per_year))


def trial_sharpe_std(return_streams: list[list[float]], min_obs: int = 5) -> float | None:
    """Cross-trial Sharpe dispersion (sigma_SR) for the Deflated Sharpe Ratio: the std of each
    trial's PER-PERIOD Sharpe. None when < 2 trials each with >= min_obs observations."""
    shrps = [sharpe(s, periods_per_year=1.0) for s in return_streams if len(s) >= min_obs]
    if len(shrps) < 2:
        return None
    return float(np.std(shrps, ddof=1))


def sortino(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR_DAILY) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    dd = float(np.sqrt(np.mean(np.minimum(arr, 0.0) ** 2)))
    if dd == 0:
        return float("inf") if arr.mean() > 0 else 0.0
    return float(arr.mean() / dd * np.sqrt(periods_per_year))


def max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0 if monotonic up / too short)."""
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def calmar(annual_return: float, mdd: float) -> float:
    return annual_return / mdd if mdd > 0 else 0.0


def hit_rate(closed: list[dict]) -> float:
    if not closed:
        return 0.0
    wins = sum(1 for d in closed if d["realized_pnl"] > 0)
    return wins / len(closed)


def profit_factor(closed: list[dict]) -> float:
    gains = sum(d["realized_pnl"] for d in closed if d["realized_pnl"] > 0)
    losses = -sum(d["realized_pnl"] for d in closed if d["realized_pnl"] < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses
