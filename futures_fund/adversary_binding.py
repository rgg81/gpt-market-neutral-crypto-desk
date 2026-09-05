"""Integrity checks binding an Adversary verdict to the precheck it actually reviewed.

These checks validate workflow provenance only. They never decide whether a proposed book is good
or bad: numeric bounds remain data, the Adversary may accept a failing bound with a written
override, and a rejection still receives the runbook's single PM revision.
"""

from __future__ import annotations

import hmac
import json
import math
import re
from hashlib import sha256

from futures_fund.desk_contracts import (
    REVISION_OVERRIDABLE_BOUND_IDS,
    AdversaryVerdict,
    Book,
    SpecialistRead,
)
from futures_fund.precheck import (
    DEPLOY_MAX,
    MAX_PAYBACK_FUNDING_INTERVALS,
    PRECHECK_SCHEMA_VERSION,
    PrecheckMetrics,
    missing_required_precheck_fields,
    precheck_sha256,
)

ECHO_REL_TOL = 0.01
ECHO_ABS_TOL = 1e-6

_ECHO_FIELDS = (
    "gross",
    "deploy_frac",
    "dollar_residual_frac",
    "beta_residual",
    "max_leg_frac_gross",
    "turnover_legs_changed",
    "turnover_aggressive_legs_changed",
    "alpha_gross",
    "hedge_gross",
    "hedge_risk_reducing",
    "hedge_counterfactual_beta_net_usd",
    "hedge_change_risk_reducing",
    "portfolio_residual_vol_annualized_frac_cash",
    "max_alpha_standalone_risk_share",
    "max_same_side_high_correlation_cluster_risk_share",
    "max_position_co_risk_cluster_risk_share",
    "portfolio_expected_total_edge_usd_per_8h",
)

_SCHEMA6_ECHO_FIELDS = (
    "cold_start_reentry_eligible",
    "b9_aggressive_change_limit",
    "risk_model_available",
)
_SCHEMA6_REQUIRED_ECHO_FIELDS = set(_SCHEMA6_ECHO_FIELDS)

_SCHEMA7_ECHO_FIELDS = (
    "controlled_restart_origin_cycle",
    "controlled_restart_phase",
    "controlled_restart_initial_eligible",
    "controlled_restart_continuation_eligible",
    "controlled_restart_lineage_valid",
    "controlled_restart_prior_cycle",
    "controlled_restart_prior_book_sha256",
    "controlled_restart_prior_origin_cycle",
    "controlled_restart_prior_phase",
)
_SCHEMA7_REQUIRED_ECHO_FIELDS = set(_SCHEMA7_ECHO_FIELDS)

_SCHEMA8_ECHO_FIELDS = ("binding_user_directive_present",)
_SCHEMA8_REQUIRED_ECHO_FIELDS = set(_SCHEMA8_ECHO_FIELDS)

_SCHEMA9_ECHO_FIELDS = (
    "binding_user_directive_controlled_restart_graduation",
)
_SCHEMA9_REQUIRED_ECHO_FIELDS = set(_SCHEMA9_ECHO_FIELDS)

_CONTROLLED_RESTART_GROSS_CAP_FRAC_CASH = 0.20
_CONTROLLED_RESTART_RESIDUAL_VOL_CAP_FRAC_CASH = 0.08
_CONTROLLED_RESTART_BETA_RESIDUAL_CAP_ABS = 0.02
_CONTROLLED_RESTART_PERFORMANCE_MIN_SCHEMA_VERSION = 11


class AdversaryBindingError(ValueError):
    """The decision artifacts are stale, tampered, or not mutually bound."""


def _canonical_sha256(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return sha256(encoded).hexdigest()


def specialist_reads_sha256(
    specialist_reads: dict[str, list[SpecialistRead]],
) -> str:
    """Hash the complete normalized specialist packet, including flat/unselected reads."""
    return _canonical_sha256(
        {
            role: [read.model_dump(mode="json") for read in specialist_reads.get(role, [])]
            for role in ("sentiment", "technical", "futures")
        }
    )


def _verify_directive_binding(
    verdict: AdversaryVerdict,
    expected_directive_sha256: str | None,
) -> None:
    """Bind any cold-start exception authority to the exact pending user instruction."""
    actual = verdict.binding_user_directive_sha256
    if expected_directive_sha256 is None:
        if actual is not None or verdict.directive_exception_audits:
            raise AdversaryBindingError(
                "verdict claims directive authority but no bound user directive was dispatched"
            )
        return
    if actual is None or not hmac.compare_digest(actual, expected_directive_sha256):
        raise AdversaryBindingError(
            "adversary binding_user_directive_sha256 does not match the dispatched directive"
        )


def _verify_controlled_restart_risk_audit(
    verdict: AdversaryVerdict,
    precheck: PrecheckMetrics,
    book: Book | None,
    performance_snapshot: dict | None,
) -> None:
    """Bind restart expansion provenance without choosing or vetoing a trade."""
    audit = verdict.controlled_restart_risk_audit
    if not precheck.controlled_restart_phase:
        if audit is not None:
            raise AdversaryBindingError(
                "controlled_restart_risk_audit must be absent outside an active phase"
            )
        return
    if audit is None:
        raise AdversaryBindingError(
            "active controlled restart requires controlled_restart_risk_audit"
        )
    if book is None or performance_snapshot is None:
        raise AdversaryBindingError(
            "restart-risk audit requires the reviewed Book and performance snapshot"
        )
    if precheck.schema_version >= 9 and (
        "directive_graduation_capability_used" not in audit.model_fields_set
    ):
        raise AdversaryBindingError(
            "current restart-risk audit omits directive graduation capability usage"
        )
    try:
        performance_schema = int(performance_snapshot.get("schema_version", 0))
        by_horizon = performance_snapshot["pm_forecast_performance"]["by_horizon_hours"]
    except (KeyError, TypeError, ValueError) as exc:
        raise AdversaryBindingError("restart-risk audit has no shaped performance packet") from exc
    if performance_schema < _CONTROLLED_RESTART_PERFORMANCE_MIN_SCHEMA_VERSION:
        raise AdversaryBindingError(
            "restart-risk audit requires latest-12 cost-net performance schema v11+"
        )

    expected_gross_cap = round(
        _CONTROLLED_RESTART_GROSS_CAP_FRAC_CASH * precheck.cash, 2
    )
    expected_abs_beta = abs(precheck.beta_residual)
    expected_expansion = bool(
        precheck.gross > expected_gross_cap
        or precheck.portfolio_residual_vol_annualized_frac_cash
        > _CONTROLLED_RESTART_RESIDUAL_VOL_CAP_FRAC_CASH
        or expected_abs_beta > _CONTROLLED_RESTART_BETA_RESIDUAL_CAP_ABS
    )
    numeric_facts = {
        "gross_usd": precheck.gross,
        "gross_seed_cap_usd": expected_gross_cap,
        "residual_vol_annualized_frac_cash": (
            precheck.portfolio_residual_vol_annualized_frac_cash
        ),
        "residual_vol_seed_cap_frac_cash": (
            _CONTROLLED_RESTART_RESIDUAL_VOL_CAP_FRAC_CASH
        ),
        "absolute_beta_residual": expected_abs_beta,
        "beta_residual_seed_cap_abs": _CONTROLLED_RESTART_BETA_RESIDUAL_CAP_ABS,
    }
    for field, expected in numeric_facts.items():
        if not math.isclose(
            float(getattr(audit, field)), float(expected), rel_tol=0.0, abs_tol=ECHO_ABS_TOL
        ):
            raise AdversaryBindingError(
                f"controlled_restart_risk_audit.{field} does not match precheck facts"
            )
    if (
        audit.expansion_requested != expected_expansion
        or audit.within_all_seed_caps != (not expected_expansion)
    ):
        raise AdversaryBindingError(
            "controlled_restart_risk_audit expansion/seed-cap facts do not match precheck"
        )
    if audit.expansion_approved and not expected_expansion:
        raise AdversaryBindingError("restart expansion cannot be approved when none was requested")
    if audit.directive_graduation_capability_used:
        if not precheck.binding_user_directive_controlled_restart_graduation:
            raise AdversaryBindingError(
                "restart-risk audit claims a directive graduation capability absent from the "
                "hash-bound typed directive scope"
            )
        if not expected_expansion:
            raise AdversaryBindingError(
                "directive graduation capability cannot be used without requested expansion"
            )

    expected_legs = {leg.symbol: leg for leg in book.legs if leg.seat_role == "alpha"}
    actual_rows = {row.symbol: row for row in audit.selected_alpha_qualifications}
    if set(actual_rows) != set(expected_legs):
        raise AdversaryBindingError(
            "restart-risk audit must exactly cover every selected alpha seat"
        )
    derived_qualifications: list[bool] = []
    for symbol, leg in expected_legs.items():
        row = actual_rows[symbol]
        try:
            bucket = by_horizon[str(leg.edge_horizon_hours)]
            expected_n = bucket["cost_net_independent_time_cohort_n"]
            expected_status = bucket["cost_net_calibration_status"]
            expected_risk_status = bucket["cost_net_residual_risk_weighted_status"]
            expected_value = bucket[
                "residual_risk_weighted_realized_round_trip_cost_net_price_edge_frac"
            ]
        except (KeyError, TypeError) as exc:
            raise AdversaryBindingError(
                f"restart-risk audit lacks a performance bucket for {symbol}"
            ) from exc
        if (
            type(expected_n) is not int
            or not isinstance(expected_status, str)
            or not isinstance(expected_risk_status, str)
            or (
                expected_value is not None
                and (
                    not isinstance(expected_value, (int, float))
                    or isinstance(expected_value, bool)
                    or not math.isfinite(float(expected_value))
                )
            )
        ):
            raise AdversaryBindingError(
                f"restart-risk audit performance bucket for {symbol} is malformed"
            )
        value_matches = (
            row.residual_risk_weighted_realized_round_trip_cost_net_price_edge_frac
            == expected_value
        )
        if (
            row.edge_horizon_hours != leg.edge_horizon_hours
            or row.cost_net_independent_time_cohort_n != expected_n
            or row.cost_net_calibration_status != expected_status
            or row.cost_net_residual_risk_weighted_status != expected_risk_status
            or not value_matches
        ):
            raise AdversaryBindingError(
                f"restart-risk audit for {symbol} does not echo its exact-horizon bucket"
            )
        qualified = bool(
            expected_n >= 12
            and expected_status == "usable"
            and expected_risk_status == "usable"
            and expected_value is not None
            and float(expected_value) > 0.0
        )
        if row.qualified != qualified:
            raise AdversaryBindingError(
                f"restart-risk audit has an incorrect derived qualification for {symbol}"
            )
        derived_qualifications.append(qualified)
    expected_all_qualified = bool(derived_qualifications) and all(derived_qualifications)
    if audit.all_selected_alpha_qualified != expected_all_qualified:
        raise AdversaryBindingError(
            "restart-risk audit all-selected qualification is not derived from its seat rows"
        )

    # The facts and typed directive scope above are deterministic provenance.  The accept/reject
    # choice is not: an accepted expansion must agree with the sole GPT Adversary's explicit
    # approval, but code never derives that decision from qualification or starter-cap facts.
    if verdict.accept and expected_expansion and not (
        audit.expansion_approved and audit.approval_note.strip()
    ):
        raise AdversaryBindingError(
            "accepted restart expansion conflicts with the Adversary's explicit non-approval"
        )


def _verify_exception_scope(
    verdict: AdversaryVerdict,
    precheck: PrecheckMetrics,
    *,
    authorized_failing_bounds: set[str],
) -> None:
    """Keep B9/B12 exceptions action-specific and fail closed on missing cost evidence."""
    audits = {audit.bound_id: audit for audit in verdict.directive_exception_audits}
    aggressive_symbols = {
        leg.symbol for leg in precheck.legs if leg.material_effect in {"entry", "flip", "increase"}
    }

    if "B9" in authorized_failing_bounds:
        audit = audits.get("B9")
        if audit is None or set(audit.symbols) != aggressive_symbols:
            raise AdversaryBindingError(
                "B9 exception must be bound to the exact directive and every final aggressive "
                "symbol"
            )
    elif "B9" in audits:
        raise AdversaryBindingError("verdict carries unused B9 directive authority")

    b12_rows = [
        row
        for row in precheck.change_costs
        if not row.b12_insurance_exempt
        and (not row.friction_priced or row.payback_intervals > MAX_PAYBACK_FUNDING_INTERVALS)
    ]
    if "B12" in authorized_failing_bounds:
        unpriced = sorted(row.symbol for row in b12_rows if not row.friction_priced)
        if unpriced:
            raise AdversaryBindingError(
                "B12 cannot override unpriced economic changes: " + ", ".join(unpriced)
            )
        non_loss_control = [
            row
            for row in b12_rows
            if row.symbol not in aggressive_symbols and row.action not in {"reduction", "drop"}
        ]
        if non_loss_control:
            raise AdversaryBindingError(
                "B12 without an aggressive directive exception is limited to "
                "loss-control reductions/drops"
            )
        aggressive = {row.symbol for row in b12_rows if row.symbol in aggressive_symbols}
        audit = audits.get("B12")
        if aggressive:
            if audit is None or set(audit.symbols) != aggressive:
                raise AdversaryBindingError(
                    "B12 aggressive exception must be bound to the exact directive and "
                    "offending symbols"
                )
        elif audit is not None:
            raise AdversaryBindingError("verdict carries unused B12 directive authority")
    elif "B12" in audits:
        raise AdversaryBindingError("verdict carries unused B12 directive authority")


def _verify_specialist_echoes(
    *,
    symbol: str,
    side: str,
    supporting,
    opposing,
    specialist_reads: dict[str, list[SpecialistRead]],
    label: str,
) -> None:
    """Bind a GPT gate judgment to every persisted non-flat read without choosing thresholds."""
    opposite_side = "short" if side == "long" else "long"
    persisted = {
        role: next(
            (read for read in specialist_reads.get(role, []) if read.symbol == symbol),
            None,
        )
        for role in ("sentiment", "technical", "futures")
    }
    expected_support = {
        role for role, read in persisted.items() if read is not None and read.lean == side
    }
    expected_opposition = {
        role for role, read in persisted.items() if read is not None and read.lean == opposite_side
    }

    def verify(rows, expected: set[str], expected_lean: str, kind: str) -> None:
        roles = [row.role for row in rows]
        if len(roles) != len(set(roles)) or set(roles) != expected:
            raise AdversaryBindingError(
                f"{label} must echo every {kind} specialist for {symbol} exactly once"
            )
        for row in rows:
            read = persisted[row.role]
            if (
                read is None
                or row.lean != expected_lean
                or row.lean != read.lean
                or not math.isclose(row.conviction, read.conviction, rel_tol=0.0, abs_tol=1e-9)
            ):
                raise AdversaryBindingError(
                    f"{label} {kind} echo does not match {row.role} read for {symbol}"
                )

    verify(supporting, expected_support, side, "chosen-side")
    verify(opposing, expected_opposition, opposite_side, "opposing")


def _evidence_urls(read: SpecialistRead) -> set[str]:
    """Extract the exact HTTP(S) URLs the sentiment read says it opened."""
    urls: set[str] = set()
    for item in read.evidence:
        for raw in re.findall(r"https?://[^\s]+", item):
            urls.add(raw.rstrip(".,);]"))
    return urls


def verify_citation_audit(
    verdict: AdversaryVerdict,
    sentiment_reads: list[SpecialistRead],
    book: Book,
) -> None:
    """Bind the Adversary's citation audit to every non-flat sentiment call and the reviewed book.

    This is provenance validation, not a deterministic truth oracle: GPT still decides whether a
    source supports the claim. Code proves that it opened every cited URL, labeled materiality
    honestly, and cannot accept a selected leg whose sentiment support it marked false.
    """
    expected = {
        read.symbol: _evidence_urls(read) for read in sentiment_reads if read.lean != "flat"
    }
    checks = verdict.citation_checks
    actual_symbols = [check.symbol for check in checks]
    if len(actual_symbols) != len(set(actual_symbols)) or set(actual_symbols) != set(expected):
        missing = sorted(set(expected) - set(actual_symbols))
        extra = sorted(set(actual_symbols) - set(expected))
        raise AdversaryBindingError(
            "adversary citation_checks must cover every non-flat sentiment symbol exactly once: "
            f"missing={missing}, extra={extra}, "
            f"duplicates={len(actual_symbols) - len(set(actual_symbols))}"
        )

    selected_symbols = {leg.symbol for leg in book.legs}
    by_symbol = {check.symbol: check for check in checks}
    for symbol, cited_urls in expected.items():
        if not cited_urls:
            raise AdversaryBindingError(
                f"non-flat sentiment read for {symbol} has no citable HTTP(S) URL"
            )
        check = by_symbol[symbol]
        if len(check.checked_urls) != len(set(check.checked_urls)):
            raise AdversaryBindingError(f"citation_checks for {symbol} contains duplicate URLs")
        if set(check.checked_urls) != cited_urls:
            raise AdversaryBindingError(
                f"citation_checks for {symbol} do not match the sentiment read URLs"
            )
        material = symbol in selected_symbols
        if check.material_to_book != material:
            raise AdversaryBindingError(
                f"citation_checks material_to_book is false for reviewed book symbol {symbol}"
                if material
                else f"citation_checks material_to_book is true for unselected symbol {symbol}"
            )
        if verdict.accept and material and not check.supported:
            raise AdversaryBindingError(
                f"accepted book uses unsupported sentiment evidence for selected symbol {symbol}"
            )


def verify_revision_citation_safety(
    verdict: AdversaryVerdict,
    sentiment_reads: list[SpecialistRead],
    final_book: Book,
) -> None:
    """Prevent a one-pass revision from selecting a claim the Adversary marked unsupported.

    Citation materiality in the recorded verdict truthfully describes the original reviewed book.
    The final book therefore cannot rewrite that field, but its selected symbols are rebound here
    to the same support judgments before any paper fill.
    """
    checks = {check.symbol: check for check in verdict.citation_checks}
    nonflat_symbols = {read.symbol for read in sentiment_reads if read.lean != "flat"}
    for leg in final_book.legs:
        if leg.symbol in nonflat_symbols:
            check = checks.get(leg.symbol)
            if check is None or not check.supported:
                raise AdversaryBindingError(
                    "PM revision selects unsupported sentiment evidence for " + leg.symbol
                )


def verify_precheck_artifact(
    artifact: PrecheckMetrics,
    expected: PrecheckMetrics,
    *,
    cycle: int,
    label: str = "precheck",
) -> None:
    """Prove a persisted precheck is intact and was computed for this exact book/input bundle."""
    if artifact.cycle != cycle:
        raise AdversaryBindingError(
            f"{label} is for cycle {artifact.cycle}, current cycle is {cycle}"
        )
    if artifact.schema_version > PRECHECK_SCHEMA_VERSION:
        raise AdversaryBindingError(
            f"{label} uses unsupported future schema v{artifact.schema_version}; "
            f"maximum supported is v{PRECHECK_SCHEMA_VERSION}"
        )
    missing_fields = missing_required_precheck_fields(artifact)
    if missing_fields:
        raise AdversaryBindingError(
            f"{label} omits required explicit schema-v{artifact.schema_version} provenance: "
            + ", ".join(missing_fields)
        )
    canonical = precheck_sha256(artifact)
    if not hmac.compare_digest(artifact.sha256, canonical):
        raise AdversaryBindingError(f"{label} sha256 does not match its contents")
    if not hmac.compare_digest(artifact.sha256, expected.sha256):
        raise AdversaryBindingError(f"{label} does not match the current book and evidence")


def _verify_prior_thesis_echo(
    audit,
    position: dict | None,
    *,
    incumbent: bool,
    require_affirmative_review: bool,
    label: str,
) -> bool:
    """Bind continuation to the newest manifest-bound committed thesis, when applicable.

    Returns whether missing provenance or a triggered prior invalidation requires fresh-entry
    requalification. GPT alone judges whether the objective invalidation fired; code binds which
    prior condition it had to inspect.
    """
    if not incumbent:
        if any(
            (
                audit.prior_thesis_available,
                audit.prior_thesis_cycle is not None,
                audit.prior_thesis_book_sha256 is not None,
                audit.prior_thesis_provenance_reviewed,
                audit.prior_invalidation_reviewed,
                audit.prior_invalidation_triggered,
            )
        ):
            raise AdversaryBindingError(
                f"{label} new/flipped seat carries inapplicable prior-thesis claims"
            )
        return False
    if position is None:
        raise AdversaryBindingError(f"{label} lacks a current performance position")
    thesis = position.get("committed_thesis")
    if not isinstance(thesis, dict):
        raise AdversaryBindingError(f"{label} lacks committed_thesis provenance")
    available = thesis.get("available") is True
    expected_cycle = thesis.get("committed_cycle") if available else None
    expected_hash = thesis.get("manifest_bound_book_sha256") if available else None
    if (
        audit.prior_thesis_available != available
        or audit.prior_thesis_cycle != expected_cycle
        or audit.prior_thesis_book_sha256 != expected_hash
    ):
        raise AdversaryBindingError(
            f"{label} prior-thesis identity does not match performance provenance"
        )
    if not available and (audit.prior_invalidation_reviewed or audit.prior_invalidation_triggered):
        raise AdversaryBindingError(f"{label} claims to inspect an unavailable prior invalidation")
    if require_affirmative_review and not audit.prior_thesis_provenance_reviewed:
        raise AdversaryBindingError(f"{label} did not review prior-thesis provenance")
    if require_affirmative_review and available and not audit.prior_invalidation_reviewed:
        raise AdversaryBindingError(f"{label} did not review the prior committed invalidation")
    return not available or audit.prior_invalidation_triggered


def verify_seat_action_audits(
    verdict: AdversaryVerdict,
    precheck: PrecheckMetrics,
    book: Book,
    *,
    specialist_reads: dict[str, list[SpecialistRead]] | None = None,
    performance_snapshot: dict | None = None,
) -> None:
    """Bind explicit Adversary seat/action coverage to the exact reviewed proposal.

    GPT supplies every semantic judgment. Code only proves there is one truthful review row for
    every selected alpha seat and every aggressive alpha mutation, so incumbency or deployment
    language cannot make a large increase disappear from the sole veto pass.
    """
    leg_metrics = {leg.symbol: leg for leg in precheck.legs}
    change_costs = {row.symbol: row for row in precheck.change_costs}
    positions: dict[str, dict] = {}
    if performance_snapshot is not None:
        raw_positions = performance_snapshot.get("positions")
        if not isinstance(raw_positions, list):
            raise AdversaryBindingError("performance positions must be a complete JSON list")
        position_symbols = [
            str(row.get("symbol"))
            for row in raw_positions
            if isinstance(row, dict) and row.get("symbol")
        ]
        if len(position_symbols) != len(raw_positions) or len(position_symbols) != len(
            set(position_symbols)
        ):
            raise AdversaryBindingError(
                "performance positions are malformed or contain duplicate symbols"
            )
        positions = {str(row["symbol"]): row for row in raw_positions}
    expected_seats = {leg.symbol: leg for leg in book.legs if leg.seat_role == "alpha"}
    seat_symbols = [audit.symbol for audit in verdict.seat_audits]
    if len(seat_symbols) != len(set(seat_symbols)) or set(seat_symbols) != set(expected_seats):
        raise AdversaryBindingError("seat_audits must cover every selected alpha seat exactly once")
    action_map = {
        "none": "hold",
        "entry": "new",
        "flip": "flip",
        "increase": "increase",
        "reduction": "reduction",
    }
    for audit in verdict.seat_audits:
        leg = expected_seats[audit.symbol]
        metric = leg_metrics[audit.symbol]
        expected_action = action_map[metric.material_effect]
        if (
            audit.side != leg.side
            or audit.seat_role != leg.seat_role
            or audit.action != expected_action
        ):
            raise AdversaryBindingError(f"seat_audits misclassifies {audit.symbol}")
        if verdict.accept and not all(
            (
                audit.forward_edge_supported,
                audit.risk_reviewed,
                audit.forecast_calibration_reviewed,
                audit.invalidation_condition_reviewed,
            )
        ):
            raise AdversaryBindingError(
                f"accepted alpha seat {audit.symbol} lacks affirmative "
                "edge/risk/calibration/invalidation audit"
            )
        incumbent = expected_action in {"hold", "increase", "reduction"}
        if incumbent:
            if specialist_reads is not None and performance_snapshot is not None:
                _verify_specialist_echoes(
                    symbol=audit.symbol,
                    side=leg.side,
                    supporting=audit.continuation_supporting_specialists,
                    opposing=audit.continuation_opposing_specialists,
                    specialist_reads=specialist_reads,
                    label="seat_audits continuation",
                )
            if verdict.accept and not all(
                (
                    audit.continuation_supported,
                    audit.cash_or_replacement_compared,
                )
            ):
                raise AdversaryBindingError(
                    f"accepted incumbent alpha seat {audit.symbol} lacks a complete "
                    "continuation/calibration audit"
                )
        elif audit.continuation_supporting_specialists or audit.continuation_opposing_specialists:
            raise AdversaryBindingError(
                f"new/flipped seat {audit.symbol} carries inapplicable continuation echoes"
            )
        if verdict.accept and (
            not leg.edge_calibration_basis.strip() or not leg.invalidation_condition.strip()
        ):
            raise AdversaryBindingError(
                f"accepted alpha seat {audit.symbol} lacks a persisted calibration basis or "
                "objective invalidation"
            )
        if performance_snapshot is not None:
            # A new or flipped seat is a fresh thesis. An old-side position with the same symbol
            # must not leak its age/expiry into the new-side audit; the aggressive action audit
            # already binds that fresh decision. Only retained-side incumbents carry thesis age.
            current_position = positions.get(audit.symbol)
            cost_row = change_costs.get(audit.symbol)
            semantic_role_entry = bool(
                not incumbent
                and audit.action == "new"
                and cost_row is not None
                and cost_row.action == "role_change"
                and cost_row.prior_seat_role == "hedge"
                and cost_row.final_seat_role == "alpha"
                and cost_row.prior_side == cost_row.final_side
            )
            if incumbent and (
                current_position is None
                or current_position.get("side") != leg.side
                or current_position.get("seat_role") != leg.seat_role
            ):
                raise AdversaryBindingError(
                    f"performance positions omit or misclassify incumbent {audit.symbol}"
                )
            if semantic_role_entry and (
                current_position is None
                or current_position.get("side") != leg.side
                or current_position.get("seat_role") != "hedge"
            ):
                raise AdversaryBindingError(
                    f"performance positions do not prove prior hedge inventory for {audit.symbol}"
                )
            if audit.action == "new" and not semantic_role_entry and current_position is not None:
                raise AdversaryBindingError(
                    f"performance positions contradict new-seat classification for {audit.symbol}"
                )
            if audit.action == "flip" and (
                current_position is None
                or cost_row is None
                or current_position.get("side") != cost_row.prior_side
                or current_position.get("seat_role") != cost_row.prior_seat_role
            ):
                raise AdversaryBindingError(
                    f"performance positions do not prove prior flipped inventory for {audit.symbol}"
                )
            position = current_position if incumbent else None
            thesis_requalification_required = _verify_prior_thesis_echo(
                audit,
                position,
                incumbent=incumbent,
                require_affirmative_review=verdict.accept,
                label=f"seat_audits {audit.symbol}",
            )
            expected_age = (
                float(position.get("funding_intervals_held") or 0.0)
                if position is not None
                else None
            )
            expected_expired = bool(
                position is not None and position.get("past_max_hold_horizon") is True
            )
            if (
                audit.past_max_hold_horizon != expected_expired
                or (expected_age is None and audit.position_age_intervals is not None)
                or (
                    expected_age is not None
                    and (
                        audit.position_age_intervals is None
                        or not math.isclose(
                            audit.position_age_intervals,
                            expected_age,
                            rel_tol=0.01,
                            abs_tol=0.05,
                        )
                    )
                )
            ):
                raise AdversaryBindingError(
                    f"seat_audits age/expiry echo does not match performance for {audit.symbol}"
                )
            requalification_required = bool(expected_expired or thesis_requalification_required)
            if verdict.accept and requalification_required and not audit.fresh_entry_requalified:
                raise AdversaryBindingError(
                    f"accepted alpha seat {audit.symbol} requiring fresh-entry requalification "
                    "was not requalified"
                )
            if requalification_required and audit.fresh_entry_requalified:
                if specialist_reads is None:
                    raise AdversaryBindingError(
                        f"alpha seat {audit.symbol} requalification lacks specialist "
                        "read provenance"
                    )
                _verify_specialist_echoes(
                    symbol=audit.symbol,
                    side=leg.side,
                    supporting=audit.requalification_supporting_specialists,
                    opposing=audit.requalification_opposing_specialists,
                    specialist_reads=specialist_reads,
                    label="seat_audits requalification",
                )
            elif requalification_required and (
                audit.requalification_supporting_specialists
                or audit.requalification_opposing_specialists
            ):
                raise AdversaryBindingError(
                    f"unrequalified seat {audit.symbol} carries requalification echoes"
                )
            elif not requalification_required and (
                audit.fresh_entry_requalified
                or audit.requalification_supporting_specialists
                or audit.requalification_opposing_specialists
            ):
                raise AdversaryBindingError(
                    f"seat {audit.symbol} carries inapplicable requalification claims"
                )

    expected_actions = {
        metric.symbol: action_map[metric.material_effect]
        for metric in precheck.legs
        if metric.seat_role == "alpha" and metric.material_effect in {"entry", "flip", "increase"}
    }
    action_symbols = [audit.symbol for audit in verdict.action_audits]
    if len(action_symbols) != len(set(action_symbols)) or set(action_symbols) != set(
        expected_actions
    ):
        raise AdversaryBindingError(
            "action_audits must cover every new, flipped, or increased alpha slice exactly once"
        )
    for audit in verdict.action_audits:
        if audit.action != expected_actions[audit.symbol]:
            raise AdversaryBindingError(f"action_audits misclassifies {audit.symbol}")
        if verdict.accept and (
            not audit.entry_gate_passed
            or not audit.opportunity_cost_compared
            or not audit.forecast_calibration_reviewed
            or not audit.risk_budget_reviewed
        ):
            raise AdversaryBindingError(
                f"accepted aggressive alpha action {audit.symbol} did not pass its complete "
                "entry/calibration/risk gate"
            )
        if specialist_reads is not None:
            leg = expected_seats[audit.symbol]
            _verify_specialist_echoes(
                symbol=audit.symbol,
                side=leg.side,
                supporting=audit.supporting_specialists,
                opposing=audit.opposing_specialists,
                specialist_reads=specialist_reads,
                label="action_audits",
            )


def verify_exit_audits(
    verdict: AdversaryVerdict,
    precheck: PrecheckMetrics,
    *,
    performance_snapshot: dict | None,
    specialist_reads: dict[str, list[SpecialistRead]] | None,
) -> None:
    """Bind the sole Adversary to every incumbent exit it accepts or orders in revision."""
    leg_metrics = {row.symbol: row for row in precheck.legs}
    change_costs = {row.symbol: row for row in precheck.change_costs}
    expected: dict[tuple[str, str], tuple[str, str]] = {
        (row.symbol, row.action): (str(row.prior_side), str(row.prior_seat_role))
        for row in precheck.change_costs
        if row.action in {"drop", "flip", "role_change"}
        and row.prior_side is not None
        and row.prior_seat_role is not None
    }
    # A rejection can order a lifecycle-ending mutation that the original proposal did not make.
    # Capture that prospective disposition in the same verdict now; the revision is not a second
    # veto pass. New-from-flat proposals have no incumbent lifecycle to audit.
    for constraint in verdict.revision_constraints:
        if not constraint.symbol:
            continue
        row = change_costs.get(constraint.symbol)
        prior_side: str | None = None
        prior_seat_role: str | None = None
        if (
            row is not None
            and row.action != "entry"
            and row.prior_side is not None
            and row.prior_seat_role is not None
        ):
            prior_side = str(row.prior_side)
            prior_seat_role = str(row.prior_seat_role)
        else:
            metric = leg_metrics.get(constraint.symbol)
            if metric is not None and metric.material_effect == "none":
                prior_side = metric.side
                prior_seat_role = metric.seat_role
        if prior_side is None or prior_seat_role is None:
            continue
        if constraint.kind == "drop_symbol":
            expected[(constraint.symbol, "drop")] = (prior_side, prior_seat_role)
            continue
        if constraint.kind not in {
            "permit_symbol_mutation",
            "max_symbol_notional",
            "min_symbol_notional",
        }:
            continue
        final_side = constraint.final_side
        final_seat_role = constraint.final_seat_role
        if final_side is None or final_seat_role is None:
            continue
        prospective_action = (
            "role_change"
            if final_seat_role != prior_seat_role
            else "flip"
            if final_side != prior_side
            else None
        )
        if prospective_action is not None:
            expected[(constraint.symbol, prospective_action)] = (
                prior_side,
                prior_seat_role,
            )
    audit_keys = [(audit.symbol, audit.action) for audit in verdict.exit_audits]
    if len(audit_keys) != len(set(audit_keys)) or set(audit_keys) != set(expected):
        raise AdversaryBindingError(
            "exit_audits misclassifies or fails to cover every ended incumbent lifecycle and "
            "prospectively authorized disposition exactly once"
        )
    positions: dict[str, dict] = {}
    if performance_snapshot is not None:
        raw_positions = performance_snapshot.get("positions")
        if not isinstance(raw_positions, list):
            raise AdversaryBindingError("exit audits require performance positions")
        positions = {
            str(row.get("symbol")): row
            for row in raw_positions
            if isinstance(row, dict) and row.get("symbol")
        }
    for audit in verdict.exit_audits:
        prior_side, prior_seat_role = expected[(audit.symbol, audit.action)]
        if audit.prior_side != prior_side or audit.prior_seat_role != prior_seat_role:
            raise AdversaryBindingError(
                f"exit_audits misclassifies prior inventory for {audit.symbol}"
            )
        if verdict.accept and not all(
            (
                audit.friction_reviewed,
                audit.current_evidence_reviewed,
                audit.loss_control_or_opportunity_reviewed,
                audit.beta_dollar_impact_reviewed,
            )
        ):
            raise AdversaryBindingError(
                f"accepted incumbent disposition {audit.symbol} lacks a complete exit audit"
            )
        if specialist_reads is not None:
            _verify_specialist_echoes(
                symbol=audit.symbol,
                side=prior_side,
                supporting=audit.supporting_specialists,
                opposing=audit.opposing_specialists,
                specialist_reads=specialist_reads,
                label="exit_audits",
            )
        if performance_snapshot is None:
            continue
        position = positions.get(audit.symbol)
        if (
            position is None
            or position.get("side") != prior_side
            or position.get("seat_role") != prior_seat_role
        ):
            raise AdversaryBindingError(
                f"exit audit {audit.symbol} does not match held performance inventory"
            )
        if prior_seat_role == "alpha":
            _verify_prior_thesis_echo(
                audit,
                position,
                incumbent=True,
                require_affirmative_review=verdict.accept,
                label=f"exit audit {audit.symbol}",
            )
        elif any(
            (
                audit.prior_thesis_available,
                audit.prior_thesis_cycle is not None,
                audit.prior_thesis_book_sha256 is not None,
                audit.prior_thesis_provenance_reviewed,
                audit.prior_invalidation_reviewed,
                audit.prior_invalidation_triggered,
            )
        ):
            raise AdversaryBindingError(
                f"hedge exit audit {audit.symbol} carries inapplicable alpha-thesis claims"
            )


def _verify_revision_exit_authority(
    verdict: AdversaryVerdict,
    final_precheck: PrecheckMetrics,
) -> None:
    """Prevent a rejected book from laundering an unreviewed incumbent drop."""
    audits = {(audit.symbol, audit.action): audit for audit in verdict.exit_audits}
    for row in final_precheck.change_costs:
        if row.action not in {"drop", "flip", "role_change"}:
            continue
        audit = audits.get((row.symbol, row.action))
        if audit is None:
            raise AdversaryBindingError(
                f"PM revision ends incumbent {row.symbol} without an Adversary ExitAudit"
            )
        if (
            audit.action != row.action
            or audit.prior_side != row.prior_side
            or audit.prior_seat_role != row.prior_seat_role
        ):
            raise AdversaryBindingError(
                f"PM revision disposition for {row.symbol} exceeds its ExitAudit"
            )
        if not all(
            (
                audit.friction_reviewed,
                audit.current_evidence_reviewed,
                audit.loss_control_or_opportunity_reviewed,
                audit.beta_dollar_impact_reviewed,
                audit.prior_thesis_provenance_reviewed
                if audit.prior_seat_role == "alpha"
                else True,
                audit.prior_invalidation_reviewed if audit.prior_thesis_available else True,
            )
        ):
            raise AdversaryBindingError(
                f"PM revision retains failed Adversary exit audit for {row.symbol}"
            )


def verify_hedge_audit(
    verdict: AdversaryVerdict,
    precheck: PrecheckMetrics,
    book: Book,
) -> None:
    """Bind the typed BTC hedge label to arithmetic and an explicit Adversary review."""
    hedges = [leg for leg in book.legs if leg.seat_role == "hedge"]
    if len(hedges) > 1:
        raise AdversaryBindingError("production book contains multiple typed BTC hedges")
    if not hedges:
        if verdict.hedge_audit is not None:
            raise AdversaryBindingError("hedge_audit is present but the reviewed book has no hedge")
        return

    hedge = hedges[0]
    audit = verdict.hedge_audit
    if audit is None:
        raise AdversaryBindingError("typed BTC hedge requires an explicit hedge_audit")
    if (
        audit.symbol != hedge.symbol
        or audit.side != hedge.side
        or not math.isclose(
            audit.target_notional,
            hedge.target_notional,
            rel_tol=0.0,
            abs_tol=ECHO_ABS_TOL,
        )
        or audit.risk_reducing_vs_alpha_book != precheck.hedge_risk_reducing
        or audit.change_risk_reducing_vs_carried_hedge != precheck.hedge_change_risk_reducing
    ):
        raise AdversaryBindingError(
            "hedge_audit does not match the typed BTC leg and precheck counterfactual"
        )
    if verdict.accept and not all(
        (
            audit.risk_reducing_vs_alpha_book,
            audit.change_risk_reducing_vs_carried_hedge,
            audit.counterfactual_reviewed,
            audit.carry_cost_reviewed,
            audit.liquidity_reviewed,
        )
    ):
        raise AdversaryBindingError(
            "accepted typed BTC hedge lacks a complete risk/counterfactual/cost audit"
        )


def verify_revision_fallback_audits(
    verdict: AdversaryVerdict,
    *,
    specialist_reads: dict[str, list[SpecialistRead]] | None,
    performance_snapshot: dict | None,
) -> None:
    """Validate explicit authority to restore a held alpha seat not reviewed in the PM book."""
    audits = verdict.revision_fallback_seat_audits
    if not audits:
        return
    if verdict.accept:
        raise AdversaryBindingError("accepted verdict cannot contain fallback seat audits")
    if specialist_reads is None or performance_snapshot is None:
        raise AdversaryBindingError(
            "revision fallback seat audits require specialist and performance provenance"
        )
    symbols = [audit.symbol for audit in audits]
    if len(symbols) != len(set(symbols)):
        raise AdversaryBindingError("revision fallback seat audits contain duplicate symbols")
    positions = {
        str(row.get("symbol")): row
        for row in performance_snapshot.get("positions", [])
        if isinstance(row, dict) and row.get("symbol")
    }
    typed_constraints = [
        constraint
        for constraint in verdict.revision_constraints
        if constraint.kind
        in {"permit_symbol_mutation", "max_symbol_notional", "min_symbol_notional"}
    ]
    for audit in audits:
        matching_constraints = [
            constraint
            for constraint in typed_constraints
            if constraint.symbol == audit.symbol
            and constraint.final_side == audit.side
            and constraint.final_seat_role == audit.seat_role
        ]
        if not matching_constraints:
            raise AdversaryBindingError(
                f"fallback audit for {audit.symbol} lacks a matching typed revision constraint"
            )
        if audit.seat_role != "alpha" or audit.action not in {"hold", "increase", "reduction"}:
            raise AdversaryBindingError(
                f"fallback audit for {audit.symbol} is not an incumbent alpha action"
            )
        if not all(
            (
                audit.forward_edge_supported,
                audit.risk_reviewed,
                audit.continuation_supported,
                audit.cash_or_replacement_compared,
                audit.forecast_calibration_reviewed,
                audit.invalidation_condition_reviewed,
            )
        ):
            raise AdversaryBindingError(
                f"fallback incumbent {audit.symbol} lacks a complete affirmative audit"
            )
        _verify_specialist_echoes(
            symbol=audit.symbol,
            side=audit.side,
            supporting=audit.continuation_supporting_specialists,
            opposing=audit.continuation_opposing_specialists,
            specialist_reads=specialist_reads,
            label="revision fallback continuation",
        )
        position = positions.get(audit.symbol)
        if (
            position is None
            or position.get("side") != audit.side
            or position.get("seat_role") != "alpha"
        ):
            raise AdversaryBindingError(
                f"fallback incumbent {audit.symbol} does not match a held current-side position"
            )
        thesis_requalification_required = _verify_prior_thesis_echo(
            audit,
            position,
            incumbent=True,
            require_affirmative_review=True,
            label=f"fallback incumbent {audit.symbol}",
        )
        expected_age = float(position.get("funding_intervals_held") or 0.0)
        expected_expired = position.get("past_max_hold_horizon") is True
        if (
            audit.position_age_intervals is None
            or not math.isclose(
                audit.position_age_intervals,
                expected_age,
                rel_tol=0.01,
                abs_tol=0.05,
            )
            or audit.past_max_hold_horizon != expected_expired
        ):
            raise AdversaryBindingError(
                f"fallback incumbent {audit.symbol} age/expiry echo does not match performance"
            )
        requalification_required = bool(expected_expired or thesis_requalification_required)
        if requalification_required and not audit.fresh_entry_requalified:
            raise AdversaryBindingError(
                f"fallback incumbent {audit.symbol} requiring fresh-entry requalification "
                "was not requalified"
            )
        if requalification_required:
            _verify_specialist_echoes(
                symbol=audit.symbol,
                side=audit.side,
                supporting=audit.requalification_supporting_specialists,
                opposing=audit.requalification_opposing_specialists,
                specialist_reads=specialist_reads,
                label="revision fallback requalification",
            )
        elif (
            audit.fresh_entry_requalified
            or audit.requalification_supporting_specialists
            or audit.requalification_opposing_specialists
        ):
            raise AdversaryBindingError(
                f"non-expired fallback incumbent {audit.symbol} carries requalification claims"
            )


def verify_revision_hedge_authority(verdict: AdversaryVerdict) -> None:
    """Validate the Adversary's prospective authority for a hedge in the sole PM revision."""
    audit = verdict.revision_hedge_audit
    if audit is None:
        return
    constraints = [
        constraint
        for constraint in verdict.revision_constraints
        if constraint.symbol == audit.symbol
        and constraint.kind
        in {"permit_symbol_mutation", "max_symbol_notional", "min_symbol_notional"}
    ]
    if not constraints or any(
        constraint.final_side != audit.side or constraint.final_seat_role != "hedge"
        for constraint in constraints
    ):
        raise AdversaryBindingError("revision_hedge_audit lacks matching typed hedge constraints")
    for constraint in constraints:
        if (
            constraint.kind == "max_symbol_notional"
            and audit.target_notional > float(constraint.value) + ECHO_ABS_TOL
        ) or (
            constraint.kind == "min_symbol_notional"
            and audit.target_notional + ECHO_ABS_TOL < float(constraint.value)
        ):
            raise AdversaryBindingError(
                "revision_hedge_audit target violates its notional constraint"
            )
    if not all(
        (
            audit.risk_reducing_vs_alpha_book,
            audit.change_risk_reducing_vs_carried_hedge,
            audit.counterfactual_reviewed,
            audit.carry_cost_reviewed,
            audit.liquidity_reviewed,
        )
    ):
        raise AdversaryBindingError(
            "revision_hedge_audit must affirm final risk, counterfactual, carry, and liquidity"
        )


def verify_verdict_binding(
    verdict: AdversaryVerdict,
    precheck: PrecheckMetrics,
    *,
    cycle: int,
    sentiment_reads: list[SpecialistRead] | None = None,
    specialist_reads: dict[str, list[SpecialistRead]] | None = None,
    performance_snapshot: dict | None = None,
    book: Book | None = None,
    entry_gate_policy_sha256: str | None = None,
    binding_user_directive_sha256: str | None = None,
) -> None:
    """Prove the verdict reviewed `precheck`, including its hash, metrics, and all bound IDs."""
    if verdict.cycle != cycle or precheck.cycle != cycle:
        raise AdversaryBindingError(
            f"cycle mismatch: verdict={verdict.cycle}, precheck={precheck.cycle}, current={cycle}"
        )
    if entry_gate_policy_sha256 is not None and not hmac.compare_digest(
        verdict.entry_gate_policy_sha256, entry_gate_policy_sha256
    ):
        raise AdversaryBindingError(
            "adversary entry_gate_policy_sha256 does not match the dispatched PM policy"
        )
    if not hmac.compare_digest(verdict.precheck_sha256, precheck.sha256):
        raise AdversaryBindingError("adversary precheck_sha256 does not match reviewed precheck")
    if precheck.schema_version >= 5:
        if "hard_ban_violations_confirmed" not in verdict.model_fields_set:
            raise AdversaryBindingError(
                "current adversary verdict omits hard_ban_violations_confirmed"
            )
        if verdict.hard_ban_violations_confirmed != precheck.hard_ban_violations:
            raise AdversaryBindingError(
                "adversary hard_ban_violations_confirmed does not exactly echo precheck facts"
            )
    elif verdict.hard_ban_violations_confirmed:
        raise AdversaryBindingError(
            "historical precheck cannot authorize newly claimed objective hard-ban facts"
        )
    _verify_directive_binding(verdict, binding_user_directive_sha256)
    if precheck.schema_version >= 8 and precheck.binding_user_directive_present != (
        binding_user_directive_sha256 is not None
    ):
        raise AdversaryBindingError(
            "precheck binding_user_directive_present does not match dispatched cycle meta"
        )
    if (
        precheck.schema_version >= 9
        and precheck.binding_user_directive_controlled_restart_graduation
        and binding_user_directive_sha256 is None
    ):
        raise AdversaryBindingError(
            "typed controlled-restart graduation scope lacks a bound user directive"
        )
    if specialist_reads is not None:
        expected_reads_sha256 = specialist_reads_sha256(specialist_reads)
        if verdict.specialist_reads_sha256 is None or not hmac.compare_digest(
            verdict.specialist_reads_sha256, expected_reads_sha256
        ):
            raise AdversaryBindingError(
                "adversary specialist_reads_sha256 does not match the complete read packet"
            )
    elif verdict.specialist_reads_sha256 is not None:
        raise AdversaryBindingError("verdict carries a specialist-read hash without a bound packet")
    if performance_snapshot is not None and precheck.schema_version >= 2:
        expected_performance_sha256 = _canonical_sha256(performance_snapshot)
        if verdict.performance_snapshot_sha256 is None or not hmac.compare_digest(
            verdict.performance_snapshot_sha256, expected_performance_sha256
        ):
            raise AdversaryBindingError(
                "adversary performance_snapshot_sha256 does not match the reviewed packet"
            )
    elif verdict.performance_snapshot_sha256 is not None:
        raise AdversaryBindingError(
            "verdict carries a performance snapshot hash without a bound packet"
        )

    if precheck.schema_version >= 6:
        missing_echo_fields = sorted(
            _SCHEMA6_REQUIRED_ECHO_FIELDS - verdict.metrics_echo.model_fields_set
        )
        if missing_echo_fields:
            raise AdversaryBindingError(
                "current adversary metrics_echo omits required precheck provenance: "
                + ", ".join(missing_echo_fields)
            )

    if precheck.schema_version >= 7:
        missing_echo_fields = sorted(
            _SCHEMA7_REQUIRED_ECHO_FIELDS - verdict.metrics_echo.model_fields_set
        )
        if missing_echo_fields:
            raise AdversaryBindingError(
                "current adversary metrics_echo omits required restart lineage: "
                + ", ".join(missing_echo_fields)
            )

    if precheck.schema_version >= 8:
        missing_echo_fields = sorted(
            _SCHEMA8_REQUIRED_ECHO_FIELDS - verdict.metrics_echo.model_fields_set
        )
        if missing_echo_fields:
            raise AdversaryBindingError(
                "current adversary metrics_echo omits required directive provenance: "
                + ", ".join(missing_echo_fields)
            )

    if precheck.schema_version >= 9:
        missing_echo_fields = sorted(
            _SCHEMA9_REQUIRED_ECHO_FIELDS - verdict.metrics_echo.model_fields_set
        )
        if missing_echo_fields:
            raise AdversaryBindingError(
                "current adversary metrics_echo omits required typed directive scope: "
                + ", ".join(missing_echo_fields)
            )

    echo_fields = (
        _ECHO_FIELDS
        + (_SCHEMA6_ECHO_FIELDS if precheck.schema_version >= 6 else ())
        + (_SCHEMA7_ECHO_FIELDS if precheck.schema_version >= 7 else ())
        + (_SCHEMA8_ECHO_FIELDS if precheck.schema_version >= 8 else ())
        + (_SCHEMA9_ECHO_FIELDS if precheck.schema_version >= 9 else ())
    )
    for field in echo_fields:
        echoed = getattr(verdict.metrics_echo, field)
        actual = getattr(precheck, field)
        if field in {
            "turnover_legs_changed",
            "turnover_aggressive_legs_changed",
            "cold_start_reentry_eligible",
            "b9_aggressive_change_limit",
            "risk_model_available",
            "controlled_restart_origin_cycle",
            "controlled_restart_phase",
            "controlled_restart_initial_eligible",
            "controlled_restart_continuation_eligible",
            "controlled_restart_lineage_valid",
            "controlled_restart_prior_cycle",
            "controlled_restart_prior_book_sha256",
            "controlled_restart_prior_origin_cycle",
            "controlled_restart_prior_phase",
            "binding_user_directive_present",
            "binding_user_directive_controlled_restart_graduation",
            "hedge_risk_reducing",
            "hedge_change_risk_reducing",
        }:
            matches = echoed == actual
        else:
            matches = math.isclose(
                float(echoed), float(actual), rel_tol=ECHO_REL_TOL, abs_tol=ECHO_ABS_TOL
            )
        if not matches:
            raise AdversaryBindingError(
                f"adversary metrics_echo.{field}={echoed} does not match precheck value {actual}"
            )

    checks = {bound.bound_id: bound for bound in precheck.bounds}
    rulings = {bound.bound_id: bound for bound in verdict.bounds_confirmed}
    if set(rulings) != set(checks):
        raise AdversaryBindingError("adversary bound IDs do not match the reviewed precheck")

    for bound_id, ruling in rulings.items():
        if ruling.ok != checks[bound_id].ok and not ruling.note.strip():
            raise AdversaryBindingError(
                f"{bound_id} ruling differs from precheck without an explanatory note"
            )

    failing = [bound.bound_id for bound in precheck.bounds if not bound.ok]
    structural_failures = sorted(set(failing).intersection({"B7", "B8", "B10", "B11"}))
    if verdict.accept and structural_failures:
        raise AdversaryBindingError(
            "accepted book cannot override structural/provenance bound failures: "
            + ", ".join(structural_failures)
        )
    if verdict.accept and precheck.schema_version >= 5 and precheck.hard_ban_violations:
        symbols = sorted({row.symbol for row in precheck.hard_ban_violations})
        raise AdversaryBindingError(
            "accepted book contains objective hard-ban violations: " + ", ".join(symbols)
        )
    if (
        verdict.accept
        and precheck.schema_version >= 7
        and not precheck.controlled_restart_lineage_valid
    ):
        raise AdversaryBindingError(
            "accepted book cannot carry invalid controlled-restart lineage"
        )
    if verdict.accept and failing and not verdict.override_rationale.strip():
        raise AdversaryBindingError(
            "accepting failing precheck bounds requires override_rationale: " + ", ".join(failing)
        )
    if verdict.accept:
        _verify_exception_scope(
            verdict,
            precheck,
            authorized_failing_bounds=set(failing),
        )
    if not verdict.accept and precheck.schema_version >= 2:
        typed_mutation_kinds = {
            "permit_symbol_mutation",
            "max_symbol_notional",
            "min_symbol_notional",
        }
        legacy_typed = [
            constraint.kind
            for constraint in verdict.revision_constraints
            if constraint.kind in typed_mutation_kinds and constraint.schema_version < 2
        ]
        if legacy_typed:
            raise AdversaryBindingError(
                "new rejected precheck requires v2 typed revision constraints with exact "
                "horizon/thesis fields: " + ", ".join(legacy_typed)
            )
    if precheck.schema_version >= 8:
        _verify_controlled_restart_risk_audit(
            verdict, precheck, book, performance_snapshot
        )
    if sentiment_reads is not None or book is not None:
        if sentiment_reads is None or book is None:
            raise AdversaryBindingError(
                "citation audit binding requires both sentiment_reads and reviewed book"
            )
        if specialist_reads is not None:
            expected_reads_sha256 = specialist_reads_sha256(specialist_reads)
            if book.specialist_reads_sha256 is None or not hmac.compare_digest(
                book.specialist_reads_sha256, expected_reads_sha256
            ):
                raise AdversaryBindingError(
                    "PM book specialist_reads_sha256 does not match the complete read packet"
                )
        if precheck.schema_version >= 2:
            try:
                book.validate_production_contract()
            except ValueError as exc:
                raise AdversaryBindingError(
                    f"reviewed PM book violates the production forecast contract: {exc}"
                ) from exc
            verify_hedge_audit(verdict, precheck, book)
            verify_revision_fallback_audits(
                verdict,
                specialist_reads=specialist_reads,
                performance_snapshot=performance_snapshot,
            )
            verify_revision_hedge_authority(verdict)
        verify_citation_audit(verdict, sentiment_reads, book)
        verify_seat_action_audits(
            verdict,
            precheck,
            book,
            specialist_reads=specialist_reads,
            performance_snapshot=performance_snapshot,
        )
        verify_exit_audits(
            verdict,
            precheck,
            performance_snapshot=performance_snapshot,
            specialist_reads=specialist_reads,
        )


def verify_revision_binding(
    verdict: AdversaryVerdict,
    original_book: Book,
    original_precheck: PrecheckMetrics,
    final_book: Book,
    final_precheck: PrecheckMetrics,
    *,
    binding_user_directive_sha256: str | None = None,
) -> None:
    """Prove the single unreviewed PM revision obeys the Adversary's structured decision.

    This is mechanical provenance enforcement: GPT chose every constraint and every explicitly
    allowed failing bound. Code neither invents a target nor judges expected profitability.
    """
    if verdict.accept:
        raise AdversaryBindingError("revision binding requires a rejected original verdict")
    if original_precheck.schema_version >= 7:
        prior_fields = (
            "controlled_restart_prior_cycle",
            "controlled_restart_prior_book_sha256",
            "controlled_restart_prior_origin_cycle",
            "controlled_restart_prior_phase",
        )
        if final_precheck.schema_version < 7 or any(
            getattr(final_precheck, field) != getattr(original_precheck, field)
            for field in prior_fields
        ):
            raise AdversaryBindingError(
                "PM revision precheck changed the manifest-bound prior restart lineage"
            )
        if not final_precheck.controlled_restart_lineage_valid:
            raise AdversaryBindingError(
                "PM revision has invalid controlled-restart lineage"
            )
        lineage_unchanged = (
            final_book.controlled_restart_origin_cycle
            == original_book.controlled_restart_origin_cycle
            and final_book.controlled_restart_phase
            == original_book.controlled_restart_phase
        )
        safe_explicit_end = bool(
            original_book.controlled_restart_phase
            and not final_book.controlled_restart_phase
            and final_book.controlled_restart_origin_cycle is None
            and not final_book.legs
        )
        if not (lineage_unchanged or safe_explicit_end):
            raise AdversaryBindingError(
                "PM revision changed controlled-restart lineage outside the reviewed original"
            )
    if original_precheck.schema_version >= 8:
        if (
            final_precheck.schema_version < 8
            or final_precheck.binding_user_directive_present
            != original_precheck.binding_user_directive_present
        ):
            raise AdversaryBindingError(
                "PM revision changed binding user-directive provenance"
            )
        if original_precheck.schema_version >= 9 and (
            final_precheck.schema_version < 9
            or final_precheck.binding_user_directive_controlled_restart_graduation
            != original_precheck.binding_user_directive_controlled_restart_graduation
        ):
            raise AdversaryBindingError(
                "PM revision changed typed user-directive capability scope"
            )
        if final_precheck.controlled_restart_phase:
            audit = verdict.controlled_restart_risk_audit
            if audit is None:
                raise AdversaryBindingError(
                    "active restart revision lacks the original restart-risk audit"
                )
            audited_horizons = {
                row.symbol: row.edge_horizon_hours
                for row in audit.selected_alpha_qualifications
            }
            final_alpha = [leg for leg in final_book.legs if leg.seat_role == "alpha"]
            if any(
                audited_horizons.get(leg.symbol) != leg.edge_horizon_hours
                for leg in final_alpha
            ):
                raise AdversaryBindingError(
                    "PM revision added or changed an active restart seat/horizon outside the "
                    "original restart-risk audit"
                )
            final_expansion = bool(
                final_precheck.gross
                > round(_CONTROLLED_RESTART_GROSS_CAP_FRAC_CASH * final_precheck.cash, 2)
                or final_precheck.portfolio_residual_vol_annualized_frac_cash
                > _CONTROLLED_RESTART_RESIDUAL_VOL_CAP_FRAC_CASH
                or abs(final_precheck.beta_residual)
                > _CONTROLLED_RESTART_BETA_RESIDUAL_CAP_ABS
            )
            if final_expansion:
                if not (audit.expansion_approved and audit.approval_note.strip()):
                    raise AdversaryBindingError(
                        "controlled-restart revision expansion exceeds the Adversary's original "
                        "explicit approval"
                    )
    if final_precheck.schema_version >= 5 and final_precheck.hard_ban_violations:
        symbols = sorted({row.symbol for row in final_precheck.hard_ban_violations})
        raise AdversaryBindingError(
            "PM revision retains objective hard-ban violations: " + ", ".join(symbols)
        )
    _verify_directive_binding(verdict, binding_user_directive_sha256)
    if original_precheck.schema_version >= 2:
        try:
            final_book.validate_production_contract()
        except ValueError as exc:
            raise AdversaryBindingError(
                f"PM revision violates the production forecast contract: {exc}"
            ) from exc
        if any(leg.seat_role == "hedge" for leg in final_book.legs) and not (
            final_precheck.hedge_risk_reducing and final_precheck.hedge_change_risk_reducing
        ):
            raise AdversaryBindingError(
                "PM revision retains a typed hedge that is not risk-reducing versus both the "
                "alpha book and the carried BTC counterfactual"
            )
        if (
            original_book.specialist_reads_sha256 is not None
            or final_book.specialist_reads_sha256 is not None
        ) and final_book.specialist_reads_sha256 != (original_book.specialist_reads_sha256):
            raise AdversaryBindingError("PM revision changed the bound specialist read packet")

    original = {leg.symbol: leg for leg in original_book.legs}
    final = {leg.symbol: leg for leg in final_book.legs}
    original_hedge = next((leg for leg in original_book.legs if leg.seat_role == "hedge"), None)
    final_hedge = next((leg for leg in final_book.legs if leg.seat_role == "hedge"), None)
    original_hedge_audit = verdict.hedge_audit
    original_hedge_audit_failed = bool(
        original_hedge is not None
        and (
            original_hedge_audit is None
            or not all(
                (
                    original_hedge_audit.risk_reducing_vs_alpha_book,
                    original_hedge_audit.change_risk_reducing_vs_carried_hedge,
                    original_hedge_audit.counterfactual_reviewed,
                    original_hedge_audit.carry_cost_reviewed,
                    original_hedge_audit.liquidity_reviewed,
                )
            )
        )
    )
    hedge_context_changed = bool(
        final_hedge is not None
        and (
            original_hedge is None
            or original_hedge_audit_failed
            or original_hedge.side != final_hedge.side
            or not math.isclose(
                original_hedge.target_notional,
                final_hedge.target_notional,
                rel_tol=0.0,
                abs_tol=ECHO_ABS_TOL,
            )
            or not math.isclose(
                original_precheck.alpha_beta_net_usd_before_hedge,
                final_precheck.alpha_beta_net_usd_before_hedge,
                rel_tol=0.0,
                abs_tol=ECHO_ABS_TOL,
            )
        )
    )
    revision_hedge_audit = verdict.revision_hedge_audit
    if hedge_context_changed:
        if revision_hedge_audit is None:
            raise AdversaryBindingError(
                "PM revision changes the hedge or its alpha-beta context without a "
                "revision_hedge_audit"
            )
        if (
            revision_hedge_audit.symbol != final_hedge.symbol
            or revision_hedge_audit.side != final_hedge.side
            or not math.isclose(
                revision_hedge_audit.target_notional,
                final_hedge.target_notional,
                rel_tol=0.0,
                abs_tol=ECHO_ABS_TOL,
            )
            or revision_hedge_audit.risk_reducing_vs_alpha_book
            != final_precheck.hedge_risk_reducing
            or revision_hedge_audit.change_risk_reducing_vs_carried_hedge
            != final_precheck.hedge_change_risk_reducing
            or not all(
                (
                    revision_hedge_audit.counterfactual_reviewed,
                    revision_hedge_audit.carry_cost_reviewed,
                    revision_hedge_audit.liquidity_reviewed,
                )
            )
        ):
            raise AdversaryBindingError(
                "revision_hedge_audit does not bind the final hedge and counterfactual"
            )
    elif revision_hedge_audit is not None:
        raise AdversaryBindingError("revision carries unused hedge authority")
    mutation_kinds = {
        "drop_symbol",
        "permit_symbol_mutation",
        "max_symbol_notional",
        "min_symbol_notional",
    }
    mutable_symbols = {
        constraint.symbol
        for constraint in verdict.revision_constraints
        if constraint.kind in mutation_kinds
    }
    corrective_constraint = False

    def _same_economic_seat(before, after) -> bool:
        if before is None or after is None:
            return before is after
        return (
            before.side == after.side
            and before.seat_role == after.seat_role
            and math.isclose(
                before.expected_price_edge_frac,
                after.expected_price_edge_frac,
                rel_tol=0.0,
                abs_tol=ECHO_ABS_TOL,
            )
            and before.edge_horizon_hours == after.edge_horizon_hours
            and before.edge_calibration_basis == after.edge_calibration_basis
            and before.invalidation_condition == after.invalidation_condition
            and math.isclose(
                before.target_notional,
                after.target_notional,
                rel_tol=0.0,
                abs_tol=ECHO_ABS_TOL,
            )
        )

    for symbol in set(original) | set(final):
        if not _same_economic_seat(original.get(symbol), final.get(symbol)):
            if symbol not in mutable_symbols:
                raise AdversaryBindingError(
                    f"PM revision mutates {symbol} outside the Adversary envelope"
                )

    for constraint in verdict.revision_constraints:
        kind = constraint.kind
        symbol = constraint.symbol
        value = float(constraint.value) if constraint.value is not None else None
        final_leg = final.get(symbol) if symbol else None

        if kind == "drop_symbol":
            if symbol not in original:
                raise AdversaryBindingError(
                    f"drop_symbol references absent original symbol {symbol}"
                )
            if final_leg is not None:
                raise AdversaryBindingError(
                    f"PM revision violates drop_symbol for {symbol}: "
                    f"retained ${final_leg.target_notional:.2f}"
                )
            corrective_constraint = True
        elif kind == "permit_symbol_mutation":
            if final_leg is None:
                raise AdversaryBindingError(
                    f"permit_symbol_mutation requires a final leg for {symbol}"
                )
        elif kind == "max_symbol_notional":
            if final_leg is None:
                raise AdversaryBindingError(
                    f"max_symbol_notional requires a final leg for {symbol}"
                )
            actual = final_leg.target_notional
            if actual > value + ECHO_ABS_TOL:
                raise AdversaryBindingError(
                    f"PM revision violates max_symbol_notional for {symbol}: {actual} > {value}"
                )
            original_actual = original[symbol].target_notional if symbol in original else 0.0
            corrective_constraint |= original_actual > value + ECHO_ABS_TOL
        elif kind == "min_symbol_notional":
            if final_leg is None:
                raise AdversaryBindingError(
                    f"min_symbol_notional requires a final leg for {symbol}"
                )
            actual = final_leg.target_notional
            if actual + ECHO_ABS_TOL < value:
                raise AdversaryBindingError(
                    f"PM revision violates min_symbol_notional for {symbol}: {actual} < {value}"
                )
            original_actual = original[symbol].target_notional if symbol in original else 0.0
            corrective_constraint |= original_actual + ECHO_ABS_TOL < value
        elif kind == "preserve_symbol":
            before = original.get(symbol)
            if before is None:
                raise AdversaryBindingError(
                    f"preserve_symbol references absent original symbol {symbol}"
                )
            if (
                final_leg is None
                or final_leg.side != before.side
                or not math.isclose(
                    final_leg.target_notional,
                    before.target_notional,
                    rel_tol=0.0,
                    abs_tol=ECHO_ABS_TOL,
                )
            ):
                raise AdversaryBindingError(f"PM revision violates preserve_symbol for {symbol}")
        elif kind == "correct_book_metadata":
            original_failed = {bound.bound_id for bound in original_precheck.bounds if not bound.ok}
            if not original_failed.intersection({"B7", "B8"}):
                raise AdversaryBindingError(
                    "correct_book_metadata is vacuous: original B7/B8 already pass"
                )
            corrective_constraint = True
        elif kind == "min_deploy_frac":
            if final_precheck.deploy_frac + ECHO_ABS_TOL < value:
                raise AdversaryBindingError(
                    f"PM revision deploy {final_precheck.deploy_frac} is below {value}"
                )
            corrective_constraint |= original_precheck.deploy_frac + ECHO_ABS_TOL < value
        elif kind == "max_deploy_frac":
            if final_precheck.deploy_frac > value + ECHO_ABS_TOL:
                raise AdversaryBindingError(
                    f"PM revision deploy {final_precheck.deploy_frac} exceeds {value}"
                )
            corrective_constraint |= original_precheck.deploy_frac > value + ECHO_ABS_TOL
        elif kind == "max_dollar_residual_frac":
            if final_precheck.dollar_residual_frac > value + ECHO_ABS_TOL:
                raise AdversaryBindingError(
                    "PM revision dollar residual "
                    f"{final_precheck.dollar_residual_frac} exceeds {value}"
                )
            corrective_constraint |= original_precheck.dollar_residual_frac > value + ECHO_ABS_TOL
        elif kind == "max_abs_beta_residual":
            if abs(final_precheck.beta_residual) > value + ECHO_ABS_TOL:
                raise AdversaryBindingError(
                    f"PM revision beta residual {final_precheck.beta_residual} exceeds +/-{value}"
                )
            corrective_constraint |= abs(original_precheck.beta_residual) > value + ECHO_ABS_TOL
        elif kind == "max_aggressive_changes":
            actual = final_precheck.turnover_aggressive_legs_changed
            if actual > int(value):
                raise AdversaryBindingError(
                    f"PM revision aggressive changes {actual} exceed {int(value)}"
                )
            corrective_constraint |= original_precheck.turnover_aggressive_legs_changed > int(value)

        if kind in {"permit_symbol_mutation", "max_symbol_notional", "min_symbol_notional"}:
            if (
                final_leg is None
                or final_leg.side != constraint.final_side
                or final_leg.seat_role != constraint.final_seat_role
            ):
                raise AdversaryBindingError(
                    f"PM revision violates final side/seat role for {symbol}"
                )
            if original_precheck.schema_version >= 2 and (
                constraint.required_expected_price_edge_frac is None
                or not math.isclose(
                    final_leg.expected_price_edge_frac,
                    constraint.required_expected_price_edge_frac,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            ):
                raise AdversaryBindingError(
                    f"PM revision price-edge forecast for {symbol} does not match the exact "
                    "Adversary constraint"
                )
            if (
                final_leg.expected_price_edge_frac
                > constraint.max_expected_price_edge_frac + ECHO_ABS_TOL
            ):
                raise AdversaryBindingError(
                    f"PM revision price-edge forecast for {symbol} exceeds Adversary ceiling"
                )
            if (
                constraint.min_edge_horizon_hours is not None
                and final_leg.edge_horizon_hours < constraint.min_edge_horizon_hours
            ):
                raise AdversaryBindingError(
                    f"PM revision edge horizon for {symbol} is shorter than Adversary minimum"
                )
            if original_precheck.schema_version >= 2:
                if (
                    constraint.required_edge_horizon_hours is None
                    or final_leg.edge_horizon_hours != constraint.required_edge_horizon_hours
                ):
                    raise AdversaryBindingError(
                        f"PM revision horizon for {symbol} does not match the exact "
                        "Adversary constraint"
                    )
                if final_leg.seat_role == "alpha" and (
                    not (constraint.required_edge_calibration_basis or "").strip()
                    or not (constraint.required_invalidation_condition or "").strip()
                    or final_leg.edge_calibration_basis
                    != constraint.required_edge_calibration_basis
                    or final_leg.invalidation_condition
                    != constraint.required_invalidation_condition
                ):
                    raise AdversaryBindingError(
                        f"PM revision thesis metadata for {symbol} does not match the exact "
                        "Adversary constraint"
                    )

    corrective_constraint |= any(
        not _same_economic_seat(original.get(symbol), final.get(symbol))
        for symbol in mutable_symbols
    )

    # A revision may restore a held side that the original PM flipped or omitted, but that side
    # was not reviewed by the original seat audit. Require a separate, provenance-bound fallback
    # incumbent audit selected by the sole Adversary; otherwise the safe revision is to leave the
    # unaudited seat out and use an allowed under-deployment/loss-control bound where necessary.
    reviewed_seats = {audit.symbol: audit for audit in verdict.seat_audits}
    fallback_seats = {audit.symbol: audit for audit in verdict.revision_fallback_seat_audits}
    final_metrics = {metric.symbol: metric for metric in final_precheck.legs}
    action_by_effect = {
        "none": "hold",
        "entry": "new",
        "flip": "flip",
        "increase": "increase",
        "reduction": "reduction",
    }
    used_fallbacks: set[str] = set()
    for leg in final_book.legs:
        if leg.seat_role != "alpha":
            continue
        reviewed = reviewed_seats.get(leg.symbol)
        if reviewed is not None and (
            reviewed.side == leg.side and reviewed.seat_role == leg.seat_role
        ):
            continue
        fallback = fallback_seats.get(leg.symbol)
        if fallback is None:
            raise AdversaryBindingError(
                "PM revision contains an alpha seat/side not covered by either the original "
                f"seat audit or a fallback incumbent audit: {leg.symbol}"
            )
        expected_action = action_by_effect[final_metrics[leg.symbol].material_effect]
        if (
            fallback.side != leg.side
            or fallback.seat_role != leg.seat_role
            or fallback.action != expected_action
            or expected_action not in {"hold", "increase", "reduction"}
        ):
            raise AdversaryBindingError(
                f"fallback incumbent audit does not cover the final action for {leg.symbol}"
            )
        used_fallbacks.add(leg.symbol)
    unused_fallbacks = sorted(set(fallback_seats) - used_fallbacks)
    if unused_fallbacks:
        raise AdversaryBindingError(
            "revision carries unused fallback incumbent authority: " + ", ".join(unused_fallbacks)
        )

    _verify_revision_exit_authority(verdict, final_precheck)

    # The revision is not a second adversarial pass. A seat the sole Adversary explicitly marked
    # unsupported, unreviewed, or expired-and-unrequalified cannot survive by fixing some unrelated
    # constraint. A failed aggressive gate may remain only after the aggressive mutation itself is
    # removed (for example, restoring an incumbent's prior size).
    for audit in verdict.seat_audits:
        incumbent = audit.action in {"hold", "increase", "reduction"}
        failed_seat = (
            not audit.forward_edge_supported
            or not audit.risk_reviewed
            or not audit.forecast_calibration_reviewed
            or not audit.invalidation_condition_reviewed
            or (
                incumbent
                and (
                    not audit.prior_thesis_provenance_reviewed
                    or (audit.prior_thesis_available and not audit.prior_invalidation_reviewed)
                    or (
                        (not audit.prior_thesis_available or audit.prior_invalidation_triggered)
                        and not audit.fresh_entry_requalified
                    )
                )
            )
            or (
                incumbent
                and (not audit.continuation_supported or not audit.cash_or_replacement_compared)
            )
            or (audit.past_max_hold_horizon and not audit.fresh_entry_requalified)
        )
        final_leg = final.get(audit.symbol)
        if (
            failed_seat
            and final_leg is not None
            and final_leg.side == audit.side
            and final_leg.seat_role == audit.seat_role
        ):
            raise AdversaryBindingError(
                f"PM revision retains failed Adversary seat audit for {audit.symbol}"
            )
    action_by_symbol = {audit.symbol: audit for audit in verdict.action_audits}
    action_kind = {"entry": "new", "flip": "flip", "increase": "increase"}
    original_metrics = {metric.symbol: metric for metric in original_precheck.legs}
    for final_metric in final_precheck.legs:
        if final_metric.seat_role != "alpha" or final_metric.material_effect not in action_kind:
            continue
        audit = action_by_symbol.get(final_metric.symbol)
        if audit is None or audit.action != action_kind[final_metric.material_effect]:
            raise AdversaryBindingError(
                "PM revision introduces an unreviewed aggressive alpha action for "
                f"{final_metric.symbol}"
            )
        reviewed_metric = original_metrics.get(final_metric.symbol)
        if (
            reviewed_metric is None
            or reviewed_metric.material_effect != final_metric.material_effect
            or final_metric.turnover_notional > reviewed_metric.turnover_notional + ECHO_ABS_TOL
        ):
            raise AdversaryBindingError(
                "PM revision enlarges an aggressive alpha slice beyond the Adversary-reviewed "
                f"increment for {final_metric.symbol}"
            )
        if (
            not audit.entry_gate_passed
            or not audit.opportunity_cost_compared
            or not audit.forecast_calibration_reviewed
            or not audit.risk_budget_reviewed
        ):
            raise AdversaryBindingError(
                f"PM revision retains failed Adversary action audit for {audit.symbol}"
            )

    if not corrective_constraint:
        raise AdversaryBindingError(
            "rejected verdict has no structured constraint that corrects the original book"
        )

    allowed = set(verdict.revision_allowed_failing_bounds)
    disallowed = allowed - REVISION_OVERRIDABLE_BOUND_IDS
    if disallowed:
        raise AdversaryBindingError(
            "revision cannot allow safety/provenance bound failures: "
            + ", ".join(sorted(disallowed))
        )
    if "B1" in allowed and final_precheck.deploy_frac > DEPLOY_MAX + ECHO_ABS_TOL:
        raise AdversaryBindingError(
            "B1 revision allowance permits under-deployment only; leverage ceiling still binds"
        )
    failing = {bound.bound_id for bound in final_precheck.bounds if not bound.ok}
    unexpected = sorted(failing - allowed)
    if unexpected:
        raise AdversaryBindingError(
            "PM revision introduced non-approved failing bounds: " + ", ".join(unexpected)
        )
    unused = sorted(allowed - failing)
    if unused:
        raise AdversaryBindingError(
            "PM revision carries unused failing-bound authority: " + ", ".join(unused)
        )
    _verify_exception_scope(
        verdict,
        final_precheck,
        authorized_failing_bounds=allowed,
    )
