"""Phase 9 — per-cycle cost/P&L attribution artifact (pnl.json) + cumulative ledger.jsonl.

build_cycle_pnl is the 'know all these data' record: opening_equity, the cumulative cost totals
(fees/slippage/funding), realized + unrealized P&L, gross/net P&L, closing_equity, turnover, and a
per-position list. Each scheduled paper cycle marks the held book to current public prices,
settles elapsed funding, and records simulated execution friction separately.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from futures_fund.account import PaperAccount
from futures_fund.models import Cadence

_NOTES = (
    "Scheduled PAPER ledger: positions are marked to current public prices, elapsed funding is "
    "settled through the execution timestamp, and paper fees/slippage are recorded separately. "
    "No exchange orders are placed."
)


def build_cycle_pnl(
    account: PaperAccount,
    *,
    opening_equity: float,
    marks: dict[str, float],
    turnover_usd: float,
    cycle: int,
    cadence: Cadence,
    now: datetime,
    prior_closing_equity: float | None = None,
) -> dict:
    """The per-cycle pnl.json record (cumulative cost totals + this-cycle marks)."""
    upnl_by_sym = account.mark_to_market(marks)
    unrealized = sum(upnl_by_sym.values())
    funding_net = account.funding_received - account.funding_paid
    gross_pnl = account.realized_pnl + unrealized + funding_net
    net_pnl = gross_pnl - account.fees_paid - account.slippage_paid
    positions = [
        {
            "symbol": p.symbol,
            "direction": p.direction,
            "qty": p.qty,
            "entry": p.entry_price,
            "mark": marks.get(p.symbol),
            "unrealized": upnl_by_sym.get(p.symbol),
            "accrued_funding": p.accrued_funding,
            "accrued_fees": p.accrued_fees,
        }
        for p in account.positions.values()
    ]
    return {
        "ts": now.isoformat(),
        "cycle": cycle,
        "cadence": cadence,
        # Legacy name retained for historical readers; this is NOT the holding-period open. It is
        # the pre-reconcile account valued at this cycle's fresh execution marks.
        "opening_equity": opening_equity,
        "pre_reconcile_equity_at_execution_marks": opening_equity,
        "prior_closing_equity": prior_closing_equity,
        "close_to_close_pnl": (
            account.equity(marks) - prior_closing_equity
            if prior_closing_equity is not None
            else None
        ),
        "close_to_close_return_frac": (
            account.equity(marks) / prior_closing_equity - 1.0
            if prior_closing_equity is not None and prior_closing_equity > 0.0
            else None
        ),
        "fees_paid": account.fees_paid,
        "slippage_paid": account.slippage_paid,
        "funding_received": account.funding_received,
        "funding_paid": account.funding_paid,
        "funding_net": funding_net,
        "realized_pnl": account.realized_pnl,
        "unrealized_pnl": unrealized,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "closing_equity": account.equity(marks),
        "turnover_usd": turnover_usd,
        "positions": positions,
        "notes": _NOTES,
    }


def latest_closing_equity(state_dir) -> float | None:
    """Return the latest immutable ledger close, failing closed on malformed audit history."""
    path = Path(state_dir) / "ledger.jsonl"
    if not path.exists():
        return None
    latest: tuple[int, float] | None = None
    seen: set[int] = set()
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            cycle = int(row["cycle"])
            equity = float(row["closing_equity"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid ledger row {path}:{line_number}") from exc
        if cycle in seen:
            raise ValueError(f"duplicate ledger cycle {cycle}")
        seen.add(cycle)
        if latest is None or cycle > latest[0]:
            latest = (cycle, equity)
    return latest[1] if latest is not None else None


def append_ledger(state_dir, record: dict) -> None:
    """Atomically insert an idempotent cycle into the cumulative ledger.

    Reconcile recovery may replay a durable transaction. Idempotence by ``cycle`` prevents that
    replay from duplicating PnL or turnover. A conflicting same-cycle row or malformed historical
    row is an audit-integrity failure, never something a rewrite may silently discard.
    """
    try:
        record_cycle = int(record["cycle"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ledger record lacks a valid integer cycle") from exc
    path = Path(state_dir) / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if path.exists():
        seen: set[int] = set()
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"malformed ledger row {path}:{line_number}") from exc
            if not isinstance(existing, dict):
                raise ValueError(f"non-object ledger row {path}:{line_number}")
            try:
                existing_cycle = int(existing["cycle"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid ledger cycle {path}:{line_number}") from exc
            if existing_cycle in seen:
                raise ValueError(f"duplicate ledger cycle {existing_cycle}")
            seen.add(existing_cycle)
            if existing_cycle == record_cycle:
                if existing != record:
                    raise ValueError(f"conflicting ledger replay for cycle {record_cycle}")
                continue
            rows.append(existing)
    rows.append(record)
    rows.sort(key=lambda row: int(row.get("cycle", 0)))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row) + "\n" for row in rows))
    os.replace(tmp, path)
