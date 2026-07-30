import math

import pytest

from futures_fund.metrics import (
    PERIODS_PER_YEAR_DAILY,
    PERIODS_PER_YEAR_WEEKLY,
    calmar,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
    trial_sharpe_std,
)


def test_periodicity_constants_are_365_daily_52_weekly():
    assert PERIODS_PER_YEAR_DAILY == pytest.approx(365.0)
    assert PERIODS_PER_YEAR_WEEKLY == pytest.approx(52.0)


def test_sharpe_default_annualizes_daily_x365():
    rets = [0.01, -0.005, 0.012, 0.003, -0.002, 0.008]
    import numpy as np
    arr = np.asarray(rets)
    expected = arr.mean() / arr.std(ddof=1) * math.sqrt(365.0)
    assert sharpe(rets) == pytest.approx(expected)


def test_sharpe_weekly_annualizes_x52():
    rets = [0.02, -0.01, 0.015, 0.005]
    import numpy as np
    arr = np.asarray(rets)
    expected = arr.mean() / arr.std(ddof=1) * math.sqrt(52.0)
    assert sharpe(rets, periods_per_year=PERIODS_PER_YEAR_WEEKLY) == pytest.approx(expected)


def test_sharpe_too_few_returns_is_zero():
    assert sharpe([0.01]) == 0.0


def test_sortino_uses_downside_rms_x365():
    rets = [0.01, -0.02, 0.01, -0.01]
    import numpy as np
    arr = np.asarray(rets)
    dd = math.sqrt(np.mean(np.minimum(arr, 0.0) ** 2))
    expected = arr.mean() / dd * math.sqrt(365.0)
    assert sortino(rets) == pytest.approx(expected)


def test_max_drawdown_peak_to_trough():
    assert max_drawdown([100.0, 120.0, 90.0, 110.0]) == pytest.approx((120.0 - 90.0) / 120.0)


def test_calmar_and_hit_rate_and_profit_factor():
    assert calmar(0.20, 0.05) == pytest.approx(4.0)
    assert hit_rate([{"realized_pnl": 1.0}, {"realized_pnl": -1.0}]) == pytest.approx(0.5)
    assert profit_factor([{"realized_pnl": 2.0}, {"realized_pnl": -1.0}]) == pytest.approx(2.0)


def test_trial_sharpe_std_is_std_of_per_period_sharpes():
    import numpy as np
    # two trials each with >= min_obs (default 5) observations
    stream_a = [0.01, 0.02, 0.03, 0.04, 0.05]
    stream_b = [0.02, -0.01, 0.03, 0.00, 0.01]
    # INDEPENDENT expected value: per-trial PER-PERIOD Sharpe (mean/std, ddof=1, no annualization),
    # then the cross-trial dispersion std(ddof=1) of those two Sharpes.
    sr_a = np.mean(stream_a) / np.std(stream_a, ddof=1)
    sr_b = np.mean(stream_b) / np.std(stream_b, ddof=1)
    expected = np.std([sr_a, sr_b], ddof=1)
    assert trial_sharpe_std([stream_a, stream_b]) == pytest.approx(expected)


def test_trial_sharpe_std_none_when_too_few_qualifying_trials():
    # fewer than 2 trials -> None
    assert trial_sharpe_std([[0.01, 0.02, 0.03, 0.04, 0.05]]) is None
    assert trial_sharpe_std([]) is None
    # 2 trials but each below min_obs -> None (dropped before the >= 2 check)
    assert trial_sharpe_std([[0.01, 0.02], [0.03, 0.04]], min_obs=5) is None
    # only ONE trial clears min_obs -> still None (need 2 qualifying)
    assert trial_sharpe_std([[0.1, 0.2, 0.3, 0.4, 0.5], [0.1, 0.2]], min_obs=5) is None


def test_sharpe_zero_variance_is_zero():
    assert sharpe([0.01, 0.01, 0.01, 0.01]) == 0.0


def test_sortino_zero_downside_is_inf_or_zero():
    # no negative returns + positive mean -> infinite Sortino
    assert sortino([0.01, 0.02, 0.03]) == float("inf")
    # no negative returns + non-positive mean (all zero) -> 0.0
    assert sortino([0.0, 0.0, 0.0]) == 0.0


def test_calmar_zero_mdd_is_zero():
    assert calmar(0.20, 0.0) == 0.0


def test_profit_factor_only_gains_is_inf_and_empty_is_zero():
    assert profit_factor([{"realized_pnl": 2.0}, {"realized_pnl": 3.0}]) == float("inf")
    assert profit_factor([]) == 0.0


def test_max_drawdown_degenerate_series_is_zero():
    assert max_drawdown([]) == 0.0
    assert max_drawdown([100.0]) == 0.0
