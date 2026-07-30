from __future__ import annotations

import time

import pandas as pd

from futures_fund.config import Settings
from futures_fund.market_data import (
    FundingInfo,
    _filter_field,
    parse_funding,
    parse_long_short_ratio,
    parse_ohlcv,
    parse_open_interest_history,
    parse_symbol_spec,
)
from futures_fund.models import MmrBracket, SymbolSpec


def build_ccxt(settings: Settings):
    """Construct a ccxt binanceusdm client (lazy import).

    The desk is structurally paper-only, so this is always a PUBLIC keyless mainnet data client.
    API credentials are never read or attached.
    """
    import ccxt

    return ccxt.binanceusdm({"enableRateLimit": True})


def default_symbol_spec(market: dict) -> SymbolSpec:
    """Build a SymbolSpec from PUBLIC exchangeInfo only (no leverage tiers); one conservative
    MMR bracket (5% maintenance, 20x cap). Used in paper/keyless mode."""
    filters = (market.get("info") or {}).get("filters") or []
    tick = _filter_field(filters, "PRICE_FILTER", "tickSize")
    step = _filter_field(filters, "LOT_SIZE", "stepSize")
    mn = _filter_field(filters, "MIN_NOTIONAL", "notional")
    if tick is None:
        tick = float(market["precision"]["price"])
    if step is None:
        step = float(market["precision"]["amount"])
    if mn is None:
        mn = float((market.get("limits", {}).get("cost", {}) or {}).get("min") or 5.0)
    return SymbolSpec(
        symbol=market["id"], tick_size=float(tick), step_size=float(step), min_notional=float(mn),
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12, mmr=0.05,
                                 maint_amount=0.0, max_leverage=20.0)],
    )


class FuturesExchange:
    """Thin wrapper over a ccxt-like client. Inject a fake client in tests."""

    # Resilience: every REST call goes through `_call`, which retries TRANSIENT rate-limit /
    # network blips (429 RateLimitExceeded, connection resets, timeouts) with bounded exponential
    # backoff so a momentary weight-limit spike does not crash a whole evidence build. A hard
    # DDoSProtection (HTTP 418 IP BAN) is NOT retried — Binance keeps the ban for a fixed window
    # and each extra request during it EXTENDS the ban (observed live 2026-07), so we re-raise
    # immediately and let the orchestrator HALT (prior book stands) and wait the ban out.
    _retry_attempts: int = 4
    _retry_base_delay: float = 0.75   # seconds; doubles each retry (0.75, 1.5, 3.0)

    def __init__(self, client, keyless: bool = False):
        self.client = client
        self.keyless = keyless

    def _call(self, fn, *args, **kwargs):
        """Invoke a ccxt client method with transient-error retry + backoff (see class docstring).
        Falls back to a plain call when ccxt is unavailable (e.g. an injected test fake)."""
        try:
            import ccxt
        except Exception:  # noqa: BLE001 — no ccxt (tests inject a fake client): just call through
            return fn(*args, **kwargs)
        transient = (ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable,
                     ccxt.NetworkError, ccxt.RequestTimeout)
        delay = self._retry_base_delay
        for attempt in range(self._retry_attempts):
            try:
                return fn(*args, **kwargs)
            except ccxt.DDoSProtection:
                raise  # HTTP 418 IP ban — retrying extends it; fail up to the orchestration HALT
            except transient:
                if attempt == self._retry_attempts - 1:
                    raise
                time.sleep(delay)
                delay *= 2

    @classmethod
    def from_settings(cls, settings: Settings) -> FuturesExchange:
        ex = build_ccxt(settings)
        ex.load_markets()
        return cls(ex, keyless=True)

    def _raw_id(self, symbol: str) -> str:
        return self.client.market(symbol)["id"]

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        market = self.client.market(symbol)
        if self.keyless:
            return default_symbol_spec(market)
        tiers = self._call(self.client.fetch_leverage_tiers, [symbol])[symbol]
        return parse_symbol_spec(market, tiers)

    def ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 500) -> pd.DataFrame:
        return parse_ohlcv(self._call(self.client.fetch_ohlcv, symbol, timeframe, None, limit))

    def funding(self, symbol: str) -> FundingInfo:
        fr = self._call(self.client.fetch_funding_rate, symbol)
        try:
            interval = self._call(self.client.fetch_funding_interval, symbol)
        except Exception:
            interval = None
        return parse_funding(fr, interval)

    def open_interest_history(
        self, symbol: str, period: str = "4h", limit: int = 200
    ) -> pd.DataFrame:
        return parse_open_interest_history(
            self._call(self.client.fetch_open_interest_history, symbol, period, None, limit)
        )

    def long_short_ratio(self, symbol: str, period: str = "4h", limit: int = 200) -> pd.DataFrame:
        raw = self._call(
            self.client.fapiDataGetGlobalLongShortAccountRatio,
            {"symbol": self._raw_id(symbol), "period": period, "limit": limit},
        )
        return parse_long_short_ratio(raw)

    def mark_price(self, symbol: str) -> float:
        return float(self._call(self.client.fetch_funding_rate, symbol)["markPrice"])

    def depth(self, symbol: str, limit: int = 20) -> dict[str, list[tuple[float, float]]]:
        """L2 order-book snapshot for the depth-aware slippage model (spec §13).

        Returns {"bids": [(price, qty), ...] descending, "asks": [(price, qty), ...] ascending}.
        `asks` is the crossing side for a BUY, `bids` for a SELL; both are (price, qty) tuples
        suitable for costs.vwap_fill / slippage.depth_slippage.
        """
        book = self._call(self.client.fetch_order_book, symbol, limit)
        bids = [(float(p), float(q)) for p, q in (book.get("bids") or [])]
        asks = [(float(p), float(q)) for p, q in (book.get("asks") or [])]
        return {"bids": bids, "asks": asks}
