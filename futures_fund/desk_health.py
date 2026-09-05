"""Read-only, token-free operational health and SLO report for the PAPER desk."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.config import load_settings
from futures_fund.durable_io import canonical_json_sha256
from futures_fund.heartbeat import verify_heartbeat_completion
from futures_fund.proxy_process import probe_binance_proxy
from futures_fund.reconcile_commit import cycle_is_complete
from futures_fund.state_transaction import (
    StateSnapshotChanged,
    account_event_chain_established,
    load_account_events,
    shared_state_transaction_lock,
)


def _utc(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _age_hours(now: datetime, value: object | None) -> float | None:
    if value is None:
        return None
    return (now - _utc(value)).total_seconds() / 3600.0


def _json(path: Path) -> object:
    return json.loads(
        path.read_text(),
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"non-finite JSON constant {value}")
        ),
    )


def _cycle_complete(state: Path, cycle: int) -> bool:
    """Treat malformed/non-finite completion state as invalid, never as a health crash."""
    try:
        return cycle_is_complete(state, cycle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _current_proxy_probe() -> dict:
    """Probe current HTTP/process/listener identity using this checkout's exact config."""
    root = Path(__file__).resolve().parents[1]
    return probe_binance_proxy(load_settings(root / "config.yaml"))


def _deduplicated_jsonl(path: Path, identity: Callable[[dict], object]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    raw = exact_duplicates = conflicts = 0
    by_identity: dict[object, dict] = {}
    if path.exists():
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            raw += 1
            try:
                row = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-finite JSON constant {value}")
                    ),
                )
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"malformed JSONL row {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"non-object JSONL row {path}:{line_number}")
            key = identity(row)
            if key in by_identity:
                if by_identity[key] == row:
                    exact_duplicates += 1
                else:
                    conflicts += 1
                continue
            by_identity[key] = row
            rows.append(row)
    return rows, {
        "path": str(path),
        "raw_rows": raw,
        "unique_rows": len(rows),
        "exact_duplicates": exact_duplicates,
        "conflicts": conflicts,
    }


def _slo(age: float | None, caution: float, critical: float) -> str:
    if age is None:
        return "UNKNOWN"
    if age < 0.0:
        return "CRITICAL"
    if age > critical:
        return "CRITICAL"
    if age > caution:
        return "CAUTION"
    return "HEALTHY"


def _latest_cycle(state: Path) -> tuple[int | None, dict | None, list[int]]:
    root = state / "rebal" / "cycle"
    numbers = (
        sorted(int(path.name) for path in root.iterdir() if path.is_dir() and path.name.isdigit())
        if root.exists()
        else []
    )
    valid = [cycle for cycle in numbers if _cycle_complete(state, cycle)]
    if not valid:
        return None, None, numbers
    cycle = valid[-1]
    directory = root / str(cycle)
    marker_path = directory / "complete.json"
    marker = _json(marker_path) if marker_path.exists() else _json(directory / "report.json")
    return cycle, marker if isinstance(marker, dict) else None, numbers


def _cycle_timestamp(state: Path, cycle: int, marker: dict) -> str | None:
    directory = state / "rebal" / "cycle" / str(cycle)
    report = _json(directory / "report.json") if (directory / "report.json").exists() else {}
    return (
        marker.get("completed_at")
        or (report.get("ran_at") if isinstance(report, dict) else None)
        or (report.get("decision_ts") if isinstance(report, dict) else None)
    )


def _cycle_account_snapshot(state: Path, cycle: int, marker: dict) -> tuple[dict | None, str]:
    manifest = marker.get("manifest") if isinstance(marker, dict) else None
    version = int(manifest.get("version", 0)) if isinstance(manifest, dict) else 0
    if version < 2:
        return None, "legacy_unbound"
    path = state / "rebal" / "cycle" / str(cycle) / "account_state.json"
    try:
        value = _json(path)
    except (OSError, json.JSONDecodeError):
        return None, "missing_or_malformed"
    return (value if isinstance(value, dict) else None), f"manifest_v{version}"


def _build_health_report_unlocked(
    state_dir: str | Path = "live_state",
    log_dir: str | Path = "logs",
    *,
    now: datetime | None = None,
) -> dict:
    """Build one report while the caller holds the shared state-transaction lock."""
    state = Path(state_dir)
    logs = Path(log_dir)
    now = (now or datetime.now(UTC)).astimezone(UTC)
    issues: list[dict] = []

    try:
        ledger, ledger_stats = _deduplicated_jsonl(
            state / "ledger.jsonl", lambda row: int(row["cycle"])
        )
        equity, equity_stats = _deduplicated_jsonl(
            state / "equity-history.jsonl", lambda row: int(row["cycle"])
        )
        heartbeats, heartbeat_stats = _deduplicated_jsonl(
            state / "portfolio-heartbeats.jsonl",
            lambda row: (row.get("schedule_slot") or row["ts"], row["kind"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        heartbeats = []
        ledger_stats = equity_stats = heartbeat_stats = {"conflicts": 1}
        issues.append({"severity": "CRITICAL", "code": "MALFORMED_STATE", "detail": str(exc)})

    for label, stats in (
        ("ledger", ledger_stats),
        ("equity", equity_stats),
        ("heartbeat", heartbeat_stats),
    ):
        if stats.get("conflicts"):
            issues.append(
                {
                    "severity": "CRITICAL",
                    "code": f"{label.upper()}_IDENTITY_CONFLICT",
                    "detail": f"{stats['conflicts']} conflicting duplicate identities",
                }
            )
        if stats.get("exact_duplicates"):
            issues.append(
                {
                    "severity": "CRITICAL",
                    "code": f"{label.upper()}_EXACT_DUPLICATES",
                    "detail": f"{stats['exact_duplicates']} duplicate append-only rows",
                }
            )

    latest_cycle, cycle_marker, directory_numbers = _latest_cycle(state)
    invalid_published_cycles = []
    for cycle in directory_numbers:
        marker_path = state / "rebal" / "cycle" / str(cycle) / "complete.json"
        if marker_path.exists() and not _cycle_complete(state, cycle):
            invalid_published_cycles.append(cycle)
    if invalid_published_cycles:
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "CYCLE_MANIFEST_INVALID",
                "detail": invalid_published_cycles,
            }
        )
    cycle_ts = (
        _cycle_timestamp(state, latest_cycle, cycle_marker)
        if latest_cycle is not None and cycle_marker is not None
        else None
    )
    cycle_age = _age_hours(now, cycle_ts)
    cycle_status = _slo(cycle_age, 30.0, 48.0)
    if cycle_status in {"UNKNOWN", "CRITICAL"}:
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "CYCLE_STALE_OR_MISSING",
                "detail": f"latest completed cycle age hours={cycle_age}",
            }
        )
    elif cycle_status == "CAUTION":
        issues.append({"severity": "CAUTION", "code": "CYCLE_AGE", "detail": cycle_age})
    if directory_numbers and latest_cycle != directory_numbers[-1]:
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "LATEST_CYCLE_INCOMPLETE",
                "detail": f"directory={directory_numbers[-1]} completed={latest_cycle}",
            }
        )

    official = [row for row in heartbeats if row.get("kind") == "token_free_funding_heartbeat"]
    official.sort(key=lambda row: str(row.get("ts", "")))
    heartbeat_integrity = True
    prior: dict | None = None
    for row in official:
        if int(row.get("heartbeat_schema_version", 0)) >= 2:
            expected = canonical_json_sha256(prior) if prior is not None else None
            if row.get("previous_heartbeat_sha256") != expected or not verify_heartbeat_completion(
                state, row
            ):
                heartbeat_integrity = False
        prior = row
    if not heartbeat_integrity:
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "HEARTBEAT_CHAIN_INVALID",
                "detail": "a v2 row or completion generation failed verification",
            }
        )
    latest_heartbeat = official[-1] if official else None
    invalid_equity: list[dict] = []
    if latest_cycle is not None:
        report_path = state / "rebal" / "cycle" / str(latest_cycle) / "report.json"
        try:
            report = _json(report_path)
            cycle_equity = float(report["equity"])
            if not math.isfinite(cycle_equity) or cycle_equity <= 0.0:
                raise ValueError(f"non-positive/non-finite equity={cycle_equity}")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid_equity.append({"source": f"cycle:{latest_cycle}", "detail": str(exc)})
    if latest_heartbeat is not None:
        try:
            heartbeat_equity = float(latest_heartbeat["equity"])
            if not math.isfinite(heartbeat_equity) or heartbeat_equity <= 0.0:
                raise ValueError(f"non-positive/non-finite equity={heartbeat_equity}")
        except (KeyError, TypeError, ValueError) as exc:
            invalid_equity.append({"source": "heartbeat", "detail": str(exc)})
    if invalid_equity:
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "PORTFOLIO_EQUITY_INVALID",
                "detail": invalid_equity,
            }
        )
    settlement_observations = [
        (timestamp, source)
        for timestamp, source in (
            (cycle_ts, "full_cycle"),
            (latest_heartbeat.get("ts") if latest_heartbeat else None, "heartbeat"),
        )
        if timestamp is not None
    ]
    latest_settlement = (
        max(settlement_observations, key=lambda item: _utc(item[0]))
        if settlement_observations
        else None
    )
    heartbeat_age = _age_hours(now, latest_settlement[0] if latest_settlement else None)
    settlement_source = latest_settlement[1] if latest_settlement else None
    heartbeat_status = _slo(heartbeat_age, 12.0, 24.0)
    if heartbeat_status == "CRITICAL":
        issues.append({"severity": "CRITICAL", "code": "HEARTBEAT_STALE", "detail": heartbeat_age})
    elif heartbeat_status in {"UNKNOWN", "CAUTION"}:
        issues.append({"severity": "CAUTION", "code": "HEARTBEAT_AGE", "detail": heartbeat_age})

    try:
        account = _json(state / "account.json")
        if not isinstance(account, dict):
            raise ValueError("account is not a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        account = {}
        issues.append({"severity": "CRITICAL", "code": "ACCOUNT_INVALID", "detail": str(exc)})

    try:
        account_events = load_account_events(state)
        account_event_established = account_event_chain_established(state)
        account_event_chain_valid: bool | None = bool(account_events)
        if account_event_established and not account_events:
            raise ValueError(
                "durable generations exist but account-events.jsonl is absent or empty"
            )
        if account_events and canonical_json_sha256(account) != account_events[-1].get(
            "account_state_sha256"
        ):
            raise ValueError("root account does not match the unified account-event head")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        account_events = []
        account_event_chain_valid = False
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "ACCOUNT_EVENT_CHAIN_INVALID",
                "detail": str(exc),
            }
        )
    if account_event_chain_valid is False and not account_event_chain_established(state):
        account_event_chain_valid = None
        issues.append(
            {
                "severity": "CAUTION",
                "code": "ACCOUNT_EVENT_LINEAGE_LEGACY",
                "detail": "the next durable transaction will establish the unified chain",
            }
        )
    if account_events:
        referenced_event_ids: set[str] = set()
        for cycle in directory_numbers:
            marker_path = state / "rebal" / "cycle" / str(cycle) / "complete.json"
            try:
                marker = _json(marker_path)
            except (OSError, json.JSONDecodeError):
                continue
            manifest = marker.get("manifest") if isinstance(marker, dict) else None
            if (
                isinstance(marker, dict)
                and isinstance(manifest, dict)
                and int(manifest.get("version", 0)) >= 3
                and _cycle_complete(state, cycle)
                and isinstance(marker.get("account_event_id"), str)
            ):
                referenced_event_ids.add(marker["account_event_id"])
        referenced_event_ids.update(
            str(row["commit_id"])
            for row in official
            if int(row.get("heartbeat_schema_version", 0)) >= 3
            and isinstance(row.get("commit_id"), str)
            and verify_heartbeat_completion(state, row)
        )
        orphaned = sorted(
            event["event_id"]
            for event in account_events
            if event["event_id"] not in referenced_event_ids
        )
        if orphaned:
            account_event_chain_valid = False
            issues.append(
                {
                    "severity": "CRITICAL",
                    "code": "ACCOUNT_EVENT_ORPHANED",
                    "detail": orphaned,
                }
            )

    latest_state_snapshot: dict | None = None
    latest_state_source = "none"
    latest_state_ts: str | None = None
    if latest_cycle is not None and cycle_marker is not None:
        latest_state_snapshot, latest_state_source = _cycle_account_snapshot(
            state, latest_cycle, cycle_marker
        )
        latest_state_ts = cycle_ts
    if latest_heartbeat is not None and (
        latest_state_ts is None or _utc(latest_heartbeat["ts"]) > _utc(latest_state_ts)
    ):
        if int(latest_heartbeat.get("heartbeat_schema_version", 0)) >= 2:
            # The verifier already authenticated the path; locate its bound snapshot through API.
            from futures_fund.heartbeat import heartbeat_generation_dir

            snapshot_path = heartbeat_generation_dir(state, latest_heartbeat) / "account_state.json"
            value = _json(snapshot_path) if snapshot_path.exists() else None
            latest_state_snapshot = value if isinstance(value, dict) else None
            latest_state_source = (
                f"heartbeat_v{int(latest_heartbeat.get('heartbeat_schema_version', 0))}"
            )
        else:
            latest_state_snapshot = None
            latest_state_source = "legacy_heartbeat_unbound"
        latest_state_ts = str(latest_heartbeat["ts"])
    account_consistent = latest_state_snapshot is not None and canonical_json_sha256(
        account
    ) == canonical_json_sha256(latest_state_snapshot)
    if latest_state_snapshot is not None and not account_consistent:
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "ACCOUNT_MANIFEST_MISMATCH",
                "detail": f"root account differs from {latest_state_source}",
            }
        )
    elif latest_state_snapshot is None:
        issues.append(
            {
                "severity": "CAUTION",
                "code": "ACCOUNT_BINDING_LEGACY",
                "detail": latest_state_source,
            }
        )

    funding_age = _age_hours(now, account.get("last_funding_ts"))
    funding_status = _slo(funding_age, 12.0, 24.0)
    if funding_status == "CRITICAL":
        issues.append(
            {"severity": "CRITICAL", "code": "FUNDING_CLOCK_STALE", "detail": funding_age}
        )
    elif funding_status in {"UNKNOWN", "CAUTION"}:
        issues.append({"severity": "CAUTION", "code": "FUNDING_CLOCK_AGE", "detail": funding_age})

    observations: list[tuple[datetime, bool, str]] = []
    for cycle in directory_numbers:
        if not _cycle_complete(state, cycle):
            continue
        report_path = state / "rebal" / "cycle" / str(cycle) / "report.json"
        try:
            report = _json(report_path)
            observations.append(
                (
                    _utc(report.get("ran_at") or report["decision_ts"]),
                    int(report.get("n_legs", 0)) == 0,
                    f"cycle:{cycle}",
                )
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    for row in official:
        observations.append((_utc(row["ts"]), not bool(row.get("positions")), "heartbeat"))
    observations.sort(key=lambda item: item[0])
    is_flat = not bool(account.get("positions"))
    flat_since: datetime | None = None
    if is_flat:
        for ts, flat, _source in reversed(observations):
            if not flat:
                break
            flat_since = ts
    flat_age = (now - flat_since).total_seconds() / 3600.0 if flat_since else None
    flat_status = "NOT_FLAT" if not is_flat else _slo(flat_age, 24.0, 72.0)
    if flat_status == "CRITICAL":
        issues.append({"severity": "CRITICAL", "code": "FLAT_BOOK_PROLONGED", "detail": flat_age})
    elif flat_status in {"UNKNOWN", "CAUTION"}:
        issues.append({"severity": "CAUTION", "code": "FLAT_BOOK_DURATION", "detail": flat_age})

    try:
        proxy_current = _current_proxy_probe()
        proxy_current_healthy = (
            proxy_current.get("status") == "HEALTHY"
            and proxy_current.get("http_healthy") is True
            and proxy_current.get("identity_bound") is True
            and bool(proxy_current.get("listener_owner_pids"))
        )
    except Exception as exc:  # noqa: BLE001 - probe failure must become report data
        proxy_current = {"status": "UNHEALTHY", "error": f"{type(exc).__name__}: {exc}"}
        proxy_current_healthy = False
    proxy_current_status = "HEALTHY" if proxy_current_healthy else "CRITICAL"
    if not proxy_current_healthy:
        issues.append(
            {
                "severity": "CRITICAL",
                "code": "PROXY_CURRENT_UNHEALTHY",
                "detail": proxy_current,
            }
        )

    monitor_path = logs / "binance-proxy-monitor.json"
    try:
        monitor = _json(monitor_path)
        checked = datetime.fromtimestamp(float(monitor["checked_at"]), UTC)
        proxy_age = (now - checked).total_seconds() / 3600.0
        proxy_healthy = monitor.get("status") == "HEALTHY"
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        monitor, proxy_age, proxy_healthy = None, None, False
    proxy_monitor_status = _slo(proxy_age, 30.0, 48.0) if proxy_healthy else "CRITICAL"
    if proxy_monitor_status == "CRITICAL":
        issues.append(
            {"severity": "CAUTION", "code": "PROXY_MONITOR_INVALID", "detail": proxy_age}
        )
    elif proxy_monitor_status == "CAUTION":
        issues.append({"severity": "CAUTION", "code": "PROXY_MONITOR_AGE", "detail": proxy_age})

    transactions = {
        "reconcile_pending": (state / "reconcile-transaction.json").exists(),
        "heartbeat_pending": (state / "heartbeat-transaction.json").exists(),
    }
    if any(transactions.values()):
        issues.append({"severity": "CRITICAL", "code": "RECOVERY_REQUIRED", "detail": transactions})
    overall = "HEALTHY"
    if any(issue["severity"] == "CRITICAL" for issue in issues):
        overall = "CRITICAL"
    elif issues:
        overall = "CAUTION"
    return {
        "schema_version": 1,
        "paper_only": True,
        "generated_at": now.isoformat(),
        "status": overall,
        "slo": {
            "cycle": {
                "status": cycle_status,
                "age_hours": cycle_age,
                "caution_hours": 30,
                "critical_hours": 48,
            },
            "heartbeat": {
                "status": heartbeat_status,
                "age_hours": heartbeat_age,
                "caution_hours": 12,
                "critical_hours": 24,
            },
            "funding_clock": {
                "status": funding_status,
                "age_hours": funding_age,
                "caution_hours": 12,
                "critical_hours": 24,
            },
            "flat_book": {
                "status": flat_status,
                "age_hours": flat_age,
                "caution_hours": 24,
                "critical_hours": 72,
            },
            "proxy_monitor": {
                "status": proxy_monitor_status,
                "age_hours": proxy_age,
                "caution_hours": 30,
                "critical_hours": 48,
            },
            "proxy_current": {
                "status": proxy_current_status,
                "http_healthy": proxy_current.get("http_healthy"),
                "identity_bound": proxy_current.get("identity_bound"),
                "listener_owner_pids": proxy_current.get("listener_owner_pids", []),
            },
        },
        "state": {
            "latest_completed_cycle": latest_cycle,
            "latest_cycle_directory": directory_numbers[-1] if directory_numbers else None,
            "invalid_published_cycles": invalid_published_cycles,
            "latest_heartbeat_ts": latest_heartbeat.get("ts") if latest_heartbeat else None,
            "latest_settlement_source": settlement_source,
            "heartbeat_chain_valid": heartbeat_integrity,
            "account_binding_source": latest_state_source,
            "account_manifest_consistent": account_consistent
            if latest_state_snapshot is not None
            else None,
            "account_event_chain_valid": account_event_chain_valid,
            "account_event_count": len(account_events),
            "account_event_head": account_events[-1]["event_id"] if account_events else None,
            "positions": len(account.get("positions") or {}),
            "transactions": transactions,
            "deduplication": {
                "ledger": ledger_stats,
                "equity": equity_stats,
                "heartbeats": heartbeat_stats,
            },
        },
        "proxy_monitor": monitor,
        "proxy_current": proxy_current,
        "issues": issues,
    }


def build_health_report(
    state_dir: str | Path = "live_state",
    log_dir: str | Path = "logs",
    *,
    now: datetime | None = None,
) -> dict:
    """Build a consistent report without observing a normal transaction mid-publication."""
    while True:
        try:
            with shared_state_transaction_lock(state_dir):
                return _build_health_report_unlocked(state_dir, log_dir, now=now)
        except StateSnapshotChanged:
            # A first writer created the state root during an otherwise empty snapshot. The next
            # pass opens and shares-locks that directory; no health path creates state itself.
            continue
