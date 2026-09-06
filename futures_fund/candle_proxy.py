"""Exclusive, freshness-enforcing client for the local Binance klines proxy.

The production desk never fetches candles through ccxt/Binance directly.  Every range request is
sent to the user's caching proxy with an explicit ``startTime`` so immutable closed candles are
served from its cache while the currently-forming candle is refreshed upstream.  A response that
does not contain the current UTC candle is rejected as stale.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


class CandleProxyError(RuntimeError):
    """The mandatory local proxy could not provide valid candle data."""


class StaleCandleData(CandleProxyError):
    """The proxy response does not include the currently-forming candle."""


def validate_candle_audit(meta: dict, evidence: list[dict]) -> None:
    """Prove that the persisted proxy audit exactly covers the cycle evidence universe.

    The evidence step records the exchange symbol id, timeframe, and requested row count it
    expected.  Every downstream deterministic stage revalidates that immutable audit instead of
    trusting a mutable ``all_fresh`` summary flag.
    """
    audit = meta.get("candle_data")
    expected = meta.get("expected_candle_requests")
    if not isinstance(audit, dict) or not isinstance(expected, list) or not expected:
        raise CandleProxyError("cycle meta lacks the complete expected candle audit")
    if audit.get("source") != "binance-proxy" or audit.get("all_fresh") is not True:
        raise CandleProxyError("cycle candle audit is not a fresh binance-proxy audit")
    requests = audit.get("requests")
    if not isinstance(requests, list) or int(audit.get("request_count", -1)) != len(requests):
        raise CandleProxyError("cycle candle audit request count is inconsistent")

    evidence_symbols = {str(row.get("symbol")) for row in evidence if row.get("symbol")}
    expected_evidence = set(meta.get("symbols") or []) | {str(meta.get("btc_symbol") or "")}
    expected_evidence.discard("")
    if evidence_symbols != expected_evidence:
        raise CandleProxyError(
            "cycle evidence symbols do not match the meta candle universe: "
            f"evidence={sorted(evidence_symbols)}, expected={sorted(expected_evidence)}"
        )

    def request_key(row: dict) -> tuple[str, str]:
        return str(row.get("symbol") or ""), str(row.get("timeframe") or "")

    audited_unified_symbols = evidence_symbols | set(meta.get("scoring_symbols") or [])
    expected_by_key: dict[tuple[str, str], dict] = {}
    for row in expected:
        if not isinstance(row, dict):
            raise CandleProxyError("expected candle request is not an object")
        key = request_key(row)
        if not all(key) or key in expected_by_key:
            raise CandleProxyError(f"invalid or duplicate expected candle request {key}")
        if row.get("unified_symbol") not in audited_unified_symbols:
            raise CandleProxyError(f"expected candle request has unknown symbol {row!r}")
        timeframe = key[1]
        if timeframe not in _INTERVAL_MS:
            raise CandleProxyError(f"unsupported audited candle interval {timeframe}")
        expected_by_key[key] = row

    observed_by_key: dict[tuple[str, str], dict] = {}
    for row in requests:
        if not isinstance(row, dict):
            raise CandleProxyError("observed candle request is not an object")
        key = request_key(row)
        if not all(key) or key in observed_by_key:
            raise CandleProxyError(f"invalid or duplicate observed candle request {key}")
        observed_by_key[key] = row
    if set(observed_by_key) != set(expected_by_key):
        raise CandleProxyError(
            "cycle candle audit coverage mismatch: "
            f"observed={sorted(observed_by_key)}, expected={sorted(expected_by_key)}"
        )

    for key, expected_row in expected_by_key.items():
        row = observed_by_key[key]
        requested_limit = int(expected_row.get("requested_limit", -1))
        if (
            requested_limit <= 0
            or int(row.get("requested_limit", -1)) != requested_limit
            or int(row.get("rows", -1)) != requested_limit
            or row.get("current_candle_present") is not True
            or row.get("range_complete") is not True
        ):
            raise CandleProxyError(f"incomplete candle audit for {key}")
        try:
            latest_open = datetime.fromisoformat(str(row["latest_open_ts"]))
            latest_close = datetime.fromisoformat(str(row["latest_close_ts"]))
            checked_at = datetime.fromisoformat(str(row["checked_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CandleProxyError(f"invalid candle audit timestamps for {key}") from exc
        interval_ms = _INTERVAL_MS[key[1]]
        open_ms = int(latest_open.timestamp() * 1000)
        close_ms = int(latest_close.timestamp() * 1000)
        checked_ms = int(checked_at.timestamp() * 1000)
        expected_open = checked_ms // interval_ms * interval_ms
        if open_ms != expected_open or close_ms != open_ms + interval_ms - 1:
            raise CandleProxyError(f"candle audit boundary mismatch for {key}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BinanceCandleProxy:
    """Synchronous USD-M klines client with a strict current-candle contract."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 15.0,
        now_fn: Callable[[], datetime] = _utc_now,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("Binance candle proxy URL must be HTTP(S)")
        self.base_url = normalized
        self._now_fn = now_fn
        self._client = httpx.Client(
            base_url=normalized,
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        )
        self._audits: list[dict[str, Any]] = []

    @classmethod
    def from_settings(cls, settings) -> BinanceCandleProxy:
        return cls(
            settings.data.binance_klines_proxy_url,
            timeout_seconds=settings.data.candle_proxy_timeout_seconds,
        )

    def healthcheck(self) -> dict[str, Any]:
        try:
            response = self._client.get("/healthz")
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - convert transport/JSON failures to one contract
            raise CandleProxyError(f"Binance candle proxy health check failed: {exc}") from exc
        if not isinstance(body, dict) or body.get("status") != "ok":
            raise CandleProxyError(f"Binance candle proxy is unhealthy: {body!r}")
        return body

    def _now_ms(self) -> int:
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return int(now.astimezone(UTC).timestamp() * 1000)

    def _fetch_klines(self, symbol_id: str, timeframe: str, limit: int) -> list[list[Any]]:
        """Fetch full Binance kline rows and prove the final row is current.

        A request crossing a candle boundary is retried once against the new boundary.  There is
        deliberately no direct-Binance fallback: proxy unavailability or stale data aborts the
        evidence step before any GPT agent sees the cycle.
        """
        try:
            interval_ms = _INTERVAL_MS[timeframe]
        except KeyError:
            raise CandleProxyError(f"unsupported fixed candle interval: {timeframe}") from None
        if not 1 <= int(limit) <= 1000:
            raise CandleProxyError(f"invalid candle limit {limit}; expected 1..1000")

        last_problem = "unknown freshness failure"
        for _attempt in range(2):
            request_ms = self._now_ms()
            request_boundary = (request_ms // interval_ms) * interval_ms
            start_time = request_boundary - (int(limit) - 1) * interval_ms
            try:
                response = self._client.get(
                    "/fapi/v1/klines",
                    params={
                        "symbol": symbol_id,
                        "interval": timeframe,
                        "startTime": start_time,
                        "limit": int(limit),
                    },
                )
                response.raise_for_status()
                raw = response.json()
            except Exception as exc:  # noqa: BLE001 - one fail-closed proxy contract
                raise CandleProxyError(
                    f"proxy candle request failed for {symbol_id} {timeframe}: {exc}"
                ) from exc

            checked_ms = self._now_ms()
            expected_boundary = (checked_ms // interval_ms) * interval_ms
            if not isinstance(raw, list) or not raw:
                raise CandleProxyError(f"proxy returned no candles for {symbol_id} {timeframe}")
            if any(not isinstance(row, list) or len(row) < 7 for row in raw):
                raise CandleProxyError(
                    f"proxy returned malformed candles for {symbol_id} {timeframe}"
                )
            for row in raw:
                try:
                    open_price, high, low, close, volume = (
                        float(row[1]),
                        float(row[2]),
                        float(row[3]),
                        float(row[4]),
                        float(row[5]),
                    )
                except (TypeError, ValueError) as exc:
                    raise CandleProxyError(
                        f"proxy returned non-numeric OHLCV for {symbol_id} {timeframe}"
                    ) from exc
                values = (open_price, high, low, close, volume)
                if (
                    not all(math.isfinite(value) for value in values)
                    or min(open_price, high, low, close) <= 0.0
                    or volume < 0.0
                    or high < max(open_price, close, low)
                    or low > min(open_price, close, high)
                ):
                    raise CandleProxyError(
                        f"proxy returned invalid OHLCV values for {symbol_id} {timeframe}"
                    )
            rows = sorted(raw, key=lambda row: int(row[0]))
            opens = [int(row[0]) for row in rows]
            if len(opens) != len(set(opens)):
                raise CandleProxyError(
                    f"proxy returned duplicate candles for {symbol_id} {timeframe}"
                )
            latest_open = opens[-1]
            latest_close = int(rows[-1][6])
            if latest_open != expected_boundary:
                last_problem = (
                    f"latest_open={latest_open}, expected_current_open={expected_boundary}"
                )
                # The request may have started immediately before a UTC boundary. Retry once with
                # the newly-computed range instead of accepting the just-closed candle as current.
                continue
            if latest_close < checked_ms:
                last_problem = f"latest_close={latest_close} precedes checked_at={checked_ms}"
                continue

            expected_start = expected_boundary - (int(limit) - 1) * interval_ms
            expected_opens = list(
                range(
                    expected_start,
                    expected_boundary + interval_ms,
                    interval_ms,
                )
            )
            if opens != expected_opens:
                raise CandleProxyError(
                    f"proxy returned incomplete/non-contiguous candles for {symbol_id} "
                    f"{timeframe}: rows={len(opens)}, expected={int(limit)}, "
                    f"first={opens[0]}, expected_first={expected_start}"
                )
            bad_closes = [
                (int(row[0]), int(row[6]))
                for row in rows
                if int(row[6]) != int(row[0]) + interval_ms - 1
            ]
            if bad_closes:
                raise CandleProxyError(
                    f"proxy returned malformed candle boundaries for {symbol_id} {timeframe}"
                )

            checked_at = datetime.fromtimestamp(checked_ms / 1000.0, tz=UTC)
            self._audits.append(
                {
                    "symbol": symbol_id,
                    "timeframe": timeframe,
                    "requested_limit": int(limit),
                    "rows": len(rows),
                    "latest_open_ts": datetime.fromtimestamp(
                        latest_open / 1000.0, tz=UTC
                    ).isoformat(),
                    "latest_close_ts": datetime.fromtimestamp(
                        latest_close / 1000.0, tz=UTC
                    ).isoformat(),
                    "checked_at": checked_at.isoformat(),
                    "current_candle_present": True,
                    "range_complete": True,
                }
            )
            return rows

        raise StaleCandleData(f"stale proxy candles for {symbol_id} {timeframe}: {last_problem}")

    def fetch_ohlcv(self, symbol_id: str, timeframe: str, limit: int) -> list[list[Any]]:
        """Return the six OHLCV fields consumed by the existing evidence pipeline."""
        return [row[:6] for row in self._fetch_klines(symbol_id, timeframe, limit)]

    def fetch_klines(self, symbol_id: str, timeframe: str, limit: int) -> list[list[Any]]:
        """Return complete Binance kline rows for deterministic quote-volume ranking.

        The weekly cross-sectional selector needs field 7 (quote-asset volume), which is more
        accurate than approximating turnover as base volume times a closing price.  The same
        freshness, contiguity, and proxy-only contract as :meth:`fetch_ohlcv` applies.
        """
        rows = self._fetch_klines(symbol_id, timeframe, limit)
        for row in rows:
            if len(row) < 8:
                raise CandleProxyError(
                    f"proxy kline lacks quote volume for {symbol_id} {timeframe}"
                )
            try:
                quote_volume = float(row[7])
            except (TypeError, ValueError) as exc:
                raise CandleProxyError(
                    f"proxy kline has non-numeric quote volume for {symbol_id} {timeframe}"
                ) from exc
            if not math.isfinite(quote_volume) or quote_volume < 0.0:
                raise CandleProxyError(
                    f"proxy kline has invalid quote volume for {symbol_id} {timeframe}"
                )
        return rows

    def audit_snapshot(self) -> dict[str, Any]:
        return {
            "source": "binance-proxy",
            "base_url": self.base_url,
            "all_fresh": bool(self._audits)
            and all(
                row["current_candle_present"] and row["range_complete"] for row in self._audits
            ),
            "request_count": len(self._audits),
            "requests": list(self._audits),
        }
