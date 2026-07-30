from __future__ import annotations

import numpy as np
import pandas as pd


def log_returns(prices: pd.Series) -> pd.Series:
    """Log returns of a mark-price series. Drops NaN/non-positive prices before
    differencing so no inf/NaN leaks into the return series."""
    clean = prices.dropna()
    clean = clean[clean > 0.0]
    if len(clean) < 2:
        return pd.Series([], dtype=float)
    return np.log(clean / clean.shift(1)).dropna()


def rolling_beta(
    asset_returns: pd.Series, btc_returns: pd.Series, lookback: int = 45
) -> float:
    """OLS beta = cov(asset, btc) / var(btc) over the last `lookback` aligned points.
    Falls back to 1.0 if fewer than 10 aligned points or BTC variance is zero."""
    aligned = pd.concat([asset_returns, btc_returns], axis=1, join="inner").dropna()
    if len(aligned) > lookback:
        aligned = aligned.iloc[-lookback:]
    if len(aligned) < 10:
        return 1.0
    a = aligned.iloc[:, 0].to_numpy()
    b = aligned.iloc[:, 1].to_numpy()
    var_b = float(np.var(b))
    if var_b <= 0.0:
        return 1.0
    cov_ab = float(np.cov(a, b, ddof=0)[0, 1])
    return cov_ab / var_b


def beta_series(
    asset_returns: pd.Series, btc_returns: pd.Series, lookback: int = 45
) -> pd.Series:
    """Rolling beta time series for drift monitoring / reviewer re-derivation. NaN until
    at least 10 aligned points are available; thereafter the trailing-`lookback` beta."""
    aligned = pd.concat([asset_returns, btc_returns], axis=1, join="inner").dropna()
    a = aligned.iloc[:, 0]
    b = aligned.iloc[:, 1]
    out: list[float] = []
    for i in range(len(aligned)):
        lo = max(0, i + 1 - lookback)
        out.append(rolling_beta(a.iloc[lo : i + 1], b.iloc[lo : i + 1], lookback=lookback))
    series = pd.Series(out, index=aligned.index, dtype=float)
    # Windows with < 10 points produce the 1.0 fallback; mask them as NaN for monitoring.
    counts = pd.Series(range(1, len(aligned) + 1), index=aligned.index)
    return series.where(counts >= 10, other=float("nan"))


def beta_for_symbols(
    marks_by_symbol: dict[str, pd.Series],
    btc_symbol: str = "BTC/USDT:USDT",
    lookback: int = 45,
) -> dict[str, float]:
    """Per-symbol rolling beta to BTC. BTC maps to 1.0 by construction. Returns {} if the
    BTC series is missing (cannot compute beta to an absent benchmark)."""
    if btc_symbol not in marks_by_symbol:
        return {}
    btc_ret = log_returns(marks_by_symbol[btc_symbol])
    out: dict[str, float] = {}
    for symbol, prices in marks_by_symbol.items():
        if symbol == btc_symbol:
            out[symbol] = 1.0
            continue
        out[symbol] = rolling_beta(log_returns(prices), btc_ret, lookback=lookback)
    return out
