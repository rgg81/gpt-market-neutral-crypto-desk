"""Fail-closed pre-cycle proof that the mandatory local candle proxy is live and fresh."""
from __future__ import annotations

import json
import sys

from futures_fund.candle_proxy import BinanceCandleProxy
from futures_fund.config import load_settings


def main() -> int:
    settings = load_settings()
    proxy = BinanceCandleProxy.from_settings(settings)
    proxy.healthcheck()
    # Exercise both production evidence intervals, including the currently-forming candle.
    proxy.fetch_ohlcv("BTCUSDT", "1h", 3)
    proxy.fetch_ohlcv("BTCUSDT", "1d", 3)
    audit = proxy.audit_snapshot()
    if not audit["all_fresh"]:
        raise RuntimeError("Binance candle proxy preflight did not prove fresh data")
    print(json.dumps({
        "status": "READY",
        "source": audit["source"],
        "base_url": audit["base_url"],
        "fresh_intervals": ["1h", "1d"],
        "request_count": audit["request_count"],
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
