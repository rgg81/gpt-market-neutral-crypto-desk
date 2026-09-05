"""Exact historical funding-boundary collection for PAPER settlement."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.costs import funding_boundary_hours


class FundingHistoryError(ValueError):
    """Historical funding data cannot prove complete boundary coverage."""


# Binance occasionally publishes an otherwise valid settlement a few milliseconds after its
# nominal funding boundary (for example 16:00:00.003). Coverage proof may tolerate that exchange
# timestamp lag, but the raw event timestamp is retained for settlement/audit and no two events may
# claim the same nominal boundary.
MAX_BOUNDARY_SETTLEMENT_LAG = timedelta(seconds=1)


def resolve_previous_intervals(state_dir, account) -> dict[str, int]:
    """Resolve prior intervals from account state or an exact legacy heartbeat audit.

    The live account predates ``funding_intervals_observed``. A bootstrap is accepted only when a
    PAPER heartbeat at the exact account funding clock covers every currently held symbol and its
    side/quantity. Anything less would turn current metadata into invented history.
    """
    held = set(account.positions)
    observed = {
        symbol: int(interval)
        for symbol, interval in account.funding_intervals_observed.items()
        if symbol in held
    }
    if set(observed) == held:
        return observed
    if not held or account.last_funding_ts is None:
        return observed

    path = Path(state_dir) / "portfolio-heartbeats.jsonl"
    clock = _utc(account.last_funding_ts)
    rows = path.read_text().splitlines() if path.exists() else []
    for line in reversed(rows):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            timestamp = _utc(datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            timestamp != clock
            or row.get("kind") != "token_free_funding_heartbeat"
            or row.get("paper_only") is not True
            or row.get("actions") != "none"
        ):
            continue
        positions = row.get("positions")
        if not isinstance(positions, list):
            continue
        by_symbol = {str(item.get("symbol")): item for item in positions if isinstance(item, dict)}
        if len(by_symbol) != len(positions) or set(by_symbol) != held:
            continue
        candidate: dict[str, int] = {}
        valid = True
        for symbol, position in account.positions.items():
            item = by_symbol[symbol]
            try:
                interval_raw = float(item["funding_interval_h"])
                interval = int(interval_raw)
                qty = float(item["qty"])
            except (KeyError, TypeError, ValueError):
                valid = False
                break
            if (
                interval_raw != interval
                or interval not in {1, 2, 4, 8}
                or item.get("side") != position.direction
                or not math.isclose(qty, position.qty, rel_tol=1e-12, abs_tol=1e-12)
            ):
                valid = False
                break
            candidate[symbol] = interval
        if valid:
            conflicts = {
                symbol: {"persisted": observed[symbol], "heartbeat": candidate[symbol]}
                for symbol in set(observed) & set(candidate)
                if observed[symbol] != candidate[symbol]
            }
            if conflicts:
                raise FundingHistoryError(
                    f"persisted funding intervals conflict with exact heartbeat audit: {conflicts}"
                )
            return candidate
    missing = sorted(held - set(observed))
    raise FundingHistoryError(
        "prior funding intervals are unproven for held symbols and no exact heartbeat audit "
        f"matches the account clock: {missing}"
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def expected_funding_timestamps(
    previous_ts: datetime,
    now: datetime,
    interval_hours: int,
) -> list[datetime]:
    """Return exchange funding boundaries in ``(previous_ts, now]``."""
    previous = _utc(previous_ts)
    end = _utc(now)
    interval = int(interval_hours)
    # Binance USD-M adaptive funding currently uses schedules whose boundaries are nested inside
    # the standard 8h clock. Refuse an unknown shape rather than making a false completeness claim.
    if interval not in {1, 2, 4, 8}:
        raise FundingHistoryError(f"unsupported funding interval: {interval_hours}")
    hours = set(funding_boundary_hours(interval))
    cursor = previous.replace(minute=0, second=0, microsecond=0)
    if cursor <= previous:
        cursor += timedelta(hours=1)
    out: list[datetime] = []
    while cursor <= end:
        if cursor.hour in hours:
            out.append(cursor)
        cursor += timedelta(hours=1)
    return out


def _nominal_boundary(symbol: str, timestamp: datetime) -> datetime:
    boundary = timestamp.replace(minute=0, second=0, microsecond=0)
    if not boundary <= timestamp <= boundary + MAX_BOUNDARY_SETTLEMENT_LAG:
        raise FundingHistoryError(
            f"{symbol}: funding event {timestamp.isoformat()} is not a nominal boundary "
            f"within {MAX_BOUNDARY_SETTLEMENT_LAG.total_seconds():g}s lag"
        )
    return boundary


def _interval_coverage_proof(
    symbol: str,
    *,
    previous: datetime,
    end: datetime,
    prior_interval: int,
    current_interval: int,
    observed: set[datetime],
) -> dict:
    """Prove one stable schedule or one old-to-new transition from exact event boundaries.

    Binance exposes the current interval but not its effective-time history. When the stored and
    current intervals differ, event timestamps can still prove settlement completeness: enumerate
    every possible first boundary governed by the new schedule and accept only if the observed set
    exactly equals old-schedule boundaries before that cut plus new-schedule boundaries from it.
    Multiple equivalent cut points are retained as an explicit uncertainty set; no rate or event is
    inferred. A second transition, missing common boundary, or unexplained extra event cannot match.
    """
    old_boundaries = set(
        expected_funding_timestamps(previous, end, prior_interval)
    )
    new_boundaries = set(
        expected_funding_timestamps(previous, end, current_interval)
    )
    observed_rendered = [value.isoformat() for value in sorted(observed)]
    base = {
        "schema_version": 1,
        "window_start": previous.isoformat(),
        "window_end": end.isoformat(),
        "prior_interval_h": prior_interval,
        "current_interval_h": current_interval,
        "observed_nominal_boundaries": observed_rendered,
    }
    if prior_interval == current_interval:
        if observed != old_boundaries:
            missing = [value.isoformat() for value in sorted(old_boundaries - observed)]
            unexpected = [value.isoformat() for value in sorted(observed - old_boundaries)]
            raise FundingHistoryError(
                f"{symbol}: stable {prior_interval}h funding schedule does not exactly match "
                f"history; missing boundaries={missing} unexpected boundaries={unexpected}"
            )
        return {
            **base,
            "kind": "stable",
            "expected_nominal_boundaries": [
                value.isoformat() for value in sorted(old_boundaries)
            ],
            "candidate_first_new_boundaries": [],
        }

    union = sorted(old_boundaries | new_boundaries)
    # ``None`` means the interval changed after the last funding boundary but before ``end``;
    # current metadata is new while every settlement in the window truthfully used the old rule.
    candidate_cuts: list[datetime | None] = [*union]
    if not union or end > union[-1]:
        candidate_cuts.append(None)
    matching_cuts: list[datetime | None] = []
    for cut in candidate_cuts:
        expected = (
            old_boundaries
            if cut is None
            else {boundary for boundary in old_boundaries if boundary < cut}
            | {boundary for boundary in new_boundaries if boundary >= cut}
        )
        if observed == expected:
            matching_cuts.append(cut)
    if not matching_cuts:
        all_possible = old_boundaries | new_boundaries
        unexpected = [value.isoformat() for value in sorted(observed - all_possible)]
        raise FundingHistoryError(
            f"{symbol}: cannot prove a single {prior_interval}h->{current_interval}h interval "
            "transition from exact nominal boundaries; "
            f"observed={observed_rendered} unexpected={unexpected}"
        )
    return {
        **base,
        "kind": "single_transition",
        "candidate_first_new_boundaries": [
            cut.isoformat() if cut is not None else "after_last_boundary_before_window_end"
            for cut in matching_cuts
        ],
    }


def collect_funding_events(
    exchange,
    symbols: list[str] | set[str] | tuple[str, ...],
    *,
    previous_ts: datetime,
    now: datetime,
    intervals: dict[str, int],
    previous_intervals: dict[str, int] | None = None,
    proof_out: dict[str, dict] | None = None,
) -> dict[str, list[dict]]:
    """Fetch and prove complete, per-boundary rate+mark history for held symbols.

    Every returned settlement in the clock window is kept. The collector paginates until the
    exchange feed is exhausted. A changed interval is accepted only when the exact observed
    nominal-boundary set proves at most one old-to-new transition. ``proof_out`` receives the
    auditable stable/transition proof only after every requested symbol validates. Applying the
    newest observed rate to older missed events is never an allowed fallback.
    """
    previous = _utc(previous_ts)
    end = _utc(now)
    if end < previous:
        raise FundingHistoryError("funding history end precedes the account funding clock")
    out: dict[str, list[dict]] = {}
    proofs: dict[str, dict] = {}
    for symbol in sorted(set(symbols)):
        if symbol not in intervals:
            raise FundingHistoryError(f"{symbol}: current funding interval is unproven")
        interval = int(intervals[symbol])
        expected_funding_timestamps(previous, end, interval)
        if previous_intervals is None or symbol not in previous_intervals:
            raise FundingHistoryError(f"{symbol}: prior funding interval is unproven")
        prior_interval = int(previous_intervals[symbol])
        expected_funding_timestamps(previous, end, prior_interval)  # validate prior metadata
        page_limit = 1000
        since_ms = int(previous.timestamp() * 1000) + 1
        raw: list[dict] = []
        while True:
            page = exchange.funding_history(symbol, since_ms=since_ms, limit=page_limit)
            if not page:
                break
            raw.extend(page)
            page_timestamps = []
            for item in page:
                timestamp = item.get("timestamp")
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp)
                if not isinstance(timestamp, datetime):
                    raise FundingHistoryError(f"{symbol}: funding event lacks a timestamp")
                page_timestamps.append(_utc(timestamp))
            next_since = int(max(page_timestamps).timestamp() * 1000) + 1
            if len(page) < page_limit:
                break
            if next_since <= since_ms:
                raise FundingHistoryError(f"{symbol}: funding history pagination did not advance")
            since_ms = next_since

        by_timestamp: dict[datetime, dict] = {}
        for item in raw:
            timestamp = item.get("timestamp")
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if not isinstance(timestamp, datetime):
                raise FundingHistoryError(f"{symbol}: funding event lacks a timestamp")
            timestamp = _utc(timestamp)
            if previous < timestamp <= end:
                if timestamp in by_timestamp:
                    raise FundingHistoryError(
                        f"{symbol}: duplicate funding event timestamp {timestamp.isoformat()}"
                    )
                rate = float(item["rate"])
                mark = float(item["mark"])
                if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0.0:
                    raise FundingHistoryError(
                        f"{symbol}: invalid funding rate/mark at {timestamp.isoformat()}"
                    )
                by_timestamp[timestamp] = {
                    "timestamp": timestamp,
                    "rate": rate,
                    "mark": mark,
                }

        by_boundary: dict[datetime, datetime] = {}
        for timestamp in by_timestamp:
            boundary = _nominal_boundary(symbol, timestamp)
            if boundary in by_boundary:
                rendered = sorted((by_boundary[boundary], timestamp))
                raise FundingHistoryError(
                    f"{symbol}: multiple funding events map to nominal boundary "
                    f"{boundary.isoformat()}: {[value.isoformat() for value in rendered]}"
                )
            by_boundary[boundary] = timestamp
        proofs[symbol] = _interval_coverage_proof(
            symbol,
            previous=previous,
            end=end,
            prior_interval=prior_interval,
            current_interval=interval,
            observed=set(by_boundary),
        )
        out[symbol] = [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]
    if proof_out is not None:
        proof_out.clear()
        proof_out.update(proofs)
    return out


def verify_execution_window_funding_safety(
    execution_ts_by_symbol: dict[str, datetime],
    symbols: set[str] | list[str] | tuple[str, ...],
    *,
    current_intervals: dict[str, int],
    decision_intervals: dict[str, int | float],
    held_interval_proofs: dict[str, dict],
) -> dict:
    """Prove an atomic PAPER basket did not observe legs across a funding boundary.

    Reconciliation settles accrued funding before applying the basket. If sequential leg
    observations straddle a settlement, that ordering would charge a pre-boundary drop after it
    closed or omit funding on a pre-boundary entry. We do not reorder or invent intra-basket state:
    exact historical boundaries govern held symbols. A new symbol has no settlement history, so
    the union of its decision-evidence and execution-time schedules governs: a metadata transition
    cannot erase a possible boundary between two observations. Any boundary in
    ``[first_observation, last_observation]`` halts; an observation at the nominal instant is
    ordering-ambiguous and therefore unsafe.
    """
    expected_symbols = set(symbols)
    if set(execution_ts_by_symbol) != expected_symbols:
        missing = sorted(expected_symbols - set(execution_ts_by_symbol))
        extra = sorted(set(execution_ts_by_symbol) - expected_symbols)
        raise FundingHistoryError(
            f"execution timestamp coverage mismatch: missing={missing} extra={extra}"
        )
    if not expected_symbols:
        return {
            "schema_version": 1,
            "safe": True,
            "window_start": None,
            "window_end": None,
            "symbols": {},
        }
    timestamps: dict[str, datetime] = {}
    for symbol, timestamp in execution_ts_by_symbol.items():
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise FundingHistoryError(
                f"{symbol}: execution observation timestamp must be timezone-aware"
            )
        timestamps[symbol] = _utc(timestamp)
    window_start = min(timestamps.values())
    window_end = max(timestamps.values())
    audit: dict[str, dict] = {}

    for symbol in sorted(expected_symbols):
        if symbol not in current_intervals:
            raise FundingHistoryError(
                f"{symbol}: current interval missing from execution-window proof"
            )
        if symbol not in decision_intervals:
            raise FundingHistoryError(
                f"{symbol}: decision interval missing from execution-window proof"
            )
        current_raw = float(current_intervals[symbol])
        decision_raw = float(decision_intervals[symbol])
        if (
            not math.isfinite(current_raw)
            or not current_raw.is_integer()
            or int(current_raw) not in {1, 2, 4, 8}
            or not math.isfinite(decision_raw)
            or not decision_raw.is_integer()
            or int(decision_raw) not in {1, 2, 4, 8}
        ):
            raise FundingHistoryError(
                f"{symbol}: decision/current funding interval is invalid"
            )
        interval = int(current_raw)
        decision_interval = int(decision_raw)
        expected_funding_timestamps(window_start, window_end, interval)
        proof = held_interval_proofs.get(symbol)
        if proof is not None:
            if not isinstance(proof, dict):
                raise FundingHistoryError(f"{symbol}: held interval proof is malformed")
            try:
                proof_start = _utc(datetime.fromisoformat(str(proof["window_start"])))
                proof_end = _utc(datetime.fromisoformat(str(proof["window_end"])))
                proof_current = int(proof["current_interval_h"])
                raw_boundaries = proof["observed_nominal_boundaries"]
            except (KeyError, TypeError, ValueError) as exc:
                raise FundingHistoryError(
                    f"{symbol}: held interval proof lacks exact window/schedule fields"
                ) from exc
            if (
                proof_start > window_start
                or proof_end < window_end
                or proof_current != interval
                or not isinstance(raw_boundaries, list)
            ):
                raise FundingHistoryError(
                    f"{symbol}: held interval proof does not cover the execution window"
                )
            try:
                boundaries = sorted(
                    {_utc(datetime.fromisoformat(str(value))) for value in raw_boundaries}
                )
            except (TypeError, ValueError) as exc:
                raise FundingHistoryError(
                    f"{symbol}: held interval proof contains an invalid boundary"
                ) from exc
            source = "exact_historical_coverage"
        else:
            boundaries = sorted(
                {
                    boundary
                    for candidate_interval in {decision_interval, interval}
                    for boundary in expected_funding_timestamps(
                        window_start - timedelta(microseconds=1),
                        window_end,
                        candidate_interval,
                    )
                }
            )
            source = "decision_and_execution_interval_union"

        straddled = [
            boundary for boundary in boundaries if window_start <= boundary <= window_end
        ]
        audit[symbol] = {
            "source": source,
            "decision_interval_h": decision_interval,
            "current_interval_h": interval,
            "applicable_boundaries_in_window": [
                boundary.isoformat() for boundary in straddled
            ],
        }
        if straddled:
            raise FundingHistoryError(
                f"atomic execution observations straddle funding boundary for {symbol}: "
                f"window=[{window_start.isoformat()}, {window_end.isoformat()}], "
                f"boundaries={[boundary.isoformat() for boundary in straddled]}"
            )

    return {
        "schema_version": 1,
        "safe": True,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "symbols": audit,
    }
