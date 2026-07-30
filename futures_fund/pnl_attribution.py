"""Phase 9 — per-cycle cost/P&L attribution artifact (pnl.json) + cumulative ledger.jsonl.

build_cycle_pnl is the 'know all these data' record: opening_equity, the cumulative cost totals
(fees/slippage/funding), realized + unrealized P&L, gross/net P&L, closing_equity, turnover, and a
per-position list. In the accelerated live demo cross-tick PRICE PnL is small (each tick re-reads
current marks) so the curve mainly reflects FUNDING carry + fees — on-thesis for a carry desk; a
true multi-day price-PnL curve needs real daily cadence or PIT historical marks (see `notes`).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from futures_fund.account import PaperAccount
from futures_fund.models import Cadence

_NOTES = (
    "Accelerated demo: each tick re-reads current marks, so cross-tick PRICE PnL is small while "
    "FUNDING carry accrues per sim-day on held positions; the curve mainly reflects funding carry "
    "+ fees (on-thesis). A true multi-day price-PnL curve needs real daily cadence or PIT marks. "
    "Funding is non-zero only across runs that advance `now` past a settlement (every 8h default)."
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
        "opening_equity": opening_equity,
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


def append_ledger(state_dir, record: dict) -> None:
    """Append one pnl record to the cumulative state/ledger.jsonl (atomic full-rewrite)."""
    path = Path(state_dir) / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    prior = path.read_text() if path.exists() else ""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(prior + json.dumps(record) + "\n")
    os.replace(tmp, path)
