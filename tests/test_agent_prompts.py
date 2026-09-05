import json
from pathlib import Path

import pytest

from futures_fund.prompt_guard import split_managed

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("sentiment", "technical", "futures", "pm", "adversary")
PRE_REFACTOR_ALWAYS_READ_WORDS = 10_477


def _text(relative: str) -> str:
    return (ROOT / relative).read_text()


def _normalized(relative: str) -> str:
    return " ".join(_text(relative).split())


def _static_role(role: str) -> str:
    prefix, _region, suffix = split_managed(_text(f"agents/{role}.md"))
    return prefix + suffix


def _cycle_reads(cycle: int) -> dict[str, list[dict[str, object]]]:
    path = ROOT / f"live_state/rebal/cycle/{cycle}/reads.json"
    return json.loads(path.read_text())


def _read(cycle: int, role: str, symbol: str) -> dict[str, object]:
    return next(row for row in _cycle_reads(cycle)[role] if row["symbol"] == symbol)


def _passes_c47_gate(cycle: int, symbol: str, side: str) -> bool:
    """Evaluate the historical c47 two-support/no-opposition calibration."""
    rows = [_read(cycle, role, symbol) for role in ("sentiment", "technical", "futures")]
    support_n = sum(
        row["lean"] == side and float(row["conviction"]) >= 0.50 for row in rows
    )
    opposite = "short" if side == "long" else "long"
    has_opposition = any(
        row["lean"] == opposite and float(row["conviction"]) >= 0.35 for row in rows
    )
    return support_n >= 2 and not has_opposition


@pytest.mark.parametrize(
    ("role", "permanent_invariant"),
    [
        ("sentiment", "Never invent a headline"),
        ("technical", "Regime warning cannot be calibrated away"),
        ("futures", "no news, no invented figures"),
        ("pm", "PAPER only; universe symbols only"),
        ("adversary", "only anti-hallucination and risk reviewer"),
    ],
)
def test_each_role_has_one_managed_block_and_static_safety(role, permanent_invariant):
    role_text = _text(f"agents/{role}.md")
    prefix, region, suffix = split_managed(role_text)
    assert permanent_invariant in prefix + suffix
    assert "REFLECTOR:" not in region


def test_live_managed_regions_retain_governed_provenance():
    from futures_fund.reflection import audit_managed_region_provenance

    assert audit_managed_region_provenance(
        ROOT / "agents",
        ROOT / "live_memory/reflector-journal.md",
        anchor_path=ROOT / "live_state/reflector-head-anchor-v1.json",
    ) == []


def test_pm_adversary_always_read_budget_and_revision_split():
    pm = _text("agents/pm.md")
    adversary = _text("agents/adversary.md")
    revision = _text("agents/pm-revision.md")
    runbook = _text("docs/desk-cycle-runbook.md")
    ops = _text("ops/desk-cycle-prompt.md")

    current_words = len(pm.split()) + len(adversary.split())
    assert current_words <= PRE_REFACTOR_ALWAYS_READ_WORDS // 2
    assert len(revision.split()) < 800
    # Decision-start sealing adds four load-bearing provenance lines; keep the root prompt compact.
    assert len(ops.splitlines()) <= 60

    assert "agents/pm-revision.md" in pm
    assert "Exact mutation envelope" not in pm
    assert "## Exact mutation envelope" in revision
    dispatch = (
        "complete current `agents/pm.md` followed by the revision-only "
        "`agents/pm-revision.md`"
    )
    assert dispatch in _normalized("docs/desk-cycle-runbook.md")
    assert runbook.index("agents/pm.md` followed") < runbook.index("pm-revision.md")


@pytest.mark.parametrize("role", ("sentiment", "technical", "futures"))
def test_specialists_cover_the_universe_and_calibrate_from_performance(role):
    prompt = _text(f"agents/{role}.md")
    lower = prompt.lower()
    assert "exactly one object per input coin" in lower
    assert "input order" in lower
    assert "performance_snapshot.json" in prompt
    assert "net paper" in lower or "net pnl" in lower
    assert "No prose outside the JSON" in prompt


def test_specialist_evidence_boundaries_are_crisp():
    sentiment = _text("agents/sentiment.md")
    technical = _text("agents/technical.md")
    futures = _text("agents/futures.md")

    assert "Re-open every final URL" in sentiment
    assert "Never upgrade a proposal into an activation" in sentiment
    assert "rolling/generated pages" in sentiment
    assert "beta-adjusted 24h/72h/168h momentum first" in technical
    assert "Regime warning cannot be calibrated away" in technical
    assert "price/vol fields" in technical
    assert "conservative_funding_8h_bps" in futures
    assert "trend_unchecked" in futures
    assert "You do not analyze price" in futures


def test_price_first_relative_profitability_and_optional_overlays():
    pm = _text("agents/pm.md")
    adversary = _text("agents/adversary.md")
    pm_norm = _normalized("agents/pm.md")
    adversary_norm = _normalized("agents/adversary.md")

    for prompt in (pm, adversary):
        assert "BTC-beta-adjusted relative performance" in prompt
        assert "performance_snapshot" in prompt
        assert "positive" in prompt and "net" in prompt
        assert "cash" in prompt and "same-side alternative" in prompt

    assert "Price path dominates small carry in a trend" in pm
    assert "Technical price evidence is primary in trend" in pm
    assert "Sentiment is optional" in pm
    assert "weak `trend_unchecked` carry read is not a veto" in pm_norm
    assert "Conservative carry may lead in verified chop only" in adversary_norm
    assert "Sentiment is non-mandatory" in adversary
    assert "weak `trend_unchecked` carry read is not a veto" in adversary
    alpha_non_arguments = (
        "Deployment, neutrality, incumbency, past PnL, or avoided fees is not alpha"
    )
    assert alpha_non_arguments in adversary_norm


def test_flat_book_reentry_is_small_neutral_and_never_forced():
    pm = _text("agents/pm.md")
    adversary = _text("agents/adversary.md")
    runbook = _text("docs/desk-cycle-runbook.md")

    for prompt in (adversary, runbook):
        assert "cold_start_reentry_eligible" in prompt
        assert "b9_aggressive_change_limit" in prompt
    for prompt in (pm, adversary, runbook):
        normalized = " ".join(prompt.split())
        assert "20%" in normalized
        assert "8% annualized residual volatility" in normalized
        assert "12 independent" in normalized
    assert "Capacity is never a mandate" in pm
    assert "cash remains valid" in adversary
    for prompt in (pm, adversary, runbook):
        normalized = " ".join(prompt.lower().split())
        assert "unknown risk" in normalized or "unavailable covariance" in normalized
    assert "`risk_model_available=true`" in adversary


def test_reflector_consolidates_multiple_recurrences_per_role():
    reflector = _text("agents/reflector.md")

    assert "exactly **one**" in reflector
    assert "Never emit duplicate `edits[].role` values" in reflector
    assert "silently losing another" in reflector


def test_forecasts_are_horizon_matched_and_do_not_replay_price_edge():
    pm = _text("agents/pm.md")
    adversary = _text("agents/adversary.md")

    for prompt in (pm, adversary):
        assert "by_horizon_hours[str(edge_horizon_hours)]" in prompt
        assert "independent_time_cohort_n" in prompt
        assert "aggregate_context_only" in prompt
        assert "24" in prompt and "72" in prompt and "168" in prompt
        assert "carry_recovery_intervals" in prompt
        assert "PRICE-REGIME HOLD BREAK" in prompt

    assert "expected_price_edge_frac <= 0" in pm
    assert "drop it completely in this cycle" in pm
    assert "cap the price contribution" in pm
    assert "continue carry only" in pm
    assert "do not replay a one-time forecast" in adversary
    assert "required_price_edge_frac_for_max_payback" in adversary


def test_candidate_review_contract_is_complete_exact_and_pm_causal():
    pm = _text("agents/pm.md")
    adversary = _text("agents/adversary.md")
    runbook = _text("docs/desk-cycle-runbook.md")
    pm_norm = _normalized("agents/pm.md")

    for prompt in (pm, adversary, runbook):
        assert "candidate_reviews" in prompt
        assert "every non-flat technical" in prompt
        assert "every selected alpha" in prompt
        assert "entry_gate" in prompt
        assert "role" in prompt and "lean" in prompt and "conviction" in prompt

    for field in (
        "symbol",
        "side",
        "status",
        "exclusion_reason",
        "expected_price_edge_frac",
        "edge_horizon_hours",
        "counterfactual_notional",
        "supporting_specialists",
        "rationale",
    ):
        assert field in pm

    assert "every and only same-side non-flat bound read" in pm
    assert 'status="selected"' in pm and 'exclusion_reason="selected"' in pm
    assert "exactly matches its alpha BookLeg" in pm_norm
    assert "only when the current hash-bound managed gate was actually decisive" in pm_norm
    assert "learning label, not deterministic permission or a veto" in pm
    assert "This ledger enables shadow learning" in adversary
    assert "neither deterministic selection nor permission to trade" in adversary


def test_historical_gate_examples_express_liveness_without_forcing_trades():
    # c47's threshold rejected both actual increases: TRUMP had weak technical support plus
    # material opposition, and SUI had only weak futures support.
    assert not _passes_c47_gate(47, "TRUMP/USDT:USDT", "long")
    assert not _passes_c47_gate(47, "SUI/USDT:USDT", "short")

    # These later rows are non-flat technical candidates. Once a governed Reflector narrows the
    # historical gate, the ledger lets the PM judge them on complete economics. This deliberately
    # does not assert that any candidate should be selected.
    later_candidates = (
        (51, "FIL/USDT:USDT", "long", 0.55),
        (53, "SOL/USDT:USDT", "short", 0.57),
        (53, "ADA/USDT:USDT", "long", 0.63),
    )
    for cycle, symbol, side, conviction in later_candidates:
        technical = _read(cycle, "technical", symbol)
        assert technical["lean"] == side
        assert float(technical["conviction"]) == pytest.approx(conviction)

    reflector = _text("agents/reflector.md")
    reflector_norm = _normalized("agents/reflector.md")
    for term in ("pm_gate_inactive", "c47 TRUMP/SUI", "c51 FIL", "c53 SOL/ADA"):
        assert term in reflector
    assert "three consecutive zero-alpha Books" in reflector_norm
    assert 'candidate_reviews.exclusion_reason="entry_gate"' in reflector
    assert (
        'causal_evidence.kind="legacy_manifest_bound_explicit_pm_gate_declaration"'
        in reflector
    )
    assert "manifest-bound c51-c53 only" in reflector
    assert "Never apply this adapter to a new Book" in reflector_norm
    assert "explicit `candidate_reviews=[]`" in reflector_norm
    narrowing = "permits narrowing or retiring only that active performance-calibration gate"
    assert narrowing in reflector_norm
    assert "never selects a symbol, forces a trade or deployment" in reflector
    assert "Reach judgment” is not “approve" in reflector


def test_paper_neutrality_bounds_and_execution_economics_remain_static():
    pm = _static_role("pm")
    adversary = _static_role("adversary")
    pm_norm = " ".join(pm.split())

    for prompt in (pm, adversary):
        for bound in (f"B{i}" for i in range(1, 13)):
            assert bound in prompt
        assert "B7, B8, B10, and B11 are never overridable" in prompt
        assert "unpriced B12" in prompt
        assert "slippage_curve_buy_bps" in prompt
        assert "slippage_curve_sell_bps" in prompt
        assert "liquidity_mid" in prompt

    assert "PAPER only" in pm
    assert "never place orders" in pm
    assert "one positive-notional net leg per symbol" in pm
    assert "solve dollar neutrality first and beta neutrality second" in pm_norm
    assert "quantity as `decision_notional / mark`" in pm
    assert "one combined signed-delta depth clip" in pm
    assert "drops and decreases are uncapped loss control" in " ".join(adversary.split())
    assert "Missing required directional depth fails closed" in adversary
    assert "a cold-start directive may narrowly authorize b9/b12 only" in adversary.lower()


def test_incumbent_thesis_role_transfer_and_expiry_stay_bound():
    pm = _text("agents/pm.md")
    adversary = _text("agents/adversary.md")
    pm_norm = _normalized("agents/pm.md")

    for prompt in (pm, adversary):
        assert "committed_thesis" in prompt
        assert "manifest-bound" in prompt
        assert "fresh-entry-quality" in prompt
        assert "older than 40" in prompt
        assert "hedge→alpha" in prompt
        assert "alpha→hedge" in prompt

    assert "rewrite never erases a triggered condition" in pm_norm
    assert "drop, flip, or role change ends the old lifecycle" in pm_norm
    assert "role-preserving reduction or hold continues" in pm_norm
    assert "Never reconstruct history" in adversary
    for field in (
        "prior_thesis_available",
        "prior_thesis_cycle",
        "prior_thesis_book_sha256",
        "prior_thesis_provenance_reviewed",
        "prior_invalidation_reviewed",
        "prior_invalidation_triggered",
    ):
        assert field in adversary


def test_controlled_restart_lineage_and_cost_net_graduation_are_explicit():
    pm = _static_role("pm")
    adversary = _static_role("adversary")
    mission = _text("MISSION.md")
    runbook = _text("docs/desk-cycle-runbook.md")

    for prompt in (pm, adversary):
        for field in (
            "controlled_restart_origin_cycle",
            "controlled_restart_phase",
            "controlled_restart_initial_eligible",
            "controlled_restart_continuation_eligible",
            "controlled_restart_lineage_valid",
            "binding_user_directive_present",
        ):
            assert field in prompt
        for field in (
            "cost_net_independent_time_cohort_n >= 12",
            'cost_net_calibration_status="usable"',
            'cost_net_residual_risk_weighted_status="usable"',
            "residual_risk_weighted_realized_round_trip_cost_net_price_edge_frac > 0",
        ):
            assert field in prompt
        assert "aggregate" in prompt.lower() and "cross-horizon" in prompt.lower()
        assert "fully flat" in prompt.lower()

    assert "newest prior completed manifest-bound `book.json`" in pm
    assert "controlled_restart_prior_book_sha256" in adversary
    assert "Passing this test permits" in " ".join(pm.split())
    assert "ordinary portfolio bounds" in pm
    assert "ordinary portfolio bounds" in adversary
    assert "phase true and preserve the exact origin until fully flat" in " ".join(
        adversary.split()
    )
    assert "excluding funding" in mission
    assert "add no deterministic trading veto" in runbook
    assert "both prechecks must carry the identical" in " ".join(runbook.split())
    assert "latest 12 consecutive complete" in pm
    assert "mature-pending" in pm
    assert "desk-process calibration facts by horizon" in adversary
    assert "controlled_restart_risk_audit" in adversary
    assert "selected_alpha_qualifications" in adversary


def test_restart_graduation_requires_exact_typed_directive_scope():
    pm = _static_role("pm")
    adversary = _static_role("adversary")
    mission = _text("MISSION.md")
    runbook = _text("docs/desk-cycle-runbook.md")
    revision = _text("agents/pm-revision.md")
    header = '<!-- desk-directive-capabilities: ["controlled_restart_graduation"] -->'

    for document in (pm, adversary, mission, runbook):
        assert header in document
        assert "binding_user_directive_controlled_restart_graduation" in document
    assert "directive_graduation_capability_used" in adversary
    assert "directive_graduation_capability_used" in runbook
    assert "Mere directive presence" in mission
    assert "generic directive" in pm.lower()
    assert "generic directive" in runbook.lower()
    assert "does not replace your sole accept/reject judgment" in " ".join(
        adversary.split()
    )
    assert "binding_user_directive_controlled_restart_graduation" in revision


def test_one_shot_directive_uses_uuid_claim_lifecycle_not_path_cleanup():
    mission = _text("MISSION.md")
    runbook = _text("docs/desk-cycle-runbook.md")
    restart = _text("docs/desk-restart-runbook.md")
    cycle_prompt = _text("ops/desk-cycle-prompt.md")

    assert "directive-claims-v1" in runbook
    assert "claim UUID" in runbook
    assert "schema-v2" in runbook
    assert "never unlinks the canonical inbox" in " ".join(runbook.split())
    assert "Failed cycles reuse the claim" in _text("README.md")
    assert "consumes only that UUID-derived claim" in mission
    assert "newer canonical inbox file remains separately queued" in restart
    assert "Never read or remove `ops/next-cycle-directive.md` directly" in cycle_prompt


def test_adversary_is_sole_veto_and_citation_audit_is_complete():
    adversary = _text("agents/adversary.md")
    adversary_norm = _normalized("agents/adversary.md")

    assert "only anti-hallucination and risk reviewer" in adversary
    assert "judge the original Book once" in adversary_norm
    assert "exactly one constrained PM revision" in adversary_norm
    assert "no second adversarial pass" in adversary
    assert "Open every URL for every non-flat sentiment read, selected or not" in adversary
    assert "exactly one `citation_checks` row per non-flat sentiment symbol" in adversary
    assert "Unsupported selected sentiment requires rejection" in adversary
    assert "unsupported unselected sentiment" in adversary
    assert "Deterministic code" in adversary
    assert "never accepts, rejects, sizes, or repairs" in adversary
    assert "Do not run a second adversary pass" in _normalized("docs/desk-cycle-runbook.md")


def test_adversary_schema_and_complete_action_lifecycle_audits_are_explicit():
    adversary = _static_role("adversary")

    for field in (
        "performance_snapshot_sha256",
        "specialist_reads_sha256",
        "entry_gate_policy_sha256",
        "binding_user_directive_sha256",
        "directive_exception_audits",
        "controlled_restart_risk_audit",
        "metrics_echo",
        "bounds_confirmed",
        "citation_checks",
        "seat_audits",
        "action_audits",
        "exit_audits",
        "hedge_audit",
        "revision_hedge_audit",
        "revision_fallback_seat_audits",
        "revision_constraints",
        "revision_allowed_failing_bounds",
    ):
        assert field in adversary

    assert "exactly every selected alpha" in adversary
    assert "exactly each new, flipped, or increased alpha slice" in adversary
    assert "exact union of every original incumbent" in adversary
    assert "exact (`symbol`, `action`) pair" in adversary
    for field in (
        "friction_reviewed",
        "current_evidence_reviewed",
        "loss_control_or_opportunity_reviewed",
        "beta_dollar_impact_reviewed",
    ):
        assert field in adversary


def test_rejection_only_prompt_preserves_one_attempt_typed_envelope():
    revision = _text("agents/pm-revision.md")

    for term in (
        "revision_dispatch_receipt.json",
        "revision_output_receipt.json",
        "no second revision",
        "no malformed-output retry",
        "specialist_reads_sha256",
        "candidate_reviews",
        "revision_fallback_seat_audits",
        "revision_hedge_audit",
        "required_expected_price_edge_frac",
        "required_edge_horizon_hours",
        "required_edge_calibration_basis",
        "required_invalidation_condition",
    ):
        assert term in revision

    for constraint in (
        "drop_symbol",
        "permit_symbol_mutation",
        "max_symbol_notional",
        "min_symbol_notional",
        "min_deploy_frac",
        "max_deploy_frac",
        "max_dollar_residual_frac",
        "max_abs_beta_residual",
        "max_aggressive_changes",
    ):
        assert constraint in revision

    prepare = "uv run python scripts/desk_revision_receipt.py prepare --memory-dir live_memory"
    seal = "uv run python scripts/desk_revision_receipt.py seal --memory-dir live_memory"
    runbook_norm = _normalized("docs/desk-cycle-runbook.md")
    assert prepare in runbook_norm
    assert seal in runbook_norm
    assert runbook_norm.index(prepare) < runbook_norm.index(seal)
    assert "There is no second PM revision" in _normalized("docs/desk-cycle-runbook.md")
    assert "Do not run a second adversary pass" in _normalized("docs/desk-cycle-runbook.md")


def test_reflector_has_pointer_scope_and_no_trade_authority():
    reflector = _text("agents/reflector.md")
    reflector_norm = _normalized("agents/reflector.md")
    assert "pending/current.json" in reflector
    assert "that exact cycle dir" in reflector
    assert "act only on these" in reflector
    assert "ONLY change the managed region" in reflector
    assert "Do not weaken a safety" in reflector
    assert "Never invent a recurrence" in reflector_norm
    assert "Do not answer inactivity by forcing calls" in reflector_norm
    assert "Without the PM's bound `entry_gate` causal label, choose no action" in reflector_norm


def test_concise_ops_prompt_delegates_complete_contract_to_runbook():
    ops = _text("ops/desk-cycle-prompt.md")
    runbook = _text("docs/desk-cycle-runbook.md")
    ops_norm = _normalized("ops/desk-cycle-prompt.md")

    assert "complete** `docs/desk-cycle-runbook.md`" in ops
    assert "gpt-5.6-sol" in ops and "xhigh" in ops
    assert "Spawn sentiment, technical, and futures concurrently" in ops_norm
    assert "at most one receipt-bound PM revision" in ops
    assert "Candidate reviews cover every current non-flat technical candidate" in ops
    assert "never calls an order-placement API" in ops
    assert "agents/pm-revision.md" in runbook
    assert "candidate_reviews" in runbook
