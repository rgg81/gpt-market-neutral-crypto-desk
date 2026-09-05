from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from futures_fund.account import PaperAccount
from futures_fund.adversary_binding import (
    AdversaryBindingError,
    specialist_reads_sha256,
    verify_exit_audits,
    verify_precheck_artifact,
    verify_revision_binding,
    verify_revision_citation_safety,
    verify_seat_action_audits,
    verify_verdict_binding,
)
from futures_fund.desk_contracts import (
    AdversaryVerdict,
    Book,
    BookLeg,
    BoundVerdict,
    CandidateReview,
    CitationCheck,
    DirectiveExceptionAudit,
    ExitAudit,
    HedgeAudit,
    MetricsEcho,
    RevisionConstraint,
    SpecialistRead,
    SpecialistSupportEcho,
)
from futures_fund.performance import PERFORMANCE_SCHEMA_VERSION, canonical_sha256
from futures_fund.precheck import PrecheckMetrics, compute_precheck
from futures_fund.prompt_guard import split_managed
from futures_fund.slippage import ExecutionRealism
from scripts.desk_reconcile import (
    _parse_specialist_reads,
    _revision_dispatch_sha256,
    _verify_decision_chain,
    main,
)
from scripts.desk_revision_receipt import main as revision_receipt_main

SYMBOLS = ("A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT", "D/USDT:USDT")
EVIDENCE = [
    {
        "symbol": symbol,
        "mark": 1.0,
        "beta_btc": 1.0,
        "est_slippage_bps_2k": 1.0,
        "expected_funding_8h_bps": 0.0,
    }
    for symbol in SYMBOLS
]
CURRENT_BOOK = [
    {"symbol": symbol, "side": "long" if i < 2 else "short", "target_notional": 4500.0}
    for i, symbol in enumerate(SYMBOLS)
]
META = {"cash": 20_000.0, "btc_symbol": "BTC/USDT:USDT"}


def _watchdog_receipt(cycle: int, as_of: datetime) -> dict:
    return {
        "schema_version": 1,
        "paper_only": True,
        "schedule_status": "ON_TIME",
        "last_completed_cycle": cycle - 1,
        "last_cycle_ts": (as_of - timedelta(hours=24)).isoformat(),
        "observed_at": as_of.isoformat(),
        "gap_hours": 24.0,
        "next_cycle": cycle,
    }


def _performance_packet(cycle: int, as_of: datetime, evidence: list[dict]) -> dict:
    return {
        "schema_version": PERFORMANCE_SCHEMA_VERSION,
        "cycle": cycle,
        "as_of_ts": as_of.isoformat(),
        "paper_only": True,
        "bindings": {
            "evidence_sha256": canonical_sha256(evidence),
            "account_sha256": canonical_sha256(PaperAccount(cash=20_000.0).to_dict()),
        },
    }


def _unavailable_thesis() -> dict:
    return {
        "available": False,
        "source_policy": "newest_prior_completion_only",
        "candidate_cycle": None,
        "committed_cycle": None,
        "manifest_bound_book_sha256": None,
        "symbol": None,
        "side": None,
        "seat_role": None,
        "expected_price_edge_frac": None,
        "edge_horizon_hours": None,
        "edge_calibration_basis": None,
        "invalidation_condition": None,
        "unavailable_reason": "legacy fixture has no manifest-bound committed thesis",
    }


def _position(
    symbol: str,
    *,
    side: str | None = None,
    seat_role: str = "alpha",
    age: float = 10.0,
    expired: bool = False,
) -> dict:
    index = SYMBOLS.index(symbol) if symbol in SYMBOLS else 0
    return {
        "symbol": symbol,
        "side": side or ("long" if index < 2 else "short"),
        "seat_role": seat_role,
        "funding_intervals_held": age,
        "past_max_hold_horizon": expired,
        "committed_thesis": _unavailable_thesis(),
    }


def _bind_performance(verdict: AdversaryVerdict, performance: dict) -> AdversaryVerdict:
    return verdict.model_copy(update={
        "performance_snapshot_sha256": canonical_sha256(performance),
    })


def _bind_reads(
    verdict: AdversaryVerdict,
    reads: dict[str, list[SpecialistRead]],
) -> AdversaryVerdict:
    return verdict.model_copy(update={
        "specialist_reads_sha256": specialist_reads_sha256(reads),
    })


def _bind_book_reads(
    book: Book,
    reads: dict[str, list[SpecialistRead]],
) -> Book:
    return book.model_copy(update={
        "specialist_reads_sha256": specialist_reads_sha256(reads),
    })


def _exit_audit(
    symbol: str,
    *,
    side: str,
    seat_role: str = "alpha",
    action: Literal["drop", "flip", "role_change"] = "drop",
) -> ExitAudit:
    return ExitAudit(
        symbol=symbol,
        prior_side=side,
        prior_seat_role=seat_role,
        action=action,
        friction_reviewed=True,
        current_evidence_reviewed=True,
        loss_control_or_opportunity_reviewed=True,
        beta_dollar_impact_reviewed=True,
        prior_thesis_provenance_reviewed=seat_role == "alpha",
        note="reviewed the incumbent exit, evidence, friction, and neutrality impact",
    )


def _book(*, first_notional: float = 4500.0) -> Book:
    return Book(
        legs=[
            BookLeg(
                symbol=symbol,
                side="long" if i < 2 else "short",
                target_notional=first_notional if i == 0 else 4500.0,
                edge_calibration_basis="test horizon-matched calibration",
                invalidation_condition="test objective invalidation",
            )
            for i, symbol in enumerate(SYMBOLS)
        ],
        stated_deploy_frac=(first_notional + 13_500.0) / 20_000.0,
        stated_dollar_residual_frac=abs(first_notional - 4500.0) / (
            first_notional + 13_500.0
        ),
        stated_beta_residual=(first_notional - 4500.0) / 20_000.0,
        turnover_legs_changed=0,
    )


def _precheck(book: Book, cycle: int = 7) -> PrecheckMetrics:
    return compute_precheck(
        book, EVIDENCE, cash=20_000.0, cycle=cycle, current_book=CURRENT_BOOK,
        meta_sha256=canonical_sha256(META),
    )


def _verdict(
    precheck: PrecheckMetrics,
    *,
    accept: bool = True,
    override_rationale: str = "",
) -> AdversaryVerdict:
    action_map = {
        "none": "hold", "entry": "new", "flip": "flip",
        "increase": "increase", "reduction": "reduction",
    }
    return AdversaryVerdict(
        accept=accept,
        cycle=precheck.cycle,
        precheck_sha256=precheck.sha256,
        entry_gate_policy_sha256="a" * 64,
        hard_ban_violations_confirmed=precheck.hard_ban_violations,
        metrics_echo=MetricsEcho(
            gross=precheck.gross,
            deploy_frac=precheck.deploy_frac,
            dollar_residual_frac=precheck.dollar_residual_frac,
            beta_residual=precheck.beta_residual,
            max_leg_frac_gross=precheck.max_leg_frac_gross,
            turnover_legs_changed=precheck.turnover_legs_changed,
            turnover_aggressive_legs_changed=precheck.turnover_aggressive_legs_changed,
            alpha_gross=precheck.alpha_gross,
            hedge_gross=precheck.hedge_gross,
            hedge_risk_reducing=precheck.hedge_risk_reducing,
            hedge_counterfactual_beta_net_usd=(
                precheck.hedge_counterfactual_beta_net_usd
            ),
            hedge_change_risk_reducing=precheck.hedge_change_risk_reducing,
            portfolio_residual_vol_annualized_frac_cash=(
                precheck.portfolio_residual_vol_annualized_frac_cash
            ),
            max_alpha_standalone_risk_share=precheck.max_alpha_standalone_risk_share,
            max_same_side_high_correlation_cluster_risk_share=(
                precheck.max_same_side_high_correlation_cluster_risk_share
            ),
            max_position_co_risk_cluster_risk_share=(
                precheck.max_position_co_risk_cluster_risk_share
            ),
            portfolio_expected_total_edge_usd_per_8h=(
                precheck.portfolio_expected_total_edge_usd_per_8h
            ),
        ),
        bounds_confirmed=[
            BoundVerdict(bound_id=bound.bound_id, ok=bound.ok) for bound in precheck.bounds
        ],
        seat_audits=[{
            "symbol": leg.symbol,
            "side": leg.side,
            "seat_role": leg.seat_role,
            "action": action_map[leg.material_effect],
            "forward_edge_supported": True,
            "risk_reviewed": True,
            "continuation_supported": True,
            "cash_or_replacement_compared": True,
            "forecast_calibration_reviewed": True,
            "invalidation_condition_reviewed": True,
            # The shared fixture models the first safe migration from legacy state. The newest
            # prior thesis is truthfully unavailable, so every retained seat is requalified from
            # the fresh specialist packet instead of silently backfilling history.
            "prior_thesis_provenance_reviewed": (
                action_map[leg.material_effect] in {"hold", "increase", "reduction"}
            ),
            "fresh_entry_requalified": (
                action_map[leg.material_effect] in {"hold", "increase", "reduction"}
            ),
            "note": "test seat audit",
        } for leg in precheck.legs if leg.seat_role == "alpha"],
        action_audits=[{
            "symbol": leg.symbol,
            "action": action_map[leg.material_effect],
            "entry_gate_passed": True,
            "opportunity_cost_compared": True,
            "forecast_calibration_reviewed": True,
            "risk_budget_reviewed": True,
            "supporting_specialists": [
                {"role": "technical", "lean": leg.side, "conviction": 0.6},
                {"role": "futures", "lean": leg.side, "conviction": 0.6},
            ],
            "note": "test action audit",
        } for leg in precheck.legs if leg.seat_role == "alpha" and leg.material_effect in {
            "entry", "flip", "increase"
        }],
        exit_audits=[
            _exit_audit(
                row.symbol,
                side=str(row.prior_side),
                seat_role=str(row.prior_seat_role),
                action=row.action,
            )
            for row in precheck.change_costs
            if row.action in {"drop", "flip", "role_change"}
            and row.prior_side is not None
            and row.prior_seat_role is not None
        ],
        override_rationale=override_rationale,
        objections=[] if accept else ["the original thesis is not supported"],
        demanded_changes=[] if accept else ["replace the unsupported original leg"],
        revision_constraints=[] if accept else [{
            "kind": "max_deploy_frac", "symbol": "", "value": 1.15,
            "note": "the revision must remain below the leverage ceiling",
        }],
    )


def _write(path: Path, model) -> None:
    path.write_text(model.model_dump_json(indent=2))


def _write_revision_receipts(
    pending: Path,
    verdict: AdversaryVerdict,
    original_book: Book,
    original_precheck: PrecheckMetrics,
    final_book: Book,
) -> None:
    dispatch_sha256 = _revision_dispatch_sha256(
        verdict, original_book, original_precheck
    )
    (pending / "revision_dispatch_receipt.json").write_text(json.dumps({
        "schema_version": 1,
        "cycle": verdict.cycle,
        "attempt": 1,
        "dispatch_sha256": dispatch_sha256,
    }))
    (pending / "revision_output_receipt.json").write_text(json.dumps({
        "schema_version": 1,
        "cycle": verdict.cycle,
        "attempt": 1,
        "dispatch_sha256": dispatch_sha256,
        "output_book_sha256": canonical_sha256(final_book.model_dump(mode="json")),
    }))


def test_valid_verdict_is_bound_to_exact_precheck():
    precheck = _precheck(_book())
    verify_precheck_artifact(precheck, precheck, cycle=7)
    verify_verdict_binding(_verdict(precheck), precheck, cycle=7)


def test_hedge_to_alpha_role_change_requires_fresh_seat_and_action_audits():
    book = Book(legs=[BookLeg(
        symbol=SYMBOLS[0], side="long", target_notional=4500.0,
        seat_role="alpha", expected_price_edge_frac=0.01,
    )])
    precheck = compute_precheck(
        book,
        EVIDENCE,
        cash=20_000.0,
        cycle=7,
        current_book=[{
            "symbol": SYMBOLS[0], "side": "long", "target_notional": 4500.0,
            "seat_role": "hedge",
        }],
        meta_sha256=canonical_sha256(META),
    )
    verdict = _verdict(precheck, accept=False)
    assert verdict.seat_audits[0].action == "new"
    assert verdict.action_audits[0].action == "new"
    verify_seat_action_audits(verdict, precheck, book)

    with pytest.raises(AdversaryBindingError, match="action_audits"):
        verify_seat_action_audits(
            verdict.model_copy(update={"action_audits": []}), precheck, book
        )


def test_typed_hedge_requires_truthful_bound_adversary_audit():
    evidence = [
        {**EVIDENCE[0], "beta_clamped": 1.5},
        {**EVIDENCE[1], "beta_clamped": 1.0},
        {
            "symbol": "BTC/USDT:USDT",
            "mark": 60_000.0,
            "beta_clamped": 1.0,
            "est_slippage_bps_2k": 1.0,
            "slippage_curve_bps": {"2k": 1.0},
            "expected_funding_8h_bps": 0.0,
        },
    ]
    current = [
        {"symbol": SYMBOLS[0], "side": "long", "target_notional": 4000.0},
        {"symbol": SYMBOLS[1], "side": "short", "target_notional": 4000.0},
    ]

    def proposal(hedge_side: str) -> tuple[Book, PrecheckMetrics, AdversaryVerdict]:
        book = Book(
            legs=[
                BookLeg(
                    symbol=SYMBOLS[0], side="long", target_notional=4000.0,
                    edge_calibration_basis="24h matched sample",
                    invalidation_condition="relative edge reverses",
                ),
                BookLeg(
                    symbol=SYMBOLS[1], side="short", target_notional=4000.0,
                    edge_calibration_basis="24h matched sample",
                    invalidation_condition="relative edge reverses",
                ),
                BookLeg(
                    symbol="BTC/USDT:USDT", side=hedge_side, target_notional=1000.0,
                    seat_role="hedge", is_new=True, hold_breaking_reason="reduce beta",
                ),
            ],
            stated_deploy_frac=0.45,
            stated_dollar_residual_frac=1_000.0 / 9_000.0,
            stated_beta_residual=0.05 if hedge_side == "short" else 0.15,
            turnover_legs_changed=1,
        )
        precheck = compute_precheck(
            book, evidence, cash=20_000.0, cycle=7, current_book=current
        )
        verdict = _verdict(precheck, override_rationale="fixture bounds")
        payload = verdict.model_dump(mode="json")
        payload["hedge_audit"] = {
            "symbol": "BTC/USDT:USDT",
            "side": hedge_side,
            "target_notional": 1000.0,
            "risk_reducing_vs_alpha_book": precheck.hedge_risk_reducing,
            "change_risk_reducing_vs_carried_hedge": (
                precheck.hedge_change_risk_reducing
            ),
            "counterfactual_reviewed": True,
            "carry_cost_reviewed": True,
            "liquidity_reviewed": True,
            "note": "exact before/after beta and costs reviewed",
        }
        return book, precheck, AdversaryVerdict.model_validate(payload)

    good_book, good_precheck, good_verdict = proposal("short")
    verify_verdict_binding(
        good_verdict,
        good_precheck,
        cycle=7,
        sentiment_reads=[],
        book=good_book,
    )
    incomplete_good = good_verdict.model_copy(update={
        "hedge_audit": good_verdict.hedge_audit.model_copy(
            update={"liquidity_reviewed": False}
        ),
    })
    with pytest.raises(AdversaryBindingError, match="complete risk/counterfactual/cost"):
        verify_verdict_binding(
            incomplete_good,
            good_precheck,
            cycle=7,
            sentiment_reads=[],
            book=good_book,
        )

    bad_book, bad_precheck, bad_verdict = proposal("long")
    assert not bad_precheck.hedge_risk_reducing
    with pytest.raises(AdversaryBindingError, match="B12 aggressive exception"):
        verify_verdict_binding(
            bad_verdict,
            bad_precheck,
            cycle=7,
            sentiment_reads=[],
            book=bad_book,
        )


def test_new_hedge_in_single_revision_requires_exact_revision_hedge_audit():
    evidence = [
        *EVIDENCE,
        {
            "symbol": "BTC/USDT:USDT",
            "mark": 60_000.0,
            "beta_clamped": 1.0,
            "est_slippage_bps_2k": 1.0,
            "slippage_curve_bps": {"2k": 1.0},
            "expected_funding_8h_bps": 0.0,
        },
    ]
    original_book = _book(first_notional=6500.0)
    original_book = original_book.model_copy(update={
        "legs": [
            original_book.legs[0].model_copy(
                update={"expected_price_edge_frac": 0.01}
            ),
            *original_book.legs[1:],
        ],
    })
    original_precheck = compute_precheck(
        original_book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=CURRENT_BOOK,
    )
    hedge = BookLeg(
        symbol="BTC/USDT:USDT",
        side="short",
        target_notional=2000.0,
        seat_role="hedge",
        is_new=True,
        hold_breaking_reason="neutralize residual beta",
    )
    final_book = original_book.model_copy(update={
        "legs": [*original_book.legs, hedge],
        "stated_deploy_frac": 1.1,
        "stated_dollar_residual_frac": 0.0,
        "stated_beta_residual": 0.0,
        "turnover_legs_changed": 2,
    })
    final_precheck = compute_precheck(
        final_book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=CURRENT_BOOK,
    )
    constraint = RevisionConstraint(
        schema_version=2,
        kind="permit_symbol_mutation",
        symbol="BTC/USDT:USDT",
        final_side="short",
        final_seat_role="hedge",
        max_expected_price_edge_frac=0.0,
        required_expected_price_edge_frac=0.0,
        required_edge_horizon_hours=24,
        note="authorize the exact risk-reducing beta hedge",
    )
    no_audit = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [constraint],
    })

    with pytest.raises(AdversaryBindingError, match="without a revision_hedge_audit"):
        verify_revision_binding(
            no_audit,
            original_book,
            original_precheck,
            final_book,
            final_precheck,
        )

    payload = no_audit.model_dump(mode="json")
    payload["revision_hedge_audit"] = {
        "symbol": "BTC/USDT:USDT",
        "side": "short",
        "target_notional": 2000.0,
        "risk_reducing_vs_alpha_book": True,
        "change_risk_reducing_vs_carried_hedge": True,
        "counterfactual_reviewed": True,
        "carry_cost_reviewed": True,
        "liquidity_reviewed": True,
        "note": "reviewed final beta, carry, depth and carried-BTC counterfactual",
    }
    with_audit = AdversaryVerdict.model_validate(payload)
    verify_verdict_binding(
        with_audit,
        original_precheck,
        cycle=7,
        sentiment_reads=[],
        book=original_book,
    )
    verify_revision_binding(
        with_audit,
        original_book,
        original_precheck,
        final_book,
        final_precheck,
    )


def test_verdict_requires_exact_selected_seat_and_aggressive_action_coverage():
    book = _book(first_notional=4_700.0)
    legs = list(book.legs)
    legs[0] = legs[0].model_copy(update={"expected_price_edge_frac": 0.05})
    book = book.model_copy(update={"legs": legs, "turnover_legs_changed": 1})
    precheck = _precheck(book)
    verdict = _verdict(precheck)
    verify_verdict_binding(verdict, precheck, cycle=7, sentiment_reads=[], book=book)

    with pytest.raises(AdversaryBindingError, match="seat_audits"):
        verify_verdict_binding(
            verdict.model_copy(update={"seat_audits": verdict.seat_audits[:-1]}),
            precheck, cycle=7, sentiment_reads=[], book=book,
        )
    with pytest.raises(AdversaryBindingError, match="action_audits"):
        verify_verdict_binding(
            verdict.model_copy(update={"action_audits": []}),
            precheck, cycle=7, sentiment_reads=[], book=book,
        )


def test_accepted_incumbent_requires_complete_continuation_and_calibration_audit():
    book = _book()
    precheck = _precheck(book)
    verdict = _verdict(precheck)
    performance = {"positions": []}
    weakened = _bind_performance(verdict.model_copy(update={
        "seat_audits": [
            audit.model_copy(update={"continuation_supported": False})
            if audit.symbol == SYMBOLS[0] else audit
            for audit in verdict.seat_audits
        ]
    }), performance)

    with pytest.raises(AdversaryBindingError, match="continuation/calibration"):
        verify_verdict_binding(
            weakened,
            precheck,
            cycle=7,
            sentiment_reads=[],
            performance_snapshot=performance,
            book=book,
        )


def test_accepted_aggressive_action_requires_calibration_and_incremental_risk_review():
    book = _book(first_notional=4_700.0).model_copy(update={"turnover_legs_changed": 1})
    book = book.model_copy(update={
        "legs": [
            book.legs[0].model_copy(update={"expected_price_edge_frac": 0.01}),
            *book.legs[1:],
        ],
    })
    precheck = _precheck(book)
    verdict = _verdict(precheck, override_rationale="test accepts fixture economics")
    weakened = verdict.model_copy(update={
        "action_audits": [
            audit.model_copy(update={"risk_budget_reviewed": False})
            for audit in verdict.action_audits
        ]
    })

    with pytest.raises(AdversaryBindingError, match="entry/calibration/risk gate"):
        verify_verdict_binding(
            weakened, precheck, cycle=7, sentiment_reads=[], book=book
        )


def test_aggressive_action_support_is_bound_to_actual_specialist_reads():
    book = _book(first_notional=4_700.0)
    book = book.model_copy(update={
        "legs": [
            book.legs[0].model_copy(update={"expected_price_edge_frac": 0.01}),
            *book.legs[1:],
        ],
        "turnover_legs_changed": 1,
    })
    precheck = _precheck(book)
    verdict = _verdict(precheck, override_rationale="test fixture accepts B12")
    reads = {
        role: [
            SpecialistRead(
                symbol=symbol,
                lean=("long" if symbol == SYMBOLS[0] and role != "sentiment" else "flat"),
                conviction=(0.6 if symbol == SYMBOLS[0] and role != "sentiment" else 0.0),
                rationale="test",
            )
            for symbol in SYMBOLS
        ]
        for role in ("sentiment", "technical", "futures")
    }
    book = _bind_book_reads(book, reads)
    verdict = _bind_reads(verdict, reads)
    verify_verdict_binding(
        verdict,
        precheck,
        cycle=7,
        sentiment_reads=reads["sentiment"],
        specialist_reads=reads,
        book=book,
    )

    bad_action = verdict.action_audits[0].model_copy(
        update={"supporting_specialists": []}
    )
    with pytest.raises(AdversaryBindingError, match="chosen-side specialist"):
        verify_verdict_binding(
            verdict.model_copy(update={"action_audits": [bad_action]}),
            precheck,
            cycle=7,
            sentiment_reads=reads["sentiment"],
            specialist_reads=reads,
            book=book,
        )


def test_binder_does_not_create_a_permanent_specialist_vote_threshold():
    book = _book(first_notional=4_700.0).model_copy(
        update={"turnover_legs_changed": 1}
    )
    book = book.model_copy(update={
        "legs": [
            book.legs[0].model_copy(update={"expected_price_edge_frac": 0.01}),
            *book.legs[1:],
        ],
    })
    precheck = _precheck(book)
    verdict = _verdict(precheck, override_rationale="test fixture accepts B12")
    verdict = verdict.model_copy(update={
        "action_audits": [audit.model_copy(update={
            "supporting_specialists": [], "opposing_specialists": [],
        }) for audit in verdict.action_audits],
    })
    flat_reads = {
        role: [SpecialistRead(
            symbol=symbol,
            lean="flat",
            conviction=0.0,
            rationale="no current role edge",
        ) for symbol in SYMBOLS]
        for role in ("sentiment", "technical", "futures")
    }
    book = _bind_book_reads(book, flat_reads)
    verdict = _bind_reads(verdict, flat_reads)

    # GPT's hash-bound managed policy and the Adversary's booleans own gate semantics. Code only
    # proves the empty support/opposition echoes are truthful when all persisted reads are flat.
    verify_verdict_binding(
        verdict,
        precheck,
        cycle=7,
        sentiment_reads=flat_reads["sentiment"],
        specialist_reads=flat_reads,
        book=book,
    )


def test_complete_specialist_hash_catches_rationale_only_mutation():
    book = _book(first_notional=4_700.0)
    book = book.model_copy(update={
        "legs": [
            book.legs[0].model_copy(update={"expected_price_edge_frac": 0.01}),
            *book.legs[1:],
        ],
        "turnover_legs_changed": 1,
    })
    precheck = _precheck(book)
    reads = {
        role: [
            SpecialistRead(
                symbol=symbol,
                lean="flat",
                conviction=0.0,
                rationale=f"original {role} rationale",
                evidence=[f"original {role} evidence"],
            )
            for symbol in SYMBOLS
        ]
        for role in ("sentiment", "technical", "futures")
    }
    book = _bind_book_reads(book, reads)
    verdict = _bind_reads(
        _verdict(precheck, override_rationale="fixture accepts its economics").model_copy(
            update={
                "action_audits": [
                    audit.model_copy(update={
                        "supporting_specialists": [],
                        "opposing_specialists": [],
                    })
                    for audit in _verdict(
                        precheck, override_rationale="fixture accepts its economics"
                    ).action_audits
                ],
            }
        ),
        reads,
    )
    changed_reads = {role: list(rows) for role, rows in reads.items()}
    changed_reads["technical"] = [
        rows.model_copy(update={"rationale": "post-verdict mutation"})
        if index == 0 else rows
        for index, rows in enumerate(reads["technical"])
    ]

    with pytest.raises(AdversaryBindingError, match="complete read packet"):
        verify_verdict_binding(
            verdict,
            precheck,
            cycle=7,
            sentiment_reads=changed_reads["sentiment"],
            specialist_reads=changed_reads,
            book=book,
        )


def test_performance_packet_hash_catches_any_post_review_mutation():
    precheck = _precheck(_book())
    performance = {"positions": []}
    verdict = _bind_performance(_verdict(precheck), performance)
    mutated = {**performance, "desk_pnl": {"net_pnl": 123.0}}

    with pytest.raises(AdversaryBindingError, match="performance_snapshot_sha256"):
        verify_verdict_binding(
            verdict,
            precheck,
            cycle=7,
            performance_snapshot=mutated,
        )


def test_b12_raw_boundary_failure_requires_exact_aggressive_directive_audit():
    target_payback = 10.0000004
    incremental_notional = 200.0
    horizon_intervals = 168.0 / 8.0
    # Legacy fixture slippage is 1bp, so the $200 increased slice costs $0.24 round trip.
    edge_frac = 0.24 * horizon_intervals / (
        incremental_notional * target_payback
    )
    base = _book(first_notional=4700.0)
    book = base.model_copy(update={
        "legs": [
            base.legs[0].model_copy(update={
                "expected_price_edge_frac": edge_frac,
                "edge_horizon_hours": 168,
            }),
            *base.legs[1:],
        ],
        "turnover_legs_changed": 1,
    })
    precheck = _precheck(book)
    assert {
        bound.bound_id for bound in precheck.bounds if not bound.ok
    } == {"B12"}
    row = precheck.change_costs[0]
    assert row.action == "increase"
    assert row.payback_intervals == pytest.approx(target_payback, abs=1e-12)
    assert row.payback_intervals > 10.0

    verdict = _verdict(
        precheck,
        override_rationale="exact directive is required for the raw B12 boundary failure",
    )
    with pytest.raises(AdversaryBindingError, match="B12 aggressive exception"):
        verify_verdict_binding(verdict, precheck, cycle=7)

    directive_sha256 = canonical_sha256("authorize exact A boundary increase")
    wrong_scope = verdict.model_copy(update={
        "binding_user_directive_sha256": directive_sha256,
        "directive_exception_audits": [DirectiveExceptionAudit(
            bound_id="B12",
            symbols=[SYMBOLS[1]],
            note="wrong aggressive symbol",
        )],
    })
    with pytest.raises(AdversaryBindingError, match="offending symbols"):
        verify_verdict_binding(
            wrong_scope,
            precheck,
            cycle=7,
            binding_user_directive_sha256=directive_sha256,
        )

    authorized = wrong_scope.model_copy(update={
        "directive_exception_audits": [DirectiveExceptionAudit(
            bound_id="B12",
            symbols=[SYMBOLS[0]],
            note="exact raw-payback offender authorized by the bound directive",
        )],
    })
    verify_verdict_binding(
        authorized,
        precheck,
        cycle=7,
        binding_user_directive_sha256=directive_sha256,
    )


def test_b9_exception_is_exactly_bound_to_directive_and_aggressive_symbols():
    base = _book()
    legs = [
        leg.model_copy(update={
            "target_notional": 4_700.0,
            "expected_price_edge_frac": 0.01,
        }) if index < 3 else leg
        for index, leg in enumerate(base.legs)
    ]
    provisional = base.model_copy(update={
        "legs": legs,
        "turnover_legs_changed": 3,
    })
    measured = _precheck(provisional)
    book = provisional.model_copy(update={
        "stated_deploy_frac": measured.deploy_frac,
        "stated_dollar_residual_frac": measured.dollar_residual_frac,
        "stated_beta_residual": measured.beta_residual,
    })
    precheck = _precheck(book)
    assert {
        bound.bound_id for bound in precheck.bounds if not bound.ok
    } == {"B9"}
    verdict = _verdict(
        precheck,
        override_rationale="exact one-shot directive authorizes this cold-start deployment",
    )
    with pytest.raises(AdversaryBindingError, match="B9 exception"):
        verify_verdict_binding(verdict, precheck, cycle=7)

    directive_sha256 = canonical_sha256("deploy these exact three symbols")
    aggressive_symbols = [SYMBOLS[index] for index in range(3)]
    authorized = verdict.model_copy(update={
        "binding_user_directive_sha256": directive_sha256,
        "directive_exception_audits": [DirectiveExceptionAudit(
            bound_id="B9",
            symbols=aggressive_symbols,
            note="the exact bound directive names this cold-start deployment",
        )],
    })
    verify_verdict_binding(
        authorized,
        precheck,
        cycle=7,
        binding_user_directive_sha256=directive_sha256,
    )
    wrong_scope = authorized.model_copy(update={
        "directive_exception_audits": [DirectiveExceptionAudit(
            bound_id="B9",
            symbols=aggressive_symbols[:-1],
            note="attempted partial scope",
        )],
    })
    with pytest.raises(AdversaryBindingError, match="every final aggressive symbol"):
        verify_verdict_binding(
            wrong_scope,
            precheck,
            cycle=7,
            binding_user_directive_sha256=directive_sha256,
        )


def test_accepted_book_cannot_override_b7_b8_b10_or_b11():
    false_claim_book = _book().model_copy(update={"stated_deploy_frac": 0.0})
    b7_precheck = _precheck(false_claim_book)

    resize = _book(first_notional=4_700.0)
    resize = resize.model_copy(update={
        "legs": [
            resize.legs[0].model_copy(update={"expected_price_edge_frac": 0.01}),
            *resize.legs[1:],
        ],
        "stated_deploy_frac": 0.91,
        "stated_dollar_residual_frac": 200.0 / 18_200.0,
        "stated_beta_residual": 0.01,
        "turnover_legs_changed": 0,
    })
    b8_precheck = _precheck(resize)

    b10_precheck = compute_precheck(
        _book(),
        [{**row, "est_slippage_bps_2k": 100.0} for row in EVIDENCE],
        cash=20_000.0,
        cycle=7,
        current_book=CURRENT_BOOK,
        meta_sha256=canonical_sha256(META),
    )

    aggressive_book = _book(first_notional=4_700.0).model_copy(update={
        "legs": [
            _book(first_notional=4_700.0).legs[0].model_copy(
                update={"expected_price_edge_frac": 0.05}
            ),
            *_book(first_notional=4_700.0).legs[1:],
        ],
        "turnover_legs_changed": 1,
    })
    b10_aggressive_precheck = compute_precheck(
        aggressive_book,
        [
            {**row, "est_slippage_bps_2k": 60.0}
            if row["symbol"] == SYMBOLS[0]
            else row
            for row in EVIDENCE
        ],
        cash=20_000.0,
        cycle=7,
        current_book=CURRENT_BOOK,
        meta_sha256=canonical_sha256(META),
    )

    b11_precheck = compute_precheck(
        _book(),
        EVIDENCE[:-1],
        cash=20_000.0,
        cycle=7,
        current_book=CURRENT_BOOK,
        meta_sha256=canonical_sha256(META),
    )
    for precheck, bound_id in (
        (b7_precheck, "B7"),
        (b8_precheck, "B8"),
        (b10_precheck, "B10"),
        (b10_aggressive_precheck, "B10"),
        (b11_precheck, "B11"),
    ):
        assert not next(
            bound for bound in precheck.bounds if bound.bound_id == bound_id
        ).ok
        verdict = _verdict(
            precheck,
            override_rationale="attempted structural override",
        )
        with pytest.raises(AdversaryBindingError, match=bound_id):
            verify_verdict_binding(verdict, precheck, cycle=7)


def test_adversary_must_echo_objective_hard_bans_and_cannot_accept_them():
    book = _book(first_notional=4_700.0).model_copy(update={
        "legs": [
            _book(first_notional=4_700.0).legs[0].model_copy(
                update={"expected_price_edge_frac": 0.05}
            ),
            *_book(first_notional=4_700.0).legs[1:],
        ],
        "turnover_legs_changed": 1,
    })
    evidence = [
        {
            **row,
            "depth_usd_bid": 50_000.0,
            "depth_usd_ask": 200_000.0,
        }
        if row["symbol"] == SYMBOLS[0]
        else row
        for row in EVIDENCE
    ]
    precheck = compute_precheck(
        book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=CURRENT_BOOK,
        meta_sha256=canonical_sha256(META),
    )
    assert [row.rule_id for row in precheck.hard_ban_violations] == [
        "aggressive_alpha_low_depth"
    ]

    accepted = _verdict(precheck)
    with pytest.raises(AdversaryBindingError, match="objective hard-ban violations"):
        verify_verdict_binding(accepted, precheck, cycle=7)

    rejected = _verdict(precheck, accept=False)
    verify_verdict_binding(rejected, precheck, cycle=7)
    omitted_payload = rejected.model_dump(mode="json")
    omitted_payload.pop("hard_ban_violations_confirmed")
    omitted_echo = AdversaryVerdict.model_validate(omitted_payload)
    with pytest.raises(AdversaryBindingError, match="omits hard_ban"):
        verify_verdict_binding(omitted_echo, precheck, cycle=7)
    missing_echo = rejected.model_copy(update={"hard_ban_violations_confirmed": []})
    with pytest.raises(AdversaryBindingError, match="does not exactly echo"):
        verify_verdict_binding(missing_echo, precheck, cycle=7)


def test_unreviewed_revision_cannot_retain_objective_hard_bans():
    book = _book(first_notional=4_700.0).model_copy(update={
        "turnover_legs_changed": 1,
    })
    evidence = [
        {
            **row,
            "depth_usd_bid": 50_000.0,
            "depth_usd_ask": 200_000.0,
        }
        if row["symbol"] == SYMBOLS[0]
        else row
        for row in EVIDENCE
    ]
    precheck = compute_precheck(
        book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=CURRENT_BOOK,
        meta_sha256=canonical_sha256(META),
    )
    assert precheck.hard_ban_violations

    with pytest.raises(AdversaryBindingError, match="revision retains objective hard-ban"):
        verify_revision_binding(_verdict(precheck, accept=False), book, precheck, book, precheck)


def test_expired_hold_must_echo_age_and_be_freshly_requalified():
    book = _book()
    precheck = _precheck(book)
    verdict = _verdict(precheck)
    positions = [
        _position(
            symbol,
            age=50.0 if symbol == SYMBOLS[0] else 10.0,
            expired=symbol == SYMBOLS[0],
        )
        for symbol in SYMBOLS
    ]
    performance = {"positions": positions}
    patched_seats = [
        audit.model_copy(update={
            "position_age_intervals": (
                50.0 if audit.symbol == SYMBOLS[0] else 10.0
            ),
            "past_max_hold_horizon": audit.symbol == SYMBOLS[0],
            "fresh_entry_requalified": audit.symbol != SYMBOLS[0],
        })
        for audit in verdict.seat_audits
    ]
    verdict = _bind_performance(
        verdict.model_copy(update={"seat_audits": patched_seats}), performance
    )
    with pytest.raises(AdversaryBindingError, match="requiring fresh-entry requalification"):
        verify_verdict_binding(
            verdict,
            precheck,
            cycle=7,
            sentiment_reads=[],
            performance_snapshot=performance,
            book=book,
        )

    reads = {
        "sentiment": [],
        "technical": [SpecialistRead(
            symbol=SYMBOLS[0], lean="long", conviction=0.42,
            rationale="fresh chosen-side read",
        )],
        "futures": [SpecialistRead(
            symbol=SYMBOLS[0], lean="short", conviction=0.31,
            rationale="fresh opposing read",
        )],
    }
    book = _bind_book_reads(book, reads)
    requalified_without_echoes = _bind_reads(verdict.model_copy(update={
        "seat_audits": [
            audit.model_copy(update={"fresh_entry_requalified": True})
            if audit.symbol == SYMBOLS[0] else audit
            for audit in verdict.seat_audits
        ]
    }), reads)
    with pytest.raises(AdversaryBindingError, match="chosen-side specialist"):
        verify_verdict_binding(
            requalified_without_echoes,
            precheck,
            cycle=7,
            sentiment_reads=[],
            specialist_reads=reads,
            performance_snapshot=performance,
            book=book,
        )

    requalified = requalified_without_echoes.model_copy(update={
        "seat_audits": [
                audit.model_copy(update={
                    "continuation_supporting_specialists": [SpecialistSupportEcho(
                        role="technical", lean="long", conviction=0.42
                    )],
                    "continuation_opposing_specialists": [SpecialistSupportEcho(
                        role="futures", lean="short", conviction=0.31
                    )],
                    "requalification_supporting_specialists": [SpecialistSupportEcho(
                    role="technical", lean="long", conviction=0.42
                )],
                "requalification_opposing_specialists": [SpecialistSupportEcho(
                    role="futures", lean="short", conviction=0.31
                )],
            }) if audit.symbol == SYMBOLS[0] else audit
            for audit in requalified_without_echoes.seat_audits
        ]
    })
    verify_verdict_binding(
        requalified,
        precheck,
        cycle=7,
        sentiment_reads=[],
        specialist_reads=reads,
        performance_snapshot=performance,
        book=book,
    )


def test_flip_does_not_inherit_expiry_from_the_closed_old_side():
    base = _book()
    flipped = base.legs[0].model_copy(update={
        "side": "short",
        "is_new": True,
        "hold_breaking_reason": "old long thesis broke; fresh short thesis qualifies",
        "expected_price_edge_frac": 0.01,
    })
    book = base.model_copy(update={
        "legs": [flipped, *base.legs[1:]],
        "stated_dollar_residual_frac": 0.5,
        "stated_beta_residual": -0.45,
        "turnover_legs_changed": 1,
        "turnover_justification": "flip the invalidated old side",
    })
    precheck = _precheck(book)
    verdict = _verdict(precheck, override_rationale="fixture accepts unrelated failed bounds")
    verdict = verdict.model_copy(update={
        "action_audits": [audit.model_copy(update={
            "supporting_specialists": [], "opposing_specialists": [],
        }) for audit in verdict.action_audits],
    })
    flat_reads = {
        role: [SpecialistRead(
            symbol=symbol, lean="flat", conviction=0.0, rationale="fixture flat read"
        ) for symbol in SYMBOLS]
        for role in ("sentiment", "technical", "futures")
    }
    book = _bind_book_reads(book, flat_reads)
    positions = [
        _position(
            symbol,
            age=50.0 if symbol == SYMBOLS[0] else 10.0,
            expired=symbol == SYMBOLS[0],
        )
        for symbol in SYMBOLS
    ]
    performance = {"positions": positions}
    verdict = _bind_reads(_bind_performance(verdict, performance), flat_reads)
    verdict = verdict.model_copy(update={
        "seat_audits": [
            audit.model_copy(update={
                "position_age_intervals": (
                    None if audit.symbol == SYMBOLS[0] else 10.0
                ),
            })
            for audit in verdict.seat_audits
        ],
    })

    verify_verdict_binding(
        verdict,
        precheck,
        cycle=7,
        sentiment_reads=flat_reads["sentiment"],
        specialist_reads=flat_reads,
        performance_snapshot=performance,
        book=book,
    )


def test_rejected_expired_seat_cannot_self_attest_requalification_before_revision():
    book = _book()
    precheck = _precheck(book)
    positions = [
        _position(
            symbol,
            age=50.0 if symbol == SYMBOLS[0] else 10.0,
            expired=symbol == SYMBOLS[0],
        )
        for symbol in SYMBOLS
    ]
    performance = {"positions": positions}
    reads = {
        "sentiment": [],
        "technical": [SpecialistRead(
            symbol=SYMBOLS[0], lean="long", conviction=0.42,
            rationale="fresh chosen-side read",
        )],
        "futures": [SpecialistRead(
            symbol=SYMBOLS[0], lean="short", conviction=0.31,
            rationale="fresh opposing read",
        )],
    }
    book = _bind_book_reads(book, reads)
    rejected = _bind_reads(_bind_performance(
        _verdict(precheck, accept=False).model_copy(update={
        "seat_audits": [
            audit.model_copy(update={
                "position_age_intervals": 50.0,
                "past_max_hold_horizon": True,
                "fresh_entry_requalified": True,
                "requalification_supporting_specialists": [],
                "requalification_opposing_specialists": [],
            }) if audit.symbol == SYMBOLS[0] else audit.model_copy(update={
                "position_age_intervals": 10.0,
            })
            for audit in _verdict(precheck, accept=False).seat_audits
        ],
        "revision_constraints": [RevisionConstraint(
            kind="drop_symbol", symbol=SYMBOLS[1], note="unrelated correction"
        )],
        "exit_audits": [_exit_audit(SYMBOLS[1], side="long")],
    }), performance), reads)

    with pytest.raises(AdversaryBindingError, match="chosen-side specialist"):
        verify_verdict_binding(
            rejected,
            precheck,
            cycle=7,
            sentiment_reads=[],
            specialist_reads=reads,
            performance_snapshot=performance,
            book=book,
        )


def test_revision_cannot_introduce_aggressive_alpha_actions_absent_from_original():
    original_book = _book()
    original_precheck = _precheck(original_book)
    final_legs = [
        leg.model_copy(update={"target_notional": 4_700.0})
        if leg.symbol in {SYMBOLS[0], SYMBOLS[2]} else leg
        for leg in original_book.legs
    ]
    final_book = original_book.model_copy(update={
        "legs": final_legs,
        "stated_deploy_frac": 0.94,
        "stated_dollar_residual_frac": 0.0,
        "stated_beta_residual": 0.0,
        "turnover_legs_changed": 2,
        "turnover_justification": "balanced revision-only alpha increases",
    })
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [
            RevisionConstraint(
                schema_version=2,
                kind="permit_symbol_mutation",
                symbol=symbol,
                final_side="long" if symbol == SYMBOLS[0] else "short",
                final_seat_role="alpha",
                max_expected_price_edge_frac=0.0,
                required_expected_price_edge_frac=0.0,
                min_edge_horizon_hours=24,
                required_edge_horizon_hours=24,
                required_edge_calibration_basis="test horizon-matched calibration",
                required_invalidation_condition="test objective invalidation",
                note="attempt to add balanced alpha risk only in the revision",
            )
            for symbol in (SYMBOLS[0], SYMBOLS[2])
        ],
        "revision_allowed_failing_bounds": [
            bound.bound_id for bound in final_precheck.bounds if not bound.ok
        ],
    })

    with pytest.raises(AdversaryBindingError, match="unreviewed aggressive alpha action"):
        verify_revision_binding(
            verdict, original_book, original_precheck, final_book, final_precheck
        )


def test_revision_cannot_amplify_a_tiny_reviewed_increase():
    original_legs = [
        leg.model_copy(update={"target_notional": 4_500.02})
        if leg.symbol in {SYMBOLS[0], SYMBOLS[2]} else leg
        for leg in _book().legs
    ]
    original_book = _book().model_copy(update={
        "legs": original_legs,
        "stated_deploy_frac": 0.900002,
        "stated_dollar_residual_frac": 0.0,
        "stated_beta_residual": 0.0,
        "turnover_legs_changed": 2,
        "turnover_justification": "two tiny reviewed increases",
    })
    original_precheck = _precheck(original_book)
    final_legs = [
        leg.model_copy(update={"target_notional": 6_000.0})
        if leg.symbol in {SYMBOLS[0], SYMBOLS[2]} else leg
        for leg in original_book.legs
    ]
    final_book = original_book.model_copy(update={
        "legs": final_legs,
        "stated_deploy_frac": 1.05,
        "turnover_justification": "attempt to amplify the reviewed slices",
    })
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [
            RevisionConstraint(
                schema_version=2,
                kind="permit_symbol_mutation",
                symbol=symbol,
                final_side="long" if symbol == SYMBOLS[0] else "short",
                final_seat_role="alpha",
                max_expected_price_edge_frac=0.0,
                required_expected_price_edge_frac=0.0,
                min_edge_horizon_hours=24,
                required_edge_horizon_hours=24,
                required_edge_calibration_basis="test horizon-matched calibration",
                required_invalidation_condition="test objective invalidation",
                note="attempt to amplify a previously reviewed action",
            )
            for symbol in (SYMBOLS[0], SYMBOLS[2])
        ],
        "revision_allowed_failing_bounds": [
            bound.bound_id for bound in final_precheck.bounds if not bound.ok
        ],
    })

    with pytest.raises(AdversaryBindingError, match="beyond the Adversary-reviewed increment"):
        verify_revision_binding(
            verdict, original_book, original_precheck, final_book, final_precheck
        )


def test_specialist_reads_require_complete_duplicate_free_coverage():
    raw = [
        {
            "symbol": symbol,
            "lean": "flat",
            "conviction": 0.0,
            "rationale": "no signal",
            "evidence": [],
        }
        for symbol in SYMBOLS
    ]
    assert len(_parse_specialist_reads(raw, list(SYMBOLS))) == len(SYMBOLS)

    with pytest.raises(ValueError, match="missing"):
        _parse_specialist_reads(raw[:-1], list(SYMBOLS))
    with pytest.raises(ValueError, match="duplicates=1"):
        _parse_specialist_reads([*raw[:-1], raw[0]], list(SYMBOLS))


def test_verdict_rejects_wrong_hash_and_metric_echo():
    precheck = _precheck(_book())
    wrong_hash = _verdict(precheck).model_copy(update={"precheck_sha256": "f" * 64})
    with pytest.raises(AdversaryBindingError, match="precheck_sha256"):
        verify_verdict_binding(wrong_hash, precheck, cycle=7)
    with pytest.raises(AdversaryBindingError, match="entry_gate_policy_sha256"):
        verify_verdict_binding(
            _verdict(precheck),
            precheck,
            cycle=7,
            entry_gate_policy_sha256="b" * 64,
        )

    verdict = _verdict(precheck)
    wrong_echo = verdict.metrics_echo.model_copy(update={"gross": precheck.gross + 1000.0})
    verdict = verdict.model_copy(update={"metrics_echo": wrong_echo})
    with pytest.raises(AdversaryBindingError, match="metrics_echo.gross"):
        verify_verdict_binding(verdict, precheck, cycle=7)


def _sentiment_read(symbol: str = "A/USDT:USDT") -> SpecialistRead:
    return SpecialistRead(
        symbol=symbol,
        lean="long",
        conviction=0.3,
        rationale="fresh permanent announcement",
        evidence=[
            "2026-07-30 | Project | permanent announcement | "
            f"https://example.com/{symbol.split('/')[0].lower()}-announcement"
        ],
    )


def test_citation_audit_covers_every_nonflat_read_and_exact_url():
    precheck = _precheck(_book())
    read = _sentiment_read()
    url = "https://example.com/a-announcement"
    verdict = _verdict(precheck).model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=True,
                    material_to_book=True,
                    checked_urls=[url],
                    note="Opened the permanent announcement; the dated claim is present.",
                )
            ]
        }
    )
    verify_verdict_binding(
        verdict, precheck, cycle=7, sentiment_reads=[read], book=_book()
    )

    with pytest.raises(AdversaryBindingError, match="cover every non-flat"):
        verify_verdict_binding(
            _verdict(precheck), precheck, cycle=7, sentiment_reads=[read], book=_book()
        )

    wrong_url = verdict.model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=True,
                    material_to_book=True,
                    checked_urls=["https://example.com/different-page"],
                    note="Opened a page.",
                )
            ]
        }
    )
    with pytest.raises(AdversaryBindingError, match="do not match"):
        verify_verdict_binding(
            wrong_url, precheck, cycle=7, sentiment_reads=[read], book=_book()
        )


def test_accepted_selected_leg_cannot_use_unsupported_sentiment():
    precheck = _precheck(_book())
    read = _sentiment_read()
    verdict = _verdict(precheck).model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=False,
                    material_to_book=True,
                    checked_urls=["https://example.com/a-announcement"],
                    note="The URL does not contain the claimed activation.",
                )
            ]
        }
    )
    with pytest.raises(AdversaryBindingError, match="uses unsupported"):
        verify_verdict_binding(
            verdict, precheck, cycle=7, sentiment_reads=[read], book=_book()
        )


def test_unsupported_unselected_sentiment_is_recorded_without_vetoing_book():
    precheck = _precheck(_book())
    read = _sentiment_read("UNI/USDT:USDT")
    verdict = _verdict(precheck).model_copy(
        update={
            "citation_checks": [
                CitationCheck(
                    symbol=read.symbol,
                    supported=False,
                    material_to_book=False,
                    checked_urls=["https://example.com/uni-announcement"],
                    note="The URL says proposed, while the read says activated.",
                )
            ]
        }
    )
    verify_verdict_binding(
        verdict, precheck, cycle=7, sentiment_reads=[read], book=_book()
    )


def test_precheck_must_match_current_book_and_its_own_hash():
    final = _precheck(_book())
    original = _precheck(_book(first_notional=4400.0))
    with pytest.raises(AdversaryBindingError, match="current book and evidence"):
        verify_precheck_artifact(original, final, cycle=7)

    tampered = final.model_copy(update={"gross": final.gross + 1.0})
    with pytest.raises(AdversaryBindingError, match="contents"):
        verify_precheck_artifact(tampered, final, cycle=7)


def test_accepting_a_precheck_failure_needs_explicit_override():
    failed = compute_precheck(Book(), [], cash=20_000.0, cycle=7)
    rulings = [
        BoundVerdict(
            bound_id=bound.bound_id,
            ok=True,
            note="Adversary override" if not bound.ok else "",
        )
        for bound in failed.bounds
    ]
    verdict = AdversaryVerdict(
        accept=True,
        cycle=7,
        precheck_sha256=failed.sha256,
        entry_gate_policy_sha256="a" * 64,
        metrics_echo=MetricsEcho(
            gross=failed.gross,
            deploy_frac=failed.deploy_frac,
            dollar_residual_frac=failed.dollar_residual_frac,
            beta_residual=failed.beta_residual,
            max_leg_frac_gross=failed.max_leg_frac_gross,
            turnover_legs_changed=failed.turnover_legs_changed,
            turnover_aggressive_legs_changed=failed.turnover_aggressive_legs_changed,
        ),
        bounds_confirmed=rulings,
        hard_ban_violations_confirmed=failed.hard_ban_violations,
    )
    with pytest.raises(AdversaryBindingError, match="override_rationale"):
        verify_verdict_binding(verdict, failed, cycle=7)


def test_accepted_decision_chain_binds_final_artifacts(tmp_path):
    book = _book()
    precheck = _precheck(book)
    _write(tmp_path / "precheck.json", precheck)

    _verify_decision_chain(
        tmp_path,
        cycle=7,
        meta=META,
        evidence=EVIDENCE,
        current_book=CURRENT_BOOK,
        book=book,
        verdict=_verdict(precheck),
    )


def test_rejected_decision_chain_binds_original_and_requires_revision_trail(tmp_path):
    original_book = _book(first_notional=4400.0)
    original_precheck = _precheck(original_book)
    final_book = _book()
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [RevisionConstraint(
            schema_version=2,
            kind="min_symbol_notional",
            symbol=SYMBOLS[0],
            value=4500.0,
            final_side="long",
            final_seat_role="alpha",
            max_expected_price_edge_frac=0.0,
            required_expected_price_edge_frac=0.0,
            min_edge_horizon_hours=24,
            required_edge_horizon_hours=24,
            required_edge_calibration_basis="test horizon-matched calibration",
            required_invalidation_condition="test objective invalidation",
            note="restore the under-sized long seat",
        )],
    })
    _write(tmp_path / "precheck.json", final_precheck)

    with pytest.raises(AdversaryBindingError, match="lacks pm_book_original"):
        _verify_decision_chain(
            tmp_path,
            cycle=7,
            meta=META,
            evidence=EVIDENCE,
            current_book=CURRENT_BOOK,
            book=final_book,
            verdict=verdict,
        )

    _write(tmp_path / "pm_book_original.json", original_book)
    _write(tmp_path / "precheck_original.json", original_precheck)
    _write_revision_receipts(
        tmp_path, verdict, original_book, original_precheck, final_book
    )
    _verify_decision_chain(
        tmp_path,
        cycle=7,
        meta=META,
        evidence=EVIDENCE,
        current_book=CURRENT_BOOK,
        book=final_book,
        verdict=verdict,
    )


def test_rejected_revision_is_mechanically_bound_to_drop_and_allowed_final_failures():
    symbols = tuple(f"{letter}/USDT:USDT" for letter in "ABCDEF")
    evidence = [
        {"symbol": symbol, "mark": 1.0, "beta_btc": 1.0,
         "est_slippage_bps_2k": 1.0, "expected_funding_8h_bps": 0.0}
        for symbol in symbols
    ]
    current = [
        {"symbol": symbol, "side": "long" if index < 3 else "short",
         "target_notional": 3000.0,
         "edge_calibration_basis": "test horizon-matched calibration",
         "invalidation_condition": "test objective invalidation"}
        for index, symbol in enumerate(symbols)
    ]
    original_book = Book(
        legs=[BookLeg(**row) for row in current],
        stated_deploy_frac=0.9,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )
    original_precheck = compute_precheck(
        original_book, evidence, cash=20_000.0, cycle=7, current_book=current
    )
    final_book = Book(
        legs=[original_book.legs[index] for index in (1, 2, 4, 5)],
        stated_deploy_frac=0.6,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
        turnover_legs_changed=2,
        turnover_justification="drop invalidated A and matching short D",
    )
    final_precheck = compute_precheck(
        final_book, evidence, cash=20_000.0, cycle=7, current_book=current
    )
    allowed = [bound.bound_id for bound in final_precheck.bounds if not bound.ok]
    assert set(allowed) <= {"B1", "B12"}
    verdict = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [
            RevisionConstraint(
                kind="drop_symbol",
                symbol=symbols[0],
                note="the long alpha thesis is invalidated",
            ),
            RevisionConstraint(
                kind="drop_symbol",
                symbol=symbols[3],
                note="matching short removal preserves dollar neutrality",
            ),
        ],
        "revision_allowed_failing_bounds": allowed,
        "exit_audits": [
            _exit_audit(symbols[0], side="long"),
            _exit_audit(symbols[3], side="short"),
        ],
    })

    verify_revision_binding(
        verdict, original_book, original_precheck, final_book, final_precheck
    )
    with pytest.raises(AdversaryBindingError, match="drop_symbol"):
        verify_revision_binding(
            verdict, original_book, original_precheck, original_book, original_precheck
        )

    no_overrides = verdict.model_copy(update={"revision_allowed_failing_bounds": []})
    with pytest.raises(AdversaryBindingError, match="non-approved failing bounds"):
        verify_revision_binding(
            no_overrides, original_book, original_precheck, final_book, final_precheck
        )


def test_revision_restoring_an_unreviewed_old_side_requires_fallback_incumbent_audit():
    final_book = _book()
    flipped_legs = list(final_book.legs)
    flipped_legs[0] = flipped_legs[0].model_copy(update={
        "side": "short",
        "is_new": True,
        "hold_breaking_reason": "proposed flip",
    })
    original_book = final_book.model_copy(update={
        "legs": flipped_legs,
        "turnover_legs_changed": 1,
    })
    original_precheck = _precheck(original_book)
    final_precheck = _precheck(final_book)
    base = _verdict(original_precheck, accept=False).model_dump(mode="json")
    base["seat_audits"] = [
        {**audit, "forward_edge_supported": False}
        if audit["symbol"] == SYMBOLS[0] else audit
        for audit in base["seat_audits"]
    ]
    base["action_audits"] = [
        {
            **audit,
            "entry_gate_passed": False,
            "supporting_specialists": [],
            "opposing_specialists": [],
        }
        if audit["symbol"] == SYMBOLS[0] else audit
        for audit in base["action_audits"]
    ]
    base["revision_constraints"] = [{
        "schema_version": 2,
        "kind": "permit_symbol_mutation",
        "symbol": SYMBOLS[0],
        "final_side": "long",
        "final_seat_role": "alpha",
        "max_expected_price_edge_frac": 0.0,
        "required_expected_price_edge_frac": 0.0,
        "required_edge_horizon_hours": 24,
        "required_edge_calibration_basis": "test horizon-matched calibration",
        "required_invalidation_condition": "test objective invalidation",
        "note": "the old side may survive only if separately requalified",
    }]
    base["revision_fallback_seat_audits"] = []
    no_fallback = AdversaryVerdict.model_validate(base)

    with pytest.raises(AdversaryBindingError, match="not covered.*fallback incumbent"):
        verify_revision_binding(
            no_fallback,
            original_book,
            original_precheck,
            final_book,
            final_precheck,
        )

    base["revision_fallback_seat_audits"] = [{
        "symbol": SYMBOLS[0],
        "side": "long",
        "seat_role": "alpha",
        "action": "hold",
        "forward_edge_supported": True,
        "risk_reviewed": True,
        "continuation_supported": True,
        "cash_or_replacement_compared": True,
        "forecast_calibration_reviewed": True,
        "invalidation_condition_reviewed": True,
        "prior_thesis_provenance_reviewed": True,
        "continuation_supporting_specialists": [],
        "continuation_opposing_specialists": [],
        "position_age_intervals": 50.0,
        "past_max_hold_horizon": True,
        "fresh_entry_requalified": True,
        "requalification_supporting_specialists": [],
        "requalification_opposing_specialists": [],
        "note": "old long independently requalified against cash and replacement",
    }]
    base["seat_audits"] = [
        {
            **audit,
            "position_age_intervals": (
                None if audit["symbol"] == SYMBOLS[0] else 10.0
            ),
        }
        for audit in base["seat_audits"]
    ]
    with_fallback = AdversaryVerdict.model_validate(base)
    flat_reads = {
        role: [SpecialistRead(
            symbol=symbol,
            lean="flat",
            conviction=0.0,
            rationale="no directional call",
        ) for symbol in SYMBOLS]
        for role in ("sentiment", "technical", "futures")
    }
    original_book = _bind_book_reads(original_book, flat_reads)
    final_book = _bind_book_reads(final_book, flat_reads)
    performance = {
        "positions": [
            _position(
                symbol,
                age=50.0 if symbol == SYMBOLS[0] else 10.0,
                expired=symbol == SYMBOLS[0],
            )
            for symbol in SYMBOLS
        ]
    }
    with_fallback = _bind_reads(
        _bind_performance(with_fallback, performance), flat_reads
    )

    verify_verdict_binding(
        with_fallback,
        original_precheck,
        cycle=7,
        sentiment_reads=flat_reads["sentiment"],
        specialist_reads=flat_reads,
        performance_snapshot=performance,
        book=original_book,
    )
    hedge_inventory = {
        "positions": [{**performance["positions"][0], "seat_role": "hedge"}]
    }
    with pytest.raises(AdversaryBindingError, match="held current-side position"):
        verify_verdict_binding(
            _bind_performance(with_fallback, hedge_inventory),
            original_precheck,
            cycle=7,
            sentiment_reads=flat_reads["sentiment"],
            specialist_reads=flat_reads,
            performance_snapshot=hedge_inventory,
            book=original_book,
        )
    verify_revision_binding(
        with_fallback,
        original_book,
        original_precheck,
        final_book,
        final_precheck,
    )


def test_revision_cannot_mutate_an_unlisted_symbol_or_use_a_vacuous_constraint():
    original_book = _book()
    original_precheck = _precheck(original_book)
    changed_book = _book(first_notional=4400.0)
    changed_precheck = _precheck(changed_book)
    verdict = _verdict(original_precheck, accept=False)

    with pytest.raises(AdversaryBindingError, match="outside the Adversary envelope"):
        verify_revision_binding(
            verdict, original_book, original_precheck, changed_book, changed_precheck
        )
    with pytest.raises(AdversaryBindingError, match="no structured constraint"):
        verify_revision_binding(
            verdict, original_book, original_precheck, original_book, original_precheck
        )


def test_revision_cannot_preserve_a_seat_the_adversary_explicitly_failed():
    original_book = _book()
    original_precheck = _precheck(original_book)
    final_book = original_book.model_copy(update={
        "legs": [original_book.legs[index] for index in (0, 2, 3)],
    })
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False)
    verdict = verdict.model_copy(update={
        "seat_audits": [
            audit.model_copy(update={"forward_edge_supported": False})
            if audit.symbol == SYMBOLS[0] else audit
            for audit in verdict.seat_audits
        ],
        "revision_constraints": [RevisionConstraint(
            kind="drop_symbol", symbol=SYMBOLS[1], note="independent correction"
        )],
        "exit_audits": [_exit_audit(SYMBOLS[1], side="long")],
    })
    with pytest.raises(AdversaryBindingError, match="retains failed Adversary seat audit"):
        verify_revision_binding(
            verdict, original_book, original_precheck, final_book, final_precheck
        )


def test_revision_cannot_preserve_an_aggressive_slice_whose_gate_failed():
    original_book = _book(first_notional=4_700.0).model_copy(
        update={"turnover_legs_changed": 1}
    )
    original_precheck = _precheck(original_book)
    final_book = original_book.model_copy(update={
        "legs": [original_book.legs[index] for index in (0, 2, 3)],
    })
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False)
    verdict = verdict.model_copy(update={
        "action_audits": [
            audit.model_copy(update={"entry_gate_passed": False})
            for audit in verdict.action_audits
        ],
        "revision_constraints": [RevisionConstraint(
            kind="drop_symbol", symbol=SYMBOLS[1], note="independent correction"
        )],
        "exit_audits": [_exit_audit(SYMBOLS[1], side="long")],
    })
    with pytest.raises(AdversaryBindingError, match="retains failed Adversary action audit"):
        verify_revision_binding(
            verdict, original_book, original_precheck, final_book, final_precheck
        )


def test_typed_mutation_binds_forecast_ceiling_and_horizon():
    original_book = _book()
    original_precheck = _precheck(original_book)
    changed_leg = original_book.legs[0].model_copy(update={
        "expected_price_edge_frac": 0.01,
        "edge_horizon_hours": 24,
    })
    final_book = original_book.model_copy(update={
        "legs": [changed_leg, *original_book.legs[1:]],
    })
    final_precheck = _precheck(final_book)

    def _with_constraint(max_edge: float, min_horizon: int):
        return _verdict(original_precheck, accept=False).model_copy(update={
            "revision_constraints": [RevisionConstraint(
                schema_version=2,
                kind="permit_symbol_mutation",
                symbol=SYMBOLS[0],
                final_side="long",
                final_seat_role="alpha",
                max_expected_price_edge_frac=max_edge,
                required_expected_price_edge_frac=max_edge,
                min_edge_horizon_hours=min_horizon,
                required_edge_horizon_hours=min_horizon,
                required_edge_calibration_basis="test horizon-matched calibration",
                required_invalidation_condition="test objective invalidation",
                note="typed forecast-only thesis correction",
            )],
        })

    with pytest.raises(AdversaryBindingError, match="does not match the exact Adversary"):
        verify_revision_binding(
            _with_constraint(0.005, 24),
            original_book,
            original_precheck,
            final_book,
            final_precheck,
        )
    with pytest.raises(AdversaryBindingError, match="shorter than Adversary minimum"):
        verify_revision_binding(
            _with_constraint(0.01, 72),
            original_book,
            original_precheck,
            final_book,
            final_precheck,
        )
    verify_revision_binding(
        _with_constraint(0.01, 24),
        original_book,
        original_precheck,
        final_book,
        final_precheck,
    )


def test_typed_revision_cannot_launder_alpha_thesis_metadata():
    original_book = _book()
    original_precheck = _precheck(original_book)
    changed_leg = original_book.legs[0].model_copy(update={
        "target_notional": 4_400.0,
        "edge_calibration_basis": "",
        "invalidation_condition": "",
    })
    final_book = original_book.model_copy(update={
        "legs": [changed_leg, *original_book.legs[1:]],
        "turnover_legs_changed": 1,
    })
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [RevisionConstraint(
            schema_version=2,
            kind="max_symbol_notional",
            symbol=SYMBOLS[0],
            value=4_400.0,
            final_side="long",
            final_seat_role="alpha",
            max_expected_price_edge_frac=0.0,
            required_expected_price_edge_frac=0.0,
            min_edge_horizon_hours=24,
            required_edge_horizon_hours=24,
            required_edge_calibration_basis="test horizon-matched calibration",
            required_invalidation_condition="test objective invalidation",
            note="reduce this seat without changing its reviewed thesis",
        )],
    })

    with pytest.raises(AdversaryBindingError, match="alpha thesis requires"):
        verify_revision_binding(
            verdict, original_book, original_precheck, final_book, final_precheck
        )


def test_v2_typed_revision_binds_exact_horizon_without_legacy_minimum():
    original_book = _book()
    original_precheck = _precheck(original_book)
    changed_leg = original_book.legs[0].model_copy(update={
        "expected_price_edge_frac": 0.01,
        "edge_horizon_hours": 24,
    })
    final_book = original_book.model_copy(update={
        "legs": [changed_leg, *original_book.legs[1:]],
    })
    final_precheck = _precheck(final_book)

    def _verdict_for(required_horizon: int):
        return _verdict(original_precheck, accept=False).model_copy(update={
            "revision_constraints": [RevisionConstraint(
                schema_version=2,
                kind="permit_symbol_mutation",
                symbol=SYMBOLS[0],
                final_side="long",
                final_seat_role="alpha",
                max_expected_price_edge_frac=0.01,
                required_expected_price_edge_frac=0.01,
                required_edge_horizon_hours=required_horizon,
                required_edge_calibration_basis="test horizon-matched calibration",
                required_invalidation_condition="test objective invalidation",
                note="bind the exact reviewed forecast horizon",
            )],
        })

    verify_revision_binding(
        _verdict_for(24), original_book, original_precheck, final_book, final_precheck
    )
    with pytest.raises(AdversaryBindingError, match="does not match the exact"):
        verify_revision_binding(
            _verdict_for(72), original_book, original_precheck, final_book, final_precheck
        )


def test_v2_typed_revision_cannot_lower_edge_and_reuse_the_original_seat_audit():
    original = _book()
    original_leg = original.legs[0].model_copy(
        update={"expected_price_edge_frac": 0.02}
    )
    original_book = original.model_copy(update={
        "legs": [original_leg, *original.legs[1:]]
    })
    original_precheck = _precheck(original_book)
    final_leg = original_leg.model_copy(update={"expected_price_edge_frac": -0.01})
    final_book = original_book.model_copy(update={
        "legs": [final_leg, *original_book.legs[1:]]
    })
    final_precheck = _precheck(final_book)
    verdict = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [RevisionConstraint(
            schema_version=2,
            kind="permit_symbol_mutation",
            symbol=SYMBOLS[0],
            final_side="long",
            final_seat_role="alpha",
            max_expected_price_edge_frac=0.02,
            required_expected_price_edge_frac=0.02,
            required_edge_horizon_hours=24,
            required_edge_calibration_basis="test horizon-matched calibration",
            required_invalidation_condition="test objective invalidation",
            note="preserve the exact Adversary-reviewed forecast",
        )],
    })

    with pytest.raises(AdversaryBindingError, match="does not match the exact Adversary"):
        verify_revision_binding(
            verdict,
            original_book,
            original_precheck,
            final_book,
            final_precheck,
        )


def test_revision_cannot_select_sentiment_the_adversary_marked_unsupported():
    read = _sentiment_read()
    precheck = _precheck(_book())
    verdict = _verdict(precheck, accept=False).model_copy(update={
        "citation_checks": [CitationCheck(
            symbol=read.symbol,
            supported=False,
            material_to_book=False,
            checked_urls=["https://example.com/a-announcement"],
            note="the page does not support the specialist claim",
        )],
    })
    with pytest.raises(AdversaryBindingError, match="unsupported sentiment"):
        verify_revision_citation_safety(verdict, [read], _book())


def test_reconcile_halts_before_state_mutation_on_unbound_verdict(
    tmp_path, capsys, monkeypatch
):
    memory = tmp_path / "memory"
    pending = memory / "pending" / "7"
    pending.mkdir(parents=True)
    now = datetime.now(UTC)
    risk_model: dict = {}
    scoring_marks = {
        "as_of_ts": now.isoformat(),
        "marks": {row["symbol"]: row["mark"] for row in EVIDENCE},
    }
    meta = {
        "cycle": 7,
        "now": now.isoformat(),
        **META,
        "symbols": list(SYMBOLS),
        "evidence_sha256": canonical_sha256(EVIDENCE),
        "risk_model_sha256": canonical_sha256(risk_model),
        "scoring_marks_sha256": canonical_sha256(scoring_marks),
    }
    watchdog_receipt = _watchdog_receipt(7, now)
    meta["watchdog_receipt"] = watchdog_receipt
    meta["watchdog_receipt_sha256"] = canonical_sha256(watchdog_receipt)
    (memory / "pending" / "current.json").write_text(json.dumps(
        {"cycle": 7, "dir": str(pending), "created": now.isoformat()}
    ))
    (pending / "meta.json").write_text(json.dumps(meta))
    (pending / "evidence.json").write_text(json.dumps(EVIDENCE))
    (pending / "risk_model.json").write_text(json.dumps(risk_model))
    (pending / "scoring_marks.json").write_text(json.dumps(scoring_marks))

    performance = _performance_packet(7, now, EVIDENCE)
    performance["bindings"].update({
        "risk_model_sha256": canonical_sha256(risk_model),
        "meta_sha256": canonical_sha256(meta),
    })
    performance["positions"] = []
    (pending / "performance_snapshot.json").write_text(json.dumps(performance))
    (pending / "performance_snapshot.sha256").write_text(
        canonical_sha256(performance) + "\n"
    )
    monkeypatch.setattr(
        "scripts.desk_reconcile.build_performance_snapshot",
        lambda *args, **kwargs: performance,
    )
    monkeypatch.setattr("scripts.desk_reconcile.validate_candle_audit", lambda *args: None)
    monkeypatch.setattr(
        "scripts.desk_reconcile.load_bound_decision_start_provenance",
        lambda *args, **kwargs: {
            "schema_version": 1,
            "captured_at": now.isoformat(),
            "provenance_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        "scripts.desk_reconcile.load_bound_pre_reflection_performance",
        lambda pending, meta: performance,
    )
    monkeypatch.setattr(
        "scripts.desk_reconcile.build_watchdog_receipt",
        lambda *_args, **_kwargs: watchdog_receipt,
    )

    raw_reads = [
        {
            "symbol": symbol,
            "lean": "flat",
            "conviction": 0.0,
            "rationale": "no signal",
            "evidence": [],
        }
        for symbol in SYMBOLS
    ]
    for role in ("sentiment", "technical", "futures"):
        (pending / f"{role}_reads.json").write_text(json.dumps(raw_reads))
    normalized_reads = {
        role: _parse_specialist_reads(raw_reads, list(SYMBOLS))
        for role in ("sentiment", "technical", "futures")
    }
    reads_sha256 = specialist_reads_sha256(normalized_reads)
    (pending / "specialist_reads.sha256").write_text(reads_sha256 + "\n")

    original = _book()
    book = original.model_copy(update={
        "specialist_reads_sha256": reads_sha256,
        "legs": [
            leg.model_copy(update={
                "is_new": True,
                "hold_breaking_reason": "production-path validation fixture",
            })
            for leg in original.legs
        ],
        "candidate_reviews": [
            CandidateReview(
                symbol=leg.symbol,
                side=leg.side,
                status="selected",
                exclusion_reason="selected",
                expected_price_edge_frac=leg.expected_price_edge_frac,
                edge_horizon_hours=24,
                counterfactual_notional=leg.target_notional,
                supporting_specialists=[],
                rationale="selected production-path validation fixture",
            )
            for leg in original.legs
        ],
        "turnover_legs_changed": 4,
    })
    precheck = compute_precheck(
        book,
        EVIDENCE,
        cash=20_000.0,
        cycle=7,
        current_book=[],
        risk_model=risk_model,
        meta_sha256=canonical_sha256(meta),
        execution_realism=ExecutionRealism(),
    )
    _write(pending / "pm_book.json", book)
    _write(pending / "precheck.json", precheck)
    pm_region = split_managed(Path("agents/pm.md").read_text())[1]
    policy_sha256 = canonical_sha256(pm_region)
    (pending / "entry_gate_policy.json").write_text(json.dumps({
        "source": "agents/pm.md",
        "managed_region": pm_region,
        "sha256": policy_sha256,
    }))
    wrong_verdict = _bind_reads(
        _bind_performance(
            _verdict(
                precheck,
                override_rationale="fixture intentionally reaches the hash-binding rejection",
            ),
            performance,
        ),
        normalized_reads,
    ).model_copy(update={
        "precheck_sha256": "f" * 64,
        "entry_gate_policy_sha256": policy_sha256,
    })
    _write(pending / "adversary.json", wrong_verdict)

    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "--memory-dir", str(memory)]) == 1
    # Recovery may create its lock directory, but an unbound verdict must publish no PAPER state.
    assert not (state / "account.json").exists()
    assert not (state / "ledger.jsonl").exists()
    assert not (state / "rebal" / "cycle" / "7" / "complete.json").exists()
    output = capsys.readouterr().out
    assert "decision-chain validation failed" in output
    assert "precheck_sha256 does not match" in output


def test_reconcile_main_uses_fresh_book_mid_and_persists_execution_audit(
    tmp_path, monkeypatch
):
    """Production reconciliation keeps decision provenance but fills from one fresh book."""
    symbol = "SOL/USDT:USDT"
    decision_ts = datetime.now(UTC) - timedelta(minutes=15)
    directive_text = "Authorize this exact four-leg cold-start execution test basket."
    directive_sha256 = canonical_sha256(directive_text)
    evidence = [{
        "symbol": symbol,
        "mark": 100.0,
        "beta_btc": 1.0,
        "est_slippage_bps_2k": 1.0,
        "expected_funding_8h_bps": 0.0,
        "funding_rate": 0.0,
        "funding_interval_h": 8.0,
        }, {
            "symbol": "BTC/USDT:USDT",
        "mark": 60_000.0,
        "beta_btc": 1.0,
        "est_slippage_bps_2k": 1.0,
        "expected_funding_8h_bps": 0.0,
            "funding_rate": 0.0,
            "funding_interval_h": 8.0,
        }]
    extra_alpha = ("A/USDT:USDT", "B/USDT:USDT")
    evidence.extend(
        {
            "symbol": extra_symbol,
            "mark": 100.0,
            "beta_btc": 1.0,
            "est_slippage_bps_2k": 1.0,
            "expected_funding_8h_bps": 0.0,
            "funding_rate": 0.0,
            "funding_interval_h": 8.0,
        }
        for extra_symbol in extra_alpha
    )
    alpha_symbols = (symbol, *extra_alpha)
    risk_model = {
        "return_label": "hourly_log_return_minus_beta_clamped_times_btc",
        "lookback_hours": 168,
        "residual_vol_annualized": {name: 0.2 for name in alpha_symbols},
        "covariance_annualized": {
            left: {right: 0.04 if left == right else 0.0 for right in alpha_symbols}
            for left in alpha_symbols
        },
        "high_correlation_pairs": [],
    }

    memory = tmp_path / "memory"
    pending = memory / "pending" / "7"
    pending.mkdir(parents=True)
    (memory / "pending" / "current.json").write_text(json.dumps(
        {"cycle": 7, "dir": str(pending), "created": decision_ts.isoformat()}
    ))
    scoring_marks = {
        "as_of_ts": decision_ts.isoformat(),
        "marks": {row["symbol"]: row["mark"] for row in evidence},
    }
    expected_requests = [
        {
            "unified_symbol": unified,
            "symbol": exchange_symbol,
            "timeframe": timeframe,
            "requested_limit": 200 if timeframe == "1h" else 60,
        }
            for unified, exchange_symbol in (
                (symbol, "SOLUSDT"),
                ("BTC/USDT:USDT", "BTCUSDT"),
                (extra_alpha[0], "AUSDT"),
                (extra_alpha[1], "BUSDT"),
            )
        for timeframe in ("1h", "1d")
    ]
    audit_requests = []
    for row in expected_requests:
        interval_ms = 3_600_000 if row["timeframe"] == "1h" else 86_400_000
        checked_ms = int(decision_ts.timestamp() * 1000)
        open_ms = checked_ms // interval_ms * interval_ms
        audit_requests.append({
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "requested_limit": row["requested_limit"],
            "rows": row["requested_limit"],
            "latest_open_ts": datetime.fromtimestamp(open_ms / 1000, tz=UTC).isoformat(),
            "latest_close_ts": datetime.fromtimestamp(
                (open_ms + interval_ms - 1) / 1000, tz=UTC
            ).isoformat(),
            "checked_at": decision_ts.isoformat(),
            "current_candle_present": True,
            "range_complete": True,
        })
    meta = {
        "cycle": 7,
        "now": decision_ts.isoformat(),
        "cash": 20_000.0,
        "symbols": [symbol, *extra_alpha],
        "btc_symbol": "BTC/USDT:USDT",
        "evidence_sha256": canonical_sha256(evidence),
        "risk_model_sha256": canonical_sha256(risk_model),
        "scoring_marks_sha256": canonical_sha256(scoring_marks),
        "binding_user_directive_sha256": directive_sha256,
        "expected_candle_requests": expected_requests,
        "candle_data": {
            "source": "binance-proxy", "all_fresh": True,
            "request_count": len(audit_requests), "requests": audit_requests,
        },
    }
    watchdog_receipt = _watchdog_receipt(7, decision_ts)
    meta["watchdog_receipt"] = watchdog_receipt
    meta["watchdog_receipt_sha256"] = canonical_sha256(watchdog_receipt)
    (pending / "meta.json").write_text(json.dumps(meta))
    (pending / "evidence.json").write_text(json.dumps(evidence))
    (pending / "risk_model.json").write_text(json.dumps(risk_model))
    (pending / "scoring_marks.json").write_text(json.dumps(scoring_marks))
    (pending / "binding_user_directive.md").write_text(directive_text)
    performance = _performance_packet(7, decision_ts, evidence)
    performance["bindings"]["risk_model_sha256"] = canonical_sha256(risk_model)
    performance["bindings"]["meta_sha256"] = canonical_sha256(meta)
    performance["positions"] = []
    (pending / "performance_snapshot.json").write_text(json.dumps(performance))
    (pending / "performance_snapshot.sha256").write_text(
        canonical_sha256(performance) + "\n"
    )
    monkeypatch.setattr(
        "scripts.desk_reconcile.build_performance_snapshot",
        lambda *args, **kwargs: performance,
    )

    flat_reads = [{
        "symbol": row["symbol"], "lean": "flat", "conviction": 0.0,
        "rationale": "no signal", "evidence": [],
    } for row in evidence]
    (pending / "sentiment_reads.json").write_text(json.dumps(flat_reads))
    for role in ("technical", "futures"):
        supportive = [
            {
                **row,
                "lean": "short" if row["symbol"] == extra_alpha[1] else "long",
                "conviction": 0.6,
            }
            if row["symbol"] in alpha_symbols else row
            for row in flat_reads
        ]
        (pending / f"{role}_reads.json").write_text(json.dumps(supportive))
    normalized_reads = {
        role: _parse_specialist_reads(
            json.loads((pending / f"{role}_reads.json").read_text()),
            [row["symbol"] for row in evidence],
        )
        for role in ("sentiment", "technical", "futures")
    }
    (pending / "specialist_reads.sha256").write_text(
        specialist_reads_sha256(normalized_reads) + "\n"
    )

    book = Book(
            legs=[BookLeg(
                symbol=symbol,
                side="long",
                target_notional=5_000.0,
                is_new=True,
                hold_breaking_reason="test fixture",
                expected_price_edge_frac=0.20,
                edge_calibration_basis="test horizon-matched calibration",
                invalidation_condition="test objective invalidation",
            ), BookLeg(
                symbol=extra_alpha[0],
                side="long",
                target_notional=5_000.0,
                is_new=True,
                hold_breaking_reason="complete the neutral test basket",
                expected_price_edge_frac=0.20,
                edge_calibration_basis="test horizon-matched calibration",
                invalidation_condition="test objective invalidation",
            ), BookLeg(
                symbol=extra_alpha[1],
                side="short",
                target_notional=5_000.0,
                is_new=True,
                hold_breaking_reason="complete the neutral test basket",
                expected_price_edge_frac=0.20,
                edge_calibration_basis="test horizon-matched calibration",
                invalidation_condition="test objective invalidation",
            ), BookLeg(
                symbol="BTC/USDT:USDT",
                side="short",
                target_notional=5_000.0,
                seat_role="hedge",
                is_new=True,
                hold_breaking_reason="neutralize the test alpha leg",
            )],
        stated_deploy_frac=1.0,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
        turnover_legs_changed=4,
        candidate_reviews=[
            CandidateReview(
                symbol=symbol,
                side="long",
                status="selected",
                exclusion_reason="selected",
                expected_price_edge_frac=0.20,
                edge_horizon_hours=24,
                counterfactual_notional=5_000.0,
                supporting_specialists=[
                    SpecialistSupportEcho(role=role, lean="long", conviction=0.6)
                    for role in ("technical", "futures")
                ],
                rationale="selected production fixture candidate",
            ),
            *[
                CandidateReview(
                    symbol=extra_symbol,
                    side="long" if extra_symbol == extra_alpha[0] else "short",
                    status="selected",
                    exclusion_reason="selected",
                    expected_price_edge_frac=0.20,
                    edge_horizon_hours=24,
                    counterfactual_notional=5_000.0,
                    supporting_specialists=[
                        SpecialistSupportEcho(
                            role=role,
                            lean="long" if extra_symbol == extra_alpha[0] else "short",
                            conviction=0.6,
                        )
                        for role in ("technical", "futures")
                    ],
                    rationale="selected production fixture candidate",
                )
                for extra_symbol in extra_alpha
            ],
        ],
    )
    book = _bind_book_reads(book, normalized_reads)
    precheck = compute_precheck(
        book, evidence, cash=20_000.0, cycle=7, current_book=[], risk_model=risk_model,
        meta_sha256=canonical_sha256(meta),
        execution_realism=ExecutionRealism(),
    )
    _write(pending / "pm_book.json", book)
    _write(pending / "precheck.json", precheck)
    pm_region = split_managed(Path("agents/pm.md").read_text())[1]
    policy_hash = canonical_sha256(pm_region)
    (pending / "entry_gate_policy.json").write_text(json.dumps({
        "source": "agents/pm.md",
        "managed_region": pm_region,
        "sha256": policy_hash,
    }))
    _write(
        pending / "adversary.json",
        _bind_reads(
                _bind_performance(_verdict(
                    precheck,
                    override_rationale=(
                        "Test fixture explicitly accepts its non-production bounds."
                    ),
                    ).model_copy(update={
                        "entry_gate_policy_sha256": policy_hash,
                        "binding_user_directive_sha256": directive_sha256,
                        "directive_exception_audits": [DirectiveExceptionAudit(
                            bound_id="B9",
                            symbols=[symbol, *extra_alpha, "BTC/USDT:USDT"],
                            note="exact bound directive authorizes this cold-start fixture",
                        )],
                    "hedge_audit": HedgeAudit(
                        symbol="BTC/USDT:USDT",
                        side="short",
                        target_notional=5_000.0,
                        risk_reducing_vs_alpha_book=True,
                        change_risk_reducing_vs_carried_hedge=True,
                        counterfactual_reviewed=True,
                        carry_cost_reviewed=True,
                        liquidity_reviewed=True,
                        note="test reviewed beta counterfactual, carry, and depth",
                    ),
                }), performance),
                normalized_reads,
            ),
    )

    class _FreshExecutionExchange:
        def symbol_spec(self, requested_symbol):
            assert requested_symbol in {symbol, "BTC/USDT:USDT", *extra_alpha}
            return SimpleNamespace(
                step_size=0.001 if requested_symbol == symbol else 0.000001,
                tick_size=0.01,
                min_notional=5.0,
            )

        def funding_interval_hours(self, requested_symbol):
            assert requested_symbol in {symbol, "BTC/USDT:USDT", *extra_alpha}
            return 8.0

        def depth(self, requested_symbol):
            assert requested_symbol in {symbol, "BTC/USDT:USDT", *extra_alpha}
            if requested_symbol == "BTC/USDT:USDT":
                return {
                    "bids": [(59_999.0, 1_000_000.0)],
                    "asks": [(60_001.0, 1_000_000.0)],
                }
            return {
                "bids": [(109.90, 1_000_000.0)],
                "asks": [(110.10, 1_000_000.0)],
            }

        def mark_price(self, requested_symbol):
            raise AssertionError("complete depth must provide its own midpoint")

    monkeypatch.setattr(
        "scripts.desk_reconcile.FuturesExchange.from_settings",
        lambda settings: _FreshExecutionExchange(),
    )
    # Simulate a hostile/concurrent rewrite during the fresh network phase. The decision chain was
    # already verified, so neither economics nor committed artifacts may re-read these bytes.
    from scripts import desk_reconcile as reconcile_module

    real_execution_inputs = reconcile_module._execution_inputs

    def _mutating_execution_inputs(*args, **kwargs):
        result = real_execution_inputs(*args, **kwargs)
        (pending / "pm_book.json").write_text("{}")
        _write(
            pending / "precheck.json",
            precheck.model_copy(update={"gross": precheck.gross + 123.0}),
        )
        return result

    monkeypatch.setattr(
        "scripts.desk_reconcile._execution_inputs", _mutating_execution_inputs
    )
    provenance_body = {
        "schema_version": 1,
        "captured_at": decision_ts.isoformat(),
        "test": True,
    }
    provenance = {
        **provenance_body,
        "provenance_sha256": canonical_sha256(provenance_body),
    }
    monkeypatch.setattr(
        "scripts.desk_reconcile.load_bound_decision_start_provenance",
        lambda pending, meta, require_current_match: provenance,
    )
    monkeypatch.setattr(
        "scripts.desk_reconcile.load_bound_pre_reflection_performance",
        lambda pending, meta: performance,
    )
    monkeypatch.setattr(
        "scripts.desk_reconcile.build_watchdog_receipt",
        lambda *_args, **_kwargs: watchdog_receipt,
    )

    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "--memory-dir", str(memory)]) == 0

    account = json.loads((state / "account.json").read_text())
    position = account["positions"][symbol]
    assert position["entry_price"] == pytest.approx(110.0)
    # $5 of half-spread/depth cost plus adverse-selection and a tiny measured legging reserve.
    assert 5.55 <= position["accrued_slippage"] < 5.70

    cycle_dir = state / "rebal" / "cycle" / "7"
    assert json.loads((cycle_dir / "book.json").read_text()) == book.model_dump(
        mode="json"
    )
    assert json.loads((cycle_dir / "precheck.json").read_text()) == precheck.model_dump(
        mode="json"
    )
    execution_artifact = json.loads((cycle_dir / "execution.json").read_text())
    execution = execution_artifact[symbol]
    assert execution["decision_mark"] == pytest.approx(100.0)
    assert execution["execution_mark"] == pytest.approx(110.0)
    assert execution["decision_to_execution_bps"] == pytest.approx(1000.0)
    assert execution["price_source"] == "book_mid"
    assert execution["step_size"] == pytest.approx(0.001)
    assert execution["min_notional"] == pytest.approx(5.0)
    assert execution["executed_target_qty_signed"] == pytest.approx(50.0)
    assert execution["post_execution_qty_signed"] == pytest.approx(50.0)
    assert execution["execution_target_attained"] is True
    assert execution["adverse_selection_bps"] == pytest.approx(1.0)
    assert execution["displayed_depth_fraction"] == pytest.approx(0.5)
    assert execution["raw_bid_ladder"] == [[109.9, 1_000_000.0]]
    assert execution["raw_ask_ladder"] == [[110.1, 1_000_000.0]]
    assert execution["effective_bid_ladder"] == [[109.9, 500_000.0]]
    assert execution["effective_ask_ladder"] == [[110.1, 500_000.0]]
    assert execution["l2_validation_passed"] is True
    assert datetime.fromisoformat(execution["submission_at"]) <= datetime.fromisoformat(
        execution["observed_at"]
    )
    assert datetime.fromisoformat(execution["observed_at"]) == datetime.fromisoformat(
        execution["execution_ts"]
    )
    assert json.loads((cycle_dir / "risk_model.json").read_text()) == risk_model
    assert json.loads((cycle_dir / "runtime_provenance.json").read_text()) == provenance
    assert json.loads(
        (cycle_dir / "performance_snapshot_pre_reflection.json").read_text()
    ) == performance
    assert json.loads(
        (cycle_dir / "performance_snapshot_pre_reflection_digest.json").read_text()
    ) == {"sha256": canonical_sha256(performance)}
    funding_window = json.loads(
        (cycle_dir / "funding_execution_window_proof.json").read_text()
    )
    assert funding_window["safe"] is True
    assert funding_window["symbols"][symbol] == {
        "source": "decision_and_execution_interval_union",
        "decision_interval_h": 8,
        "current_interval_h": 8,
        "applicable_boundaries_in_window": [],
    }
    durable_meta = json.loads((cycle_dir / "meta.json").read_text())
    assert durable_meta["candle_data"]["source"] == "binance-proxy"
    complete = json.loads((cycle_dir / "complete.json").read_text())
    assert complete["manifest"]["artifact_sha256"]["execution"] == canonical_sha256(
        execution_artifact
    )

    report = json.loads((cycle_dir / "report.json").read_text())
    assert report["decision_age_seconds"] >= 15 * 60
    assert datetime.fromisoformat(report["execution_ts"]) > decision_ts


def _seed_receipt_pending(tmp_path):
    memory = tmp_path / "memory"
    pending = memory / "pending" / "7"
    pending.mkdir(parents=True)
    now = datetime.now(UTC)
    (memory / "pending" / "current.json").write_text(json.dumps({
        "cycle": 7,
        "dir": str(pending),
        "created": now.isoformat(),
    }))
    (pending / "meta.json").write_text(json.dumps({
        "cycle": 7,
        "now": now.isoformat(),
        "cash": 20_000.0,
    }))
    original_book = _book(first_notional=4_400.0)
    original_precheck = _precheck(original_book)
    verdict = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [RevisionConstraint(
            schema_version=2,
            kind="min_symbol_notional",
            symbol=SYMBOLS[0],
            value=4_500.0,
            final_side="long",
            final_seat_role="alpha",
            max_expected_price_edge_frac=0.0,
            required_expected_price_edge_frac=0.0,
            required_edge_horizon_hours=24,
            required_edge_calibration_basis="test horizon-matched calibration",
            required_invalidation_condition="test objective invalidation",
            note="restore the exact reviewed incumbent size",
        )],
    })
    _write(pending / "pm_book_original.json", original_book)
    _write(pending / "precheck_original.json", original_precheck)
    _write(pending / "pm_book.json", original_book)
    _write(pending / "precheck.json", original_precheck)
    _write(pending / "adversary.json", verdict)
    return memory, pending, original_book, original_precheck, verdict


def test_revision_receipt_cli_enforces_prepare_then_one_seal(tmp_path):
    memory, pending, _, _, _ = _seed_receipt_pending(tmp_path)

    assert revision_receipt_main([
        "prepare", "--memory-dir", str(memory),
    ]) == 0
    dispatch = pending / "revision_dispatch_receipt.json"
    assert dispatch.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        revision_receipt_main(["prepare", "--memory-dir", str(memory)])

    final_book = _book()
    _write(pending / "pm_book.json", final_book)
    assert revision_receipt_main([
        "seal", "--memory-dir", str(memory),
    ]) == 0
    output = pending / "revision_output_receipt.json"
    assert output.stat().st_mode & 0o777 == 0o400
    receipt = json.loads(output.read_text())
    assert receipt["attempt"] == 1
    assert receipt["output_book_sha256"] == canonical_sha256(
        final_book.model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="already sealed"):
        revision_receipt_main(["seal", "--memory-dir", str(memory)])


@pytest.mark.parametrize("mutate", ["book", "precheck"])
def test_revision_receipt_cannot_be_prepared_retroactively(tmp_path, mutate):
    memory, pending, _, original_precheck, _ = _seed_receipt_pending(tmp_path)
    if mutate == "book":
        _write(pending / "pm_book.json", _book())
    else:
        _write(
            pending / "precheck.json",
            original_precheck.model_copy(update={"gross": original_precheck.gross + 1.0}),
        )

    with pytest.raises(ValueError, match="before pm_book/precheck changes"):
        revision_receipt_main(["prepare", "--memory-dir", str(memory)])
    assert not (pending / "revision_dispatch_receipt.json").exists()


@pytest.mark.parametrize(
    "artifact",
    [
        "pm_book_original.json",
        "precheck_original.json",
        "revision_dispatch_receipt.json",
        "revision_output_receipt.json",
    ],
)
def test_accepted_chain_rejects_every_revision_only_artifact(tmp_path, artifact):
    book = _book()
    precheck = _precheck(book)
    _write(tmp_path / "precheck.json", precheck)
    (tmp_path / artifact).write_text("{}")

    with pytest.raises(AdversaryBindingError, match="unexpected revision artifacts"):
        _verify_decision_chain(
            tmp_path,
            cycle=7,
            meta=META,
            evidence=EVIDENCE,
            current_book=CURRENT_BOOK,
            book=book,
            verdict=_verdict(precheck),
        )


def test_prospective_revision_drop_requires_complete_exit_authority():
    original_book = _book()
    original_precheck = _precheck(original_book)
    final_book = original_book.model_copy(update={
        "legs": [leg for leg in original_book.legs if leg.symbol != SYMBOLS[1]],
    })
    final_precheck = _precheck(final_book)
    constraint = RevisionConstraint(
        kind="drop_symbol",
        symbol=SYMBOLS[1],
        note="drop this held incumbent after reviewing its disposition",
    )
    missing = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [constraint],
    })
    with pytest.raises(AdversaryBindingError, match="exit_audits"):
        verify_verdict_binding(
            missing,
            original_precheck,
            cycle=7,
            sentiment_reads=[],
            book=original_book,
        )

    failed = missing.model_copy(update={
        "exit_audits": [
            _exit_audit(SYMBOLS[1], side="long").model_copy(
                update={"current_evidence_reviewed": False}
            )
        ],
    })
    verify_verdict_binding(
        failed,
        original_precheck,
        cycle=7,
        sentiment_reads=[],
        book=original_book,
    )
    with pytest.raises(AdversaryBindingError, match="failed Adversary exit audit"):
        verify_revision_binding(
            failed,
            original_book,
            original_precheck,
            final_book,
            final_precheck,
        )


def test_prospective_alpha_to_hedge_revision_binds_old_and_new_lifecycles():
    btc = "BTC/USDT:USDT"
    evidence = [
        {
            "symbol": symbol,
            "mark": 1.0 if symbol != btc else 60_000.0,
            "beta_btc": 1.0,
            "est_slippage_bps_2k": 1.0,
            "slippage_curve_bps": {"2k": 1.0, "5k": 1.5},
            "expected_funding_8h_bps": 0.0,
        }
        for symbol in (*SYMBOLS, btc)
    ]
    current = [
        {"symbol": SYMBOLS[0], "side": "long", "target_notional": 4_500.0},
        {"symbol": SYMBOLS[1], "side": "short", "target_notional": 4_000.0},
        {"symbol": SYMBOLS[2], "side": "long", "target_notional": 3_500.0},
        {"symbol": SYMBOLS[3], "side": "short", "target_notional": 3_000.0},
        {
            "symbol": btc,
            "side": "short",
            "target_notional": 1_000.0,
            "seat_role": "alpha",
        },
    ]
    original_legs = [
        BookLeg(
            symbol=row["symbol"],
            side=row["side"],
            target_notional=row["target_notional"],
            seat_role="alpha",
            edge_calibration_basis="test horizon-matched calibration",
            invalidation_condition="test objective invalidation",
        )
        for row in current
    ]
    original_book = Book(
        legs=original_legs,
        stated_deploy_frac=0.8,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
    )
    original_precheck = compute_precheck(
        original_book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=current,
    )
    final_legs = [
        leg.model_copy(update={
            "seat_role": "hedge",
            "edge_calibration_basis": "",
            "invalidation_condition": "",
        }) if leg.symbol == btc else leg
        for leg in original_legs
    ]
    final_book = original_book.model_copy(update={"legs": final_legs})
    final_precheck = compute_precheck(
        final_book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=current,
    )
    role_row = next(row for row in final_precheck.change_costs if row.symbol == btc)
    assert role_row.action == "role_change"
    assert role_row.executable_turnover_usd == 0.0
    assert role_row.payback_intervals == 0.0
    assert next(
        bound for bound in final_precheck.bounds if bound.bound_id == "B12"
    ).ok
    assert final_precheck.hedge_risk_reducing
    assert final_precheck.hedge_change_risk_reducing

    constraint = RevisionConstraint(
        schema_version=2,
        kind="permit_symbol_mutation",
        symbol=btc,
        final_side="short",
        final_seat_role="hedge",
        max_expected_price_edge_frac=0.0,
        required_expected_price_edge_frac=0.0,
        required_edge_horizon_hours=24,
        note="convert the exact carried BTC exposure to typed beta insurance",
    )
    hedge_audit = HedgeAudit(
        symbol=btc,
        side="short",
        target_notional=1_000.0,
        risk_reducing_vs_alpha_book=True,
        change_risk_reducing_vs_carried_hedge=True,
        counterfactual_reviewed=True,
        carry_cost_reviewed=True,
        liquidity_reviewed=True,
        note="reviewed alpha-only beta, unchanged BTC exposure, carry, and depth",
    )
    exit_audit = _exit_audit(
        btc,
        side="short",
        seat_role="alpha",
        action="role_change",
    )
    base = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [constraint],
        "revision_hedge_audit": hedge_audit,
    })
    with pytest.raises(AdversaryBindingError, match="exit_audits"):
        verify_verdict_binding(
            base,
            original_precheck,
            cycle=7,
            sentiment_reads=[],
            book=original_book,
        )
    wrong_action = base.model_copy(update={
        "exit_audits": [exit_audit.model_copy(update={"action": "drop"})],
    })
    with pytest.raises(AdversaryBindingError, match="misclassifies"):
        verify_verdict_binding(
            wrong_action,
            original_precheck,
            cycle=7,
            sentiment_reads=[],
            book=original_book,
        )

    complete = base.model_copy(update={"exit_audits": [exit_audit]})
    verify_verdict_binding(
        complete,
        original_precheck,
        cycle=7,
        sentiment_reads=[],
        book=original_book,
    )
    verify_revision_binding(
        complete,
        original_book,
        original_precheck,
        final_book,
        final_precheck,
    )


def test_rejected_original_flip_can_mandate_a_reviewed_loss_control_drop():
    """The sole revision may abandon a rejected replacement without retaining the incumbent."""
    symbols = tuple(f"{letter}/USDT:USDT" for letter in "ABCDE")
    evidence = [
        {
            "symbol": symbol,
            "mark": 1.0,
            "beta_btc": 1.0,
            "est_slippage_bps_2k": 1.0,
            "slippage_curve_bps": {"5k": 1.0},
            "expected_funding_8h_bps": 0.0,
        }
        for symbol in symbols
    ]
    current = [
        {
            "symbol": symbol,
            "side": "long" if index < 3 else "short",
            "target_notional": 4_000.0,
        }
        for index, symbol in enumerate(symbols)
    ]

    def leg(symbol: str, side: Literal["long", "short"], *, flipped: bool = False):
        return BookLeg(
            symbol=symbol,
            side=side,
            target_notional=4_000.0,
            expected_price_edge_frac=0.01 if flipped else 0.0,
            edge_calibration_basis="test horizon-matched calibration",
            invalidation_condition="test objective invalidation",
            is_new=flipped,
            hold_breaking_reason="fresh opposite thesis" if flipped else "",
        )

    original_book = Book(
        legs=[
            leg(symbols[0], "short", flipped=True),
            leg(symbols[1], "long"),
            leg(symbols[2], "long"),
            leg(symbols[3], "short"),
            leg(symbols[4], "short"),
        ],
        stated_deploy_frac=1.0,
        stated_dollar_residual_frac=0.2,
        stated_beta_residual=-0.2,
        turnover_legs_changed=1,
    )
    original_precheck = compute_precheck(
        original_book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=current,
    )
    assert next(
        row for row in original_precheck.change_costs if row.symbol == symbols[0]
    ).action == "flip"

    final_book = Book(
        legs=[
            leg(symbols[1], "long"),
            leg(symbols[2], "long"),
            leg(symbols[3], "short"),
            leg(symbols[4], "short"),
        ],
        stated_deploy_frac=0.8,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
        turnover_legs_changed=1,
    )
    final_precheck = compute_precheck(
        final_book,
        evidence,
        cash=20_000.0,
        cycle=7,
        current_book=current,
    )
    assert next(
        row for row in final_precheck.change_costs if row.symbol == symbols[0]
    ).action == "drop"
    assert {
        bound.bound_id for bound in final_precheck.bounds if not bound.ok
    } == {"B12"}

    constraint = RevisionConstraint(
        kind="drop_symbol",
        symbol=symbols[0],
        note="reject the replacement short but still close the invalidated long",
    )
    base = _verdict(original_precheck, accept=False).model_copy(update={
        "revision_constraints": [constraint],
        "revision_allowed_failing_bounds": ["B12"],
    })
    # The original flip review cannot silently authorize a different final disposition.
    with pytest.raises(AdversaryBindingError, match="prospectively authorized"):
        verify_verdict_binding(
            base,
            original_precheck,
            cycle=7,
            sentiment_reads=[],
            book=original_book,
        )

    reviewed = base.model_copy(update={
        "exit_audits": [
            _exit_audit(symbols[0], side="long", action="flip"),
            _exit_audit(symbols[0], side="long", action="drop"),
        ],
    })
    verify_verdict_binding(
        reviewed,
        original_precheck,
        cycle=7,
        sentiment_reads=[],
        book=original_book,
    )
    verify_revision_binding(
        reviewed,
        original_book,
        original_precheck,
        final_book,
        final_precheck,
    )


def test_flip_and_role_change_endings_require_exit_audits():
    base = _book()
    flip_leg = base.legs[0].model_copy(update={
        "side": "short",
        "is_new": True,
        "hold_breaking_reason": "fresh opposite thesis",
        "expected_price_edge_frac": 0.01,
    })
    flip_book = base.model_copy(update={
        "legs": [flip_leg, *base.legs[1:]],
        "stated_dollar_residual_frac": 0.5,
        "stated_beta_residual": -0.45,
        "turnover_legs_changed": 1,
    })
    flip_precheck = _precheck(flip_book)
    flip_verdict = _verdict(
        flip_precheck,
        override_rationale="fixture accepts non-disposition bounds",
    ).model_copy(update={"exit_audits": []})
    with pytest.raises(AdversaryBindingError, match="ended incumbent lifecycle"):
        verify_exit_audits(
            flip_verdict,
            flip_precheck,
            performance_snapshot=None,
            specialist_reads=None,
        )

    btc = "BTC/USDT:USDT"
    role_book = Book(
        legs=[BookLeg(
            symbol=btc,
            side="short",
            target_notional=1_000.0,
            seat_role="hedge",
            is_new=True,
            hold_breaking_reason="convert the prior alpha lifecycle to beta insurance",
        )],
        stated_deploy_frac=0.05,
        stated_dollar_residual_frac=1.0,
        stated_beta_residual=-0.05,
        turnover_legs_changed=1,
    )
    role_precheck = compute_precheck(
        role_book,
        [{
            "symbol": btc,
            "mark": 60_000.0,
            "beta_btc": 1.0,
            "est_slippage_bps_2k": 1.0,
            "slippage_curve_bps": {"2k": 1.0},
            "expected_funding_8h_bps": 0.0,
        }],
        cash=20_000.0,
        cycle=7,
        current_book=[{
            "symbol": btc,
            "side": "short",
            "target_notional": 1_000.0,
            "seat_role": "alpha",
        }],
    )
    assert next(row for row in role_precheck.change_costs if row.symbol == btc).action == (
        "role_change"
    )
    role_verdict = _verdict(
        role_precheck,
        override_rationale="fixture accepts non-disposition bounds",
    ).model_copy(update={"exit_audits": []})
    with pytest.raises(AdversaryBindingError, match="ended incumbent lifecycle"):
        verify_exit_audits(
            role_verdict,
            role_precheck,
            performance_snapshot=None,
            specialist_reads=None,
        )
