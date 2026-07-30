"""Regression tests for the 2026-07-10 forensic-review fixes: pending isolation, funding
settlement, fabricated-close removal, friction visibility, timestamp integrity, the reflector
evidence guard, and the watchdog. All offline/deterministic."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from futures_fund.account import CostInputs, PaperAccount, Position
from futures_fund.desk_contracts import Book, BookLeg
from futures_fund.desk_cycle import reconcile_book
from futures_fund.equity_log import record_equity
from futures_fund.pending_io import resolve_pending
from futures_fund.reflection import validate_edit_citations

NOW = datetime(2026, 7, 10, 8, 7, tzinfo=UTC)


# ---------- pending isolation (Incident B) ----------

def _seed_pending(root, cycle: int, now: datetime) -> None:
    d = root / "pending" / str(cycle)
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(
        {"cycle": cycle, "now": now.isoformat(), "cash": 20000.0, "symbols": []}))
    (root / "pending" / "current.json").write_text(json.dumps(
        {"cycle": cycle, "dir": str(d), "created": now.isoformat()}))


def test_resolve_pending_happy_path(tmp_path):
    wall = datetime.now(UTC)
    _seed_pending(tmp_path, 7, wall)
    pending, meta = resolve_pending(tmp_path)
    assert meta["cycle"] == 7 and pending.name == "7"


def test_stale_file_race_regression(tmp_path):
    """A slow cycle-6 specialist writing AFTER cycle 7 started lands in pending/6/ (its dispatch
    prompt named that path) — cycle 7's consumers resolve pending/7/ and can never see it."""
    wall = datetime.now(UTC)
    _seed_pending(tmp_path, 6, wall - timedelta(hours=8))
    _seed_pending(tmp_path, 7, wall)
    # the late cycle-6 writer fires now — into ITS dir
    late = tmp_path / "pending" / "6" / "sentiment_reads.json"
    late.write_text("[]")
    pending, meta = resolve_pending(tmp_path)
    assert meta["cycle"] == 7
    assert not (pending / "sentiment_reads.json").exists()   # stale file is invisible to cycle 7


def test_cycle_never_reuses_prior_cycle_artifacts(tmp_path):
    """current.json pointing at a dir whose meta says a DIFFERENT cycle is refused."""
    wall = datetime.now(UTC)
    _seed_pending(tmp_path, 6, wall)
    ptr = json.loads((tmp_path / "pending" / "current.json").read_text())
    ptr["cycle"] = 7                       # pointer claims 7, meta in dir says 6
    (tmp_path / "pending" / "current.json").write_text(json.dumps(ptr))
    with pytest.raises(ValueError, match="mismatch"):
        resolve_pending(tmp_path)


def test_pending_pointer_cannot_escape_its_cycle_directory(tmp_path):
    """A matching fresh meta in another directory is still not this desk's pending cycle."""
    wall = datetime.now(UTC)
    _seed_pending(tmp_path, 7, wall)
    external = tmp_path / "other-desk" / "7"
    external.mkdir(parents=True)
    (external / "meta.json").write_text(json.dumps(
        {"cycle": 7, "now": wall.isoformat(), "cash": 20000.0, "symbols": []}
    ))
    pointer = tmp_path / "pending" / "current.json"
    payload = json.loads(pointer.read_text())
    payload["dir"] = str(external)
    pointer.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="pointer/dir mismatch"):
        resolve_pending(tmp_path)


def test_manufactured_future_stamp_halts(tmp_path):
    _seed_pending(tmp_path, 7, datetime.now(UTC) + timedelta(hours=2))
    with pytest.raises(ValueError, match="future"):
        resolve_pending(tmp_path)


def test_dead_cycle_halts(tmp_path):
    _seed_pending(tmp_path, 7, datetime.now(UTC) - timedelta(hours=3))
    with pytest.raises(ValueError, match="dead cycle"):
        resolve_pending(tmp_path)


# ---------- funding settlement in reconcile ----------

def _neutral_book() -> Book:
    return Book(legs=[
        BookLeg(symbol="A/USDT:USDT", side="long", target_notional=9000.0),
        BookLeg(symbol="B/USDT:USDT", side="short", target_notional=9000.0),
    ])


def test_reconcile_settles_funding_across_boundary():
    acct = PaperAccount(cash=20000.0)
    marks = {"A/USDT:USDT": 100.0, "B/USDT:USDT": 10.0}
    costs = {s: CostInputs(adv_usd=1e9) for s in marks}
    # open the book at 07:00, funding clock starts there
    t0 = datetime(2026, 7, 10, 7, 0, tzinfo=UTC)
    reconcile_book(acct, _neutral_book(), marks=marks, costs=costs, betas={},
                   now=t0, cycle=1, cadence="rebal",
                   funding_by_symbol={"A/USDT:USDT": 0.0001, "B/USDT:USDT": 0.0001},
                   funding_intervals={"A/USDT:USDT": 8, "B/USDT:USDT": 8})
    assert acct.last_funding_ts == t0
    # next cycle crosses the 08:00 UTC boundary: short B RECEIVES, long A PAYS (same rate,
    # same notional -> net 0); use asymmetric rates to get a measurable net credit.
    t1 = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)
    rep = reconcile_book(acct, _neutral_book(), marks=marks, costs=costs, betas={},
                         now=t1, cycle=2, cadence="rebal",
                         funding_by_symbol={"A/USDT:USDT": -0.0001, "B/USDT:USDT": 0.0003},
                         funding_intervals={"A/USDT:USDT": 8, "B/USDT:USDT": 8})
    # long A at negative funding RECEIVES 9000*0.0001; short B at positive RECEIVES 9000*0.0003
    assert rep.funding_settled_cycle == pytest.approx(9000 * 0.0001 + 9000 * 0.0003, rel=1e-6)
    assert acct.funding_received > 0 and acct.last_funding_ts == t1


# ---------- fabricated close removal ----------

def test_held_symbol_without_mark_halts_instead_of_fabricating():
    acct = PaperAccount(cash=20000.0)
    acct.positions["GONE/USDT:USDT"] = Position(
        symbol="GONE/USDT:USDT", direction="long", qty=100.0, entry_price=50.0, opened_ts=NOW)
    with pytest.raises(ValueError, match="no mark"):
        acct.apply_fills([], marks={}, costs={})


# ---------- friction visibility ----------

def test_cycle_report_frictions_tie_to_account_deltas():
    acct = PaperAccount(cash=20000.0)
    marks = {"A/USDT:USDT": 100.0, "B/USDT:USDT": 10.0}
    costs = {s: CostInputs(adv_usd=1e9, half_spread_bps=1.0) for s in marks}
    rep = reconcile_book(acct, _neutral_book(), marks=marks, costs=costs, betas={},
                         now=NOW, cycle=1, cadence="rebal")
    assert rep.turnover_usd == pytest.approx(18000.0)
    assert rep.fees_paid_cycle == pytest.approx(acct.fees_paid)
    assert rep.slippage_paid_cycle == pytest.approx(acct.slippage_paid)
    assert rep.ran_at and rep.decision_ts == NOW.isoformat()
    # resend the identical book -> zero new frictions, zero turnover
    rep2 = reconcile_book(acct, _neutral_book(), marks=marks, costs=costs, betas={},
                          now=NOW, cycle=2, cadence="rebal")
    assert rep2.turnover_usd == pytest.approx(0.0)
    assert rep2.fees_paid_cycle == pytest.approx(0.0)
    assert rep2.slippage_paid_cycle == pytest.approx(0.0)


def test_flip_frictions_split_between_old_and_new_leg():
    acct = PaperAccount(cash=20000.0)
    marks = {"A/USDT:USDT": 100.0}
    costs = {"A/USDT:USDT": CostInputs(adv_usd=1e9, half_spread_bps=1.0)}
    acct.apply_fills([{"symbol": "A/USDT:USDT", "direction": "long", "target_notional": 5000.0}],
                     marks, costs, opened_ts=NOW, opened_cycle=1)
    fees_after_open = acct.fees_paid
    # flip long $5K -> short $3K: traded delta = $8K; old leg closes $5K, new opens $3K
    acct.apply_fills([{"symbol": "A/USDT:USDT", "direction": "short", "target_notional": 3000.0}],
                     marks, costs, opened_ts=NOW, opened_cycle=2)
    flip_fees = acct.fees_paid - fees_after_open
    closed = acct.drain_closed_legs()
    assert len(closed) == 1
    new_pos = acct.positions["A/USDT:USDT"]
    assert new_pos.direction == "short"
    # 5/8 of the flip fee on the closed old leg (plus its open fee), 3/8 on the new leg
    assert new_pos.accrued_fees == pytest.approx(flip_fees * 3000.0 / 8000.0, rel=1e-9)
    assert closed[0].fees == pytest.approx(
        fees_after_open + flip_fees * 5000.0 / 8000.0, rel=1e-9)


# ---------- timestamp integrity ----------

def test_equity_log_rejects_non_monotonic_ts(tmp_path):
    record_equity(tmp_path, NOW, 20000.0, 1)
    with pytest.raises(ValueError, match="non-monotonic"):
        record_equity(tmp_path, NOW - timedelta(hours=1), 19000.0, 2)
    # same-cycle RETRY replaces its own point and stays legal
    record_equity(tmp_path, NOW + timedelta(minutes=1), 20001.0, 1)


# ---------- reflector evidence guard ----------

def test_reflector_edit_citing_unscored_past_cycle_is_refused():
    # cites c2/c3 as measured evidence but neither has a ScoreRecord -> fabrication, refused
    edit = {"role": "futures", "region_text": "- [c5] edge was bad, be careful",
            "reason": "c2 and c3 showed negative edge", "evidence": ["cycle 3 edge=-0.04"]}
    refusal = validate_edit_citations(edit, cycles={1, 4}, current_cycle=5)
    assert refusal and "2" in refusal and "3" in refusal


def test_reflector_edit_citing_scored_past_cycle_passes():
    edit = {"role": "futures", "region_text": "- [c5] c1-c3 hi-conv lost money",
            "reason": "", "evidence": ["c1 edge=-0.01", "c2 edge=-0.02", "c3 edge=-0.03"]}
    assert validate_edit_citations(edit, cycles={1, 2, 3}, current_cycle=5) is None


def test_reflector_current_and_future_cycle_tags_are_allowed():
    # the [c4] date tag == current cycle, and retire_if 'by c12' is future — neither is a
    # past-score claim, so neither may be flagged even though the scorecard lacks them.
    edit = {"role": "futures",
            "region_text": "- [c4] futures hi-conv lost money c1-c3; reserve conviction>0.5 "
                           "for corroborated extremes. retire_if: hi_conv_hit>=0.5 by c12",
            "reason": "specialist_miscalibrated: futures",
            "evidence": ["c1 edge=-0.0006", "c2 edge=-0.0004", "c3 edge=-0.0020"]}
    assert validate_edit_citations(edit, cycles={1, 2, 3}, current_cycle=4) is None


# ---------- watchdog ----------

def test_watchdog_classification():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "desk_watchdog", Path(__file__).resolve().parents[1] / "scripts" / "desk_watchdog.py")
    wd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wd)
    assert wd.classify(0.5) == "EARLY"
    assert wd.classify(8.0) == "ON_TIME"
    assert wd.classify(9.5) == "ON_TIME"
    assert wd.classify(11.0) == "LATE"
    assert wd.classify(17.0) == "MISSED_2"
    assert wd.classify(25.0) == "MISSED_3"
