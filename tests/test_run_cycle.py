"""Full-cycle contract test — offline, with a fake exchange + StubAgentRunner (NO LLM calls).

Asserts the driver wires evidence -> specialists -> PM -> adversary -> paper-fill reconcile ->
persisted artifacts + equity, and that a neutral stub book reconciles to a dollar-neutral held book.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from futures_fund.agent_runner import StubAgentRunner
from futures_fund.desk_contracts import Book, BookLeg, SpecialistRead
from futures_fund.desk_cycle import run_cycle
from tests.conftest import make_verdict

NOW = datetime(2026, 7, 7, tzinfo=UTC)
UNIVERSE = ["SOL/USDT:USDT", "XRP/USDT:USDT"]
_MARKS = {"SOL/USDT:USDT": 100.0, "XRP/USDT:USDT": 1.0, "BTC/USDT:USDT": 60000.0}


class _FakeEx:
    def mark_price(self, s):
        return _MARKS.get(s, 100.0)

    def ohlcv(self, s, timeframe="1h", limit=200):
        return pd.DataFrame({"close": _MARKS.get(s, 100.0) * np.exp(np.cumsum(np.full(60, 0.001)))})

    def funding(self, s):
        from futures_fund.market_data import FundingInfo
        m = _MARKS.get(s, 100.0)
        return FundingInfo(symbol=s, current_rate=0.0001, next_funding_ts=NOW,
                           interval_hours=8.0, mark_price=m, index_price=m)

    def open_interest_history(self, s, **k):
        return pd.DataFrame({"oi_value": [1e6, 1.1e6]})

    def long_short_ratio(self, s, **k):
        return pd.DataFrame({"long_short_ratio": [1.0, 1.1]})

    def depth(self, s, limit=20):
        m = _MARKS.get(s, 100.0)
        return {"bids": [(m * 0.999, 1e6)], "asks": [(m * 1.001, 1e6)]}


def _reads(lean_by_sym):
    return [SpecialistRead(symbol=s, lean=lean, conviction=0.7, rationale="r", evidence=[])
            for s, lean in lean_by_sym.items()]


def _stub_runner():
    lean = {"SOL/USDT:USDT": "long", "XRP/USDT:USDT": "short"}
    book = Book(
        legs=[BookLeg(symbol="SOL/USDT:USDT", side="long", target_notional=9000.0, rationale="r"),
              BookLeg(symbol="XRP/USDT:USDT", side="short", target_notional=9000.0, rationale="r")],
        stated_deploy_frac=0.9, stated_dollar_residual_frac=0.0, stated_beta_residual=0.0)
    return StubAgentRunner(canned={
        "sentiment": _reads(lean), "technical": _reads(lean), "futures": _reads(lean),
        "pm": book, "adversary": make_verdict(True)})


def test_run_cycle_end_to_end_offline(tmp_path):
    state = tmp_path / "state"
    report = run_cycle(state, now=NOW, exchange=_FakeEx(), runner=_stub_runner(),
                       symbols=UNIVERSE, cash=20000.0, cycle=1)

    # the neutral stub book reconciled to a held book that is dollar-neutral and deployed.
    assert report.n_legs == 2
    assert report.achieved_dollar_residual_frac < 0.01
    assert report.achieved_deploy_frac > 0.5          # ~0.9 gross-of-cash for an 18k book on 20k

    # artifacts persisted under the rebal cycle root + equity appended.
    d = state / "rebal" / "cycle" / "1"
    assert (d / "book.json").exists()
    assert (d / "reads.json").exists()
    assert (d / "report.json").exists()
    reads = json.loads((d / "reads.json").read_text())
    assert set(reads) == {"sentiment", "technical", "futures"}
    assert (state / "equity-history.jsonl").exists()


def test_run_cycle_survives_a_dropped_specialist(tmp_path):
    # a specialist whose runner raises is dropped; the PM/adversary still run and reconcile.
    class _Partial(StubAgentRunner):
        def run(self, role, prompt, schema):
            if role == "technical":
                raise RuntimeError("timeout")
            return super().run(role, prompt, schema)

    runner = _Partial(canned=_stub_runner()._canned)
    report = run_cycle(tmp_path / "s", now=NOW, exchange=_FakeEx(), runner=runner,
                       symbols=UNIVERSE, cash=20000.0, cycle=1)
    assert report.n_legs == 2
    reads = json.loads((tmp_path / "s" / "rebal" / "cycle" / "1" / "reads.json").read_text())
    assert reads["technical"] == []                   # dropped, cycle still completed


def test_run_cycle_holds_when_all_specialists_fail(tmp_path):
    # ALL specialists raise -> no reads -> HOLD the prior book: no PM/adversary, no reconcile.
    class _AllFail(StubAgentRunner):
        def run(self, role, prompt, schema):
            if role in {"sentiment", "technical", "futures"}:
                raise RuntimeError("outage")
            return super().run(role, prompt, schema)

    runner = _AllFail(canned=_stub_runner()._canned)
    state = tmp_path / "s"
    report = run_cycle(state, now=NOW, exchange=_FakeEx(), runner=runner,
                       symbols=UNIVERSE, cash=20000.0, cycle=1)
    assert report.n_legs == 0                          # held the prior (empty) book, no fills
    d = state / "rebal" / "cycle" / "1"
    assert not (d / "book.json").exists()              # PM never ran (no decision on no evidence)
    assert (d / "report.json").exists()                # the cycle is still recorded