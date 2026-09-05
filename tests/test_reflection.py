import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

import pytest

from futures_fund.account import PaperAccount
from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.desk_contracts import ReflectionProposal
from futures_fund.durable_io import canonical_json_sha256
from futures_fund.prompt_guard import BEGIN, END, splice_managed, split_managed
from futures_fund.reconcile_commit import (
    recover_reconcile_transaction,
    stage_reconcile_transaction,
)
from futures_fund.reflection import (
    _adversary_recovery_recurrences,
    _build_score_record,
    _filter_recurrences,
    _marks_sha256,
    _pm_gate_inactive_recurrences,
    apply_reflection,
    audit_managed_region_provenance,
    bootstrap_reflector_heads,
    canonical_daily_score_observation,
    mark_recurrences_handled,
    persist_decision_snapshot,
    read_candidate_scorecard,
    read_forecast_scorecard,
    reflector_heads_path,
    score_mature_leg_forecasts,
    score_previous_cycle,
    score_record_is_manifest_bound,
    scored_cycles,
    write_reflection_authority,
)
from futures_fund.scorecard import BookScore, Recurrence, ScoreRecord
from futures_fund.state_transaction import current_account_sha256
from scripts.desk_score import (
    _recover_reflection_authority_packet,
    _seal_recurrences,
    score_eligible_completed_cycles,
)


def _seed_cycle(
    state_dir,
    cycle,
    *,
    marks,
    betas,
    reads,
    book,
    adversary,
    funding_bps=None,
    report=None,
):
    funding_bps = funding_bps or {}
    evidence_ts = report.get("decision_ts") if isinstance(report, dict) else None
    ev = [
        {
            "symbol": s,
            "mark": marks[s],
            "beta_btc": betas.get(s, 1.0),
            "beta_clamped": betas.get(s, 1.0),
            "expected_funding_8h_bps": funding_bps.get(s, 0.0),
            **({"as_of_ts": evidence_ts} if evidence_ts is not None else {}),
        }
        for s in marks
    ]
    save_output(state_dir, cycle, "evidence", ev, cadence="rebal")
    save_output(state_dir, cycle, "reads", reads, cadence="rebal")
    save_output(state_dir, cycle, "book", book, cadence="rebal")
    save_output(state_dir, cycle, "adversary", adversary, cadence="rebal")
    if report is not None:
        save_output(state_dir, cycle, "report", report, cadence="rebal")


def _commit_seeded_cycle(
    state_dir,
    cycle,
    scoring_marks=None,
    *,
    normalize_scoring_evidence: bool = True,
    meta_now: str | None = None,
):
    directory = cycle_dir(state_dir, cycle, cadence="rebal")
    artifacts = {
        name: json.loads((directory / f"{name}.json").read_text())
        for name in ("evidence", "reads", "book", "adversary", "report")
    }
    if scoring_marks is not None:
        artifacts["scoring_marks"] = scoring_marks
        observation_ts = scoring_marks["as_of_ts"]
        observation_marks = scoring_marks["marks"]
        evidence = []
        for row in artifacts["evidence"]:
            bound_row = {**row, "as_of_ts": observation_ts}
            if normalize_scoring_evidence and bound_row["symbol"] in observation_marks:
                bound_row["mark"] = observation_marks[bound_row["symbol"]]
            evidence.append(bound_row)
        artifacts["evidence"] = evidence
        artifacts["meta"] = {
            "cycle": cycle,
            "now": meta_now or observation_ts,
            "btc_symbol": "BTC/USDT:USDT",
            "scoring_marks_sha256": canonical_json_sha256(scoring_marks),
            "evidence_sha256": canonical_json_sha256(evidence),
        }
    entry_gate_policy = directory / "entry_gate_policy.json"
    if entry_gate_policy.exists():
        artifacts["entry_gate_policy"] = json.loads(entry_gate_policy.read_text())
    timestamp = datetime.fromisoformat(
        scoring_marks["as_of_ts"]
        if scoring_marks is not None
        else artifacts["report"]["decision_ts"]
    )
    stage_reconcile_transaction(
        state_dir,
        expected_base_account_sha256=current_account_sha256(state_dir),
        cycle=cycle,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0),
        artifacts=artifacts,
        equity_ts=timestamp,
        equity=20_000.0,
        ledger={
            "cycle": cycle,
            "opening_equity": 20_000.0,
            "closing_equity": 20_000.0,
        },
    )
    recover_reconcile_transaction(state_dir)


def _seed_gate_cycle(
    state_dir,
    cycle: int,
    *,
    timestamp: str,
    structured: bool,
    explicit_empty_candidates: bool = False,
) -> None:
    symbol = "A/USDT:USDT"
    btc = "BTC/USDT:USDT"
    marks = {symbol: 100.0 + max(0, cycle - 1) * 2.0, btc: 100.0}
    reads = {
        "sentiment": [
            {
                "symbol": symbol,
                "lean": "flat",
                "conviction": 0.0,
                "rationale": "none",
                "evidence": [],
            },
            {
                "symbol": btc,
                "lean": "flat",
                "conviction": 0.0,
                "rationale": "benchmark",
                "evidence": [],
            },
        ],
        "technical": [
            {
                "symbol": symbol,
                "lean": "long",
                "conviction": 0.7,
                "rationale": "trend",
                "evidence": [],
            },
            {
                "symbol": btc,
                "lean": "flat",
                "conviction": 0.0,
                "rationale": "benchmark",
                "evidence": [],
            },
        ],
        "futures": [
            {
                "symbol": symbol,
                "lean": "flat",
                "conviction": 0.1,
                "rationale": "none",
                "evidence": [],
            },
            {
                "symbol": btc,
                "lean": "flat",
                "conviction": 0.0,
                "rationale": "benchmark",
                "evidence": [],
            },
        ],
    }
    book = {
        "legs": [],
        "turnover_justification": (
            "No discretionary non-BTC entry satisfies the active managed entry gate."
        ),
        "notes": "explicit PM cash decision",
    }
    if structured:
        book["candidate_reviews"] = [
            {
                "symbol": symbol,
                "side": "long",
                "status": "rejected",
                "exclusion_reason": "entry_gate",
                "expected_price_edge_frac": 0.01,
                "edge_horizon_hours": 24,
                "counterfactual_notional": 1_000.0,
                "supporting_specialists": [
                    {"role": "technical", "lean": "long", "conviction": 0.7}
                ],
                "rationale": "PM declared the managed gate as the exclusion cause",
            }
        ]
    elif explicit_empty_candidates:
        book["candidate_reviews"] = []
    _seed_cycle(
        state_dir,
        cycle,
        marks=marks,
        betas={symbol: 0.0, btc: 1.0},
        reads=reads,
        book=book,
        adversary={"accept": True},
        report={
            "cycle": cycle,
            "decision_ts": timestamp,
            "funding_settled_cycle": 0.0,
        },
    )
    managed_region = "\n- active managed entry calibration\n"
    policy = {
        "source": "agents/pm.md",
        "managed_region": managed_region,
        "sha256": sha256(
            json.dumps(managed_region, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    save_output(state_dir, cycle, "entry_gate_policy", policy, cadence="rebal")
    _commit_seeded_cycle(
        state_dir,
        cycle,
        {
            "as_of_ts": timestamp,
            "marks": marks,
        },
    )


def test_pm_gate_inactive_accepts_only_explicit_bound_legacy_gate_declarations(tmp_path):
    state = str(tmp_path / "state")
    for cycle in (1, 2, 3):
        _seed_gate_cycle(
            state,
            cycle,
            timestamp=f"2026-08-0{cycle}T00:07:00+00:00",
            structured=False,
        )

    recurrence = _pm_gate_inactive_recurrences(
        state, through_cycle=3, cadence="rebal"
    )

    assert len(recurrence) == 1
    assert recurrence[0].kind == "pm_gate_inactive"
    assert recurrence[0].count == 3
    assert all(
        "legacy_manifest_bound_explicit_pm_gate_declaration" in row
        for row in recurrence[0].evidence
    )
    assert all("declaration_sha256" in row for row in recurrence[0].evidence)
    assert "Never select a candidate" in recurrence[0].suggestion


def test_new_empty_candidate_ledger_cannot_use_the_legacy_gate_adapter(tmp_path):
    state = str(tmp_path / "state")
    for cycle in (1, 2, 3):
        _seed_gate_cycle(
            state,
            cycle,
            timestamp=f"2026-08-0{cycle}T00:07:00+00:00",
            structured=False,
            explicit_empty_candidates=cycle == 2,
        )

    assert _pm_gate_inactive_recurrences(state, through_cycle=3, cadence="rebal") == []


def test_structured_gate_candidates_are_scored_at_the_bound_scheduled_horizon(tmp_path):
    state = str(tmp_path / "state")
    memory = str(tmp_path / "memory")
    _seed_gate_cycle(
        state,
        1,
        timestamp="2026-08-01T00:07:00+00:00",
        structured=True,
    )
    _seed_gate_cycle(
        state,
        2,
        timestamp="2026-08-02T00:07:00+00:00",
        structured=True,
    )

    result = score_mature_leg_forecasts(
        state,
        memory,
        through_cycle=2,
        btc_symbol="BTC/USDT:USDT",
    )

    assert result["new_candidate_shadow_scores"] == 1
    assert result["new_gate_declared_shadow_scores"] == 1
    rows = read_candidate_scorecard(
        Path(memory) / "candidate-scorecard.jsonl",
        state_dir=state,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["origin_cycle"] == 1
    assert row["gate_causal_claim"] is True
    assert row["horizon_label_eligible"] is True
    assert row["learning_eligible"] is False
    assert row["realized_selected_edge_frac"] == pytest.approx(0.02)
    assert len(row["candidate_sha256"]) == 64
    assert len(row["book_sha256"]) == 64
    assert len(row["entry_gate_policy_sha256"]) == 64

    original = (Path(memory) / "candidate-scorecard.jsonl").read_text()
    forged = {**row, "realized_selected_edge_frac": 9.0}
    (Path(memory) / "candidate-scorecard.jsonl").write_text(json.dumps(forged) + "\n")
    with pytest.raises(ValueError, match="invalid candidate score row"):
        read_candidate_scorecard(
            Path(memory) / "candidate-scorecard.jsonl",
            state_dir=state,
        )
    (Path(memory) / "candidate-scorecard.jsonl").write_text(original)


def test_persist_decision_snapshot_writes_evidence(tmp_path):
    pending = tmp_path / "mem" / "pending"
    pending.mkdir(parents=True)
    ev = [{"symbol": "A", "mark": 1.0, "beta_btc": 1.0}]
    persist_decision_snapshot(str(tmp_path / "st"), 5, evidence=ev, pending_dir=pending)
    saved = json.loads(
        (cycle_dir(str(tmp_path / "st"), 5, cadence="rebal") / "evidence.json").read_text()
    )
    assert saved[0]["symbol"] == "A"


def test_persist_snapshot_copies_original_book_when_present(tmp_path):
    pending = tmp_path / "mem" / "pending"
    pending.mkdir(parents=True)
    (pending / "pm_book_original.json").write_text(json.dumps({"legs": [], "notes": "orig"}))
    persist_decision_snapshot(str(tmp_path / "st"), 5, evidence=[], pending_dir=pending)
    orig = cycle_dir(str(tmp_path / "st"), 5, cadence="rebal") / "book_original.json"
    assert json.loads(orig.read_text())["notes"] == "orig"


def test_score_previous_cycle_no_prev_is_noop(tmp_path):
    res = score_previous_cycle(
        str(tmp_path / "st"),
        str(tmp_path / "mem"),
        scored_cycle=0,
        cur_marks={"A": 1.0},
        now="t",
        btc_symbol="BTC/USDT:USDT",
    )
    assert res["scored_cycle"] is None
    assert json.loads((tmp_path / "mem" / "pending" / "recurrences.json").read_text()) == []


def test_scoring_never_rewrites_away_a_malformed_scorecard_row(tmp_path):
    st = str(tmp_path / "st")
    mem = Path(tmp_path / "mem")
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 50_000.0},
        betas={"A": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
    )
    mem.mkdir()
    path = mem / "scorecard.jsonl"
    original = '{"cycle":1}\n{malformed score}\n'
    path.write_text(original)

    with pytest.raises(ValueError, match="invalid scorecard row"):
        score_previous_cycle(
            st,
            str(mem),
            scored_cycle=1,
            cur_marks={"A": 101.0, "BTC/USDT:USDT": 50_000.0},
            now="t",
            btc_symbol="BTC/USDT:USDT",
        )
    assert path.read_text() == original


def test_score_previous_cycle_scores_and_appends(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    reads = {
        "sentiment": [
            {"symbol": "A", "lean": "long", "conviction": 0.9, "rationale": "x", "evidence": []}
        ],
        "technical": [],
        "futures": [],
    }
    book = {
        "legs": [{"symbol": "A", "side": "long", "target_notional": 1000.0, "rationale": ""}],
        "stated_deploy_frac": 0.9,
        "stated_dollar_residual_frac": 0.0,
        "stated_beta_residual": 0.0,
        "notes": "",
    }
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 60000.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads=reads,
        book=book,
        adversary={"accept": True, "objections": [], "demanded_changes": []},
        funding_bps={"A": -2.0},
        report={"fees_paid_cycle": 1.0, "slippage_paid_cycle": 0.5},
    )
    # A rose 10% -> the long call was right; edge positive
    res = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 110.0, "BTC/USDT:USDT": 60000.0},
        now="t",
        btc_symbol="BTC/USDT:USDT",
        funding_horizon_events=3.0,
    )
    assert res["scored_cycle"] == 1
    line = json.loads((Path(mem) / "scorecard.jsonl").read_text().splitlines()[0])
    assert line["cycle"] == 1
    assert line["specialist_return_label"] == "btc_beta_adjusted"
    assert line["specialists"]["sentiment"]["hit_rate"] == 1.0
    attribution = json.loads((cycle_dir(st, 1, cadence="rebal") / "attribution.json").read_text())
    assert attribution["book"]["gross_pnl"] == pytest.approx(100.0)  # 1000 * 0.10
    assert attribution["book"]["projected_funding_pnl"] == pytest.approx(0.6)
    assert attribution["book"]["entry_friction"] == pytest.approx(1.5)
    assert attribution["book"]["strategy_net_edge"] == pytest.approx(99.1)
    assert attribution["book"]["realized_edge_ex_funding"] == pytest.approx(98.5)


def test_specialists_are_scored_on_beta_adjusted_alpha_and_btc_is_excluded(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    reads = {
        "sentiment": [
            {"symbol": "A", "lean": "long", "conviction": 1.0, "rationale": "x", "evidence": []},
            {
                "symbol": "BTC/USDT:USDT",
                "lean": "long",
                "conviction": 1.0,
                "rationale": "benchmark",
                "evidence": [],
            },
        ],
        "technical": [],
        "futures": [],
    }
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 100.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads=reads,
        book={"legs": []},
        adversary={"accept": True},
    )
    score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 105.0, "BTC/USDT:USDT": 110.0},
        now="2026-08-20T00:07:00+00:00",
        btc_symbol="BTC/USDT:USDT",
    )
    record = json.loads((Path(mem) / "scorecard.jsonl").read_text())
    score = record["specialists"]["sentiment"]
    assert score["n_available"] == 1
    assert score["n_scored"] == 1
    assert score["hit_rate"] == 0.0
    assert score["conv_weighted_edge"] == pytest.approx(-0.05)


def test_failed_scoring_is_caught_up_from_earliest_later_committed_marks(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    reads = {
        "sentiment": [
            {
                "symbol": "A",
                "lean": "long",
                "conviction": 1.0,
                "rationale": "test",
                "evidence": [],
            }
        ],
        "technical": [],
        "futures": [],
    }
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 100.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads=reads,
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 1, "decision_ts": "2026-08-01T00:07:00+00:00"},
    )
    _commit_seeded_cycle(st, 1)
    _seed_cycle(
        st,
        2,
        marks={"A": 101.0, "BTC/USDT:USDT": 100.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 2, "decision_ts": "2026-08-03T00:07:00+00:00"},
    )
    _commit_seeded_cycle(
        st,
        2,
        {
            "as_of_ts": "2026-08-03T00:07:00+00:00",
            "marks": {"A": 120.0, "BTC/USDT:USDT": 110.0},
        },
    )

    result = score_eligible_completed_cycles(
        st, mem, btc_symbol="BTC/USDT:USDT", active_calibration_roles=set()
    )

    assert result["scored_cycles"] == [1]
    assert result["unscored_waiting_for_committed_marks"] == [2]
    row = json.loads((Path(mem) / "scorecard.jsonl").read_text())
    assert row["cycle"] == 1
    assert row["scored_at"] == "2026-08-03T00:07:00+00:00"
    assert row["outcome_observation_cycle"] == 2
    assert row["outcome_provenance"] == "manifest_bound"
    assert row["specialists"]["sentiment"]["conv_weighted_edge"] == pytest.approx(0.10)
    assert result["off_horizon_scores_retained_for_audit"] == [
        {
            "origin_cycle": 1,
            "outcome_observation_cycle": 2,
            "evaluation_horizon_hours": 48.0,
            "scheduled_horizon_hours": 24.0,
            "learning_eligible": False,
        }
    ]

    # A row cannot gain authority merely by retaining valid source IDs while rewriting the
    # derived learning result.
    row["specialists"]["sentiment"]["conv_weighted_edge"] = 9.0
    (Path(mem) / "scorecard.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="invalid scorecard row"):
        scored_cycles(mem, state_dir=st)


def test_daily_score_accepts_small_scheduled_slot_jitter_and_keeps_actual_elapsed(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    origin = "2026-08-01T00:08:03+00:00"
    observation = "2026-08-02T00:07:53+00:00"
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 100.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 1, "decision_ts": origin},
    )
    _commit_seeded_cycle(st, 1)
    _seed_cycle(
        st,
        2,
        marks={"A": 101.0, "BTC/USDT:USDT": 100.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 2, "decision_ts": observation},
    )
    _commit_seeded_cycle(
        st,
        2,
        {
            "as_of_ts": observation,
            "marks": {"A": 101.0, "BTC/USDT:USDT": 100.0},
        },
    )

    result = score_eligible_completed_cycles(
        st, mem, btc_symbol="BTC/USDT:USDT", active_calibration_roles=set()
    )

    assert result["scored_cycles"] == [1]
    assert result["off_horizon_scores_retained_for_audit"] == []
    row = json.loads((Path(mem) / "scorecard.jsonl").read_text())
    assert row["evaluation_horizon_hours"] == pytest.approx(24.0 - 10.0 / 3600.0)
    assert row["scored_at"] == observation


def _seed_strict_daily_score(
    tmp_path,
    *,
    outcome_report_ts: str | None = None,
    outcome_meta_ts: str | None = None,
    outcome_evidence_marks: dict[str, float] | None = None,
    normalize_scoring_evidence: bool = True,
    score: bool = True,
):
    state = str(tmp_path / "state")
    memory = str(tmp_path / "memory")
    btc = "BTC/USDT:USDT"
    eth = "ETH/USDT:USDT"
    origin_ts = "2026-08-01T00:07:00+00:00"
    outcome_ts = "2026-08-02T00:07:00+00:00"
    reads = {
        "sentiment": [],
        "technical": [
            {"symbol": "A", "lean": "long", "conviction": 0.7, "rationale": "trend"},
            {"symbol": btc, "lean": "flat", "conviction": 0.0, "rationale": "benchmark"},
            {"symbol": eth, "lean": "flat", "conviction": 0.0, "rationale": "control"},
        ],
        "futures": [],
    }
    origin_marks = {"A": 100.0, btc: 100.0, eth: 50.0}
    _seed_cycle(
        state,
        1,
        marks=origin_marks,
        betas={"A": 0.5, btc: 1.0, eth: 0.8},
        reads=reads,
        book={"legs": [{"symbol": "A", "side": "long", "target_notional": 1_000.0}]},
        adversary={"accept": True},
        report={"cycle": 1, "decision_ts": origin_ts},
    )
    _commit_seeded_cycle(state, 1)
    outcome_marks = {"A": 110.0, btc: 102.0, eth: 55.0}
    _seed_cycle(
        state,
        2,
        marks=outcome_evidence_marks or outcome_marks,
        betas={"A": 0.5, btc: 1.0, eth: 0.8},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 2, "decision_ts": outcome_report_ts or outcome_ts},
    )
    _commit_seeded_cycle(
        state,
        2,
        {"as_of_ts": outcome_ts, "marks": outcome_marks},
        normalize_scoring_evidence=normalize_scoring_evidence,
        meta_now=outcome_meta_ts,
    )
    if not score:
        return state, memory, None, None, outcome_marks, outcome_ts
    result = score_eligible_completed_cycles(
        state, memory, btc_symbol=btc, active_calibration_roles=set()
    )
    assert result["scored_cycles"] == [1]
    path = Path(memory) / "scorecard.jsonl"
    return state, memory, path, json.loads(path.read_text()), outcome_marks, outcome_ts


def test_current_score_requires_every_serialized_field_and_rejects_extras(tmp_path):
    state, memory, path, row, _marks, _ts = _seed_strict_daily_score(tmp_path)
    assert score_record_is_manifest_bound(state, ScoreRecord.model_validate(row))

    mutations = []
    for top_field in ("book", "specialists", "decision_book_sha256", "decision_reads_sha256"):
        candidate = json.loads(json.dumps(row))
        candidate.pop(top_field)
        mutations.append(candidate)
    candidate = json.loads(json.dumps(row))
    candidate["book"].pop("decision_kind")
    mutations.append(candidate)
    candidate = json.loads(json.dumps(row))
    candidate["specialists"]["technical"].pop("conv_weighted_edge")
    mutations.append(candidate)
    candidate = json.loads(json.dumps(row))
    candidate["unrecognized_learning_claim"] = True
    mutations.append(candidate)

    for candidate in mutations:
        path.write_text(json.dumps(candidate) + "\n")
        with pytest.raises(ValueError, match="invalid scorecard row"):
            scored_cycles(memory, state_dir=state)
    path.write_text(json.dumps(row) + "\n")


@pytest.mark.parametrize(
    "fixture_kwargs",
    [
        {"outcome_report_ts": "2026-08-02T00:08:00+00:00"},
        {"outcome_meta_ts": "2026-08-02T00:08:00+00:00"},
    ],
    ids=("report-timestamp", "meta-timestamp"),
)
def test_canonical_daily_score_refuses_mismatched_outcome_timestamps(
    tmp_path, fixture_kwargs
):
    state, memory, _path, _row, marks, outcome_ts = _seed_strict_daily_score(
        tmp_path, score=False, **fixture_kwargs
    )

    with pytest.raises(ValueError, match="invalid committed scoring marks"):
        canonical_daily_score_observation(state, 1)
    scoring_sha256 = json.loads(
        (cycle_dir(state, 2, cadence="rebal") / "complete.json").read_text()
    )["manifest"]["artifact_sha256"]["scoring_marks"]
    with pytest.raises(ValueError, match="invalid committed scoring marks"):
        score_previous_cycle(
            state,
            memory,
            scored_cycle=1,
            cur_marks=marks,
            now=outcome_ts,
            btc_symbol="BTC/USDT:USDT",
            outcome_observation_cycle=2,
            outcome_scoring_marks_sha256=scoring_sha256,
        )


def test_canonical_daily_score_refuses_evidence_mark_conflicting_with_scoring_marks(
    tmp_path,
):
    state, memory, _path, _row, marks, outcome_ts = _seed_strict_daily_score(
        tmp_path,
        score=False,
        outcome_evidence_marks={
            "A": 109.0,
            "BTC/USDT:USDT": 102.0,
            "ETH/USDT:USDT": 55.0,
        },
        normalize_scoring_evidence=False,
    )

    with pytest.raises(ValueError, match="invalid committed scoring marks"):
        canonical_daily_score_observation(state, 1)
    scoring_sha256 = json.loads(
        (cycle_dir(state, 2, cadence="rebal") / "complete.json").read_text()
    )["manifest"]["artifact_sha256"]["scoring_marks"]
    with pytest.raises(ValueError, match="invalid committed scoring marks"):
        score_previous_cycle(
            state,
            memory,
            scored_cycle=1,
            cur_marks=marks,
            now=outcome_ts,
            btc_symbol="BTC/USDT:USDT",
            outcome_observation_cycle=2,
            outcome_scoring_marks_sha256=scoring_sha256,
        )


def test_current_score_json_rejects_duplicate_and_nonfinite_values(tmp_path):
    state, memory, path, row, _marks, _ts = _seed_strict_daily_score(tmp_path)
    canonical = json.dumps(row, separators=(",", ":"))
    malformed_rows = [
        canonical[:-1] + ',"cycle":1}',
        canonical.replace('"book":{', '"book":{"n_legs":0,', 1),
        canonical.replace('"evaluation_horizon_hours":24.0', '"evaluation_horizon_hours":NaN'),
        canonical.replace('"evaluation_horizon_hours":24.0', '"evaluation_horizon_hours":Infinity'),
    ]

    for malformed in malformed_rows:
        path.write_text(malformed + "\n")
        with pytest.raises(ValueError, match="invalid scorecard row"):
            scored_cycles(memory, state_dir=state)


@pytest.mark.parametrize(
    ("field_path", "wrong_value"),
    [
        (("score_schema_version",), 2.0),
        (("cycle",), "1"),
        (("adv_accepted",), 1),
        (("book", "n_legs"), "1"),
        (("specialists", "technical", "n_scored"), "1"),
    ],
)
def test_current_score_json_rejects_coercible_wrong_types(
    tmp_path, field_path, wrong_value
):
    state, memory, path, row, _marks, _ts = _seed_strict_daily_score(tmp_path)
    cursor = row
    for field in field_path[:-1]:
        cursor = cursor[field]
    cursor[field_path[-1]] = wrong_value
    path.write_text(json.dumps(row) + "\n")

    with pytest.raises(ValueError, match="invalid scorecard row"):
        scored_cycles(memory, state_dir=state)


def test_current_score_rejects_row_selected_benchmark_and_later_outcome(tmp_path, monkeypatch):
    import futures_fund.reflection as reflection

    state, _memory, _path, row, outcome_marks, outcome_ts = _seed_strict_daily_score(tmp_path)
    eth = "ETH/USDT:USDT"
    with monkeypatch.context() as context:
        context.setattr(reflection, "CANONICAL_BTC_SYMBOL", eth)
        alternate_benchmark = _build_score_record(
            state,
            scored_cycle=1,
            cur_marks=outcome_marks,
            now=outcome_ts,
            btc_symbol=eth,
            cadence="rebal",
            outcome_observation_cycle=2,
            outcome_scoring_marks_sha256=row["outcome_scoring_marks_sha256"],
            outcome_provenance="manifest_bound",
        )
    assert not score_record_is_manifest_bound(state, alternate_benchmark)

    later_ts = "2026-08-03T00:07:00+00:00"
    later_marks = {"A": 90.0, "BTC/USDT:USDT": 101.0, eth: 52.0}
    _seed_cycle(
        state,
        3,
        marks=later_marks,
        betas={"A": 0.5, "BTC/USDT:USDT": 1.0, eth: 0.8},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 3, "decision_ts": later_ts},
    )
    _commit_seeded_cycle(state, 3, {"as_of_ts": later_ts, "marks": later_marks})
    later_sha = json.loads(
        (cycle_dir(state, 3, cadence="rebal") / "complete.json").read_text()
    )["manifest"]["artifact_sha256"]["scoring_marks"]
    later = _build_score_record(
        state,
        scored_cycle=1,
        cur_marks=later_marks,
        now=later_ts,
        btc_symbol="BTC/USDT:USDT",
        cadence="rebal",
        outcome_observation_cycle=3,
        outcome_scoring_marks_sha256=later_sha,
        outcome_provenance="manifest_bound",
    )
    assert not score_record_is_manifest_bound(state, later)


def test_canonical_daily_score_skips_incomplete_mark_packet(tmp_path):
    state2 = str(tmp_path / "state")
    btc = "BTC/USDT:USDT"
    _seed_cycle(
        state2,
        1,
        marks={"A": 100.0, btc: 100.0},
        betas={"A": 1.0, btc: 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 1, "decision_ts": "2026-08-01T00:07:00+00:00"},
    )
    _commit_seeded_cycle(state2, 1)
    for cycle, ts, scoring in (
        (2, "2026-08-02T00:07:00+00:00", {btc: 102.0}),
        (3, "2026-08-03T00:07:00+00:00", {"A": 110.0, btc: 103.0}),
    ):
        _seed_cycle(
            state2,
            cycle,
            marks=scoring,
            betas={"A": 1.0, btc: 1.0},
            reads={"sentiment": [], "technical": [], "futures": []},
            book={"legs": []},
            adversary={"accept": True},
            report={"cycle": cycle, "decision_ts": ts},
        )
        _commit_seeded_cycle(state2, cycle, {"as_of_ts": ts, "marks": scoring})
    observation = canonical_daily_score_observation(state2, 1)
    assert observation is not None and observation[0] == 3


def test_leg_forecast_waits_for_declared_horizon_and_first_mature_mark_is_immutable(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    origin = "2026-08-01T00:07:00+00:00"
    book = {
        "legs": [
            {
                "symbol": "A",
                "side": "long",
                "seat_role": "alpha",
                "target_notional": 1_000.0,
                "expected_price_edge_frac": 0.02,
                "edge_horizon_hours": 168,
                "rationale": "forecast",
            }
        ],
    }
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 50_000.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book=book,
        adversary={"accept": True},
        report={"cycle": 1, "decision_ts": origin},
    )
    _commit_seeded_cycle(st, 1)

    early = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 101.0, "BTC/USDT:USDT": 50_500.0},
        now="2026-08-02T00:07:00+00:00",
        btc_symbol="BTC/USDT:USDT",
    )
    assert early["new_forecast_scores"] == 0
    assert not (Path(mem) / "forecast-scorecard.jsonl").exists()

    mature = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 110.0, "BTC/USDT:USDT": 52_500.0},
        now="2026-08-08T00:07:00+00:00",
        btc_symbol="BTC/USDT:USDT",
    )
    # Pending marks cannot publish an immutable label. The first eligible outcome is the first
    # later completed cycle's dedicated scoring packet, even when A was not in agent evidence.
    assert mature["new_forecast_scores"] == 0
    _seed_cycle(
        st,
        2,
        marks={"BTC/USDT:USDT": 52_500.0},
        betas={"BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 2, "decision_ts": "2026-08-08T00:07:00+00:00"},
    )
    _commit_seeded_cycle(
        st,
        2,
        {
            "as_of_ts": "2026-08-08T00:07:00+00:00",
            "marks": {"A": 110.0, "BTC/USDT:USDT": 52_500.0},
        },
    )
    mature = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 90.0, "BTC/USDT:USDT": 52_500.0},
        now="2026-08-09T00:07:00+00:00",
        btc_symbol="BTC/USDT:USDT",
    )
    assert mature["new_forecast_scores"] == 1
    path = Path(mem) / "forecast-scorecard.jsonl"
    first = path.read_text()
    row = json.loads(first)
    assert row["forecast_score_schema_version"] == 4
    assert row["evaluation_horizon_hours"] == pytest.approx(168.0)
    assert row["realized_selected_edge_frac"] == pytest.approx(0.05)
    assert row["selected_side_profitable"] is True
    assert row["directional_forecast_hit"] is True
    assert row["learning_eligible"] is True
    assert row["statistically_independent"] is None
    assert row["leg_nonoverlap_eligible"] is True
    assert row["outcome_observation_cycle"] == 2
    assert len(row["outcome_scoring_marks_sha256"]) == 64
    assert row["origin_standalone_vol_usd"] is None

    forged = {
        **row,
        "predicted_selected_edge_frac": 0.99,
        "realized_beta_adjusted_return_frac": 0.88,
        "realized_selected_edge_frac": 0.88,
        "forecast_error_frac": -0.11,
        "sign_hit": True,
    }
    path.write_text(json.dumps(forged) + "\n")
    with pytest.raises(ValueError, match="invalid forecast score row"):
        read_forecast_scorecard(path, state_dir=st)
    path.write_text(first)

    _seed_cycle(
        st,
        3,
        marks={"BTC/USDT:USDT": 52_500.0},
        betas={"BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 3, "decision_ts": "2026-08-09T00:07:00+00:00"},
    )
    later_marks = {"A": 150.0, "BTC/USDT:USDT": 52_500.0}
    _commit_seeded_cycle(
        st,
        3,
        {
            "as_of_ts": "2026-08-09T00:07:00+00:00",
            "marks": later_marks,
        },
    )
    complete = json.loads((cycle_dir(st, 3, cadence="rebal") / "complete.json").read_text())
    later = {
        **row,
        "evaluated_at": "2026-08-09T00:07:00+00:00",
        "outcome_observation_cycle": 3,
        "outcome_scoring_marks_sha256": complete["manifest"]["artifact_sha256"]["scoring_marks"],
        "evaluation_horizon_hours": 192.0,
        "evaluation_mark": 150.0,
        "btc_evaluation_mark": 52_500.0,
        "realized_beta_adjusted_return_frac": 0.45,
        "realized_selected_edge_frac": 0.45,
        "forecast_error_frac": 0.43,
        "sign_hit": True,
        "outcome_marks_sha256": _marks_sha256(later_marks),
    }
    path.write_text(json.dumps(later) + "\n")
    with pytest.raises(ValueError, match="invalid forecast score row"):
        read_forecast_scorecard(path, state_dir=st)
    path.write_text(first)

    retry = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 150.0, "BTC/USDT:USDT": 52_500.0},
        now="2026-08-10T00:07:00+00:00",
        btc_symbol="BTC/USDT:USDT",
    )
    assert retry["new_forecast_scores"] == 0
    assert path.read_text() == first


def test_forecast_tolerance_and_accuracy_are_distinct_from_seat_profitability(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    origin = "2026-08-01T00:08:03+00:00"
    observation = "2026-08-02T00:07:53+00:00"
    book = {
        "legs": [
            {
                "symbol": "A",
                "side": "long",
                "seat_role": "alpha",
                "target_notional": 1_000.0,
                "expected_price_edge_frac": -0.02,
                "edge_horizon_hours": 24,
                "rationale": "negative selected-side forecast",
            }
        ],
    }
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 100.0},
        betas={"A": 0.0, "BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book=book,
        adversary={"accept": True},
        report={"cycle": 1, "decision_ts": origin},
    )
    _commit_seeded_cycle(st, 1)
    _seed_cycle(
        st,
        2,
        marks={"A": 99.0, "BTC/USDT:USDT": 100.0},
        betas={"A": 0.0, "BTC/USDT:USDT": 1.0},
        reads={"sentiment": [], "technical": [], "futures": []},
        book={"legs": []},
        adversary={"accept": True},
        report={"cycle": 2, "decision_ts": observation},
    )
    _commit_seeded_cycle(
        st,
        2,
        {
            "as_of_ts": observation,
            "marks": {"A": 99.0, "BTC/USDT:USDT": 100.0},
        },
    )

    result = score_mature_leg_forecasts(st, mem, through_cycle=2, btc_symbol="BTC/USDT:USDT")

    assert result["new_forecast_learning_labels"] == 1
    row = read_forecast_scorecard(Path(mem) / "forecast-scorecard.jsonl", state_dir=st)[0]
    assert row["evaluation_horizon_hours"] == pytest.approx(24.0 - 10.0 / 3600.0)
    assert row["horizon_status"] == "on_schedule"
    assert row["selected_side_profitable"] is False
    # The PM predicted a loss and a loss occurred: direction right, selected seat unprofitable.
    assert row["directional_forecast_hit"] is True
    assert row["sign_hit"] is True


def test_overlapping_unchanged_forecast_renewals_do_not_inflate_effective_sample(tmp_path):
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    btc = "BTC/USDT:USDT"
    for cycle in range(1, 7):
        timestamp = f"2026-08-0{cycle}T00:07:00+00:00"
        if cycle <= 3:
            predicted = 0.02 if cycle <= 2 else 0.03
            book = {
                "legs": [
                    {
                        "symbol": "A",
                        "side": "long",
                        "seat_role": "alpha",
                        "target_notional": 1_000.0,
                        "expected_price_edge_frac": predicted,
                        "edge_horizon_hours": 72,
                        "rationale": "same" if cycle <= 2 else "changed magnitude",
                    }
                ],
            }
        else:
            book = {"legs": []}
        mark = 100.0 + cycle
        _seed_cycle(
            st,
            cycle,
            marks={"A": mark, btc: 100.0},
            betas={"A": 0.0, btc: 1.0},
            reads={"sentiment": [], "technical": [], "futures": []},
            book=book,
            adversary={"accept": True},
            report={"cycle": cycle, "decision_ts": timestamp},
        )
        _commit_seeded_cycle(
            st,
            cycle,
            {
                "as_of_ts": timestamp,
                "marks": {"A": mark, btc: 100.0},
            },
        )

    result = score_mature_leg_forecasts(st, mem, through_cycle=6, btc_symbol=btc)
    rows = read_forecast_scorecard(Path(mem) / "forecast-scorecard.jsonl", state_dir=st)

    assert result["new_forecast_scores"] == 3
    assert result["new_forecast_learning_labels"] == 1
    assert result["new_overlapping_renewals_excluded"] == 1
    assert result["new_leg_overlap_forecasts_audit_only"] == 2
    by_cycle = {row["origin_cycle"]: row for row in rows}
    assert by_cycle[1]["forecast_independence_reason"] == "first_forecast"
    assert by_cycle[1]["statistically_independent"] is None
    assert by_cycle[1]["leg_nonoverlap_eligible"] is True
    assert by_cycle[2]["forecast_independence_reason"] == "overlapping_unchanged_thesis"
    assert by_cycle[2]["learning_eligible"] is False
    assert by_cycle[2]["forecast_cohort_origin_cycle"] == 1
    assert by_cycle[3]["forecast_independence_reason"] == "explicit_thesis_changed"
    assert by_cycle[3]["learning_eligible"] is False
    assert by_cycle[3]["statistically_independent"] is None
    assert by_cycle[3]["leg_nonoverlap_eligible"] is False


def test_shorter_changed_forecast_cannot_shorten_independent_cohort_boundary(tmp_path):
    import futures_fund.reflection as reflection

    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    btc = "BTC/USDT:USDT"
    for cycle in range(1, 10):
        timestamp = f"2026-08-{cycle:02d}T00:07:00+00:00"
        if cycle == 1:
            horizon = 168
            predicted = 0.04
        elif cycle in {2, 3, 8}:
            horizon = 24
            predicted = 0.01
        else:
            horizon = None
            predicted = None
        book = (
            {
                "legs": [
                    {
                        "symbol": "A",
                        "side": "long",
                        "seat_role": "alpha",
                        "target_notional": 1_000.0,
                        "expected_price_edge_frac": predicted,
                        "edge_horizon_hours": horizon,
                        "rationale": "explicit forecast",
                    }
                ],
            }
            if horizon is not None
            else {"legs": []}
        )
        mark = 100.0 + cycle
        _seed_cycle(
            st,
            cycle,
            marks={"A": mark, btc: 100.0},
            betas={"A": 0.0, btc: 1.0},
            reads={"sentiment": [], "technical": [], "futures": []},
            book=book,
            adversary={"accept": True},
            report={"cycle": cycle, "decision_ts": timestamp},
        )
        _commit_seeded_cycle(
            st,
            cycle,
            {
                "as_of_ts": timestamp,
                "marks": {"A": mark, btc: 100.0},
            },
        )

    result = score_mature_leg_forecasts(st, mem, through_cycle=9, btc_symbol=btc)
    path = Path(mem) / "forecast-scorecard.jsonl"
    original = path.read_text()
    rows = read_forecast_scorecard(path, state_dir=st)
    by_cycle = {row["origin_cycle"]: row for row in rows}

    assert result["new_forecast_scores"] == 4
    assert by_cycle[1]["forecast_score_schema_version"] == 4
    assert by_cycle[1]["statistically_independent"] is None
    assert by_cycle[1]["leg_nonoverlap_eligible"] is True
    # The day-1 168h cohort remains the calibration boundary through day 8. The overlapping
    # day-2 24h change is a real decision but audit-only for calibration, and its unchanged day-3
    # renewal cannot become independent merely because that shorter thesis matured.
    assert by_cycle[2]["forecast_independence_reason"] == "explicit_thesis_changed"
    assert by_cycle[2]["decision_learning_eligible"] is True
    assert by_cycle[2]["learning_eligible"] is False
    assert by_cycle[2]["statistically_independent"] is None
    assert by_cycle[2]["leg_nonoverlap_eligible"] is False
    assert by_cycle[2]["forecast_decision_cohort_origin_cycle"] == 2
    assert by_cycle[2]["forecast_calibration_cohort_origin_cycle"] == 1
    assert by_cycle[2]["forecast_calibration_boundary_ts"] == ("2026-08-08T00:07:00+00:00")
    assert by_cycle[3]["forecast_independence_reason"] == ("overlapping_unchanged_thesis")
    assert by_cycle[3]["decision_learning_eligible"] is False
    assert by_cycle[3]["statistically_independent"] is None
    assert by_cycle[3]["leg_nonoverlap_eligible"] is False
    assert by_cycle[3]["forecast_decision_cohort_origin_cycle"] == 2
    assert by_cycle[3]["forecast_calibration_cohort_origin_cycle"] == 1
    assert by_cycle[3]["forecast_cohort_origin_cycle"] == 1
    # At the original 168h boundary, the re-entered 24h thesis can finally start a counted cohort.
    assert by_cycle[8]["statistically_independent"] is None
    assert by_cycle[8]["leg_nonoverlap_eligible"] is True
    assert by_cycle[8]["forecast_calibration_cohort_origin_cycle"] == 8
    assert by_cycle[8]["forecast_calibration_boundary_ts"] == ("2026-08-09T00:07:00+00:00")

    # New-policy metadata is deterministic, not self-declared audit text.
    forged = [{**row} for row in rows]
    forged[2]["forecast_calibration_boundary_ts"] = "2026-08-04T00:07:00+00:00"
    path.write_text("".join(json.dumps(row) + "\n" for row in forged))
    with pytest.raises(ValueError, match="invalid forecast score row"):
        read_forecast_scorecard(path, state_dir=st)
    path.write_text(original)

    # Schema-v1/v2/v3 rows remain exactly reconstructible against the same committed
    # origin/outcome artifact hashes after introducing schema v4.
    for policy_version, historical_origin_cycle in ((1, 1), (2, 3), (3, 3)):
        historical = reflection._build_forecast_score_row(
            st,
            origin_cycle=historical_origin_cycle,
            symbol="A",
            btc_symbol=btc,
            cadence="rebal",
            policy_version=policy_version,
        )
        assert historical is not None
        if policy_version == 2:
            # Exact old-policy reconstruction intentionally preserves the historical bug for
            # audit: day 2 reset the v2 anchor, so day 3 appeared independent. V2 is excluded
            # from current calibration; rewriting this immutable row would corrupt provenance.
            assert historical["forecast_score_schema_version"] == 2
            assert historical["statistically_independent"] is True
            assert historical["forecast_cohort_origin_cycle"] == 3
            assert "forecast_calibration_boundary_ts" not in historical
        if policy_version == 3:
            assert historical["forecast_score_schema_version"] == 3
            assert historical["statistically_independent"] is False
            assert historical["forecast_calibration_cohort_origin_cycle"] == 1
            assert "leg_nonoverlap_eligible" not in historical
        historical_path = Path(mem) / f"forecast-scorecard-v{policy_version}.jsonl"
        historical_path.write_text(json.dumps(historical) + "\n")
        assert read_forecast_scorecard(historical_path, state_dir=st) == [historical]
        historical_path.write_text(
            json.dumps(
                {
                    **historical,
                    "outcome_scoring_marks_sha256": "f" * 64,
                }
            )
            + "\n"
        )
        with pytest.raises(ValueError, match="invalid forecast score row"):
            read_forecast_scorecard(historical_path, state_dir=st)


def _role_file(tmp_path, role, hard_rule):
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for desk_role in ("sentiment", "technical", "futures", "pm", "adversary"):
        candidate = agents / f"{desk_role}.md"
        if not candidate.exists():
            candidate.write_text(
                f"# {desk_role}\n\n## Hard rules\n- protected\n\n{BEGIN}\n{END}\n"
                "\n## Output\nJSON\n"
            )
    p = agents / f"{role}.md"
    p.write_text(f"# {role}\n\n## Hard rules\n- {hard_rule}\n\n{BEGIN}\n{END}\n\n## Output\nJSON\n")
    return p


def test_desk_score_seals_schema_normalized_recurrence_packet(tmp_path):
    pending = tmp_path / "pending"
    pending.mkdir()
    raw = [
        {
            "kind": "pm_negative_alpha",
            "role": "pm",
            "count": 2,
            "window": 6,
        }
    ]
    (pending / "recurrences.json").write_text(json.dumps(raw))
    digest = _seal_recurrences(pending)
    normalized = json.loads((pending / "recurrences.json").read_text())
    assert normalized == [
        {
            **raw[0],
            "evidence": [],
            "suggestion": "",
        }
    ]
    assert (pending / "recurrences.sha256").read_text().strip() == digest
    assert (pending / "recurrences.sha256").stat().st_mode & 0o777 == 0o400
    assert (
        digest
        == sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_score_scan_recomputes_recurrences_after_scorecard_only_crash(tmp_path, monkeypatch):
    import futures_fund.reflection as reflection

    state, memory, _path, _row, _marks, _outcome_ts = _seed_strict_daily_score(
        tmp_path, score=False
    )
    original_write = reflection._write_scorecard

    def crash_after_scorecard(path, records):
        original_write(path, records)
        raise RuntimeError("crash after scorecard persistence")

    monkeypatch.setattr(reflection, "_write_scorecard", crash_after_scorecard)
    with pytest.raises(RuntimeError, match="crash after scorecard persistence"):
        score_eligible_completed_cycles(
            state,
            memory,
            btc_symbol="BTC/USDT:USDT",
            active_calibration_roles=set(),
        )
    persisted = (Path(memory) / "scorecard.jsonl").read_bytes()

    monkeypatch.setattr(reflection, "_write_scorecard", original_write)
    original_detect = reflection.detect_recurrences
    replayed_cycles: list[list[int]] = []

    def capture_replay(records, **kwargs):
        replayed_cycles.append([record.cycle for record in records])
        return original_detect(records, **kwargs)

    monkeypatch.setattr(reflection, "detect_recurrences", capture_replay)
    retry = score_eligible_completed_cycles(
        state,
        memory,
        btc_symbol="BTC/USDT:USDT",
        active_calibration_roles=set(),
    )

    assert retry["scored_cycles"] == []
    assert retry["recurrence_refresh_cycle"] == 1
    assert replayed_cycles == [[1]]
    assert (Path(memory) / "scorecard.jsonl").read_bytes() == persisted


def _pending_score_packet(tmp_path, *, cycle: int = 7):
    from scripts import desk_score

    _role_file(tmp_path, "pm", "protected")
    state = tmp_path / "state"
    memory = tmp_path / "memory"
    agents = tmp_path / "agents"
    bootstrap_reflector_heads(agents, memory / "reflector-journal.md")
    pending = memory / "pending" / str(cycle)
    pending.mkdir(parents=True)
    now = datetime.now().astimezone().isoformat()
    scoring = {
        "as_of_ts": now,
        "marks": {"BTC/USDT:USDT": 100_000.0},
    }
    meta = {
        "cycle": cycle,
        "now": now,
        "btc_symbol": "BTC/USDT:USDT",
        "scoring_marks_sha256": canonical_json_sha256(scoring),
    }
    (pending / "scoring_marks.json").write_text(json.dumps(scoring))
    (pending / "meta.json").write_text(json.dumps(meta))
    (memory / "pending" / "current.json").write_text(
        json.dumps({"cycle": cycle, "dir": str(pending.resolve())})
    )
    argv = [
        "--state-dir",
        str(state),
        "--memory-dir",
        str(memory),
        "--agents-dir",
        str(agents),
    ]
    return desk_score, state, memory, pending, argv


def test_desk_score_recovers_seal_before_authority_and_same_dir_consumed_retry(tmp_path):
    from futures_fund.reflection import (
        reflection_authority_recovery_status,
        write_reflection_authority_consumption,
    )

    desk_score, state, memory, pending, argv = _pending_score_packet(tmp_path)
    recurrences = [
        {
            "kind": "pm_negative_alpha",
            "role": "pm",
            "count": 3,
            "window": 6,
            "evidence": ["bound"],
            "suggestion": "review",
        }
    ]
    (pending / "recurrences.json").write_text(json.dumps(recurrences))
    prepared_sha256 = _seal_recurrences(pending)

    # Simulate a host crash after the immutable pending seal and before authority publication.
    assert desk_score.main(argv) == 0
    assert json.loads((pending / "recurrences.json").read_text()) == recurrences
    assert (pending / "recurrences.sha256").read_text().strip() == prepared_sha256
    assert reflection_authority_recovery_status(state, memory, 7)["status"] == "unconsumed"

    write_reflection_authority_consumption(
        state,
        memory,
        source_cycle=7,
        recurrences_sha256=prepared_sha256,
        outcome="no_head_change",
    )
    (pending / "reflection.json").write_text(json.dumps({"edits": []}))

    # A same-directory retry must not collide with the old non-empty seal or expose the proposal a
    # second time. The immutable authority/consumption pair is the permission for this rotation.
    assert desk_score.main(argv) == 0
    assert json.loads((pending / "recurrences.json").read_text()) == []
    assert (pending / "recurrences.sha256").read_text().strip() == canonical_json_sha256([])
    assert not (pending / "reflection.json").exists()
    assert reflection_authority_recovery_status(state, memory, 7)["status"] == "consumed"


@pytest.mark.parametrize(
    "crash_primitive",
    ["durable_unlink", "durable_write_text", "seal_publish"],
)
def test_consumed_recurrence_rotation_recovers_every_durable_boundary(
    tmp_path, monkeypatch, crash_primitive
):
    from futures_fund.reflection import write_reflection_authority_consumption

    desk_score, state, memory, pending, argv = _pending_score_packet(tmp_path)
    recurrences = [
        {
            "kind": "pm_negative_alpha",
            "role": "pm",
            "count": 3,
            "window": 6,
            "evidence": ["bound"],
            "suggestion": "review",
        }
    ]
    (pending / "recurrences.json").write_text(json.dumps(recurrences))
    prepared_sha256 = _seal_recurrences(pending)
    assert desk_score.main(argv) == 0
    write_reflection_authority_consumption(
        state,
        memory,
        source_cycle=7,
        recurrences_sha256=prepared_sha256,
        outcome="no_head_change",
    )

    primitive_name = (
        "durable_write_text" if crash_primitive == "seal_publish" else crash_primitive
    )
    original = getattr(desk_score, primitive_name)
    crashed = False

    def crash_once_after_operation(*args, **kwargs):
        nonlocal crashed
        result = original(*args, **kwargs)
        is_selected_write = (
            crash_primitive != "seal_publish"
            or Path(args[0]).name == "recurrences.sha256"
        )
        if not crashed and is_selected_write:
            crashed = True
            raise OSError(f"crash after {crash_primitive}")
        return result

    monkeypatch.setattr(desk_score, primitive_name, crash_once_after_operation)
    with pytest.raises(OSError, match=f"crash after {crash_primitive}"):
        desk_score._recover_reflection_authority_packet(
            str(state), str(memory), pending, 7
        )

    recovered = desk_score._recover_reflection_authority_packet(
        str(state), str(memory), pending, 7
    )
    assert recovered["reflection_authority_recovery"] == "consumed"
    assert recovered["recurrences"] == []
    assert json.loads((pending / "recurrences.json").read_text()) == []
    assert (pending / "recurrences.sha256").read_text().strip() == canonical_json_sha256([])


def test_reflection_rejects_unsealed_or_mutated_recurrence_authority(tmp_path):
    role_path = _role_file(tmp_path, "pm", "protected")
    agents = tmp_path / "agents"
    journal = tmp_path / "reflector-journal.md"
    bootstrap_reflector_heads(agents, journal)
    original = role_path.read_bytes()
    proposal = {
        "edits": [
            {
                "role": "pm",
                "region_text": "- unauthorized",
                "reason": "test",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    recurrences = [
        {
            "kind": "pm_negative_alpha",
            "role": "pm",
            "count": 2,
            "window": 6,
            "evidence": [],
            "suggestion": "review",
        }
    ]
    with pytest.raises(ValueError, match="desk_score seal"):
        apply_reflection(
            proposal,
            agents,
            journal,
            allowed_roles={"pm"},
            current_cycle=5,
            surfaced_recurrences=recurrences,
            sealed_recurrences_sha256="0" * 64,
        )
    assert role_path.read_bytes() == original


def test_incomplete_cycle_authority_replays_before_apply_then_stands_down_after_apply(tmp_path):
    _role_file(tmp_path, "pm", "protected")
    agents = tmp_path / "agents"
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    journal = memory / "reflector-journal.md"
    anchor = state / "reflector-head-anchor-v1.json"
    bootstrap_reflector_heads(agents, journal, anchor_path=anchor)
    recurrences = [
        {
            "kind": "pm_negative_alpha",
            "role": "pm",
            "count": 3,
            "window": 6,
            "evidence": ["bound"],
            "suggestion": "review",
        }
    ]
    recurrence_digest = sha256(
        json.dumps(recurrences, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    write_reflection_authority(
        state,
        memory,
        source_cycle=21,
        recurrences_sha256=recurrence_digest,
        recurrences=recurrences,
    )

    retry_before = tmp_path / "retry-before"
    retry_before.mkdir()
    recovered = _recover_reflection_authority_packet(str(state), str(memory), retry_before, 21)
    assert recovered["reflection_authority_recovery"] == "unconsumed"
    assert json.loads((retry_before / "recurrences.json").read_text()) == recurrences

    proposal = {
        "edits": [
            {
                "role": "pm",
                "region_text": "- applied before host crash",
                "reason": "test",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    apply_reflection(
        proposal,
        agents,
        journal,
        allowed_roles={"pm"},
        current_cycle=21,
        surfaced_recurrences=recurrences,
        sealed_recurrences_sha256=recurrence_digest,
        anchor_path=anchor,
    )

    retry_after = tmp_path / "retry-after"
    retry_after.mkdir()
    stood_down = _recover_reflection_authority_packet(str(state), str(memory), retry_after, 21)
    assert stood_down["reflection_authority_recovery"] == "consumed"
    assert json.loads((retry_after / "recurrences.json").read_text()) == []


def _apply_with_heads(proposal, agents, journal, *, allowed_roles=None, cycle=2, anchor_path=None):
    bootstrap_reflector_heads(agents, journal, anchor_path=anchor_path)
    surfaced_roles = (
        set(allowed_roles)
        if allowed_roles is not None
        else {
            edit.get("role")
            for edit in proposal.get("edits", [])
            if edit.get("role") in {"sentiment", "technical", "futures", "pm", "adversary"}
        }
    )
    recurrences = [
        {
            "kind": "test_recurrence",
            "role": role,
            "count": 1,
            "window": 1,
            "evidence": [],
            "suggestion": "test",
        }
        for role in sorted(surfaced_roles)
    ]
    canonical_recurrences = [
        Recurrence.model_validate(item).model_dump(mode="json") for item in recurrences
    ]
    recurrence_seal = sha256(
        json.dumps(canonical_recurrences, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    authority_state = Path(anchor_path).parent if anchor_path is not None else Path(journal).parent
    write_reflection_authority(
        authority_state,
        Path(journal).parent,
        source_cycle=cycle,
        recurrences_sha256=recurrence_seal,
        recurrences=canonical_recurrences,
        journal_path=journal,
        heads_path=reflector_heads_path(journal),
    )
    return apply_reflection(
        proposal,
        agents,
        journal,
        allowed_roles=surfaced_roles,
        current_cycle=cycle,
        surfaced_recurrences=recurrences,
        sealed_recurrences_sha256=recurrence_seal,
        anchor_path=anchor_path,
    )


def test_apply_reflection_edits_region_only(tmp_path):
    p = _role_file(tmp_path, "sentiment", "never invent a headline")
    proposal = {
        "edits": [
            {
                "role": "sentiment",
                "region_text": "- demand a 48h catalyst",
                "reason": "miscalibrated",
                "evidence": ["c1"],
                "retire_if": "hit>=0.5",
            }
        ]
    }
    res = _apply_with_heads(proposal, tmp_path / "agents", tmp_path / "journal.md")
    assert res["applied"] == ["sentiment"] and res["skipped"] == []
    text = p.read_text()
    _pre, region, _suf = split_managed(text)
    assert "48h catalyst" in region
    assert "never invent a headline" in text  # hard rule intact
    assert (tmp_path / "journal.md").exists()


def test_active_managed_region_must_match_local_reflector_journal(tmp_path):
    agents = tmp_path / "agents"
    for role in ("sentiment", "technical", "futures", "pm", "adversary"):
        _role_file(tmp_path, role, "protected")
    journal = tmp_path / "journal.md"
    proposal = {
        "edits": [
            {
                "role": "futures",
                "region_text": "- [c2] cap conviction until recovery",
                "reason": "measured recurrence",
                "evidence": ["c1 edge=-0.01"],
                "retire_if": "edge >= 0 by c6",
            }
        ]
    }
    assert _apply_with_heads(proposal, agents, journal)["applied"] == ["futures"]
    assert audit_managed_region_provenance(agents, journal) == []

    sentiment = agents / "sentiment.md"
    sentiment.write_text(
        sentiment.read_text().replace(
            f"{BEGIN}\n{END}",
            f"{BEGIN}\n- [c9] imported predecessor score\n{END}",
        )
    )
    assert audit_managed_region_provenance(agents, journal) == [
        "sentiment: active managed region does not match its latest head"
    ]


def test_latest_head_rejects_rollback_to_historically_journaled_region(tmp_path):
    import futures_fund.reflection as reflection

    role_path = _role_file(tmp_path, "futures", "protected")
    agents = tmp_path / "agents"
    journal = tmp_path / "reflector-journal.md"
    old_proposal = {
        "edits": [
            {
                "role": "futures",
                "region_text": "- old calibration",
                "reason": "first",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    new_proposal = {
        "edits": [
            {
                "role": "futures",
                "region_text": "- current calibration",
                "reason": "second",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    assert _apply_with_heads(old_proposal, agents, journal, cycle=1)["applied"] == ["futures"]
    assert _apply_with_heads(new_proposal, agents, journal, cycle=2)["applied"] == ["futures"]
    assert "- old calibration" in reflection.journaled_managed_regions(journal)["futures"]

    role_path.write_text(splice_managed(role_path.read_text(), "- old calibration"))
    assert audit_managed_region_provenance(agents, journal) == [
        "futures: active managed region does not match its latest head"
    ]


def test_external_anchor_rejects_coordinated_memory_prompt_rollback_and_rebootstrap(tmp_path):
    role_path = _role_file(tmp_path, "pm", "protected")
    agents = tmp_path / "agents"
    journal = tmp_path / "memory" / "reflector-journal.md"
    anchor = tmp_path / "state" / "reflector-head-anchor-v1.json"
    first = {
        "edits": [
            {
                "role": "pm",
                "region_text": "- generation one",
                "reason": "one",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    second = {
        "edits": [
            {
                "role": "pm",
                "region_text": "- generation two",
                "reason": "two",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    _apply_with_heads(first, agents, journal, cycle=11, anchor_path=anchor)
    generation_one = {
        "prompt": role_path.read_bytes(),
        "journal": journal.read_bytes(),
        "heads": reflector_heads_path(journal).read_bytes(),
    }
    _apply_with_heads(second, agents, journal, cycle=12, anchor_path=anchor)

    role_path.write_bytes(generation_one["prompt"])
    journal.write_bytes(generation_one["journal"])
    reflector_heads_path(journal).write_bytes(generation_one["heads"])
    issues = audit_managed_region_provenance(agents, journal, reflector_heads_path(journal), anchor)
    assert issues == ["reflector-head-anchor-v1.json does not authorize the current reflector head"]

    reflector_heads_path(journal).unlink()
    with pytest.raises(ValueError, match="already exists; refusing rebootstrap"):
        bootstrap_reflector_heads(agents, journal, reflector_heads_path(journal), anchor)


def test_reflection_source_cycle_is_positive_monotonic_and_single_use(tmp_path):
    _role_file(tmp_path, "sentiment", "protected")
    agents = tmp_path / "agents"
    journal = tmp_path / "reflector-journal.md"
    proposal = {
        "edits": [
            {
                "role": "sentiment",
                "region_text": "- first",
                "reason": "test",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    _apply_with_heads(proposal, agents, journal, cycle=3)
    replay = {
        "edits": [
            {
                "role": "sentiment",
                "region_text": "- replay",
                "reason": "test",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    recurrences = [
        {
            "kind": "test_recurrence",
            "role": "sentiment",
            "count": 1,
            "window": 1,
            "evidence": [],
            "suggestion": "test",
        }
    ]
    recurrence_seal = sha256(
        json.dumps(recurrences, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ValueError, match="already handled or predates"):
        apply_reflection(
            replay,
            agents,
            journal,
            allowed_roles={"sentiment"},
            current_cycle=3,
            surfaced_recurrences=recurrences,
            sealed_recurrences_sha256=recurrence_seal,
        )
    with pytest.raises(ValueError, match="positive source cycle"):
        apply_reflection(
            replay,
            agents,
            journal,
            allowed_roles={"sentiment"},
            current_cycle=0,
            surfaced_recurrences=recurrences,
            sealed_recurrences_sha256=recurrence_seal,
        )


def test_blank_retirement_is_a_bound_head_with_source_provenance(tmp_path):
    role_path = _role_file(tmp_path, "technical", "protected")
    agents = tmp_path / "agents"
    journal = tmp_path / "reflector-journal.md"
    active = {
        "edits": [
            {
                "role": "technical",
                "region_text": "- temporary calibration",
                "reason": "activate",
                "evidence": [],
                "retire_if": "recovered",
            }
        ]
    }
    retirement = {
        "edits": [
            {
                "role": "technical",
                "region_text": "",
                "reason": "measured recovery",
                "evidence": ["manifest-bound recovery"],
                "retire_if": "",
            }
        ]
    }
    _apply_with_heads(active, agents, journal, cycle=7)
    recurrences = [
        {
            "kind": "specialist_recovered",
            "role": "technical",
            "count": 3,
            "window": 3,
            "evidence": [],
            "suggestion": "retire",
        },
        {
            "kind": "pm_negative_alpha",
            "role": "pm",
            "count": 2,
            "window": 6,
            "evidence": [],
            "suggestion": "review",
        },
    ]
    recurrence_seal = sha256(
        json.dumps(recurrences, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    write_reflection_authority(
        journal.parent,
        journal.parent,
        source_cycle=8,
        recurrences_sha256=recurrence_seal,
        recurrences=recurrences,
        journal_path=journal,
        heads_path=reflector_heads_path(journal),
    )
    apply_reflection(
        retirement,
        agents,
        journal,
        allowed_roles={"technical", "pm"},
        current_cycle=8,
        surfaced_recurrences=recurrences,
        sealed_recurrences_sha256=recurrence_seal,
    )

    _prefix, region, _suffix = split_managed(role_path.read_text())
    assert region.strip() == ""
    assert audit_managed_region_provenance(agents, journal) == []
    artifact = json.loads(reflector_heads_path(journal).read_text())
    head = artifact["roles"]["technical"]

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    assert head["source"] == "reflection"
    assert head["source_cycle"] == 8
    assert head["region"] == ""
    canonical_retirement = ReflectionProposal.model_validate(retirement).model_dump(mode="json")
    assert head["proposal_sha256"] == sha256(canonical(canonical_retirement)).hexdigest()
    assert head["surfaced_recurrences_sha256"] == sha256(canonical(recurrences)).hexdigest()
    assert "head_event_v1" in journal.read_text()


def test_static_head_verifier_rebinds_retained_proposal_semantics():
    import futures_fund.reflection as reflection

    proposal = ReflectionProposal.model_validate(
        {
            "edits": [
                {
                    "role": "pm",
                    "region_text": "- different region",
                    "reason": "test",
                    "evidence": [],
                    "retire_if": "",
                }
            ]
        }
    ).model_dump(mode="json")
    recurrences = [
        {
            "kind": "pm_negative_alpha",
            "role": "pm",
            "count": 2,
            "window": 6,
            "evidence": [],
            "suggestion": "review",
        }
    ]
    proposal_sha = sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    recurrence_sha = sha256(
        json.dumps(recurrences, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    entry = reflection._journal_entry_bytes(
        "pm",
        proposal["edits"][0],
        generation=1,
        proposal_sha256=proposal_sha,
        source_cycle=9,
        surfaced_recurrences_sha256=recurrence_sha,
        canonical_proposal=proposal,
        canonical_recurrences=recurrences,
        region="- unauthorized actual region",
    )
    record = {
        "head_generation": 1,
        "journal_entry_length": len(entry),
        "journal_entry_sha256": sha256(entry).hexdigest(),
        "journal_entry_start": 0,
        "proposal_sha256": proposal_sha,
        "region": "- unauthorized actual region",
        "region_sha256": sha256(b"- unauthorized actual region").hexdigest(),
        "source": "reflection",
        "source_cycle": 9,
        "surfaced_recurrences_sha256": recurrence_sha,
    }
    with pytest.raises(ValueError, match="does not authorize the head region"):
        reflection._validate_reflection_head_entry("pm", record, journal=entry, generation=1)


def test_reflection_head_write_failure_rolls_back_prompt_journal_and_head(tmp_path, monkeypatch):
    import futures_fund.reflection as reflection

    role_path = _role_file(tmp_path, "pm", "protected")
    agents = tmp_path / "agents"
    journal = tmp_path / "reflector-journal.md"
    bootstrap_reflector_heads(agents, journal)
    heads = reflector_heads_path(journal)
    anchor = journal.with_name("reflector-head-anchor-v1.json")
    prompt_before = role_path.read_bytes()
    heads_before = heads.read_bytes()
    anchor_before = anchor.read_bytes()
    original_write = reflection._atomic_write_bytes

    def fail_on_heads(path, content):
        if Path(path) == heads:
            raise OSError("injected heads write failure")
        return original_write(path, content)

    monkeypatch.setattr(reflection, "_atomic_write_bytes", fail_on_heads)
    proposal = {
        "edits": [
            {
                "role": "pm",
                "region_text": "- should roll back",
                "reason": "test",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    recurrences = [
        {
            "kind": "test",
            "role": "pm",
            "count": 1,
            "window": 1,
            "evidence": [],
            "suggestion": "test",
        }
    ]
    recurrence_seal = sha256(
        json.dumps(recurrences, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # The authority receipt is intentionally outside the injected head-write path.
    monkeypatch.setattr(reflection, "_atomic_write_bytes", original_write)
    write_reflection_authority(
        journal.parent,
        journal.parent,
        source_cycle=4,
        recurrences_sha256=recurrence_seal,
        recurrences=recurrences,
        journal_path=journal,
        heads_path=heads,
    )
    monkeypatch.setattr(reflection, "_atomic_write_bytes", fail_on_heads)
    with pytest.raises(OSError, match="injected heads write failure"):
        apply_reflection(
            proposal,
            agents,
            journal,
            allowed_roles={"pm"},
            current_cycle=4,
            surfaced_recurrences=recurrences,
            sealed_recurrences_sha256=recurrence_seal,
        )
    assert role_path.read_bytes() == prompt_before
    assert heads.read_bytes() == heads_before
    assert anchor.read_bytes() == anchor_before
    assert not journal.exists()
    assert audit_managed_region_provenance(agents, journal) == []


def test_heads_bootstrap_is_idempotent_and_refuses_tamper(tmp_path):
    role_path = _role_file(tmp_path, "sentiment", "protected")
    agents = tmp_path / "agents"
    journal = tmp_path / "reflector-journal.md"
    first = bootstrap_reflector_heads(agents, journal)
    heads = reflector_heads_path(journal)
    original = heads.read_bytes()
    second = bootstrap_reflector_heads(agents, journal)
    assert first["created"] is True
    assert second["created"] is False
    assert heads.read_bytes() == original

    tampered = json.loads(original)
    tampered["generation"] = 99
    heads.write_text(json.dumps(tampered))
    tampered_bytes = heads.read_bytes()
    with pytest.raises(ValueError, match="self-digest mismatch"):
        bootstrap_reflector_heads(agents, journal)
    assert heads.read_bytes() == tampered_bytes  # bootstrap never repairs or blesses tamper

    heads.write_bytes(original)
    role_path.write_text(splice_managed(role_path.read_text(), "- unreviewed replacement"))
    with pytest.raises(ValueError, match="does not match its latest head"):
        bootstrap_reflector_heads(agents, journal)
    assert heads.read_bytes() == original


def test_bootstrap_uses_reviewed_active_region_not_latest_legacy_blank_row(tmp_path):
    role_path = _role_file(tmp_path, "pm", "protected")
    role_path.write_text(splice_managed(role_path.read_text(), "- reviewed active calibration"))
    agents = tmp_path / "agents"
    journal = tmp_path / "reflector-journal.md"
    journal.write_text(
        "## pm — original applied reflection\n"
        "- retire_if:\n- evidence: c1\n"
        "- region:\n- reviewed active calibration\n\n"
        "## pm — legacy accounting correction only\n"
        "- retire_if:\n- evidence: changes no managed prompt region\n"
        "- region:\n\n"
    )
    result = bootstrap_reflector_heads(agents, journal)
    assert result["artifact"]["roles"]["pm"]["region"] == "- reviewed active calibration"
    assert result["artifact"]["roles"]["pm"]["source"] == "audited_legacy_bootstrap"
    assert audit_managed_region_provenance(agents, journal) == []


def test_apply_reflection_skips_oversize_region(tmp_path):
    _role_file(tmp_path, "pm", "Deploy >=90%")
    proposal = {
        "edits": [
            {"role": "pm", "region_text": "x" * 5000, "reason": "", "evidence": [], "retire_if": ""}
        ]
    }
    res = _apply_with_heads(proposal, tmp_path / "agents", tmp_path / "journal.md")
    assert res["applied"] == [] and res["skipped"][0][0] == "pm"


def test_apply_reflection_rejects_unknown_role_at_contract_boundary(tmp_path):
    _role_file(tmp_path, "sentiment", "protected")
    proposal = {
        "edits": [
            {"role": "cfo", "region_text": "x", "reason": "", "evidence": [], "retire_if": ""}
        ]
    }
    with pytest.raises(ValueError):
        _apply_with_heads(proposal, tmp_path / "agents", tmp_path / "journal.md")


def test_scorecard_immutable_cycle_prevents_double_count(tmp_path):
    """A resume that re-scores the same cycle must not double-count in the K-gate."""
    st, mem = str(tmp_path / "st"), str(tmp_path / "mem")
    reads = {
        "sentiment": [
            {"symbol": "A", "lean": "long", "conviction": 0.9, "rationale": "x", "evidence": []}
        ],
        "technical": [],
        "futures": [],
    }
    book = {"legs": [{"symbol": "A", "side": "long", "target_notional": 1000.0, "rationale": ""}]}
    # seed cycle 1 evidence/reads/book/adversary via _seed_cycle helper already in this file
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 60000.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads=reads,
        book=book,
        adversary={"accept": True, "objections": [], "demanded_changes": []},
    )
    # score cycle 1 THREE times against a DOWN move (A long that fell -> a "bad" record each time).
    # WITHOUT dedup, 3 identical cycle-1 bad records satisfy the K=3 gate and fire a
    # specialist_miscalibrated recurrence off ONE real cycle; dedup collapses them to 1 -> no fire.
    first = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 90.0, "BTC/USDT:USDT": 60000.0},
        now="t",
        btc_symbol="BTC/USDT:USDT",
    )
    scorecard_before = (Path(mem) / "scorecard.jsonl").read_bytes()
    attribution_before = (cycle_dir(st, 1, cadence="rebal") / "attribution.json").read_bytes()
    for mark in (80.0, 120.0):
        retried = score_previous_cycle(
            st,
            mem,
            scored_cycle=1,
            cur_marks={"A": mark, "BTC/USDT:USDT": 60000.0},
            now="later",
            btc_symbol="BTC/USDT:USDT",
        )
        assert retried["immutable_score_reused"] is True
    assert first["immutable_score_reused"] is False
    assert (Path(mem) / "scorecard.jsonl").read_bytes() == scorecard_before
    attribution_path = cycle_dir(st, 1, cadence="rebal") / "attribution.json"
    assert attribution_path.read_bytes() == attribution_before
    lines = [ln for ln in (Path(mem) / "scorecard.jsonl").read_text().splitlines() if ln.strip()]
    assert len(lines) == 1  # retry replaces the same cycle atomically
    recs = json.loads((Path(mem) / "pending" / "recurrences.json").read_text())
    # one real bad cycle duplicated 3x must NOT trip the K=3 gate
    assert not any(r["kind"] == "specialist_miscalibrated" for r in recs)


def test_attribution_intent_survives_crash_before_scorecard_commit(tmp_path, monkeypatch):
    import futures_fund.reflection as reflection

    st, mem = str(tmp_path / "st"), str(tmp_path / "mem")
    reads = {
        "sentiment": [
            {"symbol": "A", "lean": "long", "conviction": 0.9, "rationale": "x", "evidence": []}
        ],
        "technical": [],
        "futures": [],
    }
    book = {"legs": [{"symbol": "A", "side": "long", "target_notional": 1000.0}]}
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 60_000.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads=reads,
        book=book,
        adversary={"accept": True},
    )
    original_write = reflection._write_scorecard

    def crash(*_args, **_kwargs):
        raise RuntimeError("injected scorecard crash")

    monkeypatch.setattr(reflection, "_write_scorecard", crash)
    with pytest.raises(RuntimeError, match="injected scorecard crash"):
        score_previous_cycle(
            st,
            mem,
            scored_cycle=1,
            cur_marks={"A": 90.0, "BTC/USDT:USDT": 60_000.0},
            now="first",
            btc_symbol="BTC/USDT:USDT",
        )
    attribution_path = cycle_dir(st, 1, cadence="rebal") / "attribution.json"
    first_attribution = attribution_path.read_bytes()

    monkeypatch.setattr(reflection, "_write_scorecard", original_write)
    result = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 120.0, "BTC/USDT:USDT": 60_000.0},
        now="later",
        btc_symbol="BTC/USDT:USDT",
    )
    assert result["immutable_score_reused"] is True
    assert attribution_path.read_bytes() == first_attribution
    score = json.loads((Path(mem) / "scorecard.jsonl").read_text())
    assert score["book"]["gross_pnl"] == pytest.approx(-100.0)


def test_preexisting_conflicting_duplicate_scorecard_fails_closed(tmp_path):
    import futures_fund.reflection as reflection

    path = tmp_path / "scorecard.jsonl"
    first = {"cycle": 7, "scored_at": "first", "n_symbols": 1}
    later = {"cycle": 7, "scored_at": "later", "n_symbols": 99}
    path.write_text(json.dumps(first) + "\n" + json.dumps(later) + "\n")
    with pytest.raises(ValueError, match="invalid scorecard row"):
        reflection._read_scorecard(path)


def test_score_previous_cycle_clears_stale_reflection(tmp_path):
    st, mem = str(tmp_path / "st"), str(tmp_path / "mem")
    pending = Path(mem) / "pending"
    pending.mkdir(parents=True)
    (pending / "reflection.json").write_text('{"edits": [{"role": "sentiment"}]}')
    score_previous_cycle(
        st, mem, scored_cycle=0, cur_marks={"A": 1.0}, now="t", btc_symbol="BTC/USDT:USDT"
    )
    assert not (pending / "reflection.json").exists()  # consume-once cleared it


def test_recovery_events_require_active_note_and_handled_events_cool_down(tmp_path):
    event = Recurrence(kind="specialist_recovered", role="technical", count=3, window=6)
    assert (
        _filter_recurrences(
            [event], memory_dir=tmp_path, scored_cycle=10, active_calibration_roles=set()
        )
        == []
    )
    assert _filter_recurrences(
        [event],
        memory_dir=tmp_path,
        scored_cycle=10,
        active_calibration_roles={"technical"},
    ) == [event]
    mark_recurrences_handled(tmp_path, [event.model_dump()], cycle=10)
    assert (
        _filter_recurrences(
            [event],
            memory_dir=tmp_path,
            scored_cycle=11,
            active_calibration_roles={"technical"},
        )
        == []
    )
    assert _filter_recurrences(
        [event],
        memory_dir=tmp_path,
        scored_cycle=13,
        active_calibration_roles={"technical"},
    ) == [event]

    adversary_event = Recurrence(kind="adversary_recovered", role="adversary", count=5, window=6)
    assert (
        _filter_recurrences(
            [adversary_event],
            memory_dir=tmp_path,
            scored_cycle=13,
            active_calibration_roles=set(),
        )
        == []
    )
    assert _filter_recurrences(
        [adversary_event],
        memory_dir=tmp_path,
        scored_cycle=13,
        active_calibration_roles={"adversary"},
    ) == [adversary_event]


def test_adversary_recovered_uses_exact_trailing_six_eligible_condition():
    def record(cycle, edge, *, accepted=True, horizon=24.0, provenance="manifest_bound"):
        manifest = provenance == "manifest_bound"
        return ScoreRecord(
            cycle=cycle,
            scored_at="2026-08-01T00:07:00+00:00",
            evaluation_horizon_hours=horizon,
            outcome_marks_sha256="a" * 64 if manifest else "",
            outcome_observation_cycle=cycle + 1 if manifest else None,
            outcome_scoring_marks_sha256="b" * 64 if manifest else "",
            outcome_provenance=provenance,
            book=BookScore(
                n_legs=4,
                gross_notional=20_000.0,
                realized_edge_ex_funding_frac=edge,
            ),
            adv_accepted=accepted,
            adv_revised=not accepted,
        )

    recovered = [record(cycle, -0.001 if cycle == 3 else 0.001) for cycle in range(1, 7)]
    events = _adversary_recovery_recurrences(recovered)
    assert len(events) == 1
    event = events[0]
    assert (event.kind, event.role, event.count, event.window) == (
        "adversary_recovered",
        "adversary",
        5,
        6,
    )
    assert len(event.evidence) == 6
    assert sum("accepted_losing_original" in item for item in event.evidence) == 1

    two_losses = [record(cycle, -0.001 if cycle in {3, 6} else 0.001) for cycle in range(1, 7)]
    assert _adversary_recovery_recurrences(two_losses) == []

    # Off-horizon and unverified rows cannot fill the exact six-row retirement window.
    mixed = [
        *recovered[:4],
        record(5, 0.001, horizon=48.0),
        record(6, 0.001, provenance="legacy_unverified"),
    ]
    assert _adversary_recovery_recurrences(mixed) == []

    unavailable = recovered.copy()
    unavailable[0] = unavailable[0].model_copy(
        update={
            "book": unavailable[0].book.model_copy(update={"realized_edge_ex_funding_frac": None})
        }
    )
    assert _adversary_recovery_recurrences(unavailable) == []


def test_adv_revised_derived_from_verdict(tmp_path):
    st, mem = str(tmp_path / "st"), str(tmp_path / "mem")
    reads = {"sentiment": [], "technical": [], "futures": []}
    book = {"legs": [{"symbol": "A", "side": "long", "target_notional": 1000.0, "rationale": ""}]}
    _seed_cycle(
        st,
        1,
        marks={"A": 100.0, "BTC/USDT:USDT": 60000.0},
        betas={"A": 1.0, "BTC/USDT:USDT": 1.0},
        reads=reads,
        book=book,
        adversary={
            "accept": False,
            "objections": ["too concentrated"],
            "demanded_changes": ["spread it"],
        },
    )
    # NOTE: no book_original.json written — adv_revised must still be True from accept=False
    score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 110.0, "BTC/USDT:USDT": 60000.0},
        now="t",
        btc_symbol="BTC/USDT:USDT",
    )
    rec = json.loads((cycle_dir(st, 1, cadence="rebal") / "attribution.json").read_text())
    assert rec["adv_revised"] is True
    assert rec["adv_accepted"] is False


def test_apply_reflection_skips_role_without_recurrence(tmp_path):
    p = _role_file(tmp_path, "sentiment", "never invent a headline")
    proposal = {
        "edits": [
            {
                "role": "sentiment",
                "region_text": "- note",
                "reason": "",
                "evidence": [],
                "retire_if": "",
            }
        ]
    }
    res = _apply_with_heads(
        proposal,
        tmp_path / "agents",
        tmp_path / "j.md",
        allowed_roles={"technical"},
    )  # sentiment NOT surfaced
    assert res["applied"] == []
    assert res["skipped"][0][0] == "sentiment"
    _pre, region, _suf = split_managed(p.read_text())
    assert region.strip() == ""  # unchanged


def test_apply_reflection_dedupes_multiple_edits_per_role(tmp_path):
    p = _role_file(tmp_path, "sentiment", "never invent a headline")
    proposal = {
        "edits": [
            {
                "role": "sentiment",
                "region_text": "- first",
                "reason": "",
                "evidence": [],
                "retire_if": "",
            },
            {
                "role": "sentiment",
                "region_text": "- second",
                "reason": "",
                "evidence": [],
                "retire_if": "",
            },
        ]
    }
    res = _apply_with_heads(
        proposal,
        tmp_path / "agents",
        tmp_path / "j.md",
        allowed_roles={"sentiment"},
    )
    assert res["applied"] == ["sentiment"]  # applied ONCE, not twice
    _pre, region, _suf = split_managed(p.read_text())
    assert "second" in region and "first" not in region  # last edit wins


def test_reconcile_wiring_persists_then_scores(tmp_path):
    """Mirror what desk_reconcile + desk_score do in sequence, offline: reconcile persists the
    evidence snapshot for cycle N; the next cycle's score reads it back."""
    st = str(tmp_path / "st")
    mem = str(tmp_path / "mem")
    pending = Path(mem) / "pending"
    pending.mkdir(parents=True)
    ev1 = [
        {"symbol": "A", "mark": 100.0, "beta_btc": 1.0},
        {"symbol": "BTC/USDT:USDT", "mark": 60000.0, "beta_btc": 1.0},
    ]
    persist_decision_snapshot(st, 1, evidence=ev1, pending_dir=pending)
    save_output(st, 1, "reads", {"sentiment": [], "technical": [], "futures": []}, cadence="rebal")
    save_output(st, 1, "book", {"legs": []}, cadence="rebal")
    save_output(st, 1, "adversary", {"accept": True}, cadence="rebal")
    res = score_previous_cycle(
        st,
        mem,
        scored_cycle=1,
        cur_marks={"A": 105.0, "BTC/USDT:USDT": 60000.0},
        now="t",
        btc_symbol="BTC/USDT:USDT",
    )
    assert res["scored_cycle"] == 1
