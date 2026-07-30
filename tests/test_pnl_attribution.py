from __future__ import annotations

import json
from datetime import UTC, datetime

from futures_fund.account import PaperAccount, Position
from futures_fund.pnl_attribution import append_ledger, build_cycle_pnl


def _acct():
    acct = PaperAccount(cash=20_050.0)
    acct.positions["ETH/USDT:USDT"] = Position(
        symbol="ETH/USDT:USDT", direction="short", qty=2.0, entry_price=2000.0,
        opened_ts=datetime(2026, 6, 10, tzinfo=UTC),
        accrued_funding=6.0, accrued_fees=2.0, accrued_slippage=1.0)
    acct.realized_pnl = 10.0
    acct.fees_paid = 4.0
    acct.slippage_paid = 2.0
    acct.funding_received = 6.0
    acct.funding_paid = 0.0
    return acct


def test_build_cycle_pnl_record_shape_and_arithmetic():
    acct = _acct()
    marks = {"ETH/USDT:USDT": 1950.0}              # short upnl = 2*(2000-1950)=100
    rec = build_cycle_pnl(
        acct, opening_equity=20_000.0, marks=marks, turnover_usd=4000.0,
        cycle=2, cadence="daily", now=datetime(2026, 6, 11, tzinfo=UTC))

    assert rec["opening_equity"] == 20_000.0
    assert rec["fees_paid"] == 4.0
    assert rec["slippage_paid"] == 2.0
    assert rec["funding_received"] == 6.0
    assert rec["funding_paid"] == 0.0
    assert rec["funding_net"] == 6.0
    assert rec["realized_pnl"] == 10.0
    assert rec["unrealized_pnl"] == 100.0
    assert rec["gross_pnl"] == 10.0 + 100.0 + 6.0
    assert rec["net_pnl"] == rec["gross_pnl"] - 4.0 - 2.0
    assert rec["closing_equity"] == acct.equity(marks)
    assert rec["turnover_usd"] == 4000.0
    assert rec["cycle"] == 2
    assert rec["cadence"] == "daily"
    pos = rec["positions"][0]
    assert pos["symbol"] == "ETH/USDT:USDT"
    assert pos["direction"] == "short"
    assert pos["qty"] == 2.0
    assert pos["entry"] == 2000.0
    assert pos["mark"] == 1950.0
    assert pos["unrealized"] == 100.0
    assert pos["accrued_funding"] == 6.0
    assert pos["accrued_fees"] == 2.0
    assert "funding" in rec["notes"].lower()


def test_append_ledger_accumulates_lines(tmp_path):
    state = tmp_path / "state"
    append_ledger(state, {"cycle": 1, "net_pnl": 1.0})
    append_ledger(state, {"cycle": 2, "net_pnl": 2.0})
    lines = (state / "ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["cycle"] == 1
    assert json.loads(lines[1])["net_pnl"] == 2.0
