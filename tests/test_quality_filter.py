from __future__ import annotations

from datetime import UTC, datetime, timedelta

from futures_fund.market_data import quality_filter

_NOW = datetime(2026, 6, 12, tzinfo=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class _FakeDepthExchange:
    """Deep books for the established names, a thin book for VELVET."""

    _DEEP = {"bids": [(100.0, 5000.0)], "asks": [(100.0, 5000.0)]}      # ~$500k/side
    _THIN = {"bids": [(1.0, 1000.0)], "asks": [(1.0, 1000.0)]}          # ~$1k/side

    def depth(self, symbol, limit=20):
        return self._THIN if symbol.startswith("VELVET") else self._DEEP

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        raise AssertionError("quality_filter must not issue an undeclared candle request")


def _rows():
    old = _ms(_NOW - timedelta(days=900))
    young = _ms(_NOW - timedelta(days=5))
    return [
        {"symbol": "BTC/USDT:USDT", "last": 60000.0, "chg_24h_pct": 1.0,
         "vol_24h_usd": 2e9, "onboard_date": old},
        {"symbol": "ETH/USDT:USDT", "last": 3000.0, "chg_24h_pct": -0.5,
         "vol_24h_usd": 1e9, "onboard_date": old},
        {"symbol": "SOL/USDT:USDT", "last": 150.0, "chg_24h_pct": 2.0,
         "vol_24h_usd": 8e8, "onboard_date": old},
        # VELVET: new (5d) AND a +130% pump AND a thin book -> fails THREE gates
        {"symbol": "VELVET/USDT:USDT", "last": 1.0, "chg_24h_pct": 130.0,
         "vol_24h_usd": 7e8, "onboard_date": young},
    ]


def test_velvet_excluded_majors_included():
    kept, drops = quality_filter(
        _rows(), now=_NOW, exchange=_FakeDepthExchange(),
        min_adv_usd=5e8, min_age_days=30, max_abs_chg_24h_pct=25.0,
        min_depth_usd=250_000.0, depth_ref_usd=100_000.0, symbol_count=30,
    )
    syms = [r["symbol"] for r in kept]
    assert "BTC/USDT:USDT" in syms   # ~$500k deep book CLEARS the 250k floor
    assert "ETH/USDT:USDT" in syms
    assert "SOL/USDT:USDT" in syms
    assert "VELVET/USDT:USDT" not in syms


def test_drop_counts_are_explicit_no_silent_truncation():
    kept, drops = quality_filter(
        _rows(), now=_NOW, exchange=_FakeDepthExchange(),
        min_adv_usd=5e8, min_age_days=30, max_abs_chg_24h_pct=25.0,
        min_depth_usd=250_000.0, depth_ref_usd=100_000.0, symbol_count=30,
    )
    # VELVET fails the age gate first (gates short-circuit in order), so age==1, others 0
    assert drops["age"] == 1
    assert drops["chg_24h"] == 0
    assert drops["depth"] == 0
    assert drops["adv"] == 0
    assert len(kept) == 3


def test_missing_onboard_date_is_explicit_and_never_fetches_unbound_candles():
    rows = [{"symbol": "UNKNOWN/USDT:USDT", "last": 1.0, "chg_24h_pct": 0.0,
             "vol_24h_usd": 1e9, "onboard_date": None}]
    kept, drops = quality_filter(
        rows, now=_NOW, exchange=_FakeDepthExchange(), min_adv_usd=1e8,
        min_age_days=30, max_abs_chg_24h_pct=25.0, min_depth_usd=250_000.0,
        depth_ref_usd=100_000.0, symbol_count=30,
    )
    assert [r["symbol"] for r in kept] == ["UNKNOWN/USDT:USDT"]
    assert drops["age"] == 0
    assert drops["age_unknown"] == 1


def test_invalid_onboard_date_is_explicit_age_unknown_without_candle_probe():
    rows = [{"symbol": "YOUNG/USDT:USDT", "last": 1.0, "chg_24h_pct": 0.0,
             "vol_24h_usd": 1e9, "onboard_date": float("nan")}]
    kept, drops = quality_filter(
        rows, now=_NOW, exchange=_FakeDepthExchange(), min_adv_usd=1e8,
        min_age_days=30, max_abs_chg_24h_pct=25.0, min_depth_usd=250_000.0,
        depth_ref_usd=100_000.0, symbol_count=30,
    )
    assert [r["symbol"] for r in kept] == ["YOUNG/USDT:USDT"]
    assert drops["age_unknown"] == 1


def test_depth_floor_excludes_thin_book():
    rows = [{"symbol": "VELVET/USDT:USDT", "last": 1.0, "chg_24h_pct": 0.0,
             "vol_24h_usd": 1e9, "onboard_date": _ms(_NOW - timedelta(days=900))}]
    kept, drops = quality_filter(
        rows, now=_NOW, exchange=_FakeDepthExchange(), min_adv_usd=1e8,
        min_age_days=30, max_abs_chg_24h_pct=25.0, min_depth_usd=250_000.0,
        depth_ref_usd=100_000.0, symbol_count=30,
    )
    assert kept == []
    assert drops["depth"] == 1


def test_depth_unavailable_keeps_name_and_is_counted():
    # exchange.depth raises -> the name is KEPT (not silently dropped) and counted.
    class _NoDepth(_FakeDepthExchange):
        def depth(self, symbol, limit=20):
            raise RuntimeError("no order book")

    rows = [{"symbol": "OK/USDT:USDT", "last": 1.0, "chg_24h_pct": 0.0,
             "vol_24h_usd": 1e9, "onboard_date": _ms(_NOW - timedelta(days=900))}]
    kept, drops = quality_filter(
        rows, now=_NOW, exchange=_NoDepth(), min_adv_usd=1e8,
        min_age_days=30, max_abs_chg_24h_pct=25.0, min_depth_usd=250_000.0,
        depth_ref_usd=100_000.0, symbol_count=30,
    )
    assert [r["symbol"] for r in kept] == ["OK/USDT:USDT"]
    assert drops["depth_unavailable"] == 1
    assert drops["depth"] == 0
