from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from futures_fund.account import ClosedLeg, PaperAccount, Position, save_account
from futures_fund.performance import (
    PERFORMANCE_SCHEMA_VERSION,
    _agent_performance,
    _benchmark_comparisons,
    _cost_net_benchmark_outcome,
    _forecast_performance,
    _portfolio_risk_context,
    _recent_windows,
    _time_based_performance,
    build_performance_snapshot,
    canonical_sha256,
    write_performance_snapshot,
)
from futures_fund.reconcile_commit import (
    recover_reconcile_transaction,
    stage_reconcile_transaction,
)
from futures_fund.reflection import read_forecast_scorecard, score_previous_cycle
from futures_fund.scorecard import BookScore, ScoreRecord, SpecialistScore
from futures_fund.slippage import ExecutionRealism
from futures_fund.state_transaction import current_account_sha256

NOW = datetime(2026, 8, 19, 0, 7, tzinfo=UTC)


def _calibration_score_row(
    cycle: int, *, horizon_hours: float = 24.0, calls_per_role: int = 3
) -> dict:
    specialists = {
        role: SpecialistScore(
            role=role,
            n_available=max(calls_per_role, 5),
            n_scored=calls_per_role,
            hit_rate=2.0 / 3.0 if calls_per_role else 0.0,
            conv_weighted_edge=0.001,
        )
        for role in ("sentiment", "technical", "futures")
    }
    return ScoreRecord(
        cycle=cycle,
        scored_at=f"2026-08-{cycle:02d}T00:07:00+00:00",
        evaluation_horizon_hours=horizon_hours,
        outcome_marks_sha256="a" * 64,
        outcome_observation_cycle=cycle + 1,
        outcome_scoring_marks_sha256="b" * 64,
        outcome_provenance="manifest_bound",
        n_symbols=5,
        specialist_return_label="btc_beta_adjusted",
        specialists=specialists,
        book=BookScore(
            n_legs=2,
            gross_notional=1_000.0,
            realized_edge_ex_funding=1.0,
            realized_edge_ex_funding_frac=0.001,
        ),
    ).model_dump(mode="json")


def test_agent_calibration_default_window_can_reach_declared_minimums():
    rows = [_calibration_score_row(cycle) for cycle in range(1, 13)]
    rows.append(_calibration_score_row(13, horizon_hours=48.0))

    result = _agent_performance(rows)

    assert result["calibration_window_limit_cycles"] == 30
    assert result["eligible_horizon_cycles_available"] == 12
    assert result["window_cycles"] == 12
    assert result["manifest_bound_off_horizon_cycles_ignored"] == 1
    for role in ("sentiment", "technical", "futures"):
        calibration = result["roles"][role]
        assert calibration["window_cycles"] == 12
        assert calibration["directional_calls"] == 36
        assert calibration["complete_output_cycles"] == 12
        assert calibration["output_coverage_rate"] == 1.0
        assert calibration["calibration_sample_status"] == "usable"
        assert calibration["calibration_sample_deficits"] == []
        assert calibration["minimum_complete_output_cycles"] == 12
    pm = result["roles"]["pm"]
    assert pm["scored_books"] == 12
    assert pm["calibration_sample_status"] == "usable"
    assert pm["calibration_sample_deficits"] == []
    assert pm["minimum_horizon_matched_books"] == 12
    assert "does not claim statistical independence" in result["sample_dependence_note"]


def test_agent_calibration_keeps_cycle_and_call_shortfalls_explicit():
    eleven_cycles = _agent_performance([_calibration_score_row(cycle) for cycle in range(1, 12)])
    technical = eleven_cycles["roles"]["technical"]
    assert technical["directional_calls"] == 33
    assert technical["calibration_sample_status"] == "insufficient_horizon_matched_history"
    assert technical["calibration_sample_deficits"] == ["complete_output_cycles:11/12"]
    assert eleven_cycles["roles"]["pm"]["calibration_sample_deficits"] == [
        "horizon_matched_books:11/12"
    ]

    too_few_calls = _agent_performance(
        [_calibration_score_row(cycle, calls_per_role=2) for cycle in range(1, 13)]
    )
    technical = too_few_calls["roles"]["technical"]
    assert technical["window_cycles"] == 12
    assert technical["directional_calls"] == 24
    assert technical["calibration_sample_status"] == "insufficient_horizon_matched_history"
    assert technical["calibration_sample_deficits"] == ["directional_calls:24/30"]
    assert too_few_calls["roles"]["pm"]["calibration_sample_status"] == "usable"


def test_specialist_calibration_requires_complete_output_coverage():
    rows = [_calibration_score_row(cycle) for cycle in range(1, 31)]
    for row in rows[15:]:
        row["specialists"]["technical"].update({"n_available": 0, "n_scored": 0})

    technical = _agent_performance(rows)["roles"]["technical"]

    assert technical["complete_output_cycles"] == 15
    assert technical["failed_or_incomplete_output_cycles"] == 15
    assert technical["directional_calls"] == 45
    assert technical["output_coverage_rate"] == pytest.approx(0.5)
    assert technical["calibration_sample_status"] == "insufficient_horizon_matched_history"
    assert technical["calibration_sample_deficits"] == ["output_coverage_rate:0.500/0.800"]


def test_all_flat_full_coverage_is_complete_output_not_a_failed_specialist():
    result = _agent_performance(
        [_calibration_score_row(cycle, calls_per_role=0) for cycle in range(1, 13)]
    )
    technical = result["roles"]["technical"]

    assert technical["complete_output_cycles"] == 12
    assert technical["failed_or_incomplete_output_cycles"] == 0
    assert technical["output_coverage_rate"] == 1.0
    assert technical["abstention_rate"] == 1.0
    assert technical["calibration_sample_deficits"] == ["directional_calls:0/30"]


def _seed(tmp_path):
    state = tmp_path / "state"
    memory = tmp_path / "memory"
    pending = memory / "pending" / "2"
    pending.mkdir(parents=True)
    account = PaperAccount(
        cash=19_950.0,
        fees_paid=20.0,
        slippage_paid=30.0,
        funding_received=8.0,
        funding_paid=3.0,
        realized_pnl=0.0,
        last_funding_ts=datetime(2026, 8, 18, 16, 7, tzinfo=UTC),
        positions={
            "A": Position(
                symbol="A",
                direction="long",
                qty=100.0,
                entry_price=10.0,
                opened_ts=NOW,
                accrued_funding=-1.0,
                accrued_fees=2.0,
                accrued_slippage=3.0,
            ),
            "B": Position(
                symbol="B",
                direction="short",
                qty=50.0,
                entry_price=20.0,
                opened_ts=NOW,
                accrued_funding=2.0,
                accrued_fees=2.0,
                accrued_slippage=3.0,
            ),
        },
        closed_legs=[
            ClosedLeg(
                symbol="C",
                direction="long",
                opened_cycle=1,
                opened_cadence="rebal",
                fees=2.0,
                slippage=3.0,
                realized_funding=4.0,
                realized_pnl=10.0,
            )
        ],
    )
    save_account(state, account)
    (pending / "evidence.json").write_text(
        json.dumps(
            [
                {
                    "symbol": "A",
                    "mark": 11.0,
                    "beta_clamped": 1.2,
                    "expected_funding_8h_bps": 2.0,
                    "momentum_pct": 8.0,
                    "momentum_24h_pct": 2.0,
                    "momentum_72h_pct": 5.0,
                    "beta_adjusted_momentum_24h_pct": 0.5,
                    "beta_adjusted_momentum_72h_pct": 1.5,
                    "momentum_acceleration_24h_pct": 0.25,
                    "drawdown_from_72h_high_pct": -1.0,
                },
                {
                    "symbol": "B",
                    "mark": 18.0,
                    "beta_clamped": 0.8,
                    "expected_funding_8h_bps": 3.0,
                    "momentum_pct": -25.0,
                    "momentum_72h_pct": -12.0,
                    "momentum_168h_pct": -25.0,
                },
            ]
        )
    )
    (pending / "risk_model.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "return_label": "hourly_log_return_minus_beta_clamped_times_btc",
                "lookback_hours": 168,
                "residual_vol_annualized": {"A": 0.20, "B": 0.30},
                "covariance_annualized": {
                    "A": {"A": 0.04, "B": 0.01},
                    "B": {"A": 0.01, "B": 0.09},
                },
                "high_correlation_pairs": [],
            }
        )
    )
    origin_ts = datetime(2026, 8, 15, 0, 7, tzinfo=UTC)
    outcome_ts = datetime(2026, 8, 16, 0, 7, tzinfo=UTC)
    origin_artifacts = {
        "evidence": [
            {"symbol": "A", "mark": 100.0, "beta_clamped": 1.0},
            {"symbol": "B", "mark": 100.0, "beta_clamped": 0.0},
            {"symbol": "BTC/USDT:USDT", "mark": 100.0, "beta_clamped": 1.0},
        ],
        "reads": {
            "sentiment": [],
            "technical": [
                {
                    "symbol": "A",
                    "lean": "long",
                    "conviction": 1.0,
                    "rationale": "fixture",
                    "evidence": [],
                },
                {
                    "symbol": "B",
                    "lean": "flat",
                    "conviction": 0.0,
                    "rationale": "fixture",
                    "evidence": [],
                },
            ],
            "futures": [],
        },
        "book": {"legs": []},
        "adversary": {"accept": True},
        "report": {"cycle": 1, "decision_ts": origin_ts.isoformat()},
    }
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=1,
        cadence="rebal",
        account=account,
        artifacts=origin_artifacts,
        equity_ts=origin_ts,
        equity=20_050.0,
        ledger={
            "cycle": 1,
            "opening_equity": 20_000.0,
            "closing_equity": 20_050.0,
            "turnover_usd": 500.0,
        },
    )
    recover_reconcile_transaction(state)
    outcome_marks = {"A": 102.0, "B": 100.0, "BTC/USDT:USDT": 100.0}
    scoring_packet = {"as_of_ts": outcome_ts.isoformat(), "marks": outcome_marks}
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=2,
        cadence="rebal",
        account=account,
        artifacts={
            "scoring_marks": scoring_packet,
            "report": {"cycle": 2, "decision_ts": outcome_ts.isoformat()},
        },
        equity_ts=outcome_ts,
        equity=20_050.0,
        ledger={
            "cycle": 2,
            "opening_equity": 20_050.0,
            "closing_equity": 20_050.0,
            "turnover_usd": 0.0,
        },
    )
    recover_reconcile_transaction(state)
    (state / "portfolio-heartbeats.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-08-18T08:07:00+00:00",
                "equity": 20_200.0,
            }
        )
        + "\n"
    )
    complete = json.loads((state / "rebal" / "cycle" / "2" / "complete.json").read_text())
    scoring_sha256 = complete["manifest"]["artifact_sha256"]["scoring_marks"]
    score_previous_cycle(
        state,
        memory,
        scored_cycle=1,
        cur_marks=outcome_marks,
        now=outcome_ts.isoformat(),
        btc_symbol="BTC/USDT:USDT",
        outcome_observation_cycle=2,
        outcome_scoring_marks_sha256=scoring_sha256,
    )
    score_path = memory / "scorecard.jsonl"
    score_path.write_text(score_path.read_text() * 2)
    return state, memory, pending


def _thesis_book(*, a_side: str = "long", include_a: bool = True) -> dict:
    legs = []
    if include_a:
        legs.append(
            {
                "symbol": "A",
                "side": a_side,
                "seat_role": "alpha",
                "target_notional": 1_100.0,
                "expected_price_edge_frac": 0.012,
                "edge_horizon_hours": 72,
                "edge_calibration_basis": "72h manifest-bound cohort; conservative shrinkage",
                "invalidation_condition": "beta-adjusted 72h path loses chosen-side sign",
            }
        )
    legs.append(
        {
            "symbol": "B",
            "side": "short",
            "seat_role": "alpha",
            "target_notional": 900.0,
            "expected_price_edge_frac": 0.009,
            "edge_horizon_hours": 24,
            "edge_calibration_basis": "24h manifest-bound cohort; conservative shrinkage",
            "invalidation_condition": "beta-adjusted 24h path turns positive",
        }
    )
    return {
        "legs": legs,
        "stated_deploy_frac": 0.10,
        "stated_dollar_residual_frac": 0.10,
        "stated_beta_residual": 0.01,
        "turnover_legs_changed": 0,
        "turnover_justification": "fixture",
        "notes": "fixture",
    }


def _commit_thesis_generation(state, *, cycle: int, book: dict) -> None:
    account = PaperAccount.from_dict(json.loads((state / "account.json").read_text()))
    book_sha256 = canonical_sha256(book)
    legs = {str(leg["symbol"]): leg for leg in book["legs"]}
    for symbol, position in account.positions.items():
        leg = legs.get(symbol)
        position.thesis_cycle = cycle
        position.thesis_book_sha256 = book_sha256
        if leg is None:
            continue
        position.expected_price_edge_frac = float(leg["expected_price_edge_frac"])
        position.edge_horizon_hours = int(leg["edge_horizon_hours"])
        position.edge_calibration_basis = str(leg["edge_calibration_basis"])
        position.invalidation_condition = str(leg["invalidation_condition"])
    ts = NOW + timedelta(days=cycle)
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=current_account_sha256(state),
        cycle=cycle,
        cadence="rebal",
        account=account,
        artifacts={
            "book": book,
            "report": {"cycle": cycle, "decision_ts": ts.isoformat()},
        },
        equity_ts=ts,
        equity=20_150.0,
        ledger={
            "cycle": cycle,
            "opening_equity": 20_150.0,
            "closing_equity": 20_150.0,
            "turnover_usd": 0.0,
        },
    )
    recover_reconcile_transaction(state)


def test_snapshot_exposes_exact_manifest_bound_committed_alpha_thesis(tmp_path):
    state, memory, pending = _seed(tmp_path)
    book = _thesis_book()
    _commit_thesis_generation(state, cycle=3, book=book)

    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=4, as_of_ts=NOW, starting_capital=20_000.0
    )
    positions = {row["symbol"]: row for row in snapshot["positions"]}
    thesis = positions["A"]["committed_thesis"]

    assert thesis == {
        "available": True,
        "source_policy": ("exact_position_reference_to_latest_prior_completed_manifest_bound_book"),
        "candidate_cycle": 3,
        "committed_cycle": 3,
        "manifest_bound_book_sha256": canonical_sha256(book),
        "symbol": "A",
        "side": "long",
        "seat_role": "alpha",
        "expected_price_edge_frac": 0.012,
        "edge_horizon_hours": 72,
        "edge_calibration_basis": ("72h manifest-bound cohort; conservative shrinkage"),
        "invalidation_condition": "beta-adjusted 72h path loses chosen-side sign",
        "unavailable_reason": None,
    }
    assert positions["B"]["committed_thesis"]["available"] is True
    assert snapshot["data_quality"]["committed_alpha_thesis_available"] == 2
    assert snapshot["data_quality"]["committed_alpha_thesis_unavailable_symbols"] == []


def test_committed_thesis_fails_closed_on_tampered_latest_book(tmp_path):
    state, memory, pending = _seed(tmp_path)
    _commit_thesis_generation(state, cycle=3, book=_thesis_book())
    path = state / "rebal" / "cycle" / "3" / "book.json"
    tampered = json.loads(path.read_text())
    tampered["legs"][0]["expected_price_edge_frac"] = 0.99
    path.write_text(json.dumps(tampered))

    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=4, as_of_ts=NOW, starting_capital=20_000.0
    )
    thesis = {row["symbol"]: row["committed_thesis"] for row in snapshot["positions"]}

    assert thesis["A"]["available"] is False
    assert thesis["A"]["candidate_cycle"] == 3
    assert thesis["A"]["unavailable_reason"] == ("latest_prior_completion_manifest_invalid")
    assert thesis["A"]["expected_price_edge_frac"] is None


def test_committed_thesis_does_not_fall_back_past_unbound_newest_book(tmp_path):
    state, memory, pending = _seed(tmp_path)
    _commit_thesis_generation(state, cycle=3, book=_thesis_book())
    _commit_thesis_generation(state, cycle=4, book=_thesis_book())
    marker_path = state / "rebal" / "cycle" / "4" / "complete.json"
    marker = json.loads(marker_path.read_text())
    marker["manifest"]["artifact_sha256"].pop("book")
    marker_path.write_text(json.dumps(marker))

    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=5, as_of_ts=NOW, starting_capital=20_000.0
    )
    thesis = {row["symbol"]: row["committed_thesis"] for row in snapshot["positions"]}

    assert thesis["A"]["available"] is False
    assert thesis["A"]["candidate_cycle"] == 4
    assert thesis["A"]["committed_cycle"] is None
    assert thesis["A"]["unavailable_reason"] == ("latest_prior_completion_manifest_invalid")


def test_committed_thesis_does_not_fall_back_to_older_matching_leg(tmp_path):
    state, memory, pending = _seed(tmp_path)
    _commit_thesis_generation(state, cycle=3, book=_thesis_book())
    _commit_thesis_generation(state, cycle=4, book=_thesis_book(a_side="short"))

    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=5, as_of_ts=NOW, starting_capital=20_000.0
    )
    thesis = {row["symbol"]: row["committed_thesis"] for row in snapshot["positions"]}

    assert thesis["A"]["available"] is False
    assert thesis["A"]["candidate_cycle"] == 4
    assert thesis["A"]["committed_cycle"] is None
    assert thesis["A"]["unavailable_reason"] == ("latest_prior_bound_book_side_or_role_mismatch")
    assert thesis["A"]["expected_price_edge_frac"] is None


def test_snapshot_exposes_desk_positions_and_agent_calibration(tmp_path):
    state, memory, pending = _seed(tmp_path)
    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=2, as_of_ts=NOW, starting_capital=20_000.0
    )
    assert snapshot["schema_version"] == PERFORMANCE_SCHEMA_VERSION
    # A gains $100; B short gains $100; cash already includes cumulative frictions/funding.
    assert snapshot["desk"]["equity"] == pytest.approx(20_150.0)
    assert snapshot["desk"]["net_pnl"] == pytest.approx(150.0)
    assert snapshot["desk"]["peak_equity"] == pytest.approx(20_200.0)
    assert snapshot["desk"]["drawdown_frac"] == pytest.approx(20_150.0 / 20_200.0 - 1.0)
    assert snapshot["desk"]["gross_usd"] == pytest.approx(2_000.0)
    assert snapshot["desk"]["funding_clock_age_hours"] == pytest.approx(8.0)
    assert snapshot["data_quality"]["score_duplicate_cycles_removed"] == 1
    assert snapshot["data_quality"]["heartbeat_rows"] == 1
    assert len(snapshot["bindings"]["evidence_sha256"]) == 64
    assert len(snapshot["bindings"]["account_sha256"]) == 64
    assert len(snapshot["bindings"]["risk_model_sha256"]) == 64
    technical = snapshot["agent_performance"]["roles"]["technical"]
    assert technical["active_cycles"] == 1
    assert technical["abstention_rate"] == pytest.approx(0.5)
    assert technical["hit_rate"] == 1.0
    assert technical["conviction_weighted_edge"] == pytest.approx(0.02)
    positions = {row["symbol"]: row for row in snapshot["positions"]}
    assert positions["A"]["expected_carry_usd_per_8h"] == pytest.approx(-0.22)
    assert positions["A"]["position_age_hours"] == pytest.approx(0.0)
    assert positions["A"]["funding_intervals_held"] == pytest.approx(0.0)
    assert positions["A"]["past_max_hold_horizon"] is False
    assert positions["A"]["lifetime_net_pnl"] == pytest.approx(94.0)
    assert positions["B"]["expected_carry_usd_per_8h"] == pytest.approx(0.27)
    assert positions["A"]["price_regime"] == "chop"
    assert positions["A"]["momentum_24h_pct"] == pytest.approx(2.0)
    assert positions["A"]["side_opposed_momentum_72h_pct"] == pytest.approx(-5.0)
    assert positions["A"]["beta_adjusted_momentum_24h_pct"] == pytest.approx(0.5)
    assert positions["A"]["momentum_acceleration_24h_pct"] == pytest.approx(0.25)
    assert positions["B"]["price_regime"] == "trend"
    assert positions["B"]["side_opposed_momentum_pct"] == pytest.approx(-25.0)
    assert positions["B"]["side_opposed_trend"] is False
    assert positions["B"]["carry_recovery_intervals"] is None
    risk = snapshot["desk"]["portfolio_risk"]
    assert risk["available"] is True
    assert risk["max_alpha_standalone_risk_symbol"] == "B"
    assert risk["max_alpha_standalone_risk_share"] == pytest.approx(270.0 / 490.0)
    assert snapshot["closed_leg_performance"]["realized_lifecycle_net_pnl"] == 9.0
    forecast = snapshot["pm_forecast_performance"]
    assert forecast["total_audited_forecasts"] == 0
    assert forecast["independent_time_cohort_n_in_context_window"] == 0
    aggregate = forecast["aggregate_context_only"]
    assert aggregate["sign_hit_rate"] is None
    assert aggregate["mean_absolute_forecast_error_frac"] is None
    benchmarks = snapshot["frozen_benchmarks"]
    assert benchmarks["primary_market_neutral_benchmark"] == "residual_momentum_ls"
    assert len(benchmarks["policy_sha256"]) == 64
    assert benchmarks["comparisons"]["cash"]["status"] == (
        "insufficient_horizon_matched_history"
    )
    assert benchmarks["comparisons"]["cash"]["information_ratio_annualized"] is None
    assert benchmarks["comparisons"]["cash"]["warning"] is not None
    assert snapshot["candidate_opportunity_performance"]["total_bound_candidate_outcomes"] == 0
    for horizon in ("24", "72", "168"):
        bucket = forecast["by_horizon_hours"][horizon]
        assert bucket["n"] == 0
        assert bucket["leg_n"] == 0
        assert bucket["effective_n"] == 0
        assert bucket["minimum_observations"] == 12
        assert bucket["calibration_status"] == "insufficient_independent_time_cohorts"


def test_snapshot_rejects_duplicate_immutable_forecast_outcomes(tmp_path):
    _state, memory, _pending = _seed(tmp_path)
    path = memory / "forecast-scorecard.jsonl"
    row = {
        "origin_cycle": 1,
        "symbol": "A",
        "forecast_horizon_hours": 24,
        "outcome_observation_cycle": 2,
        "outcome_scoring_marks_sha256": "a" * 64,
        "predicted_selected_edge_frac": 0.02,
        "realized_selected_edge_frac": -0.01,
        "forecast_error_frac": -0.03,
        "sign_hit": False,
    }
    path.write_text((json.dumps(row) + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate forecast score"):
        read_forecast_scorecard(path)

    forged = {**row, "forecast_error_frac": 99.0, "sign_hit": True}
    path.write_text(json.dumps(forged) + "\n")
    with pytest.raises(ValueError, match="invalid forecast score row"):
        read_forecast_scorecard(path)


def test_snapshot_rejects_a_self_declared_manifest_bound_normal_score(tmp_path):
    state, memory, pending = _seed(tmp_path)
    path = memory / "scorecard.jsonl"
    row = json.loads(path.read_text().splitlines()[0])
    row["specialists"]["technical"]["conv_weighted_edge"] = 9.0
    path.write_text(json.dumps(row) + "\n")

    with pytest.raises(ValueError, match="not bound to committed artifacts"):
        build_performance_snapshot(
            state, memory, pending, cycle=2, as_of_ts=NOW, starting_capital=20_000.0
        )


def test_snapshot_ignores_closed_legs_added_after_cycle_commit(tmp_path):
    state, memory, pending = _seed(tmp_path)
    injected = state / "rebal" / "cycle" / "2" / "closed_legs.json"
    injected.write_text(
        json.dumps(
            [
                {
                    "symbol": "FORGED",
                    "direction": "long",
                    "opened_cycle": 1,
                    "opened_cadence": "rebal",
                    "fees": 0.0,
                    "slippage": 0.0,
                    "realized_funding": 0.0,
                    "realized_pnl": 99_999.0,
                }
            ]
        )
    )

    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=2, as_of_ts=NOW, starting_capital=20_000.0
    )
    assert snapshot["closed_leg_performance"]["realized_lifecycle_net_pnl"] == 9.0


def _forecast_performance_row(
    *,
    origin_cycle: int,
    symbol: str = "A",
    horizon: int = 24,
    start: datetime | None = None,
    predicted: float = 0.01,
    realized: float = 0.02,
    decision_eligible: bool = True,
    learning_eligible: bool = True,
    leg_nonoverlap_eligible: bool = True,
    target_notional: float = 1_000.0,
    standalone_vol: float | None = 200.0,
) -> dict:
    start = start or datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=origin_cycle - 1)
    directional_hit = (predicted > 0.0) == (realized > 0.0)
    return {
        "forecast_score_schema_version": 4,
        "origin_cycle": origin_cycle,
        "symbol": symbol,
        "origin_ts": start.isoformat(),
        "evaluated_at": (start + timedelta(hours=horizon)).isoformat(),
        "outcome_observation_cycle": origin_cycle + 1,
        "outcome_scoring_marks_sha256": "a" * 64,
        "forecast_horizon_hours": horizon,
        "predicted_selected_edge_frac": predicted,
        "realized_selected_edge_frac": realized,
        "forecast_error_frac": realized - predicted,
        "sign_hit": directional_hit,
        "directional_forecast_hit": directional_hit,
        "selected_side_profitable": realized > 0.0,
        "horizon_label_eligible": True,
        "decision_learning_eligible": decision_eligible,
        "learning_eligible": learning_eligible,
        "statistically_independent": None,
        "leg_nonoverlap_eligible": leg_nonoverlap_eligible,
        "statistical_independence_unit": (
            "nonoverlapping_time_outcome_cohort_assigned_in_performance"
        ),
        "target_notional": target_notional,
        "origin_standalone_vol_usd": standalone_vol,
    }


def test_forecast_performance_discloses_missing_residual_risk_coverage():
    result = _forecast_performance(
        [
            _forecast_performance_row(
                origin_cycle=1,
                predicted=0.02,
                realized=-0.01,
                standalone_vol=None,
            )
        ]
    )
    assert result["total_audited_forecasts"] == 1
    assert result["independent_time_cohort_n_in_context_window"] == 1
    bucket = result["by_horizon_hours"]["24"]
    assert bucket["n"] == 1
    assert bucket["leg_n"] == 1
    assert bucket["effective_n"] == 1
    assert bucket["sign_hit_rate"] == 0.0
    assert bucket["selected_side_profit_rate"] == 0.0
    assert bucket["mean_absolute_forecast_error_frac"] == pytest.approx(0.03)
    assert bucket["residual_risk_weight_coverage_frac"] == 0.0
    assert bucket["risk_capacity_status"] == "insufficient_independent_time_cohorts"


def test_pre_v4_forecasts_remain_auditable_but_cannot_calibrate_v4_policy():
    legacy = {
        "forecast_score_schema_version": 2,
        "origin_cycle": 1,
        "symbol": "A",
        "forecast_horizon_hours": 24,
        "predicted_selected_edge_frac": 0.02,
        "realized_selected_edge_frac": 0.03,
        "forecast_error_frac": 0.01,
        "sign_hit": True,
        "directional_forecast_hit": True,
        "selected_side_profitable": True,
        "horizon_label_eligible": True,
        "decision_learning_eligible": True,
        "learning_eligible": True,
        "statistically_independent": True,
        "target_notional": 1_000.0,
        "origin_standalone_vol_usd": 100.0,
    }
    result = _forecast_performance(
        [
            legacy,
            {
                **legacy,
                "forecast_score_schema_version": 3,
                "origin_cycle": 2,
                "symbol": "B",
                "outcome_observation_cycle": 3,
            },
        ]
    )

    assert result["total_audited_forecasts"] == 2
    assert result["legacy_policy_forecasts_audit_only"] == 2
    assert result["decision_eligible_forecasts_total"] == 0
    assert result["leg_nonoverlap_eligible_forecasts_total"] == 0
    assert result["total_independent_time_cohorts"] == 0
    assert result["by_horizon_hours"]["24"]["n"] == 0
    assert len(result["audit_only_outcomes"]) == 2


def test_forecast_performance_reports_audit_rows_and_effective_sample_separately():
    base = _forecast_performance_row(origin_cycle=1)
    rows = [
        base,
        {
            **base,
            "origin_cycle": 2,
            "decision_learning_eligible": False,
            "learning_eligible": False,
            "leg_nonoverlap_eligible": False,
            "forecast_independence_reason": "overlapping_unchanged_thesis",
        },
        {
            **base,
            "origin_cycle": 3,
            "horizon_label_eligible": False,
            "learning_eligible": False,
            "leg_nonoverlap_eligible": False,
        },
    ]

    result = _forecast_performance(rows)

    assert result["total_audited_forecasts"] == 3
    assert result["decision_eligible_forecasts_total"] == 1
    assert result["independent_time_cohort_n_in_context_window"] == 1
    assert result["overlapping_unchanged_renewals_audit_only"] == 1
    assert result["off_horizon_forecasts_audit_only"] == 1
    assert len(result["time_cohort_outcomes_context_only"]) == 1
    assert len(result["audit_only_outcomes"]) == 2


def test_overlapping_changed_theses_cannot_amplify_headline_or_risk_metrics():
    independent_loss = {
        **_forecast_performance_row(origin_cycle=1, realized=-0.01, standalone_vol=100.0),
        "forecast_independence_reason": "first_forecast",
    }
    overlapping_winners = [
        {
            **independent_loss,
            "origin_cycle": cycle,
            "realized_selected_edge_frac": 0.50,
            "forecast_error_frac": 0.49,
            "sign_hit": True,
            "directional_forecast_hit": True,
            "selected_side_profitable": True,
            "learning_eligible": False,
            "leg_nonoverlap_eligible": False,
            "forecast_independence_reason": "explicit_thesis_changed",
            "origin_standalone_vol_usd": 10_000.0,
        }
        for cycle in range(2, 12)
    ]

    result = _forecast_performance([independent_loss, *overlapping_winners])

    assert result["decision_eligible_forecasts_total"] == 11
    assert result["independent_time_cohort_n_in_context_window"] == 1
    assert result["overlapping_decision_forecasts_audit_only"] == 10
    assert result["overlapping_changed_thesis_forecasts_audit_only"] == 10
    bucket = result["by_horizon_hours"]["24"]
    assert bucket["directional_forecast_accuracy_rate"] == 0.0
    assert bucket["selected_side_profit_rate"] == 0.0
    assert bucket["mean_realized_selected_edge_frac"] == pytest.approx(-0.01)
    assert bucket["mean_absolute_forecast_error_frac"] == pytest.approx(0.02)
    assert bucket["notional_weighted_realized_selected_edge_frac"] == pytest.approx(-0.01)
    assert bucket["residual_risk_weighted_realized_selected_edge_frac"] == pytest.approx(-0.01)
    assert bucket["residual_risk_weighted_observations"] == 1
    assert result["aggregate_context_only"]["calibration_status"] == ("context_only_cross_horizon")
    assert len(result["time_cohort_outcomes_context_only"]) == 1
    assert len(result["audit_only_outcomes"]) == 10


def test_forecast_calibration_is_bucketed_by_matching_production_horizon():
    rows = []
    for horizon, realized in ((24, -0.01), (72, 0.03)):
        horizon_start = datetime(2026 + horizon // 72, 1, 1, tzinfo=UTC)
        for index in range(12):
            rows.append(
                {
                    **_forecast_performance_row(
                        origin_cycle=horizon * 100 + index,
                        symbol=f"A{index}",
                        horizon=horizon,
                        start=horizon_start + timedelta(hours=horizon * index),
                        realized=realized,
                    ),
                    "forecast_independence_reason": "prior_cohort_matured",
                }
            )

    result = _forecast_performance(rows)

    twenty_four = result["by_horizon_hours"]["24"]
    seventy_two = result["by_horizon_hours"]["72"]
    one_sixty_eight = result["by_horizon_hours"]["168"]
    assert twenty_four["n"] == 12
    assert twenty_four["leg_n"] == 12
    assert twenty_four["effective_n"] == 12
    assert twenty_four["calibration_status"] == "usable"
    assert twenty_four["mean_realized_selected_edge_frac"] == pytest.approx(-0.01)
    assert twenty_four["forecast_bias_frac"] == pytest.approx(-0.02)
    assert twenty_four["selected_side_profit_rate"] == 0.0
    assert twenty_four["risk_capacity_status"] == "usable"
    assert seventy_two["n"] == 12
    assert seventy_two["leg_n"] == 12
    assert seventy_two["calibration_status"] == "usable"
    assert seventy_two["mean_realized_selected_edge_frac"] == pytest.approx(0.03)
    assert seventy_two["forecast_bias_frac"] == pytest.approx(0.02)
    assert seventy_two["directional_forecast_accuracy_rate"] == 1.0
    assert seventy_two["notional_weight_coverage_frac"] == 1.0
    assert one_sixty_eight["n"] == 0
    assert one_sixty_eight["minimum_observations"] == 12
    assert one_sixty_eight["calibration_status"] == ("insufficient_independent_time_cohorts")
    aggregate = result["aggregate_context_only"]
    assert aggregate["mean_realized_selected_edge_frac"] == pytest.approx(0.01)
    assert aggregate["calibration_status"] == "context_only_cross_horizon"
    assert result["risk_capacity_evidence_basis"] == (
        "matching_horizon_nonoverlapping_time_outcome_cohorts_only"
    )


def test_many_legs_in_one_market_window_count_as_one_effective_observation():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        _forecast_performance_row(
            origin_cycle=1,
            symbol=f"S{index}",
            start=start,
            realized=0.02 if index < 10 else -0.02,
        )
        for index in range(20)
    ]

    result = _forecast_performance(rows)
    bucket = result["by_horizon_hours"]["24"]

    assert bucket["leg_n"] == 20
    assert bucket["independent_time_cohort_n"] == 1
    assert bucket["effective_n"] == 1
    assert bucket["n"] == 1
    assert bucket["calibration_status"] == "insufficient_independent_time_cohorts"
    assert bucket["risk_capacity_status"] == "insufficient_independent_time_cohorts"
    assert bucket["time_cohorts"][0]["leg_n"] == 20
    assert result["leg_nonoverlap_eligible_forecasts_total"] == 20
    assert result["total_independent_time_cohorts"] == 1


def test_multi_leg_forecasts_become_usable_only_after_twelve_time_cohorts():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for cohort_index in range(12):
        for leg_index in range(10):
            rows.append(
                _forecast_performance_row(
                    origin_cycle=cohort_index + 1,
                    symbol=f"S{cohort_index}-{leg_index}",
                    start=start + timedelta(hours=24 * cohort_index),
                )
            )

    bucket = _forecast_performance(rows)["by_horizon_hours"]["24"]

    assert bucket["leg_n"] == 120
    assert bucket["effective_n"] == 12
    assert bucket["independent_time_cohort_n"] == 12
    assert bucket["calibration_status"] == "usable"
    assert bucket["risk_capacity_status"] == "usable"
    assert len(bucket["time_cohorts"]) == 12
    assert all(cohort["leg_n"] == 10 for cohort in bucket["time_cohorts"])


def test_snapshot_exposes_when_tiny_carry_is_overwhelmed_by_opposing_trend(tmp_path):
    state, memory, pending = _seed(tmp_path)
    account = PaperAccount(
        cash=19_950.0,
        last_funding_ts=NOW,
        positions={
            "XRP": Position(
                symbol="XRP",
                direction="short",
                qty=100.0,
                entry_price=1.0,
                opened_ts=NOW,
            )
        },
    )
    save_account(state, account)
    (pending / "evidence.json").write_text(
        json.dumps(
            [
                {
                    "symbol": "XRP",
                    "mark": 1.35,
                    "beta_clamped": 1.4,
                    "expected_funding_8h_bps": 1.0,
                    "momentum_pct": 35.0,
                    "momentum_72h_pct": 22.0,
                    "momentum_168h_pct": 35.0,
                }
            ]
        )
    )
    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=2, as_of_ts=NOW, starting_capital=20_000.0
    )
    seat = snapshot["positions"][0]
    assert seat["unrealized_pnl"] == pytest.approx(-35.0)
    assert seat["unrealized_pnl_frac_entry_notional"] == pytest.approx(-0.35)
    assert seat["side_opposed_momentum_pct"] == pytest.approx(35.0)
    assert seat["side_opposed_trend"] is True
    assert seat["expected_carry_usd_per_8h"] == pytest.approx(0.0135)
    assert seat["expected_carry_usd_max_horizon"] == pytest.approx(0.54)
    assert seat["carry_recovery_intervals"] == pytest.approx(35.0 / 0.0135)
    assert seat["max_horizon_carry_loss_coverage_frac"] == pytest.approx(0.54 / 35.0)


def test_portfolio_risk_uses_signed_position_correlation_for_clusters():
    model = {
        "available": True,
        "residual_vol_annualized": {"A": 0.2, "B": 0.2},
        "covariance_annualized": {
            "A": {"A": 0.04, "B": -0.032},
            "B": {"A": -0.032, "B": 0.04},
        },
        "high_correlation_pairs": [
            {
                "left": "A",
                "right": "B",
                "correlation": -0.8,
                "samples": 168,
            }
        ],
    }
    same_side_account = PaperAccount(
        cash=20_000.0,
        positions={
            "A": Position(symbol="A", direction="long", qty=400.0, entry_price=10.0, opened_ts=NOW),
            "B": Position(symbol="B", direction="long", qty=400.0, entry_price=10.0, opened_ts=NOW),
        },
    )
    same_side = _portfolio_risk_context(same_side_account, {"A": 10.0, "B": 10.0}, 20_000.0, model)
    assert same_side["same_side_high_correlation_clusters"] == []
    assert same_side["position_co_risk_clusters"] == []

    opposite_account = same_side_account.model_copy(deep=True)
    opposite_account.positions["B"].direction = "short"
    opposite = _portfolio_risk_context(opposite_account, {"A": 10.0, "B": 10.0}, 20_000.0, model)
    assert opposite["same_side_high_correlation_clusters"] == []
    assert opposite["max_position_co_risk_cluster_risk_share"] == 1.0
    assert opposite["position_co_risk_clusters"][0]["side"] == "mixed"


def test_curated_stress_paths_are_not_mislabeled_as_portfolio_var_or_es():
    model = {
        "available": True,
        "residual_vol_annualized": {"A": 0.2, "B": 0.2},
        "covariance_annualized": {
            "A": {"A": 0.04, "B": 0.0},
            "B": {"A": 0.0, "B": 0.04},
        },
        "high_correlation_pairs": [],
        "residual_horizon_scenarios": {
            "24": {
                "available": True,
                "observations": 100,
                "scenario_selection": {
                    "method": "cross_sectional_rms_extremes_plus_even_time_grid",
                    "maximum_stored_scenarios": 12,
                    "stored_scenarios_are_distributionally_representative": False,
                },
                "expected_shortfall_97_5_lower_by_symbol": {"A": -0.08, "B": -0.06},
                "expected_shortfall_97_5_upper_by_symbol": {"A": 0.07, "B": 0.09},
                "scenarios": [
                    {"residual_returns": {"A": -0.10, "B": 0.10}},
                    {"residual_returns": {"A": 0.05, "B": -0.05}},
                    {"residual_returns": {"A": 0.0, "B": 0.0}},
                ],
            }
        },
    }
    account = PaperAccount(
        cash=20_000.0,
        last_funding_ts=NOW,
        positions={
            "A": Position(
                symbol="A", direction="long", qty=100.0, entry_price=10.0, opened_ts=NOW
            ),
            "B": Position(
                symbol="B", direction="short", qty=100.0, entry_price=10.0, opened_ts=NOW
            ),
        },
    )

    stress = _portfolio_risk_context(
        account, {"A": 10.0, "B": 10.0}, 20_000.0, model
    )["historical_residual_stress"]["24"]

    assert stress["available"] is True
    assert stress["observations"] == 100
    assert stress["stored_scenarios"] == 3
    assert stress["full_distribution_portfolio_es_available"] is False
    assert stress["var_97_5_usd"] is None
    assert stress["expected_shortfall_97_5_usd"] is None
    assert stress["curated_worst_joint_scenario_usd"] == pytest.approx(-200.0)
    assert stress["componentwise_marginal_tail_sum_97_5_usd"] == pytest.approx(-170.0)
    assert "cannot estimate portfolio VaR/ES" in stress["warning"]


def test_frozen_shadow_benchmark_charges_conservative_turnover_costs():
    curve = {"2k": 2.0, "5k": 4.0, "10k": 8.0, "20k": 12.0}
    evidence = {
        symbol: {
            "symbol": symbol,
            "mark": 10.0,
            "liquidity_mid": 10.0,
            "depth_usd_bid": 1_000_000.0,
            "depth_usd_ask": 1_000_000.0,
            "slippage_curve_buy_bps": curve,
            "slippage_curve_sell_bps": curve,
        }
        for symbol in ("A", "B")
    }
    realism = ExecutionRealism(allow_partial_fills=False)

    first = _cost_net_benchmark_outcome(
        [("A", "long"), ("B", "short")],
        {"A": 0.02, "B": -0.01},
        evidence,
        {},
        shadow_gross_usd=10_000.0,
        execution_realism=realism,
    )

    assert first["available"] is True
    assert first["gross_price_return_frac"] == pytest.approx(0.015)
    assert first["turnover_usd"] == pytest.approx(10_000.0)
    # Each $5k order looks up the $10k curve after the 50% displayed-depth haircut.
    assert first["friction_usd"] == pytest.approx(14.125)
    assert first["transaction_cost_frac"] == pytest.approx(0.0014125)
    assert first["cost_net_price_return_frac"] == pytest.approx(0.0135875)

    unchanged = _cost_net_benchmark_outcome(
        [("A", "long"), ("B", "short")],
        {"A": 0.02, "B": -0.01},
        evidence,
        first["_target_signed_quantities"],
        shadow_gross_usd=10_000.0,
        execution_realism=realism,
    )
    assert unchanged["turnover_usd"] == 0.0
    assert unchanged["friction_usd"] == 0.0
    assert unchanged["cost_net_price_return_frac"] == pytest.approx(0.015)

    drifted_evidence = {
        **evidence,
        "A": {**evidence["A"], "mark": 12.0, "liquidity_mid": 12.0},
    }
    drifted = _cost_net_benchmark_outcome(
        [("A", "long"), ("B", "short")],
        {"A": 0.0, "B": 0.0},
        drifted_evidence,
        first["_target_signed_quantities"],
        shadow_gross_usd=10_000.0,
        execution_realism=realism,
    )
    # The carried A quantity appreciated 20%, so restoring 50/50 requires a $1,000 sale.
    assert drifted["turnover_usd"] == pytest.approx(1_000.0)
    assert drifted["friction_usd"] > 0.0
    assert drifted["cost_net_price_return_frac"] < drifted["gross_price_return_frac"]


def test_shadow_transition_advances_without_complete_future_outcome():
    curve = {"2k": 2.0, "5k": 4.0, "10k": 8.0, "20k": 12.0}
    evidence = {
        symbol: {
            "symbol": symbol,
            "mark": 10.0,
            "liquidity_mid": 10.0,
            "depth_usd_bid": 1_000_000.0,
            "depth_usd_ask": 1_000_000.0,
            "slippage_curve_buy_bps": curve,
            "slippage_curve_sell_bps": curve,
        }
        for symbol in ("A", "B")
    }

    result = _cost_net_benchmark_outcome(
        [("A", "long"), ("B", "short")],
        # B's future outcome is unavailable. That cannot retroactively cancel a knowable trade.
        {"A": 0.02},
        evidence,
        {},
        shadow_gross_usd=10_000.0,
        execution_realism=ExecutionRealism(allow_partial_fills=False),
    )

    assert result["available"] is False
    assert result["origin_transaction_available"] is True
    assert result["transition_executed"] is True
    assert result["outcome_complete"] is False
    assert result["outcome_missing_symbols"] == ["B"]
    assert result["ending_signed_quantities"] == {"A": 500.0, "B": -500.0}
    assert result["marked_gross_coverage_frac"] == pytest.approx(0.5)
    assert result["marked_gross_price_pnl_usd"] == pytest.approx(100.0)
    assert result["marked_cost_net_price_pnl_usd"] == pytest.approx(
        100.0 - result["friction_usd"]
    )
    assert result["gross_price_return_frac"] is None
    assert result["cost_net_price_return_frac"] is None


def test_shadow_information_ratio_is_never_usable_across_a_continuity_gap():
    history = []
    for cycle in range(1, 22):
        available = cycle != 7
        benchmark = {
            "available": available,
            "transition_executed": True,
            "cost_net_price_return_frac": 0.001 * ((cycle % 3) - 1) if available else None,
        }
        history.append(
            {
                "cycle": cycle,
                "scheduled_sequence_gap_before": cycle == 15,
                "desk_cost_net_price_edge_frac": 0.002 * (cycle % 2),
                "benchmarks": {
                    "cash": {"available": True, "gross_price_return_frac": 0.0},
                    "residual_momentum_ls": benchmark,
                    "selected_equal_weight_ls": benchmark,
                    "carry_ls": benchmark,
                    "no_change": benchmark,
                },
            }
        )

    comparisons = _benchmark_comparisons(history)
    momentum = comparisons["residual_momentum_ls"]
    assert momentum["paired_observations"] == 20
    assert momentum["continuity_gap_cycles"] == [7, 15]
    assert momentum["status"] == "gapped_history"
    assert momentum["information_ratio_annualized"] is None
    # A skipped scheduled desk observation invalidates even the otherwise continuous cash pair.
    assert comparisons["cash"]["status"] == "gapped_history"
    assert comparisons["cash"]["continuity_gap_cycles"] == [15]


def test_snapshot_marks_an_expired_position_thesis(tmp_path):
    state, memory, pending = _seed(tmp_path)
    account = PaperAccount(
        cash=20_000.0,
        last_funding_ts=NOW,
        positions={
            "A": Position(
                symbol="A",
                direction="long",
                qty=100.0,
                entry_price=10.0,
                opened_ts=datetime(2026, 8, 1, 0, 7, tzinfo=UTC),
                opened_cycle=1,
            )
        },
    )
    save_account(state, account)
    (pending / "evidence.json").write_text(
        json.dumps(
            [
                {
                    "symbol": "A",
                    "mark": 11.0,
                    "beta_clamped": 1.0,
                    "expected_funding_8h_bps": -1.0,
                }
            ]
        )
    )
    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=47, as_of_ts=NOW, starting_capital=20_000.0
    )
    seat = snapshot["positions"][0]
    assert seat["funding_intervals_held"] > 40
    assert seat["past_max_hold_horizon"] is True
    assert seat["cycles_held"] == 47


def test_fresh_multi_horizon_break_overrides_masking_legacy_endpoint(tmp_path):
    state, memory, pending = _seed(tmp_path)
    account = PaperAccount(
        cash=20_000.0,
        last_funding_ts=NOW,
        positions={
            "XRP": Position(
                symbol="XRP", direction="short", qty=100.0, entry_price=1.0, opened_ts=NOW
            )
        },
    )
    save_account(state, account)
    (pending / "evidence.json").write_text(
        json.dumps(
            [
                {
                    "symbol": "XRP",
                    "mark": 1.1,
                    "beta_clamped": 1.2,
                    "expected_funding_8h_bps": 0.2,
                    "momentum_pct": -5.0,
                    "momentum_24h_pct": 8.0,
                    "momentum_72h_pct": 21.0,
                    "momentum_168h_pct": 28.0,
                    "beta_adjusted_momentum_72h_pct": 8.0,
                    "beta_adjusted_momentum_168h_pct": 12.0,
                    "realized_vol": 0.40,
                }
            ]
        )
    )
    snapshot = build_performance_snapshot(
        state, memory, pending, cycle=2, as_of_ts=NOW, starting_capital=20_000.0
    )
    seat = snapshot["positions"][0]
    assert seat["price_regime"] == "trend"
    assert seat["trend_direction"] == "up"
    assert seat["trend_basis"] == "raw_72h_and_168h"
    assert seat["side_opposed_trend"] is True
    assert seat["persistent_relative_trend"] is True
    assert seat["relative_trend_direction"] == "up"
    assert seat["side_opposed_relative_trend"] is True


def test_snapshot_refuses_to_hide_unmarked_held_position(tmp_path):
    state, memory, pending = _seed(tmp_path)
    evidence = json.loads((pending / "evidence.json").read_text())
    (pending / "evidence.json").write_text(json.dumps(evidence[:1]))
    with pytest.raises(ValueError, match="lacks positive marks"):
        build_performance_snapshot(
            state, memory, pending, cycle=2, as_of_ts=NOW, starting_capital=20_000.0
        )


def test_snapshot_fails_closed_on_a_malformed_audit_row(tmp_path):
    state, memory, pending = _seed(tmp_path)
    path = state / "ledger.jsonl"
    original = path.read_text() + "{malformed loss}\n"
    path.write_text(original)
    with pytest.raises(ValueError, match="malformed JSONL row"):
        build_performance_snapshot(
            state, memory, pending, cycle=2, as_of_ts=NOW, starting_capital=20_000.0
        )
    assert path.read_text() == original


def test_performance_write_is_atomic_and_newline_terminated(tmp_path):
    path = tmp_path / "packet.json"
    write_performance_snapshot(path, {"cycle": 7})
    assert json.loads(path.read_text()) == {"cycle": 7}
    assert path.read_text().endswith("\n")
    assert not path.with_suffix(".json.tmp").exists()


def test_recent_window_appends_current_mark_and_preserves_alternating_signs():
    ledger = [
        {"cycle": 1, "closing_equity": 110.0, "turnover_usd": 1.0},
        {"cycle": 2, "closing_equity": 90.0, "turnover_usd": 2.0},
        {"cycle": 3, "closing_equity": 120.0, "turnover_usd": 3.0},
    ]
    window = _recent_windows(ledger, equity=100.0, starting_capital=100.0)["3"]
    # Endpoints are 110 -> 90 -> 120 -> current 100: loss, win, loss.
    assert window["start_equity"] == 110.0
    assert window["winning_marks"] == 1
    assert window["losing_marks"] == 2
    assert window["turnover_usd"] == 5.0


def test_time_based_performance_uses_fixed_daily_marks_and_calendar_boundaries():
    start = datetime(2026, 8, 17, 16, 7, tzinfo=UTC)
    heartbeats = [
        {
            "ts": (start + timedelta(days=i)).isoformat(),
            "schedule_slot": (start + timedelta(days=i)).isoformat(),
            "equity": 20_000.0 + i * 25.0,
        }
        for i in range(9)
    ]
    # A non-official intraday mark may support an exact boundary, but not daily Sharpe.
    heartbeats.append(
        {
            "ts": (start + timedelta(days=8, hours=4)).isoformat(),
            "schedule_slot": (start + timedelta(days=8, hours=4)).isoformat(),
            "equity": 20_250.0,
        }
    )
    now = start + timedelta(days=8, hours=6)
    result = _time_based_performance([], heartbeats, now=now, equity=20_260.0)

    assert result["daily_observations"] == 9
    assert result["daily_return_observations"] == 8
    assert result["sharpe_status"] == "insufficient_history"
    assert result["rolling_windows"]["7"]["available"] is True
    assert result["rolling_windows"]["7"]["return_frac"] == pytest.approx(20_260.0 / 20_025.0 - 1.0)
    assert result["calendar_week_to_date"]["available"] is True


def test_time_performance_preserves_gap_loss_and_never_labels_gapped_sharpe_usable():
    start = datetime(2026, 8, 1, 16, 7, tzinfo=UTC)
    heartbeats = [
        {"ts": start.isoformat(), "schedule_slot": start.isoformat(), "equity": 20_000.0},
        {
            "ts": (start + timedelta(days=4)).isoformat(),
            "schedule_slot": (start + timedelta(days=4)).isoformat(),
            "equity": 18_000.0,
        },
    ]
    result = _time_based_performance(
        [], heartbeats, now=start + timedelta(days=4, hours=1), equity=18_000.0
    )
    assert result["daily_return_observations"] == 4
    assert result["missing_daily_gaps"] == 3
    assert result["sharpe_status"] == "gapped_history"
    assert result["sortino_status"] == "gapped_history"
    assert result["daily_mean_return_frac"] is None
    assert result["daily_volatility_frac"] is None
    assert result["downside_deviation_frac"] is None
    assert result["sharpe_annualized"] is None
    assert result["sortino_annualized"] is None
    assert result["profitable_daily_rate"] is None
    assert result["diagnostic_gap_compounded_return_frac"] == pytest.approx(-0.10)
    diagnostic_mean = result["diagnostic_geometric_daily_equivalent_mean_return_frac"]
    assert (1.0 + diagnostic_mean) ** 4 - 1.0 == pytest.approx(-0.10)


def test_time_performance_serializes_no_downside_sortino_as_null():
    start = datetime(2026, 8, 1, 16, 7, tzinfo=UTC)
    heartbeats = [
        {
            "ts": (start + timedelta(days=i)).isoformat(),
            "schedule_slot": (start + timedelta(days=i)).isoformat(),
            "equity": 20_000.0 + 100.0 * i,
        }
        for i in range(3)
    ]
    result = _time_based_performance(
        [], heartbeats, now=start + timedelta(days=2, hours=1), equity=20_200.0
    )
    assert result["sortino_annualized"] is None
    assert result["sortino_status"] == "insufficient_history"
    assert result["sortino_computation_status"] == "no_downside_observations"
    assert "Infinity" not in json.dumps(result)


def test_completed_week_stats_do_not_bridge_a_missing_iso_week():
    first = datetime(2026, 1, 4, 16, 7, tzinfo=UTC)  # Sunday of ISO week 1
    third = first + timedelta(days=14)
    heartbeats = [
        {"ts": first.isoformat(), "schedule_slot": first.isoformat(), "equity": 20_000.0},
        {"ts": third.isoformat(), "schedule_slot": third.isoformat(), "equity": 22_000.0},
    ]
    result = _time_based_performance([], heartbeats, now=third + timedelta(days=1), equity=22_000.0)
    assert result["completed_week_close_observations"] == 2
    assert result["completed_week_return_observations"] == 0
    assert result["missing_completed_week_gaps"] == 1
    assert result["completed_week_status"] == "gapped_history"


def test_trailing_missing_daily_marks_make_old_sharpe_unusable():
    start = datetime(2026, 7, 1, 16, 7, tzinfo=UTC)
    heartbeats = [
        {
            "ts": (start + timedelta(days=i)).isoformat(),
            "schedule_slot": (start + timedelta(days=i)).isoformat(),
            "equity": 20_000.0 + i * 10.0,
        }
        for i in range(21)
    ]
    result = _time_based_performance(
        [], heartbeats, now=start + timedelta(days=23, hours=1), equity=20_150.0
    )

    assert result["daily_return_observations"] == 20
    assert result["missing_trailing_daily_marks"] == 3
    assert result["sharpe_status"] == "gapped_history"
    assert result["sortino_status"] == "gapped_history"
    assert result["sharpe_annualized"] is None
    assert result["profitable_daily_rate"] is None


def test_trailing_missing_sunday_close_invalidates_week_hit_rate():
    first = datetime(2026, 1, 4, 16, 7, tzinfo=UTC)
    second = first + timedelta(days=7)
    heartbeats = [
        {"ts": first.isoformat(), "schedule_slot": first.isoformat(), "equity": 20_000.0},
        {"ts": second.isoformat(), "schedule_slot": second.isoformat(), "equity": 21_000.0},
    ]
    result = _time_based_performance(
        [], heartbeats, now=second + timedelta(days=8), equity=21_500.0
    )

    assert result["completed_week_return_observations"] == 1
    assert result["missing_trailing_completed_weeks"] == 1
    assert result["completed_week_status"] == "gapped_history"
    assert result["profitable_week_rate"] is None
    assert result["diagnostic_profitable_week_rate"] == 1.0
