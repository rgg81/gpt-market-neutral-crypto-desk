"""Structured-output contracts between the LLM agents and the deterministic driver."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Lean = Literal["long", "short", "flat"]
Side = Literal["long", "short"]
BoundId = Literal[
    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10", "B11", "B12"
]
EXPECTED_BOUND_IDS = tuple(f"B{i}" for i in range(1, 13))


class SpecialistRead(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    lean: Lean
    conviction: float = Field(ge=0.0, le=1.0)
    rationale: str
    evidence: list[str] = Field(default_factory=list)


class BookLeg(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    side: Side
    target_notional: float = Field(gt=0.0)
    rationale: str = ""
    is_new: bool = Field(default=False, description="True if this leg was NOT in current_book")
    hold_breaking_reason: str = Field(default="", description=
        "REQUIRED if is_new=True or side changed - which specific rule exception "
        "justifies this change")


class Book(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    legs: list[BookLeg] = Field(default_factory=list)
    stated_deploy_frac: float = 0.0
    stated_dollar_residual_frac: float = 0.0
    stated_beta_residual: float = 0.0
    turnover_legs_changed: int = Field(default=0, ge=0, description=
        "Number of legs that changed vs current_book (new+dropped+flipped)")
    turnover_justification: str = Field(default="", description=
        "REQUIRED if turnover_legs_changed > 2 - why this exception is necessary")
    notes: str = ""


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


class BoundVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bound_id: BoundId                   # B1..B12
    ok: bool                            # the Adversary's own pass/fail call on this bound
    note: str = ""                      # required reasoning when overriding a failing bound


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
    metrics_echo: MetricsEcho
    bounds_confirmed: list[BoundVerdict] = Field(min_length=12, max_length=12)
    citation_checks: list[CitationCheck] = Field(default_factory=list)
    override_rationale: str = ""
    objections: list[str] = Field(default_factory=list)
    demanded_changes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_decision_completeness(self) -> AdversaryVerdict:
        ids = [bound.bound_id for bound in self.bounds_confirmed]
        if set(ids) != set(EXPECTED_BOUND_IDS) or len(ids) != len(set(ids)):
            raise ValueError("bounds_confirmed must contain each of B1..B12 exactly once")
        if self.accept and any(not bound.ok for bound in self.bounds_confirmed):
            if not self.override_rationale.strip():
                raise ValueError("accepting a failing bound requires override_rationale")
        if not self.accept:
            if not any(item.strip() for item in self.objections):
                raise ValueError("a rejection requires at least one specific objection")
            if not any(item.strip() for item in self.demanded_changes):
                raise ValueError("a rejection requires at least one demanded change")
        return self


class CycleReport(BaseModel):
    cycle: int
    achieved_deploy_frac: float
    achieved_dollar_residual_frac: float
    achieved_beta_residual: float
    equity: float
    n_legs: int
    # friction + integrity visibility (2026-07-10 forensic review: 77.5% of losses were
    # fees+slippage that appeared in NO report; stated-vs-achieved divergence had no consumer)
    ran_at: str = ""                       # real wall-clock of the reconcile (not evidence 'now')
    decision_ts: str = ""                  # the evidence 'now' the agents decided on
    execution_ts: str = ""                 # fresh mark+book snapshot used for paper fills
    decision_age_seconds: float = 0.0       # execution_ts - decision_ts; execution-staleness audit
    turnover_usd: float = 0.0              # |delta| notional actually traded this cycle
    fees_paid_cycle: float = 0.0
    slippage_paid_cycle: float = 0.0
    funding_settled_cycle: float = 0.0     # signed net funding credited this cycle
    stated_deploy_frac: float = 0.0        # PM's claims, recorded beside achieved for the delta
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
