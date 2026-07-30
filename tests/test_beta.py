from __future__ import annotations

import numpy as np
import pandas as pd

from futures_fund.beta import (
    beta_for_symbols,
    beta_series,
    log_returns,
    rolling_beta,
)


def test_log_returns_basic():
    prices = pd.Series([100.0, 110.0, 121.0])
    r = log_returns(prices)
    assert len(r) == 2
    assert np.isclose(r.iloc[0], np.log(110.0 / 100.0))
    assert np.isclose(r.iloc[1], np.log(121.0 / 110.0))


def test_log_returns_drops_nan_and_nonpositive():
    prices = pd.Series([100.0, np.nan, 121.0, 0.0, 130.0])
    r = log_returns(prices)
    # NaN and non-positive prices removed before differencing; no inf/NaN remains
    assert not r.isna().any()
    assert np.isfinite(r.to_numpy()).all()


def test_log_returns_empty_series_returns_empty():
    r = log_returns(pd.Series([], dtype=float))
    assert len(r) == 0


def test_rolling_beta_recovers_known_beta(btc_returns, beta_returns):
    asset = beta_returns(1.3, noise_sd=0.0)  # noiseless => exact beta
    b = rolling_beta(asset, btc_returns, lookback=60)
    assert abs(b - 1.3) < 1e-6


def test_rolling_beta_uses_last_lookback_points(btc_returns, beta_returns):
    asset = beta_returns(2.0, noise_sd=0.0)
    b = rolling_beta(asset, btc_returns, lookback=30)
    assert abs(b - 2.0) < 1e-6


def test_rolling_beta_fallback_when_too_few_points(btc_returns, beta_returns):
    asset = beta_returns(1.5, noise_sd=0.0)
    b = rolling_beta(asset.iloc[:5], btc_returns.iloc[:5], lookback=45)
    assert b == 1.0


def test_rolling_beta_fallback_on_zero_variance():
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=50, freq="D", tz="UTC")
    flat_btc = pd.Series([0.0] * 50, index=idx)
    asset = pd.Series([0.01] * 50, index=idx)
    assert rolling_beta(asset, flat_btc, lookback=45) == 1.0


def test_beta_series_is_rolling_and_recovers_beta(btc_returns, beta_returns):
    asset = beta_returns(1.4, noise_sd=0.0)
    s = beta_series(asset, btc_returns, lookback=30)
    # First valid window appears once >= 10 aligned points exist
    valid = s.dropna()
    assert len(valid) > 0
    assert abs(valid.iloc[-1] - 1.4) < 1e-6


def test_beta_for_symbols_maps_btc_to_one(btc_returns, beta_returns):
    # Build price series from the noiseless return series for two symbols
    import numpy as np
    import pandas as pd

    def prices_from_returns(r):
        return pd.Series(100.0 * np.exp(r.cumsum()), index=r.index)

    btc_prices = prices_from_returns(btc_returns)
    eth_prices = prices_from_returns(beta_returns(1.2, noise_sd=0.0))
    marks = {"BTC/USDT:USDT": btc_prices, "ETH/USDT:USDT": eth_prices}
    out = beta_for_symbols(marks, btc_symbol="BTC/USDT:USDT", lookback=60)
    assert out["BTC/USDT:USDT"] == 1.0
    assert abs(out["ETH/USDT:USDT"] - 1.2) < 1e-3


def test_beta_for_symbols_missing_btc_returns_empty():
    import pandas as pd

    # No BTC series in the marks dict => cannot compute beta to an absent benchmark.
    marks = {"ETH/USDT:USDT": pd.Series([100.0, 101.0])}
    out = beta_for_symbols(marks, btc_symbol="BTC/USDT:USDT")
    assert out == {}
