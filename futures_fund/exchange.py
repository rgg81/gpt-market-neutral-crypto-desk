from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation

import pandas as pd

from futures_fund.candle_proxy import BinanceCandleProxy, CandleProxyError
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


def quantize_order_quantity(quantity: float, step_size: float) -> float:
    """Round a signed order quantity toward zero to an exchange-valid lot step."""
    try:
        quantity_decimal = Decimal(str(quantity))
        step_decimal = Decimal(str(step_size))
    except InvalidOperation as exc:
        raise ValueError("quantity and step size must be finite decimals") from exc
    if not quantity_decimal.is_finite() or not step_decimal.is_finite() or step_decimal <= 0:
        raise ValueError("quantity must be finite and step size must be finite and positive")
    units = (abs(quantity_decimal) / step_decimal).to_integral_value(rounding=ROUND_DOWN)
    quantized = units * step_decimal
    if quantity_decimal < 0:
        quantized = -quantized
    return float(quantized)


def build_ccxt(settings: Settings):
    """Construct a ccxt binanceusdm client (lazy import).

    The desk is structurally paper-only, so this is always a PUBLIC keyless mainnet data client.
    API credentials are never read or attached.
    """
    import ccxt

    return ccxt.binanceusdm({"enableRateLimit": True})


def default_symbol_spec(market: dict) -> SymbolSpec:
    """Build a SymbolSpec from PUBLIC exchangeInfo only (no leverage tiers); one conservative
    MMR bracket (5% maintenance, 20x cap). Used in paper/keyless mode.

    PAPER fills simulate market orders, so ``MARKET_LOT_SIZE`` is authoritative. Binance may
    publish zero-valued market-lot fields for a contract; only then do we fall back to the ordinary
    ``LOT_SIZE`` rule. The maximum order quantity is retained for fail-closed execution checks.
    """
    filters = (market.get("info") or {}).get("filters") or []
    tick = _filter_field(filters, "PRICE_FILTER", "tickSize")
    market_step = _filter_field(filters, "MARKET_LOT_SIZE", "stepSize")
    lot_step = _filter_field(filters, "LOT_SIZE", "stepSize")
    step = market_step if market_step is not None and market_step > 0.0 else lot_step
    market_min_qty = _filter_field(filters, "MARKET_LOT_SIZE", "minQty")
    lot_min_qty = _filter_field(filters, "LOT_SIZE", "minQty")
    min_qty = (
        market_min_qty
        if market_min_qty is not None and market_min_qty > 0.0
        else lot_min_qty
    )
    market_max_qty = _filter_field(filters, "MARKET_LOT_SIZE", "maxQty")
    lot_max_qty = _filter_field(filters, "LOT_SIZE", "maxQty")
    max_qty = (
        market_max_qty
        if market_max_qty is not None and market_max_qty > 0.0
        else lot_max_qty
    )
    mn = _filter_field(filters, "MIN_NOTIONAL", "notional")
    if tick is None:
        tick = float(market["precision"]["price"])
    if step is None:
        step = float(market["precision"]["amount"])
    if mn is None:
        mn = float((market.get("limits", {}).get("cost", {}) or {}).get("min") or 5.0)
    return SymbolSpec(
        symbol=market["id"],
        tick_size=float(tick),
        step_size=float(step),
        min_notional=float(mn),
        min_qty=float(min_qty) if min_qty is not None and min_qty > 0.0 else None,
        max_qty=float(max_qty) if max_qty is not None and max_qty > 0.0 else None,
        mmr_brackets=[
            MmrBracket(
                notional_floor=0.0, notional_cap=1e12, mmr=0.05, maint_amount=0.0, max_leverage=20.0
            )
        ],
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
    _retry_base_delay: float = 0.75  # seconds; doubles each retry (0.75, 1.5, 3.0)

    def __init__(
        self,
        client,
        keyless: bool = False,
        kline_proxy: BinanceCandleProxy | None = None,
    ):
        self.client = client
        self.keyless = keyless
        self.kline_proxy = kline_proxy

    def _call(self, fn, *args, **kwargs):
        """Invoke a ccxt client method with transient-error retry + backoff (see class docstring).
        Falls back to a plain call when ccxt is unavailable (e.g. an injected test fake)."""
        try:
            import ccxt
        except Exception:  # noqa: BLE001 — no ccxt (tests inject a fake client): just call through
            return fn(*args, **kwargs)
        transient = (
            ccxt.RateLimitExceeded,
            ccxt.ExchangeNotAvailable,
            ccxt.NetworkError,
            ccxt.RequestTimeout,
        )
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
        return cls(
            ex,
            keyless=True,
            kline_proxy=BinanceCandleProxy.from_settings(settings),
        )

    def _raw_id(self, symbol: str) -> str:
        return self.client.market(symbol)["id"]

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        market = self.client.market(symbol)
        if self.keyless:
            return default_symbol_spec(market)
        tiers = self._call(self.client.fetch_leverage_tiers, [symbol])[symbol]
        return parse_symbol_spec(market, tiers)

    def ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 500) -> pd.DataFrame:
        if self.kline_proxy is None:
            raise CandleProxyError(
                "mandatory Binance candle proxy is not configured; direct OHLCV is forbidden"
            )
        rows = self.kline_proxy.fetch_ohlcv(self._raw_id(symbol), timeframe, limit)
        return parse_ohlcv(rows)

    def klines(self, symbol: str, timeframe: str, limit: int) -> list[list]:
        """Full proxy-only Binance kline rows, including quote-asset volume."""
        if self.kline_proxy is None:
            raise CandleProxyError(
                "mandatory Binance candle proxy is not configured; direct klines are forbidden"
            )
        return self.kline_proxy.fetch_klines(self._raw_id(symbol), timeframe, limit)

    def require_candle_proxy(self) -> None:
        if self.kline_proxy is None:
            raise CandleProxyError("mandatory Binance candle proxy is not configured")
        self.kline_proxy.healthcheck()

    def candle_audit(self) -> dict:
        if self.kline_proxy is None:
            raise CandleProxyError("mandatory Binance candle proxy is not configured")
        return self.kline_proxy.audit_snapshot()

    def funding(self, symbol: str) -> FundingInfo:
        fr = self._call(self.client.fetch_funding_rate, symbol)
        interval_hours = self.funding_interval_hours(symbol)
        interval = {"info": {"fundingIntervalHours": interval_hours}}
        return parse_funding(fr, interval)

    def funding_interval_hours(self, symbol: str) -> float:
        """Return a proven current settlement interval; transport/shape failures propagate.

        Binance's ``fundingInfo`` endpoint lists adjusted schedules only.  A successful plural
        response that omits a symbol therefore proves the standard 8h schedule.  That is distinct
        from a failed request, malformed response, or singular test seam returning no metadata;
        those cases raise so a held-position settlement can never silently assume 8h.
        """
        plural = getattr(self.client, "fetch_funding_intervals", None)
        if callable(plural):
            intervals = self._call(plural, [symbol])
            if not isinstance(intervals, dict):
                raise ValueError(f"funding interval response for {symbol} is not a mapping")
            interval = intervals.get(symbol)
            if interval is None:
                return 8.0
        else:
            singular = getattr(self.client, "fetch_funding_interval", None)
            if not callable(singular):
                raise ValueError(f"funding interval endpoint unavailable for {symbol}")
            interval = self._call(singular, symbol)
            if not isinstance(interval, dict):
                raise ValueError(f"funding interval metadata unavailable for {symbol}")

        info = interval.get("info") or {}
        raw = info.get("fundingIntervalHours")
        if raw is None:
            raw = interval.get("fundingIntervalHours", interval.get("interval"))
        try:
            hours = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid funding interval metadata for {symbol}: {raw!r}") from exc
        if not hours.is_integer() or int(hours) not in {1, 2, 4, 8}:
            raise ValueError(f"unsupported funding interval for {symbol}: {hours}")
        return hours

    def funding_history(self, symbol: str, *, since_ms: int, limit: int = 1000) -> list[dict]:
        """Return exact historical settlement rate+mark pairs from the public exchange feed."""
        raw = self._call(
            self.client.fetch_funding_rate_history,
            symbol,
            since_ms,
            limit,
        )
        events: list[dict] = []
        for item in raw:
            info = item.get("info") or {}
            timestamp_raw = item.get("timestamp", info.get("fundingTime"))
            rate_raw = item.get("fundingRate", info.get("fundingRate"))
            mark_raw = item.get("markPrice", info.get("markPrice"))
            if timestamp_raw is None or rate_raw is None or mark_raw is None:
                raise ValueError(f"historical funding event for {symbol} lacks timestamp/rate/mark")
            events.append(
                {
                    "timestamp": datetime.fromtimestamp(float(timestamp_raw) / 1000.0, tz=UTC),
                    "rate": float(rate_raw),
                    "mark": float(mark_raw),
                }
            )
        return events

    def open_interest_history(
        self, symbol: str, period: str = "4h", limit: int = 200
    ) -> pd.DataFrame:
        """Timestamped contract-amount and USD-value OI history (about 33d at defaults).

        Evidence computes explicit 24h/72h/168h changes from ``oi_amount``.  ``oi_value`` remains
        separate because price movement changes USD OI even when the number of contracts does not.
        """
        return parse_open_interest_history(
            self._call(self.client.fetch_open_interest_history, symbol, period, None, limit)
        )

    def long_short_ratio(self, symbol: str, period: str = "4h", limit: int = 200) -> pd.DataFrame:
        """Timestamped global account long/short history for horizon changes/normalization."""
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
