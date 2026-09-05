"""Structured-output contracts between the LLM agents and the deterministic driver."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Lean = Literal["long", "short", "flat"]
Side = Literal["long", "short"]
SeatRole = Literal["alpha", "hedge"]
SeatAction = Literal["hold", "new", "flip", "increase", "reduction"]
AggressiveAction = Literal["new", "flip", "increase"]
ObjectiveHardBanAction = Literal["new", "flip", "increase", "hedge_to_alpha"]
ObjectiveHardBanRule = Literal[
    "post_crash_short",
    "fade_short",
    "aggressive_alpha_2k_slippage",
    "aggressive_alpha_low_depth",
]
SpecialistRole = Literal["sentiment", "technical", "futures"]
CandidateStatus = Literal["selected", "rejected", "deferred"]
CandidateExclusionReason = Literal[
    "selected",
    "entry_gate",
    "economics",
    "risk_budget",
    "neutrality",
    "liquidity",
    "evidence_quality",
    "other",
]
BoundId = Literal["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10", "B11", "B12"]
DirectiveExceptionBoundId = Literal["B9", "B12"]
EXPECTED_BOUND_IDS = tuple(f"B{i}" for i in range(1, 13))
REVISION_OVERRIDABLE_BOUND_IDS = frozenset({"B1", "B9", "B12"})
RevisionConstraintKind = Literal[
    "drop_symbol",
    "permit_symbol_mutation",
    "max_symbol_notional",
    "min_symbol_notional",
    "preserve_symbol",
    "correct_book_metadata",
    "min_deploy_frac",
    "max_deploy_frac",
    "max_dollar_residual_frac",
    "max_abs_beta_residual",
    "max_aggressive_changes",
]
PRODUCTION_FORECAST_HORIZON_HOURS = frozenset({24, 72, 168})


class ObjectiveHardBanViolation(BaseModel):
    """One objective entry-policy violation computed from the immutable decision packet.

    The fields are deliberately factual rather than a free-form reason. The Adversary copies the
    exact rows into its verdict; deterministic binding then proves that none was silently waived.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    rule_id: ObjectiveHardBanRule
    symbol: str = Field(min_length=1)
    action: ObjectiveHardBanAction
    side: Side
    target_notional: float = Field(gt=0.0)
    momentum_pct: float | None = None
    est_slippage_bps_2k: float | None = Field(default=None, ge=0.0)
    depth_usd_bid: float | None = Field(default=None, ge=0.0)
    depth_usd_ask: float | None = Field(default=None, ge=0.0)


class SpecialistRead(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    lean: Lean
    conviction: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence: list[str] = Field(default_factory=list)


class SpecialistSupportEcho(BaseModel):
    """Exact persisted specialist read echoed in a governed decision artifact."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    role: SpecialistRole
    lean: Lean
    conviction: float = Field(ge=0.0, le=1.0)


class CandidateReview(BaseModel):
    """A PM-declared candidate and its disposition, retained for shadow scoring.

    ``exclusion_reason='entry_gate'`` is only the PM's causal declaration. Deterministic code
    binds it to the immutable decision inputs and later measures its outcome; it never infers why
    a candidate was omitted and never uses this record to create or veto a trade.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str = Field(min_length=1)
    side: Side
    status: CandidateStatus
    exclusion_reason: CandidateExclusionReason
    expected_price_edge_frac: float = Field(ge=-1.0, le=1.0)
    edge_horizon_hours: Literal[24, 72, 168]
    counterfactual_notional: float = Field(gt=0.0)
    supporting_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_disposition(self) -> CandidateReview:
        if (self.status == "selected") != (self.exclusion_reason == "selected"):
            raise ValueError("selected candidate status and selected exclusion_reason must agree")
        roles = [echo.role for echo in self.supporting_specialists]
        if len(roles) != len(set(roles)):
            raise ValueError("candidate supporting_specialists contains duplicate roles")
        if any(echo.lean != self.side for echo in self.supporting_specialists):
            raise ValueError("candidate supporting specialist must lean to the candidate side")
        return self


class BookLeg(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    side: Side
    target_notional: float = Field(gt=0.0)
    seat_role: SeatRole = Field(
        default="alpha",
        description="alpha seeks forward edge; hedge exists only to reduce portfolio beta",
    )
    expected_price_edge_frac: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description=(
            "Expected selected-side beta-adjusted price contribution over edge_horizon_hours; "
            "positive is favorable to this position and excludes funding"
        ),
    )
    # The broad range preserves immutable historical parseability. New production decisions are
    # separately constrained to PRODUCTION_FORECAST_HORIZON_HOURS before reconcile.
    edge_horizon_hours: int = Field(default=24, ge=8, le=24 * 14)
    edge_calibration_basis: str = Field(
        default="",
        description=(
            "Auditable empirical basis for the conservative price-edge forecast, including "
            "horizon-matched sample size/error warning; never a restatement of past momentum"
        ),
    )
    invalidation_condition: str = Field(
        default="",
        description="Objective current-evidence condition that invalidates the forward thesis",
    )
    rationale: str = ""
    is_new: bool = Field(default=False, description="True if this leg was NOT in current_book")
    hold_breaking_reason: str = Field(
        default="",
        description="REQUIRED if is_new=True or side changed - which specific rule exception "
        "justifies this change",
    )

    @model_validator(mode="after")
    def validate_seat_role(self) -> BookLeg:
        if self.seat_role == "hedge" and self.symbol != "BTC/USDT:USDT":
            raise ValueError("seat_role=hedge is reserved for BTC/USDT:USDT")
        return self


class Book(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    specialist_reads_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Exact complete specialist packet used by the PM",
    )
    # Historical books predate candidate opportunity accounting and remain parseable. New
    # production orchestration validates complete coverage against the bound specialist packet.
    candidate_reviews: list[CandidateReview] = Field(default_factory=list)
    legs: list[BookLeg] = Field(default_factory=list)
    stated_deploy_frac: float = 0.0
    stated_dollar_residual_frac: float = 0.0
    stated_beta_residual: float = 0.0
    turnover_legs_changed: int = Field(
        default=0,
        ge=0,
        description=(
            "Executable legs changed vs current_book "
            "(new+dropped+flipped+any >$0.01 resize)"
        ),
    )
    turnover_justification: str = Field(
        default="",
        description=(
            "Explain executable turnover; exits/reductions are reported even though B9 caps only "
            "aggressive entries/flips/increases"
        ),
    )
    notes: str = ""

    def validate_production_contract(self) -> Book:
        """Validate fields required only for newly proposed production PAPER books.

        Base-model parsing remains deliberately broader so immutable historical artifacts stay
        readable. Production horizons are restricted to mark schedules the desk can actually
        score, and every alpha thesis must carry auditable calibration/invalidation text.
        """
        leg_symbols = [leg.symbol for leg in self.legs]
        if len(leg_symbols) != len(set(leg_symbols)):
            duplicates = sorted(
                symbol for symbol in set(leg_symbols) if leg_symbols.count(symbol) > 1
            )
            raise ValueError(
                "production Book requires exactly one net BookLeg per symbol; "
                f"duplicates={duplicates}"
            )
        candidate_keys = [(item.symbol, item.side) for item in self.candidate_reviews]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("candidate_reviews contains duplicate symbol/side rows")
        selected_candidates = {
            (item.symbol, item.side): item
            for item in self.candidate_reviews
            if item.status == "selected"
        }
        alpha_legs = {(leg.symbol, leg.side): leg for leg in self.legs if leg.seat_role == "alpha"}
        if self.candidate_reviews and set(selected_candidates) != set(alpha_legs):
            raise ValueError("selected candidate_reviews must exactly cover selected alpha legs")
        for key, candidate in selected_candidates.items():
            leg = alpha_legs[key]
            if (
                candidate.counterfactual_notional != leg.target_notional
                or candidate.expected_price_edge_frac != leg.expected_price_edge_frac
                or candidate.edge_horizon_hours != leg.edge_horizon_hours
            ):
                raise ValueError(
                    f"selected candidate review does not bind the BookLeg economics: {key}"
                )
        for leg in self.legs:
            if leg.edge_horizon_hours not in PRODUCTION_FORECAST_HORIZON_HOURS:
                raise ValueError(
                    f"{leg.symbol} edge_horizon_hours={leg.edge_horizon_hours} is not one of "
                    f"the schedulable production buckets "
                    f"{sorted(PRODUCTION_FORECAST_HORIZON_HOURS)}"
                )
            if leg.seat_role == "alpha" and (
                not leg.edge_calibration_basis.strip() or not leg.invalidation_condition.strip()
            ):
                raise ValueError(
                    f"{leg.symbol} alpha thesis requires calibration basis and invalidation"
                )
            if leg.seat_role == "hedge" and (
                leg.expected_price_edge_frac != 0.0
                or leg.edge_horizon_hours != 24
                or leg.edge_calibration_basis.strip()
                or leg.invalidation_condition.strip()
            ):
                raise ValueError(
                    f"{leg.symbol} hedge must use zero price edge, the canonical 24h horizon, "
                    "and no alpha calibration/invalidation thesis"
                )
        return self

    def validate_candidate_review_coverage(self, reads: dict[str, list[SpecialistRead]]) -> Book:
        """Bind optional candidate rows to every supporting read and required opportunity.

        This validator is intentionally separate from ordinary model parsing because historical
        books have no candidate ledger. Production orchestration calls it only after the complete
        specialist packet has been sealed.
        """
        if not self.candidate_reviews:
            raise ValueError("new production Book requires candidate_reviews")
        read_map: dict[tuple[str, str], dict[str, SpecialistRead]] = {}
        for role in ("sentiment", "technical", "futures"):
            for read in reads.get(role, []):
                if read.lean == "flat":
                    continue
                read_map.setdefault((read.symbol, read.lean), {})[role] = read
        candidates = {(item.symbol, item.side): item for item in self.candidate_reviews}
        required = {
            (read.symbol, read.lean) for read in reads.get("technical", []) if read.lean != "flat"
        }
        required.update((leg.symbol, leg.side) for leg in self.legs if leg.seat_role == "alpha")
        missing = sorted(required - set(candidates))
        if missing:
            raise ValueError(
                f"candidate_reviews lacks non-flat technical/selected alpha candidates: {missing}"
            )
        for key, candidate in candidates.items():
            expected = read_map.get(key, {})
            actual = {echo.role: echo for echo in candidate.supporting_specialists}
            if set(actual) != set(expected):
                raise ValueError(
                    f"candidate supporting_specialists does not exactly cover reads: {key}"
                )
            for role, read in expected.items():
                echo = actual[role]
                if echo.lean != read.lean or echo.conviction != read.conviction:
                    raise ValueError(
                        f"candidate supporting specialist echo differs from bound read: {key}"
                    )
        return self


class RevisionConstraint(BaseModel):
    """One machine-checkable instruction from the sole-veto Adversary to the one PM revision.

    Deterministic code never invents the constraint or decides whether it is desirable. It only
    proves that the PM's unreviewed revision obeys the decision the Adversary already made.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: int = Field(default=1, ge=1)
    kind: RevisionConstraintKind
    symbol: str = ""
    value: float | None = None
    final_side: Side | None = None
    final_seat_role: SeatRole | None = None
    max_expected_price_edge_frac: float | None = Field(default=None, ge=-1.0, le=1.0)
    required_expected_price_edge_frac: float | None = Field(default=None, ge=-1.0, le=1.0)
    min_edge_horizon_hours: int | None = Field(
        default=None,
        ge=8,
        le=24 * 14,
    )
    required_edge_horizon_hours: Literal[24, 72, 168] | None = None
    required_edge_calibration_basis: str | None = None
    required_invalidation_condition: str | None = None
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_shape(self) -> RevisionConstraint:
        symbol_kinds = {
            "drop_symbol",
            "permit_symbol_mutation",
            "max_symbol_notional",
            "min_symbol_notional",
            "preserve_symbol",
        }
        value_kinds = {
            "max_symbol_notional",
            "min_symbol_notional",
            "min_deploy_frac",
            "max_deploy_frac",
            "max_dollar_residual_frac",
            "max_abs_beta_residual",
            "max_aggressive_changes",
        }
        if self.kind in symbol_kinds and not self.symbol.strip():
            raise ValueError(f"{self.kind} requires symbol")
        if self.kind not in symbol_kinds and self.symbol.strip():
            raise ValueError(f"{self.kind} does not accept symbol")
        if self.kind in value_kinds:
            if self.value is None or self.value < 0.0:
                raise ValueError(f"{self.kind} requires a non-negative value")
        elif self.value is not None:
            raise ValueError(f"{self.kind} does not accept value")
        typed_mutation_kinds = {
            "permit_symbol_mutation",
            "max_symbol_notional",
            "min_symbol_notional",
        }
        if self.kind in typed_mutation_kinds:
            if self.final_side is None or self.final_seat_role is None:
                raise ValueError(f"{self.kind} requires final_side and final_seat_role")
            if self.max_expected_price_edge_frac is None or (
                self.min_edge_horizon_hours is None and self.required_edge_horizon_hours is None
            ):
                raise ValueError(
                    f"{self.kind} requires a price-edge ceiling and horizon constraint"
                )
            if self.final_seat_role == "hedge" and self.symbol != "BTC/USDT:USDT":
                raise ValueError("only BTC/USDT:USDT may be authorized as a hedge")
        elif (
            self.final_side is not None
            or self.final_seat_role is not None
            or self.max_expected_price_edge_frac is not None
            or self.required_expected_price_edge_frac is not None
            or self.min_edge_horizon_hours is not None
            or self.required_edge_horizon_hours is not None
            or self.required_edge_calibration_basis is not None
            or self.required_invalidation_condition is not None
        ):
            raise ValueError(f"{self.kind} does not accept final seat/forecast fields")
        if self.kind == "max_aggressive_changes" and self.value is not None:
            if not float(self.value).is_integer():
                raise ValueError("max_aggressive_changes value must be an integer")
        if self.schema_version >= 2 and self.kind in typed_mutation_kinds:
            if self.required_edge_horizon_hours is None:
                raise ValueError(f"{self.kind} v2 requires an exact production forecast horizon")
            if self.required_expected_price_edge_frac is None:
                raise ValueError(f"{self.kind} v2 requires an exact price-edge forecast")
            if (
                self.max_expected_price_edge_frac is not None
                and self.required_expected_price_edge_frac > self.max_expected_price_edge_frac
            ):
                raise ValueError(f"{self.kind} v2 exact price edge exceeds its legacy ceiling")
            if self.final_seat_role == "alpha" and (
                not (self.required_edge_calibration_basis or "").strip()
                or not (self.required_invalidation_condition or "").strip()
            ):
                raise ValueError(
                    f"{self.kind} v2 requires exact calibration and invalidation text for alpha"
                )
            if self.final_seat_role == "hedge" and (
                self.required_edge_calibration_basis is not None
                or self.required_invalidation_condition is not None
            ):
                raise ValueError(f"{self.kind} v2 does not accept alpha thesis fields for a hedge")
            if self.final_seat_role == "hedge" and (
                self.max_expected_price_edge_frac != 0.0
                or self.required_expected_price_edge_frac != 0.0
                or self.required_edge_horizon_hours != 24
                or (self.min_edge_horizon_hours is not None and self.min_edge_horizon_hours > 24)
            ):
                raise ValueError(
                    f"{self.kind} v2 hedge must authorize exactly zero price edge and the "
                    "canonical 24h horizon"
                )
        return self


class MetricsEcho(BaseModel):
    """The precheck numbers the Adversary must transcribe into its verdict — proof it looked.

    Cycle 4 shipped on a 66-byte bare accept; requiring the echo makes that literal payload fail
    the `model_validate` that already runs in desk_reconcile.py, BEFORE any fill. Form validation,
    not a code veto: the Adversary still owns the decision."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    gross: float = Field(ge=0.0)
    deploy_frac: float
    dollar_residual_frac: float
    beta_residual: float
    max_leg_frac_gross: float = Field(ge=0.0)
    turnover_legs_changed: int = Field(ge=0)
    turnover_aggressive_legs_changed: int = Field(default=0, ge=0)
    alpha_gross: float = Field(default=0.0, ge=0.0)
    hedge_gross: float = Field(default=0.0, ge=0.0)
    hedge_risk_reducing: bool = False
    hedge_counterfactual_beta_net_usd: float = 0.0
    hedge_change_risk_reducing: bool = False
    portfolio_residual_vol_annualized_frac_cash: float = Field(default=0.0, ge=0.0)
    max_alpha_standalone_risk_share: float = Field(default=0.0, ge=0.0)
    max_same_side_high_correlation_cluster_risk_share: float = Field(default=0.0, ge=0.0)
    max_position_co_risk_cluster_risk_share: float = Field(default=0.0, ge=0.0)
    portfolio_expected_total_edge_usd_per_8h: float = 0.0


class BoundVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bound_id: BoundId  # B1..B12
    ok: bool  # the Adversary's own pass/fail call on this bound
    note: str = ""  # required reasoning when overriding a failing bound


class CitationCheck(BaseModel):
    """One independently opened sentiment claim, recorded by the Adversary.

    `checked_urls` must be the exact URLs carried by that symbol's non-flat sentiment read.
    Decision-chain validation binds coverage and materiality to the persisted reads/book; the
    Adversary remains responsible for the semantic fact-check.
    """

    model_config = ConfigDict(extra="forbid")
    symbol: str
    supported: bool
    material_to_book: bool
    checked_urls: list[str] = Field(min_length=1)
    note: str = Field(min_length=1)


class SeatAudit(BaseModel):
    """Adversary's explicit economic review of every selected alpha seat."""

    model_config = ConfigDict(extra="forbid")
    symbol: str
    side: Side
    seat_role: SeatRole
    action: SeatAction
    forward_edge_supported: bool
    risk_reviewed: bool
    continuation_supported: bool = False
    cash_or_replacement_compared: bool = False
    forecast_calibration_reviewed: bool = False
    invalidation_condition_reviewed: bool = False
    prior_thesis_available: bool = False
    prior_thesis_cycle: int | None = Field(default=None, ge=1)
    prior_thesis_book_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prior_thesis_provenance_reviewed: bool = False
    prior_invalidation_reviewed: bool = False
    prior_invalidation_triggered: bool = False
    continuation_supporting_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    continuation_opposing_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    position_age_intervals: float | None = Field(default=None, ge=0.0)
    past_max_hold_horizon: bool = False
    fresh_entry_requalified: bool = False
    requalification_supporting_specialists: list[SpecialistSupportEcho] = Field(
        default_factory=list
    )
    requalification_opposing_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_prior_thesis_shape(self) -> SeatAudit:
        if self.prior_thesis_available != bool(
            self.prior_thesis_cycle is not None and self.prior_thesis_book_sha256 is not None
        ):
            raise ValueError("prior thesis availability requires exact cycle and bound book hash")
        if self.prior_invalidation_triggered and not self.prior_invalidation_reviewed:
            raise ValueError("a triggered prior invalidation must have been explicitly reviewed")
        return self


class ActionAudit(BaseModel):
    """Fresh-entry gate review for each new, flipped, or increased alpha slice."""

    model_config = ConfigDict(extra="forbid")
    symbol: str
    action: AggressiveAction
    entry_gate_passed: bool
    opportunity_cost_compared: bool
    forecast_calibration_reviewed: bool = False
    risk_budget_reviewed: bool = False
    supporting_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    opposing_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    note: str = Field(min_length=1)


class ExitAudit(BaseModel):
    """Adversary review of an incumbent lifecycle ended by drop, flip, or role change."""

    model_config = ConfigDict(extra="forbid")
    symbol: str
    prior_side: Side
    prior_seat_role: SeatRole
    action: Literal["drop", "flip", "role_change"]
    friction_reviewed: bool
    current_evidence_reviewed: bool
    loss_control_or_opportunity_reviewed: bool
    beta_dollar_impact_reviewed: bool
    prior_thesis_available: bool = False
    prior_thesis_cycle: int | None = Field(default=None, ge=1)
    prior_thesis_book_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    prior_thesis_provenance_reviewed: bool = False
    prior_invalidation_reviewed: bool = False
    prior_invalidation_triggered: bool = False
    supporting_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    opposing_specialists: list[SpecialistSupportEcho] = Field(default_factory=list)
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_prior_thesis_shape(self) -> ExitAudit:
        if self.prior_thesis_available != bool(
            self.prior_thesis_cycle is not None and self.prior_thesis_book_sha256 is not None
        ):
            raise ValueError("prior thesis availability requires exact cycle and bound book hash")
        if self.prior_invalidation_triggered and not self.prior_invalidation_reviewed:
            raise ValueError("a triggered prior invalidation must have been explicitly reviewed")
        return self


class HedgeAudit(BaseModel):
    """Adversary's exact audit of the single typed BTC beta hedge, when present."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    side: Side
    target_notional: float = Field(gt=0.0)
    risk_reducing_vs_alpha_book: bool
    change_risk_reducing_vs_carried_hedge: bool
    counterfactual_reviewed: bool
    carry_cost_reviewed: bool
    liquidity_reviewed: bool
    note: str = Field(min_length=1)


class DirectiveExceptionAudit(BaseModel):
    """Exact cold-start actions the Adversary says a hash-bound directive authorizes.

    The directive remains a user instruction interpreted by GPT. Deterministic binding only
    prevents a generic B9/B12 allowance from outliving that exact instruction or silently
    covering additional symbols.
    """

    model_config = ConfigDict(extra="forbid")
    bound_id: DirectiveExceptionBoundId
    symbols: list[str] = Field(min_length=1)
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_symbols(self) -> DirectiveExceptionAudit:
        if any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("directive exception symbols must be non-empty")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("directive exception symbols contain duplicates")
        return self


class AdversaryVerdict(BaseModel):
    """The Adversary's verdict. `cycle`, `precheck_sha256`, `metrics_echo`, and `bounds_confirmed`
    are REQUIRED so an accept must demonstrate the arithmetic was seen (cycle-4 regression).
    `citation_checks` records URL-by-URL review of every non-flat sentiment call; the decision-chain
    binder requires complete coverage on new cycles while retaining a default for historical
    artifacts written before this field existed.
    An accept with any failing bound needs a non-empty `override_rationale` — the veto stays the
    Adversary's, but silence is no longer a valid form of approval."""

    model_config = ConfigDict(extra="forbid")
    accept: bool
    cycle: int = Field(ge=1)
    precheck_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_gate_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    performance_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    specialist_reads_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    binding_user_directive_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    directive_exception_audits: list[DirectiveExceptionAudit] = Field(default_factory=list)
    hard_ban_violations_confirmed: list[ObjectiveHardBanViolation] = Field(
        default_factory=list
    )
    metrics_echo: MetricsEcho
    bounds_confirmed: list[BoundVerdict] = Field(min_length=12, max_length=12)
    citation_checks: list[CitationCheck] = Field(default_factory=list)
    seat_audits: list[SeatAudit] = Field(default_factory=list)
    action_audits: list[ActionAudit] = Field(default_factory=list)
    exit_audits: list[ExitAudit] = Field(default_factory=list)
    hedge_audit: HedgeAudit | None = None
    revision_hedge_audit: HedgeAudit | None = None
    revision_fallback_seat_audits: list[SeatAudit] = Field(default_factory=list)
    override_rationale: str = ""
    objections: list[str] = Field(default_factory=list)
    demanded_changes: list[str] = Field(default_factory=list)
    revision_constraints: list[RevisionConstraint] = Field(default_factory=list)
    revision_allowed_failing_bounds: list[BoundId] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decision_completeness(self) -> AdversaryVerdict:
        ids = [bound.bound_id for bound in self.bounds_confirmed]
        if set(ids) != set(EXPECTED_BOUND_IDS) or len(ids) != len(set(ids)):
            raise ValueError("bounds_confirmed must contain each of B1..B12 exactly once")
        if self.accept and any(not bound.ok for bound in self.bounds_confirmed):
            if not self.override_rationale.strip():
                raise ValueError("accepting a failing bound requires override_rationale")
        directive_bounds = [audit.bound_id for audit in self.directive_exception_audits]
        if len(directive_bounds) != len(set(directive_bounds)):
            raise ValueError("directive_exception_audits contains duplicate bound IDs")
        if self.directive_exception_audits and self.binding_user_directive_sha256 is None:
            raise ValueError("directive exception audits require binding_user_directive_sha256")
        if not self.accept:
            if not any(item.strip() for item in self.objections):
                raise ValueError("a rejection requires at least one specific objection")
            if not any(item.strip() for item in self.demanded_changes):
                raise ValueError("a rejection requires at least one demanded change")
            if not self.revision_constraints:
                raise ValueError("a rejection requires at least one revision constraint")
            if len(self.revision_allowed_failing_bounds) != len(
                set(self.revision_allowed_failing_bounds)
            ):
                raise ValueError("revision_allowed_failing_bounds contains duplicates")
            disallowed = set(self.revision_allowed_failing_bounds) - REVISION_OVERRIDABLE_BOUND_IDS
            if disallowed:
                raise ValueError(
                    "revision cannot allow safety/provenance bound failures: "
                    + ", ".join(sorted(disallowed))
                )
        elif (
            self.revision_constraints
            or self.revision_allowed_failing_bounds
            or self.revision_hedge_audit is not None
            or self.revision_fallback_seat_audits
        ):
            raise ValueError("an accepted book cannot carry revision-only constraints")
        return self


class CycleReport(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cycle: int
    achieved_deploy_frac: float
    achieved_dollar_residual_frac: float
    achieved_beta_residual: float  # achieved signed beta-dollars / execution-mark equity
    equity: float
    n_legs: int
    # friction + integrity visibility (2026-07-10 forensic review: 77.5% of losses were
    # fees+slippage that appeared in NO report; stated-vs-achieved divergence had no consumer)
    ran_at: str = ""  # real wall-clock of the reconcile (not evidence 'now')
    decision_ts: str = ""  # the evidence 'now' the agents decided on
    execution_ts: str = ""  # fresh mark+book snapshot used for paper fills
    decision_age_seconds: float = 0.0  # execution_ts - decision_ts; execution-staleness audit
    turnover_usd: float = 0.0  # |delta| notional actually traded this cycle
    fees_paid_cycle: float = 0.0
    slippage_paid_cycle: float = 0.0
    funding_settled_cycle: float = 0.0  # signed net funding credited this cycle
    stated_deploy_frac: float = 0.0  # PM's claims, recorded beside achieved for the delta
    stated_dollar_residual_frac: float = 0.0
    stated_beta_residual: float = 0.0
    specialist_failed: list[str] = Field(default_factory=list)
    unpriced_legs: list[str] = Field(default_factory=list)


ReflectorRole = Literal["sentiment", "technical", "futures", "pm", "adversary"]


class ReflectorEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: ReflectorRole
    region_text: str  # the FULL new managed-region body for this role
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    retire_if: str = ""


class ReflectionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[ReflectorEdit] = Field(default_factory=list)
    no_action_reason: str = ""
