"""Deterministic, read-only desk status and calendar performance reporting.

The report joins only durable PAPER state: completed-cycle ledger/equity rows, official funding
heartbeats, the bound account head, and the existing operational health audit.  It never fetches
market data, invokes an agent, writes state, or estimates an unobserved portfolio mark.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from futures_fund.account import PaperAccount
from futures_fund.desk_health import build_health_report
from futures_fund.heartbeat import verify_heartbeat_completion
from futures_fund.metrics import max_drawdown
from futures_fund.performance import _time_based_performance
from futures_fund.state_transaction import StateSnapshotChanged, shared_state_transaction_lock

_RECONCILIATION_ABS_TOLERANCE = 1e-6


@dataclass(frozen=True)
class _Mark:
    ts: datetime
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    funding_net: float
    fees_paid: float
    slippage_paid: float
    source: str
    lineage: str
    cycle: int | None = None
    raw: dict[str, Any] | None = None


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value}")


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(), parse_constant=_reject_constant)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON artifact {path}: {exc}") from exc


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ValueError(f"required PAPER history is missing: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"malformed JSONL row {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"non-object JSONL row {path}:{line_number}")
        rows.append(row)
    if not rows:
        raise ValueError(f"required PAPER history is empty: {path}")
    return rows


def _utc(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is not timezone-aware: {value!r}")
    return parsed.astimezone(UTC)


def _number(
    row: dict[str, Any],
    key: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{key} is boolean, not numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} is not numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{key} is not finite")
    if positive and result <= 0.0:
        raise ValueError(f"{key} is not positive")
    if nonnegative and result < 0.0:
        raise ValueError(f"{key} is negative")
    return result


def _close_enough(left: float, right: float) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=1e-12,
        abs_tol=_RECONCILIATION_ABS_TOLERANCE,
    )


def _ledger_marks(
    state: Path, starting_capital: float
) -> tuple[list[_Mark], list[dict[str, Any]]]:
    ledger = _jsonl(state / "ledger.jsonl")
    seen_cycles: set[int] = set()
    marks: list[_Mark] = []
    previous_cycle = 0
    previous_ts: datetime | None = None
    previous_costs: tuple[float, float] | None = None
    for index, row in enumerate(ledger, start=1):
        try:
            cycle = int(row["cycle"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"ledger row {index} has no valid cycle") from exc
        if cycle in seen_cycles or cycle <= previous_cycle:
            raise ValueError(f"ledger cycles are duplicate or non-monotonic at cycle {cycle}")
        seen_cycles.add(cycle)
        previous_cycle = cycle
        ts = _utc(row.get("ts"))
        if previous_ts is not None and ts < previous_ts:
            raise ValueError(f"ledger timestamp regressed at cycle {cycle}")
        previous_ts = ts
        equity = _number(row, "closing_equity", positive=True)
        realized = _number(row, "realized_pnl")
        unrealized = _number(row, "unrealized_pnl")
        funding = _number(row, "funding_net")
        fees = _number(row, "fees_paid", nonnegative=True)
        slippage = _number(row, "slippage_paid", nonnegative=True)
        _number(row, "turnover_usd", nonnegative=True)
        if previous_costs is not None and (
            fees + _RECONCILIATION_ABS_TOLERANCE < previous_costs[0]
            or slippage + _RECONCILIATION_ABS_TOLERANCE < previous_costs[1]
        ):
            raise ValueError(f"cumulative execution costs regressed at cycle {cycle}")
        previous_costs = (fees, slippage)
        reconciled = starting_capital + realized + unrealized + funding - fees - slippage
        if not _close_enough(equity, reconciled):
            raise ValueError(
                f"ledger cycle {cycle} does not reconcile: equity={equity} components={reconciled}"
            )
        marks.append(
            _Mark(
                ts=ts,
                equity=equity,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                funding_net=funding,
                fees_paid=fees,
                slippage_paid=slippage,
                source="cycle",
                lineage="manifest_complete",
                cycle=cycle,
                raw=row,
            )
        )
    return marks, ledger


def _validate_equity_history(state: Path, cycle_marks: list[_Mark]) -> list[dict[str, Any]]:
    rows = _jsonl(state / "equity-history.jsonl")
    by_cycle: dict[int, tuple[datetime, float]] = {}
    for index, row in enumerate(rows, start=1):
        try:
            cycle = int(row["cycle"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"equity row {index} has no valid cycle") from exc
        if cycle in by_cycle:
            raise ValueError(f"duplicate equity-history cycle {cycle}")
        by_cycle[cycle] = (_utc(row.get("ts")), _number(row, "equity", positive=True))
    ledger_cycles = {mark.cycle for mark in cycle_marks}
    if set(by_cycle) != ledger_cycles:
        raise ValueError("ledger and equity-history cycle membership differ")
    for mark in cycle_marks:
        assert mark.cycle is not None
        if not _close_enough(by_cycle[mark.cycle][1], mark.equity):
            raise ValueError(f"ledger/equity-history mismatch at cycle {mark.cycle}")
    return rows


def _heartbeat_marks(
    state: Path,
    cycle_marks: list[_Mark],
    starting_capital: float,
) -> tuple[list[_Mark], list[dict[str, Any]], dict[str, int]]:
    path = state / "portfolio-heartbeats.jsonl"
    rows = _jsonl(path) if path.exists() and path.read_text().strip() else []
    official: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    marks: list[_Mark] = []
    lineage_counts = {"manifest_bound": 0, "legacy_unbound": 0}
    for index, row in enumerate(rows, start=1):
        if row.get("kind") != "token_free_funding_heartbeat":
            continue
        if row.get("paper_only") is not True:
            raise ValueError(f"heartbeat row {index} is not PAPER-only")
        identity = (str(row.get("schedule_slot") or row.get("ts")), str(row.get("kind")))
        if not identity[0] or identity in seen:
            raise ValueError(f"duplicate or invalid heartbeat identity at row {index}")
        seen.add(identity)
        ts = _utc(row.get("ts"))
        equity = _number(row, "equity", positive=True)
        funding = _number(row, "funding_net_cumulative")
        prior_cycle = next((mark for mark in reversed(cycle_marks) if mark.ts <= ts), None)
        if prior_cycle is None:
            raise ValueError(f"heartbeat at {ts.isoformat()} has no prior cycle ledger anchor")
        version = int(row.get("heartbeat_schema_version", 0) or 0)
        if version >= 2:
            if not verify_heartbeat_completion(state, row):
                raise ValueError(f"heartbeat completion is invalid at {ts.isoformat()}")
            lineage = "manifest_bound"
        else:
            lineage = "legacy_unbound"
        lineage_counts[lineage] += 1
        # Heartbeats never trade. Realized price PnL, fees, and slippage therefore remain at the
        # preceding cycle's cumulative values. The exact funding total is carried by the heartbeat;
        # unrealized PnL is the sole residual needed to reconcile its marked equity.
        unrealized = (
            equity
            - starting_capital
            - prior_cycle.realized_pnl
            - funding
            + prior_cycle.fees_paid
            + prior_cycle.slippage_paid
        )
        marks.append(
            _Mark(
                ts=ts,
                equity=equity,
                realized_pnl=prior_cycle.realized_pnl,
                unrealized_pnl=unrealized,
                funding_net=funding,
                fees_paid=prior_cycle.fees_paid,
                slippage_paid=prior_cycle.slippage_paid,
                source="heartbeat",
                lineage=lineage,
                cycle=prior_cycle.cycle,
                raw=row,
            )
        )
        official.append(row)
    return marks, official, lineage_counts


def _month_start(value: datetime) -> datetime:
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _delta(end: _Mark, start: _Mark, field: str) -> float:
    return float(getattr(end, field)) - float(getattr(start, field))


def _monthly_rows(
    marks: list[_Mark],
    cycle_marks: list[_Mark],
    *,
    starting_capital: float,
) -> list[dict[str, Any]]:
    first = marks[0]
    last = marks[-1]
    inception = _Mark(
        ts=first.ts,
        equity=starting_capital,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        funding_net=0.0,
        fees_paid=0.0,
        slippage_paid=0.0,
        source="inception",
        lineage="configured_starting_capital",
    )
    rows: list[dict[str, Any]] = []
    cursor = _month_start(first.ts)
    while cursor <= _month_start(last.ts):
        following = _next_month(cursor)
        in_month = [mark for mark in marks if cursor <= mark.ts < following]
        prior = [mark for mark in marks if mark.ts < cursor]
        baseline = prior[-1] if prior else inception
        if not in_month:
            rows.append(
                {
                    "month": cursor.strftime("%Y-%m"),
                    "available": False,
                    "coverage_status": "no_recorded_mark",
                    "start_equity": baseline.equity,
                    "end_equity": None,
                    "net_pnl": None,
                    "return_frac": None,
                    "realized_price_pnl": None,
                    "unrealized_pnl_change": None,
                    "funding_net": None,
                    "fees_paid": None,
                    "slippage_paid": None,
                    "turnover_usd": 0.0,
                    "max_drawdown_frac": None,
                    "cycle_count": 0,
                    "mark_count": 0,
                    "start_mark_ts": baseline.ts.isoformat(),
                    "end_mark_ts": None,
                    "end_mark_source": None,
                    "end_mark_lineage": None,
                }
            )
            cursor = following
            continue
        end = in_month[-1]
        net_pnl = end.equity - baseline.equity
        attribution = (
            _delta(end, baseline, "realized_pnl")
            + _delta(end, baseline, "unrealized_pnl")
            + _delta(end, baseline, "funding_net")
            - _delta(end, baseline, "fees_paid")
            - _delta(end, baseline, "slippage_paid")
        )
        if not _close_enough(net_pnl, attribution):
            raise ValueError(f"monthly attribution does not reconcile for {cursor:%Y-%m}")
        month_cycles = [mark for mark in cycle_marks if cursor <= mark.ts < following]
        path = [baseline.equity, *(mark.equity for mark in in_month)]
        rows.append(
            {
                "month": cursor.strftime("%Y-%m"),
                "available": True,
                "coverage_status": "partial_inception_month" if not prior else "observed",
                "start_equity": baseline.equity,
                "end_equity": end.equity,
                "net_pnl": net_pnl,
                "return_frac": (
                    end.equity / baseline.equity - 1.0 if baseline.equity > 0.0 else None
                ),
                "realized_price_pnl": _delta(end, baseline, "realized_pnl"),
                "unrealized_pnl_change": _delta(end, baseline, "unrealized_pnl"),
                "funding_net": _delta(end, baseline, "funding_net"),
                "fees_paid": _delta(end, baseline, "fees_paid"),
                "slippage_paid": _delta(end, baseline, "slippage_paid"),
                "turnover_usd": sum(
                    _number(mark.raw or {}, "turnover_usd", nonnegative=True)
                    for mark in month_cycles
                ),
                "max_drawdown_frac": max_drawdown(path),
                "cycle_count": len(month_cycles),
                "mark_count": len(in_month),
                "start_mark_ts": baseline.ts.isoformat(),
                "end_mark_ts": end.ts.isoformat(),
                "end_mark_source": end.source,
                "end_mark_lineage": end.lineage,
            }
        )
        cursor = following
    return rows


def _latest_exposure(latest: _Mark) -> dict[str, Any]:
    raw = latest.raw or {}
    if latest.source == "heartbeat":
        gross = _number(raw, "gross", nonnegative=True)
        longs = _number(raw, "longs_usd", nonnegative=True)
        shorts = _number(raw, "shorts_usd", nonnegative=True)
        return {
            "positions": len(raw.get("positions") or []),
            "gross_usd": gross,
            "deploy_frac": _number(raw, "deploy_frac", nonnegative=True),
            "longs_usd": longs,
            "shorts_usd": shorts,
            "dollar_residual_frac": _number(raw, "dollar_residual_frac"),
            "beta_residual": _number(raw, "beta_residual"),
        }
    positions = raw.get("positions") or []
    longs = shorts = 0.0
    for position in positions:
        notional = _number(position, "qty", nonnegative=True) * _number(
            position, "mark", positive=True
        )
        if position.get("direction") == "long":
            longs += notional
        elif position.get("direction") == "short":
            shorts += notional
        else:
            raise ValueError("ledger position has an invalid direction")
    gross = longs + shorts
    return {
        "positions": len(positions),
        "gross_usd": gross,
        "deploy_frac": gross / latest.equity,
        "longs_usd": longs,
        "shorts_usd": shorts,
        "dollar_residual_frac": abs(longs - shorts) / latest.equity,
        "beta_residual": None,
    }


def _performance_unlocked(
    state: Path,
    *,
    starting_capital: float,
    now: datetime,
) -> dict[str, Any]:
    if not math.isfinite(starting_capital) or starting_capital <= 0.0:
        raise ValueError("starting capital must be finite and positive")
    cycle_marks, ledger = _ledger_marks(state, starting_capital)
    equity_rows = _validate_equity_history(state, cycle_marks)
    heartbeat_marks, heartbeats, lineage_counts = _heartbeat_marks(
        state,
        cycle_marks,
        starting_capital,
    )
    marks = sorted(
        [*cycle_marks, *heartbeat_marks],
        key=lambda mark: (mark.ts, 0 if mark.source == "cycle" else 1),
    )
    marks = [mark for mark in marks if mark.ts <= now]
    if not marks:
        raise ValueError("no PAPER marks exist at or before the requested timestamp")
    latest = marks[-1]

    account_raw = _json(state / "account.json")
    if not isinstance(account_raw, dict):
        raise ValueError("account.json is not an object")
    account = PaperAccount.from_dict(account_raw)
    account_funding = account.funding_received - account.funding_paid
    for label, observed, expected in (
        ("realized PnL", latest.realized_pnl, account.realized_pnl),
        ("funding", latest.funding_net, account_funding),
        ("fees", latest.fees_paid, account.fees_paid),
        ("slippage", latest.slippage_paid, account.slippage_paid),
    ):
        if not _close_enough(observed, expected):
            raise ValueError(f"latest recorded {label} does not match the account head")
    account_unrealized = latest.equity - account.cash
    if not _close_enough(latest.unrealized_pnl, account_unrealized):
        raise ValueError("latest marked unrealized PnL does not match account cash/equity")
    lifetime_net = latest.equity - starting_capital
    lifetime_gross = (
        account.realized_pnl + account_unrealized + account_funding
    )
    reconciled_net = lifetime_gross - account.fees_paid - account.slippage_paid
    if not _close_enough(lifetime_net, reconciled_net):
        raise ValueError("lifetime PAPER account attribution does not reconcile")

    monthly = _monthly_rows(marks, cycle_marks, starting_capital=starting_capital)
    monthly_net = sum(float(row["net_pnl"]) for row in monthly if row["available"])
    if not _close_enough(monthly_net, lifetime_net):
        raise ValueError("calendar-month net PnL does not sum to accumulated net PnL")
    equity_path = [starting_capital, *(mark.equity for mark in marks)]
    peak = max(equity_path)
    time_metrics = _time_based_performance(
        ledger,
        heartbeats,
        now=latest.ts,
        equity=latest.equity,
    )
    risk = {
        key: time_metrics.get(key)
        for key in (
            "official_daily_mark",
            "daily_observations",
            "daily_return_observations",
            "missing_daily_gaps",
            "sharpe_status",
            "sharpe_annualized",
            "sortino_status",
            "sortino_annualized",
            "profitable_daily_rate",
            "completed_week_return_observations",
            "completed_week_status",
            "profitable_week_rate",
            "worst_completed_week_return_frac",
        )
    }
    return {
        "as_of_ts": latest.ts.isoformat(),
        "calendar_timezone": "UTC",
        "return_basis": "marked_equity_net_after_fees_slippage_including_funding",
        "latest_mark_source": latest.source,
        "latest_mark_lineage": latest.lineage,
        "latest_completed_cycle": cycle_marks[-1].cycle,
        "latest_cycle_ts": cycle_marks[-1].ts.isoformat(),
        "latest_heartbeat_ts": (
            heartbeat_marks[-1].ts.isoformat() if heartbeat_marks else None
        ),
        "portfolio": {
            "cash": account.cash,
            "equity": latest.equity,
            **_latest_exposure(latest),
        },
        "monthly": monthly,
        "accumulated": {
            "starting_capital": starting_capital,
            "equity": latest.equity,
            "net_pnl": lifetime_net,
            "return_frac": latest.equity / starting_capital - 1.0,
            "gross_pnl_before_execution_costs": lifetime_gross,
            "realized_price_pnl": account.realized_pnl,
            "unrealized_pnl": account_unrealized,
            "funding_net": account_funding,
            "fees_paid": account.fees_paid,
            "slippage_paid": account.slippage_paid,
            "turnover_usd": sum(
                _number(row, "turnover_usd", nonnegative=True) for row in ledger
            ),
            "peak_equity": peak,
            "current_drawdown_frac": latest.equity / peak - 1.0,
            "max_drawdown_frac": max_drawdown(equity_path),
            "cycle_count": len(cycle_marks),
            "mark_count": len(marks),
        },
        "risk": risk,
        "data_integrity": {
            "account_reconciled": True,
            "ledger_equity_membership_matched": True,
            "ledger_rows": len(ledger),
            "equity_rows": len(equity_rows),
            "official_heartbeat_rows": len(heartbeats),
            "heartbeat_lineage": lineage_counts,
        },
    }


def build_performance_summary(
    state_dir: str | Path = "live_state",
    *,
    starting_capital: float = 20_000.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a transaction-consistent, read-only calendar/lifetime performance summary."""
    state = Path(state_dir)
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    while True:
        try:
            with shared_state_transaction_lock(state):
                return _performance_unlocked(
                    state,
                    starting_capital=starting_capital,
                    now=observed_at,
                )
        except StateSnapshotChanged:
            continue


def _compact_health(health: dict[str, Any]) -> dict[str, Any]:
    state = health.get("state") or {}
    slo = health.get("slo") or {}
    return {
        "status": health.get("status"),
        "paper_only": health.get("paper_only") is True,
        "generated_at": health.get("generated_at"),
        "slo": {
            name: {
                key: block.get(key)
                for key in ("status", "age_hours", "http_healthy", "identity_bound")
                if key in block
            }
            for name in (
                "cycle",
                "heartbeat",
                "funding_clock",
                "flat_book",
                "proxy_current",
            )
            if isinstance((block := slo.get(name)), dict)
        },
        "state": {
            key: state.get(key)
            for key in (
                "latest_completed_cycle",
                "latest_cycle_directory",
                "invalid_published_cycles",
                "latest_heartbeat_ts",
                "latest_settlement_source",
                "heartbeat_chain_valid",
                "account_binding_source",
                "account_manifest_consistent",
                "account_event_chain_valid",
                "account_event_count",
                "positions",
                "transactions",
                "directive_lifecycle",
            )
        },
        "issues": health.get("issues") or [],
    }


def build_status_performance_report(
    state_dir: str | Path = "live_state",
    log_dir: str | Path = "logs",
    *,
    starting_capital: float = 20_000.0,
    now: datetime | None = None,
    health_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a concise status/performance report and reject a cross-transaction observation."""
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    health = health_report or build_health_report(state_dir, log_dir, now=observed_at)
    performance = build_performance_summary(
        state_dir,
        starting_capital=starting_capital,
        now=observed_at,
    )
    health_state = health.get("state") or {}
    if health_state.get("latest_completed_cycle") != performance["latest_completed_cycle"]:
        raise ValueError("state changed between health and performance snapshots; rerun report")
    health_heartbeat = health_state.get("latest_heartbeat_ts")
    performance_heartbeat = performance["latest_heartbeat_ts"]
    if (health_heartbeat is None) != (performance_heartbeat is None) or (
        health_heartbeat is not None
        and performance_heartbeat is not None
        and _utc(health_heartbeat) != _utc(performance_heartbeat)
    ):
        raise ValueError("heartbeat changed between health and performance snapshots; rerun report")
    return {
        "schema_version": 1,
        "paper_only": True,
        "generated_at": observed_at.isoformat(),
        "health": _compact_health(health),
        "performance": performance,
    }


def _money(value: object) -> str:
    return f"${float(value):,.2f}"


def _signed_money(value: object) -> str:
    return f"{float(value):+,.2f}"


def _pct(value: object) -> str:
    return f"{100.0 * float(value):+.2f}%"


def _age(block: dict[str, Any]) -> str:
    value = block.get("age_hours")
    return "n/a" if value is None else f"{float(value):.1f}h"


def format_text(report: dict[str, Any]) -> str:
    """Render the stable, token-compact operator view."""
    health = report["health"]
    performance = report["performance"]
    portfolio = performance["portfolio"]
    slo = health["slo"]
    lines = [
        "PAPER DESK STATUS + PERFORMANCE",
        f"As of {performance['as_of_ts']} | health {health['status']}",
        (
            f"Cycle {performance['latest_completed_cycle']} "
            f"{slo.get('cycle', {}).get('status', 'UNKNOWN')} ({_age(slo.get('cycle', {}))}) | "
            f"heartbeat {slo.get('heartbeat', {}).get('status', 'UNKNOWN')} "
            f"({_age(slo.get('heartbeat', {}))}) | "
            f"funding {slo.get('funding_clock', {}).get('status', 'UNKNOWN')} "
            f"({_age(slo.get('funding_clock', {}))}) | "
            f"proxy {slo.get('proxy_current', {}).get('status', 'UNKNOWN')}"
        ),
    ]
    issues = health.get("issues") or []
    issue_labels = []
    for issue in issues:
        code = str(issue.get("code"))
        detail = issue.get("detail")
        if code in {"FLAT_BOOK_DURATION", "FLAT_BOOK_PROLONGED"} and isinstance(
            detail, int | float
        ):
            code = f"{code} ({float(detail):.1f}h)"
        issue_labels.append(code)
    lines.append("Issues: " + (", ".join(issue_labels) if issue_labels else "none"))
    beta = portfolio.get("beta_residual")
    beta_label = "n/a" if beta is None else f"{100 * float(beta):.2f}%"
    lines.append(
        "Book: "
        f"{portfolio['positions']} positions | equity {_money(portfolio['equity'])} | "
        f"gross {_money(portfolio['gross_usd'])} ({100 * portfolio['deploy_frac']:.1f}%) | "
        f"long {_money(portfolio['longs_usd'])} / short {_money(portfolio['shorts_usd'])} | "
        f"dollar residual {100 * portfolio['dollar_residual_frac']:.2f}% | "
        f"beta residual {beta_label}"
    )
    lines.extend(
        [
            "",
            "MONTHLY (funding included; after fees and slippage)",
            (
                "Month     Start        End          Net PnL    Return   Realized   "
                "dUnreal   Funding   Fees   Slip    Turnover    MDD"
            ),
        ]
    )
    for row in performance["monthly"]:
        month_label = (
            f"{row['month']}*"
            if row.get("coverage_status") == "partial_inception_month"
            else row["month"]
        )
        if not row["available"]:
            lines.append(f"{month_label:<8}  no recorded mark")
            continue
        lines.append(
            f"{month_label:<8}  "
            f"{_money(row['start_equity']):>11}  {_money(row['end_equity']):>11}  "
            f"{_signed_money(row['net_pnl']):>10}  {_pct(row['return_frac']):>8}  "
            f"{_signed_money(row['realized_price_pnl']):>9}  "
            f"{_signed_money(row['unrealized_pnl_change']):>8}  "
            f"{_signed_money(row['funding_net']):>8}  "
            f"{_money(row['fees_paid']):>7}  {_money(row['slippage_paid']):>7}  "
            f"{_money(row['turnover_usd']):>11}  "
            f"{100 * row['max_drawdown_frac']:>5.2f}%"
        )
    if any(
        row.get("coverage_status") == "partial_inception_month"
        for row in performance["monthly"]
    ):
        first_ts = performance["monthly"][0]["start_mark_ts"]
        inception_date = first_ts[:10] if isinstance(first_ts, str) else "inception"
        lines.append(f"* Partial inception month; desk history begins {inception_date}.")

    total = performance["accumulated"]
    lines.extend(
        [
            "",
            "ACCUMULATED",
            (
                f"Start {_money(total['starting_capital'])} | equity {_money(total['equity'])} | "
                f"net PnL {_signed_money(total['net_pnl'])} | return {_pct(total['return_frac'])}"
            ),
            (
                f"Price realized {_signed_money(total['realized_price_pnl'])} | "
                f"unrealized {_signed_money(total['unrealized_pnl'])} | "
                f"funding {_signed_money(total['funding_net'])} | "
                f"fees {_money(total['fees_paid'])} | slippage {_money(total['slippage_paid'])}"
            ),
            (
                "Gross PnL before execution costs "
                f"{_signed_money(total['gross_pnl_before_execution_costs'])} | "
                f"turnover {_money(total['turnover_usd'])} | peak {_money(total['peak_equity'])} | "
                f"current DD {_pct(total['current_drawdown_frac'])} | "
                f"max DD {-100 * total['max_drawdown_frac']:.2f}%"
            ),
        ]
    )
    risk = performance["risk"]
    sharpe = (
        f"{float(risk['sharpe_annualized']):.2f}"
        if risk.get("sharpe_annualized") is not None
        else "n/a"
    )
    sortino = (
        f"{float(risk['sortino_annualized']):.2f}"
        if risk.get("sortino_annualized") is not None
        else "n/a"
    )
    lines.append(
        f"Risk: Sharpe {sharpe} ({risk.get('sharpe_status')}) | "
        f"Sortino {sortino} ({risk.get('sortino_status')}) | "
        f"official daily returns {risk.get('daily_return_observations')} | "
        f"missing daily marks {risk.get('missing_daily_gaps')}"
    )
    integrity = performance["data_integrity"]
    lines.append(
        f"Integrity: reconciled | {integrity['ledger_rows']} cycles | "
        f"{integrity['official_heartbeat_rows']} heartbeats "
        f"({integrity['heartbeat_lineage']['manifest_bound']} bound, "
        f"{integrity['heartbeat_lineage']['legacy_unbound']} legacy-unbound)"
    )
    return "\n".join(lines)
