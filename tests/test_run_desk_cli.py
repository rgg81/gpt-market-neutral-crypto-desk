"""run_desk_cli contract test — the 24h due-gate + lock around run_cycle, offline (no LLM/network).

The three external seams (universe fetch, exchange, agent runner) are monkeypatched to fakes/stubs.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from futures_fund.agent_runner import StubAgentRunner
from futures_fund.desk_contracts import Book, BookLeg, SpecialistRead
from tests.conftest import make_verdict

NOW_ISO = "2026-07-07T08:00:00+00:00"
_MARKS = {
    "SOL/USDT:USDT": 100.0,
    "ADA/USDT:USDT": 2.0,
    "XRP/USDT:USDT": 1.0,
    "DOGE/USDT:USDT": 0.2,
    "BTC/USDT:USDT": 60_000.0,
}


class _FakeEx:
    def symbol_spec(self, s):
        return SimpleNamespace(step_size=0.001, tick_size=0.0001, min_notional=5.0)

    def mark_price(self, s):
        return _MARKS.get(s, 100.0)

    def ohlcv(self, s, timeframe="1h", limit=200):
        periods = 60 if timeframe == "1d" else 200
        freq = "1D" if timeframe == "1d" else "1h"
        now = datetime.fromisoformat(NOW_ISO)
        return pd.DataFrame({
            "timestamp": pd.date_range(end=now, periods=periods, freq=freq, tz="UTC"),
            "close": _MARKS.get(s, 100.0) * np.exp(
                np.cumsum(np.full(periods, 0.001))
            ),
        })

    def funding(self, s):
        from futures_fund.market_data import FundingInfo
        m = _MARKS.get(s, 100.0)
        return FundingInfo(symbol=s, current_rate=0.0001,
                           next_funding_ts=datetime(2026, 7, 7, tzinfo=UTC),
                           interval_hours=8.0, mark_price=m, index_price=m)

    def open_interest_history(self, s, **k):
        return pd.DataFrame({"oi_value": [1e6, 1.1e6]})

    def long_short_ratio(self, s, **k):
        return pd.DataFrame({"long_short_ratio": [1.0, 1.1]})

    def depth(self, s, limit=20):
        m = _MARKS.get(s, 100.0)
        return {"bids": [(m * 0.999, 1e6)], "asks": [(m * 1.001, 1e6)]}


def _stub_runner():
    lean = {
        "SOL/USDT:USDT": "long",
        "ADA/USDT:USDT": "long",
        "XRP/USDT:USDT": "short",
        "DOGE/USDT:USDT": "short",
    }
    reads = [SpecialistRead(symbol=s, lean=v, conviction=0.7, rationale="r", evidence=[])
             for s, v in lean.items()]
    book = Book(
        legs=[
            BookLeg(
                symbol="SOL/USDT:USDT", side="long", target_notional=4500.0, rationale="r"
            ),
            BookLeg(
                symbol="ADA/USDT:USDT", side="long", target_notional=4500.0, rationale="r"
            ),
            BookLeg(
                symbol="XRP/USDT:USDT", side="short", target_notional=4500.0, rationale="r"
            ),
            BookLeg(
                symbol="DOGE/USDT:USDT", side="short", target_notional=4500.0, rationale="r"
            ),
        ],
        stated_deploy_frac=0.9, stated_dollar_residual_frac=0.0, stated_beta_residual=0.0)
    return StubAgentRunner(
        canned={
            "sentiment": reads,
            "technical": reads,
            "futures": reads,
            "pm": book,
            "adversary": make_verdict(True),
        }
    )


def _patch(monkeypatch):
    import scripts.run_desk_cli as cli
    monkeypatch.setattr(cli, "_build_exchange", lambda settings: _FakeEx())
    monkeypatch.setattr(cli, "_build_runner", lambda settings: _stub_runner())
    monkeypatch.setattr(cli, "_fetch_universe", lambda settings: list(_MARKS))


def test_cli_fires_a_fresh_cycle(tmp_path, monkeypatch):
    _patch(monkeypatch)
    from scripts.run_desk_cli import main
    state = tmp_path / "state"
    main(["--offline-injected-only", "--now", NOW_ISO, "--state-dir", str(state)])
    assert (state / "rebal" / "cycle" / "1" / "book.json").exists()
    assert (state / "rebal" / "cycle" / "1" / "report.json").exists()
    assert (state / "equity-history.jsonl").exists()
    execution = json.loads((state / "rebal" / "cycle" / "1" / "execution.json").read_text())
    assert all(row["quantity_source"] == "decision_mark" for row in execution.values())
    assert all("decision_target_qty_signed" in row for row in execution.values())


def test_cli_skips_a_served_candle(tmp_path, monkeypatch):
    _patch(monkeypatch)
    from scripts.run_desk_cli import main
    state = tmp_path / "state"
    argv = ["--offline-injected-only", "--now", NOW_ISO, "--state-dir", str(state)]
    main(argv)                                                    # fires cycle 1
    main(argv)                                                    # same daily candle -> SKIP
    assert not (state / "rebal" / "cycle" / "2").exists()        # no second cycle fired


def test_cli_fails_closed_without_explicit_offline_ack_before_any_runtime_access(
    tmp_path, monkeypatch, capsys
):
    import scripts.run_desk_cli as cli

    def _unexpected_runtime_access():
        pytest.fail("CLI touched runtime settings before enforcing its offline-only gate")

    monkeypatch.setattr(cli, "load_settings", _unexpected_runtime_access)
    state = tmp_path / "must-not-exist"
    with pytest.raises(SystemExit) as exc:
        cli.main(["--state-dir", str(state)])

    assert exc.value.code == 2
    assert "disabled without --offline-injected-only" in capsys.readouterr().err
    assert not state.exists()


def test_checked_in_cli_cannot_construct_an_agent_runner():
    from scripts.run_desk_cli import _build_runner

    with pytest.raises(RuntimeError, match="No raw-API agent runner exists"):
        _build_runner(SimpleNamespace())
