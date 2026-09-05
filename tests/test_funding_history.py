from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from futures_fund.account import PaperAccount, Position
from futures_fund.funding_history import (
    FundingHistoryError,
    collect_funding_events,
    resolve_previous_intervals,
    verify_execution_window_funding_safety,
)

PREVIOUS = datetime(2026, 8, 18, 8, 7, tzinfo=UTC)
NOW = datetime(2026, 8, 19, 0, 7, tzinfo=UTC)


class _Exchange:
    def __init__(self, events):
        self.events = events

    def funding_history(self, symbol, *, since_ms, limit):
        assert symbol == "A"
        assert since_ms > int(PREVIOUS.timestamp() * 1000)
        assert limit >= 2
        return self.events


def test_collect_requires_every_expected_boundary_and_preserves_values():
    events = [
        {"timestamp": datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
         "rate": 0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    out = collect_funding_events(
        _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 8},
        previous_intervals={"A": 8},
    )
    assert out["A"] == events


def test_collect_requires_explicit_current_interval_metadata():
    with pytest.raises(FundingHistoryError, match="current funding interval is unproven"):
        collect_funding_events(
            _Exchange([]),
            {"A"},
            previous_ts=PREVIOUS,
            now=NOW,
            intervals={},
            previous_intervals={"A": 8},
        )


def test_collect_accepts_small_positive_exchange_timestamp_lag_and_preserves_it():
    lagged = datetime(2026, 8, 18, 16, 0, tzinfo=UTC) + timedelta(milliseconds=4)
    events = [
        {"timestamp": lagged, "rate": 0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    out = collect_funding_events(
        _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 8},
        previous_intervals={"A": 8},
    )
    assert out["A"] == events
    assert out["A"][0]["timestamp"] == lagged


def test_collect_rejects_event_beyond_strict_boundary_lag():
    events = [
        {"timestamp": datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
         + timedelta(seconds=1, microseconds=1), "rate": 0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    with pytest.raises(FundingHistoryError, match="not a nominal boundary"):
        collect_funding_events(
            _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 8},
            previous_intervals={"A": 8},
        )


def test_collect_rejects_two_events_for_one_nominal_boundary():
    boundary = datetime(2026, 8, 18, 16, 0, tzinfo=UTC)
    events = [
        {"timestamp": boundary, "rate": 0.0001, "mark": 10.0},
        {"timestamp": boundary + timedelta(milliseconds=4),
         "rate": 0.0002, "mark": 10.1},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    with pytest.raises(FundingHistoryError, match="multiple funding events map"):
        collect_funding_events(
            _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 8},
            previous_intervals={"A": 8},
        )


def test_collect_missing_boundary_fails_closed():
    events = [{"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
               "rate": -0.0002, "mark": 11.0}]
    with pytest.raises(FundingHistoryError, match="missing boundaries"):
        collect_funding_events(
            _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 8},
            previous_intervals={"A": 8},
        )


def test_8h_to_4h_transition_after_last_boundary_is_proven_and_recorded():
    events = [
        {"timestamp": datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
         "rate": 0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    proof = {}
    out = collect_funding_events(
        _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 4},
        previous_intervals={"A": 8}, proof_out=proof,
    )
    assert out["A"] == events
    assert proof["A"]["kind"] == "single_transition"
    assert "after_last_boundary_before_window_end" in proof["A"][
        "candidate_first_new_boundaries"
    ]


def test_extra_boundary_under_stable_interval_fails_closed():
    events = [
        {"timestamp": datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
         "rate": 0.0003, "mark": 9.5},
        {"timestamp": datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
         "rate": 0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    with pytest.raises(FundingHistoryError, match="unexpected boundaries"):
        collect_funding_events(
            _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 8},
            previous_intervals={"A": 8},
        )


def test_4h_to_8h_transition_is_proven_without_permanent_dense_schedule_halt():
    events = [
        {"timestamp": datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
         "rate": 0.0003, "mark": 9.5},
        {"timestamp": datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
         "rate": 0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    proof = {}
    assert collect_funding_events(
        _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW,
        intervals={"A": 8}, previous_intervals={"A": 4}, proof_out=proof,
    )["A"] == events
    assert proof["A"]["kind"] == "single_transition"
    assert proof["A"]["prior_interval_h"] == 4
    assert proof["A"]["current_interval_h"] == 8


def test_changed_interval_missing_common_boundary_cannot_be_explained_by_one_switch():
    events = [
        {"timestamp": datetime(2026, 8, 18, 12, 0, tzinfo=UTC),
         "rate": 0.0003, "mark": 9.5},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    with pytest.raises(FundingHistoryError, match="cannot prove a single 4h->8h"):
        collect_funding_events(
            _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW,
            intervals={"A": 8}, previous_intervals={"A": 4},
        )


def test_8h_to_4h_interior_transition_is_proven():
    events = [
        {"timestamp": datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
         "rate": 0.0001, "mark": 10.0},
        {"timestamp": datetime(2026, 8, 18, 20, 0, tzinfo=UTC),
         "rate": 0.0004, "mark": 10.5},
        {"timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
         "rate": -0.0002, "mark": 11.0},
    ]
    proof = {}
    assert collect_funding_events(
        _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW,
        intervals={"A": 4}, previous_intervals={"A": 8}, proof_out=proof,
    )["A"] == events
    assert proof["A"]["kind"] == "single_transition"
    assert datetime(2026, 8, 18, 20, 0, tzinfo=UTC).isoformat() in proof["A"][
        "candidate_first_new_boundaries"
    ]


def test_duplicate_raw_funding_timestamp_fails_closed():
    event = {"timestamp": datetime(2026, 8, 18, 16, 0, tzinfo=UTC),
             "rate": 0.0001, "mark": 10.0}
    events = [event, dict(event), {
        "timestamp": datetime(2026, 8, 19, 0, 0, tzinfo=UTC),
        "rate": -0.0002,
        "mark": 11.0,
    }]
    with pytest.raises(FundingHistoryError, match="duplicate funding event timestamp"):
        collect_funding_events(
            _Exchange(events), {"A"}, previous_ts=PREVIOUS, now=NOW, intervals={"A": 8},
            previous_intervals={"A": 8},
        )


def test_funding_history_paginates_until_exhausted():
    previous = datetime(2026, 7, 1, 0, 7, tzinfo=UTC)
    events = [
        {
            "timestamp": previous.replace(minute=0) + timedelta(hours=8 * i),
            "rate": 0.0001,
            "mark": 10.0,
        }
        for i in range(1, 1002)
    ]
    now = events[-1]["timestamp"]

    class Paginated:
        calls = 0

        def funding_history(self, symbol, *, since_ms, limit):
            self.calls += 1
            eligible = [
                event for event in events
                if int(event["timestamp"].timestamp() * 1000) >= since_ms
            ]
            return eligible[:limit]

    exchange = Paginated()
    out = collect_funding_events(
        exchange, {"A"}, previous_ts=previous, now=now, intervals={"A": 8},
        previous_intervals={"A": 8},
    )
    assert len(out["A"]) == 1001
    assert exchange.calls == 2


def test_legacy_account_bootstraps_only_from_exact_clock_and_book_audit(tmp_path):
    account = PaperAccount(
        cash=20_000.0,
        last_funding_ts=NOW,
        positions={
            "A": Position(
                symbol="A", direction="long", qty=2.0, entry_price=10.0,
                opened_ts=PREVIOUS,
            )
        },
    )
    path = tmp_path / "portfolio-heartbeats.jsonl"
    path.write_text(json.dumps({
        "ts": NOW.isoformat(), "kind": "token_free_funding_heartbeat",
        "paper_only": True, "actions": "none",
        "positions": [{
            "symbol": "A", "side": "long", "qty": 2.0, "funding_interval_h": 4,
        }],
    }) + "\n")
    assert resolve_previous_intervals(tmp_path, account) == {"A": 4}

    account.positions["A"].qty = 3.0
    with pytest.raises(FundingHistoryError, match="no exact heartbeat audit"):
        resolve_previous_intervals(tmp_path, account)


def test_partial_persisted_intervals_must_agree_with_legacy_audit(tmp_path):
    account = PaperAccount(
        cash=20_000.0,
        last_funding_ts=NOW,
        funding_intervals_observed={"A": 8},
        positions={
            symbol: Position(
                symbol=symbol, direction="long", qty=2.0, entry_price=10.0,
                opened_ts=PREVIOUS,
            )
            for symbol in ("A", "B")
        },
    )
    (tmp_path / "portfolio-heartbeats.jsonl").write_text(json.dumps({
        "ts": NOW.isoformat(), "kind": "token_free_funding_heartbeat",
        "paper_only": True, "actions": "none",
        "positions": [
            {"symbol": "A", "side": "long", "qty": 2.0, "funding_interval_h": 4},
            {"symbol": "B", "side": "long", "qty": 2.0, "funding_interval_h": 8},
        ],
    }) + "\n")
    with pytest.raises(FundingHistoryError, match="conflict with exact heartbeat"):
        resolve_previous_intervals(tmp_path, account)


def test_collector_refuses_missing_prior_interval_observation():
    with pytest.raises(FundingHistoryError, match="prior funding interval is unproven"):
        collect_funding_events(
            _Exchange([]), {"A"}, previous_ts=PREVIOUS, now=PREVIOUS,
            intervals={"A": 8}, previous_intervals={},
        )


def test_production_callers_persist_interval_coverage_proofs():
    reconcile = Path("scripts/desk_reconcile.py").read_text()
    heartbeat = Path("scripts/desk_heartbeat.py").read_text()
    for source in (reconcile, heartbeat):
        assert "proof_out=funding_interval_proofs" in source
        assert '"funding_interval_proofs"' in source


def test_held_drop_execution_window_cannot_straddle_exact_funding_boundary():
    before = datetime(2026, 9, 5, 7, 59, 59, tzinfo=UTC)
    after = datetime(2026, 9, 5, 8, 0, 1, tzinfo=UTC)
    held_proof = {
        "schema_version": 1,
        "kind": "stable",
        "window_start": datetime(2026, 9, 5, 0, 7, tzinfo=UTC).isoformat(),
        "window_end": after.isoformat(),
        "prior_interval_h": 8,
        "current_interval_h": 8,
        "observed_nominal_boundaries": [
            datetime(2026, 9, 5, 8, 0, tzinfo=UTC).isoformat()
        ],
        "expected_nominal_boundaries": [
            datetime(2026, 9, 5, 8, 0, tzinfo=UTC).isoformat()
        ],
        "candidate_first_new_boundaries": [],
    }

    with pytest.raises(FundingHistoryError, match="straddle funding boundary for DROP"):
        verify_execution_window_funding_safety(
            {"DROP": before, "RETAIN": after},
            {"DROP", "RETAIN"},
            current_intervals={"DROP": 8, "RETAIN": 8},
            decision_intervals={"DROP": 8, "RETAIN": 8},
            held_interval_proofs={"DROP": held_proof, "RETAIN": held_proof},
        )


def test_new_entry_execution_window_cannot_straddle_current_funding_boundary():
    before = datetime(2026, 9, 5, 7, 59, 59, tzinfo=UTC)
    after = datetime(2026, 9, 5, 8, 0, 1, tzinfo=UTC)

    with pytest.raises(FundingHistoryError, match="straddle funding boundary for ENTRY"):
        verify_execution_window_funding_safety(
            {"ENTRY": before, "OTHER": after},
            {"ENTRY", "OTHER"},
            current_intervals={"ENTRY": 8, "OTHER": 8},
            decision_intervals={"ENTRY": 8, "OTHER": 8},
            held_interval_proofs={},
        )


def test_execution_window_funding_proof_is_auditable_when_no_boundary_crossed():
    first = datetime(2026, 9, 5, 8, 0, 1, tzinfo=UTC)
    last = datetime(2026, 9, 5, 8, 0, 2, tzinfo=UTC)

    proof = verify_execution_window_funding_safety(
        {"ENTRY": first, "OTHER": last},
        {"ENTRY", "OTHER"},
        current_intervals={"ENTRY": 8, "OTHER": 4},
        decision_intervals={"ENTRY": 8, "OTHER": 4},
        held_interval_proofs={},
    )

    assert proof == {
        "schema_version": 1,
        "safe": True,
        "window_start": first.isoformat(),
        "window_end": last.isoformat(),
        "symbols": {
            "ENTRY": {
                "source": "decision_and_execution_interval_union",
                "decision_interval_h": 8,
                "current_interval_h": 8,
                "applicable_boundaries_in_window": [],
            },
            "OTHER": {
                "source": "decision_and_execution_interval_union",
                "decision_interval_h": 4,
                "current_interval_h": 4,
                "applicable_boundaries_in_window": [],
            },
        },
    }


def test_exact_boundary_observation_is_ordering_ambiguous_and_halts():
    boundary = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)

    with pytest.raises(FundingHistoryError, match="straddle funding boundary for ENTRY"):
        verify_execution_window_funding_safety(
            {"ENTRY": boundary},
            {"ENTRY"},
            current_intervals={"ENTRY": 8},
            decision_intervals={"ENTRY": 8},
            held_interval_proofs={},
        )


@pytest.mark.parametrize(
    ("decision_interval", "current_interval"),
    [(4, 8), (8, 4)],
)
def test_new_entry_interval_transition_checks_union_of_both_schedules(
    decision_interval, current_interval
):
    before = datetime(2026, 9, 5, 11, 59, 59, tzinfo=UTC)
    after = datetime(2026, 9, 5, 12, 0, 1, tzinfo=UTC)

    with pytest.raises(FundingHistoryError, match="straddle funding boundary for ENTRY"):
        verify_execution_window_funding_safety(
            {"ENTRY": before, "OTHER": after},
            {"ENTRY", "OTHER"},
            current_intervals={
                "ENTRY": current_interval,
                "OTHER": current_interval,
            },
            decision_intervals={
                "ENTRY": decision_interval,
                "OTHER": decision_interval,
            },
            held_interval_proofs={},
        )


def test_reconcile_binds_decision_intervals_and_persists_execution_window_proof():
    source = Path("scripts/desk_reconcile.py").read_text()

    assert "decision_intervals=" in source
    assert 'row["funding_interval_h"]' in source
    assert '"funding_execution_window_proof"' in source
