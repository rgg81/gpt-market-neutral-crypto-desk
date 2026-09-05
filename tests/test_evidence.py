from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from futures_fund.evidence import EvidencePack, build_evidence

NOW = datetime(2026, 7, 7, tzinfo=UTC)
BTC = "BTC/USDT:USDT"


class _FakeEx:
    """Fake exchange whose columns match the REAL parse_* schemas (oi_value / long_short_ratio),
    so a test can never mask an evidence column-name bug again."""

    def mark_price(self, s):
        return 100.0

    def ohlcv(self, s, timeframe="1h", limit=200):
        periods = 60 if timeframe == "1d" else 200
        close = 100.0 * np.exp(np.cumsum(np.full(periods, 0.001)))  # gently trending
        freq = "1D" if timeframe == "1d" else "1h"
        timestamp = pd.date_range(end=NOW, periods=periods, freq=freq, tz="UTC")
        return pd.DataFrame({"timestamp": timestamp, "close": close})

    def funding(self, s):
        from futures_fund.market_data import FundingInfo

        return FundingInfo(
            symbol=s,
            current_rate=0.0001,
            next_funding_ts=NOW,
            interval_hours=8.0,
            mark_price=100.0,
            index_price=99.9,
        )

    def funding_history(self, s, *, since_ms, limit):
        timestamps = pd.date_range(end=NOW, periods=25, freq="8h", tz="UTC")
        return [
            {"timestamp": timestamp.to_pydatetime(), "rate": 0.0001, "mark": 100.0}
            for timestamp in timestamps
            if int(timestamp.timestamp() * 1000) >= since_ms
        ][:limit]

    def open_interest_history(self, s, **k):
        timestamps = pd.date_range(end=NOW, periods=50, freq="4h", tz="UTC")
        oi_amount = 1e4 * np.power(1.01, np.arange(len(timestamps)))
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "oi_amount": oi_amount,
                "oi_value": oi_amount * 100.0,
            }
        )

    def long_short_ratio(self, s, **k):
        timestamps = pd.date_range(end=NOW, periods=50, freq="4h", tz="UTC")
        ratios = np.linspace(0.8, 1.2, len(timestamps))
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "long_short_ratio": ratios,
                "long_account": ratios / (1.0 + ratios),
                "short_account": 1.0 / (1.0 + ratios),
            }
        )

    def depth(self, s):
        return {
            "bids": [(99.9, 1_000_000.0)],
            "asks": [(100.1, 1_000_000.0)],
        }


def _by_symbol(packs):
    return {p.symbol: p for p in packs}


def test_build_evidence_assembles_per_coin_fields():
    risk_model = {}
    packs = build_evidence(
        _FakeEx(),
        ["SOL/USDT:USDT"],
        now=NOW,
        btc_symbol=BTC,
        risk_model_out=risk_model,
    )
    # BTC is always included (hedge mark + beta reference), so SOL + BTC.
    by = _by_symbol(packs)
    assert set(by) == {"SOL/USDT:USDT", BTC}
    p = by["SOL/USDT:USDT"]
    assert isinstance(p, EvidencePack)
    assert p.mark == 100.0
    assert p.last_settled_funding_rate == pytest.approx(0.0001)
    assert p.last_settled_funding_8h_bps == pytest.approx(1.0)
    assert p.conservative_funding_8h_bps == pytest.approx(1.0)
    assert p.expected_funding_8h_bps == p.conservative_funding_8h_bps
    assert p.funding_rate == 0.0001
    assert p.funding_history_available is True
    assert p.funding_observations_24h == 3
    assert p.funding_observations_72h == 9
    assert p.funding_observations_168h == 21
    assert p.momentum_pct > 0  # trending up
    assert 0.0 < p.momentum_6h_pct < p.momentum_24h_pct < p.momentum_72h_pct
    assert p.momentum_72h_pct < p.momentum_168h_pct
    assert p.beta_adjusted_momentum_24h_pct == pytest.approx(0.0, abs=1e-10)
    assert p.momentum_acceleration_24h_pct == pytest.approx(0.0, abs=1e-10)
    assert p.drawdown_from_72h_high_pct == pytest.approx(0.0)
    assert p.realized_vol >= 0.0
    assert p.open_interest == p.open_interest_usd
    assert p.open_interest_contracts > 0.0
    assert p.open_interest_history_available is True
    assert p.open_interest_latest_fresh is True
    assert p.open_interest_latest_age_hours == pytest.approx(0.0)
    assert p.oi_contract_change_24h_pct > 0.0
    assert p.oi_contract_change_72h_pct > p.oi_contract_change_24h_pct
    assert p.oi_contract_change_168h_pct > p.oi_contract_change_72h_pct
    assert p.oi_change_pct == p.oi_contract_change_168h_pct
    assert p.oi_contract_change_24h_available is True
    assert p.oi_contract_change_24h_observation_hours == pytest.approx(24.0)
    assert p.oi_contract_change_24h_coverage_frac == pytest.approx(1.0)
    assert p.long_short_ratio == 1.2  # latest long_short_ratio
    assert p.long_short_ratio_latest_fresh is True
    assert p.long_short_ratio_change_24h_pct > 0.0
    assert p.long_short_ratio_change_168h_pct > p.long_short_ratio_change_24h_pct
    assert p.long_short_ratio_zscore_168h > 0.0
    assert p.long_short_ratio_percentile_168h > 95.0
    assert p.beta_btc == pytest.approx(1.0)  # identical series -> beta 1.0
    assert p.beta_n_samples == 45  # actual timestamp-aligned lookback count
    assert p.beta_standard_error == pytest.approx(0.0, abs=1e-12)
    assert p.beta_r_squared == pytest.approx(1.0)
    assert p.beta_uncertainty_status == "usable"
    assert p.beta_adjusted_cross_sectional_zscore_24h is None
    assert p.beta_adjusted_cross_sectional_percentile_24h is None
    assert p.residual_trend_by_horizon["168"]["samples"] == 169
    assert p.residual_return_observations == 198
    assert p.realized_vol_annualized_by_horizon["168"] is not None
    assert p.volume_features_available is False
    assert p.volume_participation_24h is None
    assert p.volume_surprise_24h is None
    assert p.quote_volume_24h is None
    assert p.liquidity_mid == pytest.approx(100.0)
    assert p.momentum_mark_is_partial is True
    assert p.momentum_candle_open_ts == NOW
    assert p.hourly_statistics_as_of_ts == NOW
    assert p.daily_beta_as_of_ts == NOW
    assert risk_model["return_label"] == "hourly_log_return_minus_beta_clamped_times_btc"
    assert risk_model["symbols"] == ["SOL/USDT:USDT"]
    assert risk_model["pairwise_samples"]["SOL/USDT:USDT"]["SOL/USDT:USDT"] == 168
    assert BTC not in risk_model["symbols"]
    assert risk_model["preferred_covariance_estimator"] == "ewma_diagonal_shrinkage"
    assert risk_model["multi_window_estimators"]["48"]["available"] is True
    stress_24h = risk_model["residual_horizon_scenarios"]["24"]
    assert stress_24h["observations"] == 145
    assert stress_24h["stored_scenarios"] == 12
    assert len(stress_24h["scenarios"]) == 12
    assert (
        stress_24h["scenario_selection"]["stored_scenarios_are_distributionally_representative"]
        is False
    )
    assert stress_24h["expected_shortfall_97_5_lower_by_symbol"]["SOL/USDT:USDT"] <= 0.0
    assert stress_24h["expected_shortfall_97_5_upper_by_symbol"]["SOL/USDT:USDT"] >= 0.0


def test_richer_price_features_are_available_and_stress_paths_stay_compact():
    class _FeatureExchange(_FakeEx):
        def ohlcv(self, symbol, timeframe="1h", limit=200):
            periods = 60 if timeframe == "1d" else 200
            timestamps = pd.date_range(
                end=NOW,
                periods=periods,
                freq="1D" if timeframe == "1d" else "1h",
                tz="UTC",
            )
            step = np.arange(periods, dtype=float)
            btc_returns = 0.0001 + 0.0008 * np.sin(step / 4.0)
            beta, residual = {
                BTC: (1.0, 0.0),
                "A/USDT:USDT": (1.2, 0.00035),
                "B/USDT:USDT": (0.8, -0.00025),
                "C/USDT:USDT": (1.0, 0.00005 * np.cos(step / 3.0)),
            }[symbol]
            returns = beta * btc_returns + residual
            close = 100.0 * np.exp(np.cumsum(returns))
            volume = np.full(periods, 1_000.0)
            volume[-25:-1] = 2_000.0
            return pd.DataFrame(
                {"timestamp": timestamps, "close": close, "volume": volume}
            )

    risk_model = {}
    rows = _by_symbol(
        build_evidence(
            _FeatureExchange(),
            ["A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"],
            now=NOW,
            btc_symbol=BTC,
            risk_model_out=risk_model,
        )
    )

    for symbol in ("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"):
        row = rows[symbol]
        assert row.beta_adjusted_cross_sectional_zscore_24h is not None
        assert row.beta_adjusted_cross_sectional_percentile_24h is not None
        assert row.residual_trend_by_horizon["168"]["slope_log_return_per_hour"] is not None
        assert row.realized_vol_annualized_by_horizon["24"] is not None
        assert row.volume_features_available is True
        assert row.volume_surprise_24h == pytest.approx(1.0)
        assert row.quote_volume_24h > 0.0
    assert rows["A/USDT:USDT"].beta_adjusted_cross_sectional_percentile_24h == 100.0
    assert rows["B/USDT:USDT"].beta_adjusted_cross_sectional_percentile_24h < 50.0

    for packet in risk_model["residual_horizon_scenarios"].values():
        assert len(packet["scenarios"]) <= 12
        assert packet["stored_scenarios"] == len(packet["scenarios"])
    stress = risk_model["residual_horizon_scenarios"]["72"]
    assert stress["observations"] > stress["stored_scenarios"]
    assert set(stress["expected_shortfall_97_5_lower_by_symbol"]) == {
        "A/USDT:USDT",
        "B/USDT:USDT",
        "C/USDT:USDT",
    }
    assert set(stress["expected_shortfall_97_5_upper_by_symbol"]) == set(
        stress["expected_shortfall_97_5_lower_by_symbol"]
    )


def test_build_evidence_is_fail_soft_per_coin():
    class _Broken(_FakeEx):
        def funding(self, s):
            raise RuntimeError("boom")

    packs = build_evidence(_Broken(), ["X/USDT:USDT"], now=NOW, btc_symbol=BTC)
    by = _by_symbol(packs)
    assert by["X/USDT:USDT"].funding_rate == 0.0  # missing datum -> neutral default, not a crash
    assert by["X/USDT:USDT"].conservative_funding_8h_bps == 0.0


def test_last_settled_funding_is_not_mislabeled_as_expected_when_history_is_unstable():
    class _UnstableFunding(_FakeEx):
        def funding_history(self, s, *, since_ms, limit):
            timestamps = pd.date_range(end=NOW, periods=25, freq="8h", tz="UTC")
            return [
                {
                    "timestamp": timestamp.to_pydatetime(),
                    "rate": 0.0001 if index % 2 == 0 else -0.0001,
                    "mark": 100.0,
                }
                for index, timestamp in enumerate(timestamps)
                if int(timestamp.timestamp() * 1000) >= since_ms
            ][:limit]

    row = _by_symbol(
        build_evidence(_UnstableFunding(), ["SOL/USDT:USDT"], now=NOW, btc_symbol=BTC)
    )["SOL/USDT:USDT"]
    assert row.last_settled_funding_8h_bps == pytest.approx(1.0)
    # Persistence is the documented dominant-sign fraction: 2 positive / 1 negative.
    assert row.funding_sign_persistence_24h == pytest.approx(2.0 / 3.0)
    assert row.conservative_funding_8h_bps == 0.0
    # The deprecated alias follows the conservative statistic, never the latest settled print.
    assert row.expected_funding_8h_bps == 0.0


def test_incomplete_funding_history_cannot_produce_conservative_carry():
    class _GappedFunding(_FakeEx):
        def funding_history(self, s, *, since_ms, limit):
            events = super().funding_history(s, since_ms=since_ms, limit=limit)
            return [event for index, event in enumerate(events) if index != len(events) - 5]

    row = _by_symbol(build_evidence(_GappedFunding(), ["SOL/USDT:USDT"], now=NOW, btc_symbol=BTC))[
        "SOL/USDT:USDT"
    ]
    assert row.last_settled_funding_8h_bps == pytest.approx(1.0)
    assert row.funding_sign_persistence_168h == pytest.approx(1.0)
    assert row.conservative_funding_8h_bps == 0.0


def test_current_forming_candle_is_live_momentum_only_not_statistical_input():
    baseline_risk = {}
    baseline = _by_symbol(
        build_evidence(
            _FakeEx(),
            ["SOL/USDT:USDT"],
            now=NOW,
            btc_symbol=BTC,
            risk_model_out=baseline_risk,
        )
    )["SOL/USDT:USDT"]

    class _PartialSpike(_FakeEx):
        def ohlcv(self, s, timeframe="1h", limit=200):
            frame = super().ohlcv(s, timeframe=timeframe, limit=limit)
            if s == "SOL/USDT:USDT":
                frame.loc[frame.index[-1], "close"] *= 10.0
            return frame

    spiked_risk = {}
    spiked = _by_symbol(
        build_evidence(
            _PartialSpike(),
            ["SOL/USDT:USDT"],
            now=NOW,
            btc_symbol=BTC,
            risk_model_out=spiked_risk,
        )
    )["SOL/USDT:USDT"]
    assert spiked.momentum_mark_is_partial is True
    assert spiked.momentum_mark > baseline.momentum_mark * 9.0
    assert spiked.intrabar_move_from_last_completed_pct > 800.0
    for field in (
        "momentum_pct",
        "momentum_6h_pct",
        "momentum_24h_pct",
        "momentum_72h_pct",
        "momentum_168h_pct",
        "beta_adjusted_momentum_6h_pct",
        "beta_adjusted_momentum_24h_pct",
        "beta_adjusted_momentum_72h_pct",
        "beta_adjusted_momentum_168h_pct",
        "momentum_acceleration_24h_pct",
        "drawdown_from_72h_high_pct",
    ):
        assert getattr(spiked, field) == pytest.approx(getattr(baseline, field))
    assert spiked.realized_vol == pytest.approx(baseline.realized_vol)
    assert spiked.beta_btc == pytest.approx(baseline.beta_btc)
    assert spiked.residual_trend_by_horizon == baseline.residual_trend_by_horizon
    assert (
        spiked.realized_vol_annualized_by_horizon
        == baseline.realized_vol_annualized_by_horizon
    )
    assert spiked.downside_vol_annualized_by_horizon == baseline.downside_vol_annualized_by_horizon
    assert spiked_risk == baseline_risk
    assert spiked.hourly_statistics_as_of_ts == NOW
    assert spiked.daily_beta_as_of_ts == NOW


def test_contract_oi_change_is_not_confused_with_price_driven_usd_oi_change():
    class _PriceOnlyOiMove(_FakeEx):
        def open_interest_history(self, s, **k):
            timestamps = pd.date_range(end=NOW, periods=50, freq="4h", tz="UTC")
            return pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "oi_amount": np.full(len(timestamps), 10_000.0),
                    "oi_value": np.linspace(1_000_000.0, 2_000_000.0, len(timestamps)),
                }
            )

    row = _by_symbol(
        build_evidence(_PriceOnlyOiMove(), ["SOL/USDT:USDT"], now=NOW, btc_symbol=BTC)
    )["SOL/USDT:USDT"]
    assert row.open_interest_contracts == pytest.approx(10_000.0)
    assert row.open_interest_usd == pytest.approx(2_000_000.0)
    assert row.oi_contract_change_24h_pct == pytest.approx(0.0)
    assert row.oi_contract_change_72h_pct == pytest.approx(0.0)
    assert row.oi_contract_change_168h_pct == pytest.approx(0.0)


def test_stale_positioning_history_is_audited_and_neutralized():
    class _StalePositioning(_FakeEx):
        def open_interest_history(self, s, **k):
            frame = super().open_interest_history(s, **k)
            frame["timestamp"] = frame["timestamp"] - pd.Timedelta(hours=12)
            return frame

        def long_short_ratio(self, s, **k):
            frame = super().long_short_ratio(s, **k)
            frame["timestamp"] = frame["timestamp"] - pd.Timedelta(hours=12)
            return frame

    row = _by_symbol(
        build_evidence(_StalePositioning(), ["SOL/USDT:USDT"], now=NOW, btc_symbol=BTC)
    )["SOL/USDT:USDT"]
    assert row.open_interest_history_available is True
    assert row.open_interest_latest_age_hours == pytest.approx(12.0)
    assert row.open_interest_latest_fresh is False
    assert row.oi_contract_change_24h_available is False
    assert row.oi_contract_change_24h_pct is None
    assert row.long_short_ratio_latest_age_hours == pytest.approx(12.0)
    assert row.long_short_ratio_latest_fresh is False
    assert row.long_short_ratio_change_24h_available is False
    assert row.long_short_ratio_change_24h_pct is None
    assert row.long_short_ratio_zscore_168h is None
    assert row.long_short_ratio_percentile_168h is None


def test_undercovered_positioning_horizon_is_audited_and_neutralized():
    class _MissingRecentObservation(_FakeEx):
        def open_interest_history(self, s, **k):
            frame = super().open_interest_history(s, **k)
            return frame.drop(frame.index[-3]).reset_index(drop=True)

        def long_short_ratio(self, s, **k):
            frame = super().long_short_ratio(s, **k)
            return frame.drop(frame.index[-3]).reset_index(drop=True)

    row = _by_symbol(
        build_evidence(
            _MissingRecentObservation(),
            ["SOL/USDT:USDT"],
            now=NOW,
            btc_symbol=BTC,
        )
    )["SOL/USDT:USDT"]
    assert row.open_interest_latest_fresh is True
    assert row.oi_contract_change_24h_coverage_frac < 0.90
    assert row.oi_contract_change_24h_available is False
    assert row.oi_contract_change_24h_pct is None
    assert row.long_short_ratio_change_24h_coverage_frac < 0.90
    assert row.long_short_ratio_change_24h_available is False
    assert row.long_short_ratio_change_24h_pct is None


def test_build_evidence_fails_closed_when_required_candles_fail():
    class _BrokenCandles(_FakeEx):
        def ohlcv(self, s, timeframe="1h", limit=200):
            raise RuntimeError("proxy unavailable")

    with pytest.raises(RuntimeError, match="proxy unavailable"):
        build_evidence(_BrokenCandles(), ["X/USDT:USDT"], now=NOW, btc_symbol=BTC)


def test_slippage_curve_floored_at_half_spread():
    """Cycle-17 regression: a deep book gives ~0 depth-walk impact, but crossing the book still
    costs the HALF-SPREAD. The curve must never read below it, else break-even math prices a
    rotation ~5x too cheap (ADA: 0.0 walk, 6.08bps spread -> real fill cost $20+ on $4,300)."""
    from futures_fund.evidence import _slip_bps_at

    # a very deep single level right at the mark -> depth-walk cost ~0
    deep_asks = [(1.0, 10_000_000.0)]
    no_spread = _slip_bps_at("X/USDT:USDT", 1.0, deep_asks, 4300.0, half_spread_bps=0.0)
    floored = _slip_bps_at("X/USDT:USDT", 1.0, deep_asks, 4300.0, half_spread_bps=3.042)
    assert no_spread < 0.5  # deep book -> negligible walk cost
    assert floored >= 3.042  # ...but never below the half-spread
    # empty-book fallback still returns the floor, not 0
    empty = _slip_bps_at("X/USDT:USDT", 1.0, [], 4300.0, half_spread_bps=3.042)
    assert empty == pytest.approx(3.042)


def test_liquidity_curve_uses_same_book_mid_not_an_earlier_funding_mark():
    """Cycle-3 regression: an earlier mark must not make a deep fresh book look illiquid.

    The decision/funding mark is 90, while the L2 book is 99.9/100.1. Liquidity is the cost of
    crossing that book from its own 100 midpoint: 10bps, not the ~1,122bps distance from 90.
    """

    class _EarlierFundingMark(_FakeEx):
        def funding(self, s):
            from futures_fund.market_data import FundingInfo

            return FundingInfo(
                symbol=s,
                current_rate=0.0001,
                next_funding_ts=NOW,
                interval_hours=8.0,
                mark_price=90.0,
                index_price=90.0,
            )

    packs = build_evidence(
        _EarlierFundingMark(),
        ["SOL/USDT:USDT"],
        now=NOW,
        btc_symbol=BTC,
    )
    row = _by_symbol(packs)["SOL/USDT:USDT"]
    assert row.mark == pytest.approx(90.0)
    assert row.liquidity_mid == pytest.approx(100.0)
    assert row.spread_bps == pytest.approx(20.0)
    assert row.est_slippage_bps_2k == pytest.approx(10.0)
    assert row.slippage_curve_bps == {
        "2k": 10.0,
        "5k": 10.0,
        "10k": 10.0,
        "20k": 10.0,
    }
    assert row.slippage_curve_buy_bps == row.slippage_curve_bps
    assert row.slippage_curve_sell_bps == row.slippage_curve_bps


def test_liquidity_curve_uses_worse_crossing_side():
    from futures_fund.evidence import _depth_fields

    class _AsymmetricBook:
        def depth(self, s):
            return {
                "bids": [(99.9, 1_000_000.0)],
                "asks": [(100.1, 1.0), (101.0, 1_000_000.0)],
            }

    _, _, mid, _, _, curve, buy_curve, sell_curve = _depth_fields(_AsymmetricBook(), "X/USDT:USDT")
    assert mid == pytest.approx(100.0)
    assert curve["2k"] > 10.0
    assert curve["2k"] == buy_curve["2k"]
    assert sell_curve["2k"] == pytest.approx(10.0)


def test_liquidity_curve_omits_points_not_fully_covered_on_both_sides():
    from futures_fund.evidence import _depth_fields

    class _ThinBook:
        def depth(self, s):
            return {
                "bids": [(99.9, 10.0)],
                "asks": [(100.1, 10.0)],
            }

    bid_usd, ask_usd, _, _, slip2k, curve, buy_curve, sell_curve = _depth_fields(
        _ThinBook(), "X/USDT:USDT"
    )

    assert bid_usd < 2000.0 and ask_usd < 2000.0
    assert curve == {}
    assert buy_curve == {}
    assert sell_curve == {}
    assert slip2k == 0.0


def test_liquidity_curve_preserves_a_fully_covered_direction_when_other_side_is_thin():
    from futures_fund.evidence import _depth_fields

    class _AsymmetricCoverage:
        def depth(self, s):
            return {
                "bids": [(99.9, 1_000_000.0)],
                "asks": [(100.1, 1.0)],
            }

    bid_usd, ask_usd, mid, _, slip2k, curve, buy_curve, sell_curve = _depth_fields(
        _AsymmetricCoverage(), "X/USDT:USDT"
    )

    assert mid == pytest.approx(100.0)
    assert bid_usd > 20_000.0 and ask_usd < 2_000.0
    assert curve == {}  # aggregate remains a both-directions compatibility field
    assert buy_curve == {}
    assert set(sell_curve) == {"2k", "5k", "10k", "20k"}
    assert slip2k == 0.0


def test_build_evidence_derives_mark_from_funding_without_mark_price():
    """Dedup (rate-limit fix): funding carries the mark, so a valid mark must be produced even if
    the dedicated mark_price() endpoint is unavailable — proving the redundant call was removed."""

    class _NoMarkPrice(_FakeEx):
        def mark_price(self, s):  # must NOT be needed when funding already carries the mark
            raise RuntimeError("mark_price endpoint should not be called")

    packs = build_evidence(_NoMarkPrice(), ["SOL/USDT:USDT"], now=NOW, btc_symbol=BTC)
    by = _by_symbol(packs)
    assert by["SOL/USDT:USDT"].mark == pytest.approx(100.0)  # taken from funding's mark_price
