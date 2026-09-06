from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from futures_fund.candle_proxy import BinanceCandleProxy, CandleProxyError, StaleCandleData

NOW = datetime(2026, 8, 24, 10, 30, tzinfo=UTC)
HOUR_MS = 3_600_000


def _row(open_time: int, interval_ms: int = HOUR_MS) -> list:
    return [
        open_time, "1", "2", "0.5", "1.5", "10", open_time + interval_ms - 1,
        "15", 3, "1", "1", "0",
    ]


def test_proxy_range_request_contains_current_candle_and_is_audited():
    boundary = int(NOW.timestamp() * 1000) // HOUR_MS * HOUR_MS
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[
            _row(boundary - 2 * HOUR_MS),
            _row(boundary - HOUR_MS),
            _row(boundary),
        ])

    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(handler),
    )
    rows = proxy.fetch_ohlcv("BTCUSDT", "1h", 3)

    assert len(rows) == 3
    assert all(len(row) == 6 for row in rows)
    assert "fapi/v1/klines" in seen["url"]
    assert "startTime=" in seen["url"]  # activates proxy cache/gap-fill, not live-tail bypass
    audit = proxy.audit_snapshot()
    assert audit["source"] == "binance-proxy"
    assert audit["all_fresh"] is True
    assert audit["requests"][0]["current_candle_present"] is True
    assert audit["requests"][0]["range_complete"] is True


def test_proxy_full_klines_preserve_valid_quote_volume():
    boundary = int(NOW.timestamp() * 1000) // HOUR_MS * HOUR_MS
    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[_row(boundary)])
        ),
    )
    rows = proxy.fetch_klines("BTCUSDT", "1h", 1)
    assert len(rows[0]) == 12
    assert rows[0][7] == "15"


def test_proxy_full_klines_reject_invalid_quote_volume():
    boundary = int(NOW.timestamp() * 1000) // HOUR_MS * HOUR_MS
    row = _row(boundary)
    row[7] = "NaN"
    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[row])
        ),
    )
    with pytest.raises(CandleProxyError, match="invalid quote volume"):
        proxy.fetch_klines("BTCUSDT", "1h", 1)


def test_proxy_rejects_sparse_rows_that_fake_fresh_multi_horizon_data():
    boundary = int(NOW.timestamp() * 1000) // HOUR_MS * HOUR_MS

    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[
            _row(boundary - 168 * HOUR_MS),
            _row(boundary - 24 * HOUR_MS),
            _row(boundary),
        ])),
    )
    with pytest.raises(CandleProxyError, match="incomplete/non-contiguous"):
        proxy.fetch_ohlcv("BTCUSDT", "1h", 169)


def test_proxy_rejects_overlong_candle_close_boundary():
    boundary = int(NOW.timestamp() * 1000) // HOUR_MS * HOUR_MS
    overlong = _row(boundary)
    overlong[6] = boundary + 10 * HOUR_MS - 1
    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[overlong])
        ),
    )
    with pytest.raises(CandleProxyError, match="malformed candle boundaries"):
        proxy.fetch_ohlcv("BTCUSDT", "1h", 1)


def test_proxy_rejects_nonfinite_incoherent_or_negative_ohlcv_before_green_audit():
    boundary = int(NOW.timestamp() * 1000) // HOUR_MS * HOUR_MS
    invalid = _row(boundary)
    invalid[1:6] = ["1", ".5", "2", "NaN", "-10"]
    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[invalid])
        ),
    )
    with pytest.raises(CandleProxyError, match="invalid OHLCV"):
        proxy.fetch_ohlcv("BTCUSDT", "1h", 1)
    assert proxy.audit_snapshot()["all_fresh"] is False


def test_proxy_rejects_stale_tail_after_single_boundary_retry():
    boundary = int(NOW.timestamp() * 1000) // HOUR_MS * HOUR_MS
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[_row(boundary - HOUR_MS)])

    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(StaleCandleData, match="stale proxy candles"):
        proxy.fetch_ohlcv("BTCUSDT", "1h", 3)
    assert calls["n"] == 2
    assert proxy.audit_snapshot()["all_fresh"] is False


def test_proxy_health_requires_explicit_ok_status():
    proxy = BinanceCandleProxy(
        "http://127.0.0.1:8000",
        now_fn=lambda: NOW,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"status": "ok"})
        ),
    )
    assert proxy.healthcheck() == {"status": "ok"}
