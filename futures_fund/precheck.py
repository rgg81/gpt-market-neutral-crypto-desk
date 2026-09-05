"""Deterministic PRE-TRADE precheck on a PROPOSED book (charter: code FEEDS data, never vetoes).

Computes the exact numbers the PM must state honestly and the Adversary must audit — gross,
deploy, dollar/beta residuals, per-leg concentration, hedge share, per-leg beta-dollars, and
turnover vs the currently-held book — plus a table of numeric bounds (B1-B12). The result is
handed to BOTH agents; the Adversary's verdict must echo it (see `AdversaryVerdict`), so a
content-free accept can no longer pass validation. This module makes NO decision: `bounds` are
data for the Adversary, and the Adversary may override any failing bound with a written
rationale. It exists because cycle 4 shipped a 5.98x-cash book whose PM stated deploy=1.069 and
whose adversary verdict was a 66-byte bare accept — nothing deterministic had computed a single
number on the proposed book before fills.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from futures_fund.desk_contracts import Book, ObjectiveHardBanViolation, SeatRole
from futures_fund.risk_context import position_correlation_context
from futures_fund.slippage import ExecutionRealism

# Bound limits (ratified from the 2026-07-10 forensic review; backtested 5/5 on cycles 1-5:
# every defective book fires, the two honest c5 constructions pass).
DEPLOY_MIN = 0.75  # B1 low
DEPLOY_MAX = 1.15  # B1 high (gross / cash)
DOLLAR_RESIDUAL_MAX = 0.10  # B2 |longs-shorts|/gross
BETA_RESIDUAL_MAX = 0.15  # B3 |net beta-$| / cash
MAX_LEG_FRAC_GROSS = 0.35  # B4 single-leg concentration
HEDGE_FRAC_CASH_MAX = 0.50  # B5 BTC hedge leg vs cash
LEG_BETA_USD_FRAC_MAX = 0.60  # B6 per-leg |notional x beta| vs cash (the c4 root-cause bound)
STATED_TOL = 0.05  # B7 |stated - computed| tolerance on each stated_* metric
MAX_LEGS_CHANGED = 2  # ordinary B9 aggressive-turnover cap
COLD_START_MAX_LEGS_CHANGED = 4  # exact-flat, all-non-BTC-alpha re-entry cap
EST_SLIPPAGE_BPS_MAX = 75.0  # B10 ceiling for held seats and loss-control actions
AGGRESSIVE_ALPHA_EST_SLIPPAGE_BPS_MAX = 50.0  # B10 fresh-entry-quality 2k screen
POST_CRASH_SHORT_MOMENTUM_PCT = -40.0
FADE_SHORT_MOMENTUM_PCT = 40.0
LOW_DEPTH_USD = 100_000.0
LOW_DEPTH_MAX_AGGRESSIVE_NOTIONAL = 1_500.0
NOTIONAL_NOOP_ABS_TOL = 0.01  # sub-cent target noise is a no-op; every executable resize is costed
MAX_PAYBACK_FUNDING_INTERVALS = 10.0  # B12 maximum normalized 8h events to repay friction
TAKER_FEE_BPS = 5.0  # matches FeeSettings.taker_bps
PRECHECK_SCHEMA_VERSION = 9

# Preserve the historical pure-function behavior for callers that do not provide an execution
# policy. Production callers pass ``settings.execution`` explicitly; the policy and every reserve
# are then content-addressed in current precheck artifacts.
_LEGACY_EXECUTION_REALISM = ExecutionRealism(
    latency_ms=0.0,
    displayed_depth_fraction=1.0,
    adverse_selection_bps=0.0,
    legging_bps_per_second=0.0,
    allow_partial_fills=False,
)


class PriorBookLineage(BaseModel):
    """Minimal manifest-proven prior Book state needed for restart continuity.

    The full prior Book is deliberately not an input to arithmetic. Production loaders first
    verify the newest completion manifest and exact raw ``book.json`` hash, then reduce it to this
    immutable provenance record. Historical Books parse as an ended/non-restart lineage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    cycle: int = Field(ge=1)
    book_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    controlled_restart_origin_cycle: int | None = Field(default=None, ge=1)
    controlled_restart_phase: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> PriorBookLineage:
        if self.controlled_restart_phase != (
            self.controlled_restart_origin_cycle is not None
        ):
            raise ValueError("prior controlled-restart phase/origin is inconsistent")
        return self


def load_prior_book_lineage(
    state_dir: str | Path,
    *,
    before_cycle: int,
    cadence: str = "rebal",
) -> PriorBookLineage | None:
    """Load only the newest prior completion and prove its exact Book through its manifest.

    A corrupt newest completion is an integrity failure, never permission to search backward for
    a more convenient lineage. A desk with no prior completed cycle has no lineage.
    """

    from futures_fund.durable_io import canonical_json_sha256
    from futures_fund.reconcile_commit import (  # local import keeps pure callers lightweight
        completed_artifact_sha256,
        cycle_is_complete,
    )

    root = Path(state_dir) / cadence / "cycle"
    candidates = (
        sorted(
            (
                int(path.name)
                for path in root.iterdir()
                if path.is_dir()
                and path.name.isdigit()
                and int(path.name) < before_cycle
                and (path / "complete.json").is_file()
            ),
            reverse=True,
        )
        if root.exists()
        else []
    )
    if not candidates:
        return None
    prior_cycle = candidates[0]
    if not cycle_is_complete(
        state_dir, prior_cycle, cadence=cadence, require_manifest=True
    ):
        raise ValueError(
            f"latest prior completion {prior_cycle} lacks an intact required manifest"
        )
    book_sha256 = completed_artifact_sha256(
        state_dir, prior_cycle, "book", cadence=cadence
    )
    if book_sha256 is None:
        raise ValueError(
            f"latest prior completion {prior_cycle} has no intact manifest-bound Book"
        )
    book_path = root / str(prior_cycle) / "book.json"
    try:
        raw_book = json.loads(book_path.read_text())
        if not isinstance(raw_book, dict):
            raise TypeError("book is not an object")
        if canonical_json_sha256(raw_book) != book_sha256:
            raise ValueError("book changed after manifest verification")
        prior_book = Book.model_validate(raw_book)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"latest prior manifest-bound Book {prior_cycle} is unreadable"
        ) from exc
    if prior_book.controlled_restart_phase:
        precheck_sha = completed_artifact_sha256(
            state_dir, prior_cycle, "precheck", cadence=cadence
        )
        if precheck_sha is None:
            raise ValueError(
                f"active prior restart {prior_cycle} lacks a manifest-bound precheck"
            )
        precheck_path = root / str(prior_cycle) / "precheck.json"
        try:
            raw_precheck = json.loads(precheck_path.read_text())
            if not isinstance(raw_precheck, dict):
                raise TypeError("precheck is not an object")
            if canonical_json_sha256(raw_precheck) != precheck_sha:
                raise ValueError("precheck changed after manifest verification")
            prior_precheck = PrecheckMetrics.model_validate(raw_precheck)
            if prior_precheck.schema_version < 7:
                raise ValueError("active restart precheck predates authenticated lineage")
            if prior_precheck.schema_version > PRECHECK_SCHEMA_VERSION:
                raise ValueError("active restart precheck uses an unsupported future schema")
            missing_fields = missing_required_precheck_fields(prior_precheck)
            if missing_fields:
                raise ValueError(
                    "active restart precheck omits required explicit provenance: "
                    + ", ".join(missing_fields)
                )
            if not hmac.compare_digest(
                prior_precheck.sha256, precheck_sha256(prior_precheck)
            ):
                raise ValueError("active restart precheck has an invalid internal hash")
            if (
                prior_precheck.cycle != prior_cycle
                or prior_precheck.controlled_restart_phase
                != prior_book.controlled_restart_phase
                or prior_precheck.controlled_restart_origin_cycle
                != prior_book.controlled_restart_origin_cycle
                or not prior_precheck.controlled_restart_lineage_valid
                or not (
                    prior_precheck.controlled_restart_initial_eligible
                    or prior_precheck.controlled_restart_continuation_eligible
                )
            ):
                raise ValueError(
                    "active restart Book does not match a valid initial/continuation precheck"
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"latest prior active-restart precheck {prior_cycle} is unauthenticated"
            ) from exc
    return PriorBookLineage(
        cycle=prior_cycle,
        book_sha256=book_sha256,
        controlled_restart_origin_cycle=prior_book.controlled_restart_origin_cycle,
        controlled_restart_phase=prior_book.controlled_restart_phase,
    )


def _slip_bps_for(
    ev_row: dict,
    notional: float,
    execution_side: Literal["buy", "sell"] | None = None,
    *,
    displayed_depth_fraction: float = 1.0,
) -> float:
    """SIZE-AWARE one-way slippage (bps) for a clip and actual crossing side.

    New evidence publishes BUY/ask and SELL/bid curves from one midpoint/L2 snapshot. Reads the
    requested directional curve at the smallest priced clip >= the notional. A present-but-empty
    directional curve means that crossing side did not visibly cover the clip and fails closed.
    Immutable legacy evidence without directional fields uses the conservative aggregate curve.
    """
    side_key = f"slippage_curve_{execution_side}_bps" if execution_side is not None else None
    directional_schema_present = any(
        key in ev_row for key in ("slippage_curve_buy_bps", "slippage_curve_sell_bps")
    )
    curve_key = (
        side_key if side_key is not None and directional_schema_present else "slippage_curve_bps"
    )
    curve_present = bool(
        curve_key in ev_row or (side_key is not None and directional_schema_present)
    )
    curve = ev_row.get(curve_key) or {}
    # Walking X dollars through a book whose quantity at every level is haircutted by f has the
    # same VWAP as walking X/f through the original snapshot. Evidence curves are measured on the
    # unhaircutted snapshot, so this lookup exactly mirrors the execution transform at curve tiers.
    curve_lookup_notional = notional / displayed_depth_fraction
    if curve:
        priced = sorted((int(k.rstrip("k")) * 1000, v) for k, v in curve.items())
        for size, bps in priced:
            if curve_lookup_notional <= size:
                return float(bps)
        # No measured depth cost exists beyond the widest clip. Flattening bps here understates a
        # convex book, especially for flips, so fail B12 closed instead of extrapolating fiction.
        return float("inf")
    # A fresh evidence row explicitly carrying an empty curve means the visible book could not
    # fully price even the smallest probe. Only immutable legacy evidence, where the curve key did
    # not yet exist, may use the old 2k scalar fallback.
    if curve_present:
        return float("inf")
    return float(ev_row.get("est_slippage_bps_2k") or 0.0)


def _liquidity_clip_usd(ev_row: dict, decision_notional: float) -> float:
    """Value a decision-anchored quantity at the L2 curve's own midpoint.

    PM notionals are denominated at ``mark`` and reconcile freezes
    ``qty = decision_notional / mark``.  The depth curves, however, are keyed by USD clips at
    ``liquidity_mid``.  Looking up the raw decision notional can therefore select the wrong
    convex depth tier and also misstate fee/slippage dollars whenever those prices differ.

    Fresh directional evidence must carry a positive same-snapshot midpoint and fails closed if
    it does not.  Immutable aggregate-only evidence predates ``liquidity_mid``; retain its exact
    historical interpretation rather than retroactively inventing a midpoint.
    """
    if decision_notional <= 0.0:
        return 0.0
    directional_schema_present = any(
        key in ev_row for key in ("slippage_curve_buy_bps", "slippage_curve_sell_bps")
    )
    mark = float(ev_row.get("mark") or 0.0)
    liquidity_mid = float(ev_row.get("liquidity_mid") or 0.0)
    if math.isfinite(mark) and mark > 0.0 and math.isfinite(liquidity_mid) and liquidity_mid > 0.0:
        return decision_notional / mark * liquidity_mid
    if directional_schema_present:
        return float("inf")
    return decision_notional


def _entry_execution_side(position_side: str) -> Literal["buy", "sell"]:
    """Crossing side that opens/increases `position_side`."""
    return "buy" if position_side == "long" else "sell"


def _exit_execution_side(position_side: str) -> Literal["buy", "sell"]:
    """Crossing side that reduces/closes `position_side`."""
    return "sell" if position_side == "long" else "buy"


def _one_way_friction_usd(
    ev_row: dict,
    decision_notional: float,
    execution_side: Literal["buy", "sell"],
    *,
    execution_realism: ExecutionRealism = _LEGACY_EXECUTION_REALISM,
    legging_reserve_bps: float = 0.0,
) -> tuple[float, float, float, float | None]:
    """Return conservative friction, clips, and visible full-fill fraction.

    The evidence curve is denominated on the unhaircutted L2 snapshot. The lookup clip scales by
    ``1 / displayed_depth_fraction`` while fee and reserve dollars remain based on the requested
    executable clip. A directional depth total, when present, also exposes whether reconciliation
    should expect a partial fill under the exact same displayed-depth haircut.
    """
    executable_clip = _liquidity_clip_usd(ev_row, decision_notional)
    curve_lookup_clip = executable_clip / execution_realism.displayed_depth_fraction
    slip_bps = _slip_bps_for(
        ev_row,
        executable_clip,
        execution_side,
        displayed_depth_fraction=execution_realism.displayed_depth_fraction,
    )
    reserve_bps = execution_realism.adverse_selection_bps + legging_reserve_bps
    friction = (slip_bps + TAKER_FEE_BPS + reserve_bps) / 1e4 * executable_clip
    depth_key = "depth_usd_ask" if execution_side == "buy" else "depth_usd_bid"
    fill_fraction: float | None = None
    if depth_key in ev_row and executable_clip > 0.0:
        visible_depth = max(float(ev_row.get(depth_key) or 0.0), 0.0)
        effective_depth = visible_depth * execution_realism.displayed_depth_fraction
        fill_fraction = min(effective_depth / executable_clip, 1.0)
    return friction, executable_clip, curve_lookup_clip, fill_fraction


def _forecast_payback_intervals(
    friction: float,
    price_edge_usd: float,
    carry_usd_per_8h: float,
    horizon_intervals: float,
) -> float:
    """First break-even time without extrapolating a one-time price forecast past its horizon."""
    if not math.isfinite(friction) or friction < 0.0 or horizon_intervals <= 0.0:
        return float("inf")
    within_horizon_rate = carry_usd_per_8h + price_edge_usd / horizon_intervals
    if within_horizon_rate > 1e-9:
        within_horizon_payback = friction / within_horizon_rate
        if within_horizon_payback <= horizon_intervals:
            return within_horizon_payback
    # Beyond the declared horizon, the price forecast is exhausted. Carry alone may continue.
    if carry_usd_per_8h > 1e-9:
        after_horizon_payback = (friction - price_edge_usd) / carry_usd_per_8h
        if after_horizon_payback >= horizon_intervals:
            return after_horizon_payback
    return float("inf")


def _required_price_edge_frac_for_max_payback(
    friction: float,
    carry_usd_per_8h: float,
    horizon_intervals: float,
    clip: float,
) -> float | None:
    """Price move required to break even by B12 without extending adverse carry.

    A forecast is available only through its declared horizon.  Positive carry may keep earning
    through the ten-interval B12 window, but zero/adverse carry beyond the forecast horizon cannot
    be charged against a price thesis that has already expired.
    """
    if not math.isfinite(friction) or friction < 0.0 or horizon_intervals <= 0.0 or clip <= 0.0:
        return None
    forecast_fraction_available = min(MAX_PAYBACK_FUNDING_INTERVALS / horizon_intervals, 1.0)
    carry_intervals = (
        MAX_PAYBACK_FUNDING_INTERVALS
        if carry_usd_per_8h > 0.0 or MAX_PAYBACK_FUNDING_INTERVALS <= horizon_intervals
        else horizon_intervals
    )
    required_price_edge_usd = (
        max(friction - carry_usd_per_8h * carry_intervals, 0.0) / forecast_fraction_available
    )
    return required_price_edge_usd / clip


class LegMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    side: str
    seat_role: SeatRole = "alpha"
    notional: float
    beta: float
    beta_usd: float  # signed: +long, -short
    frac_gross: float
    change: str  # held | new | flipped | resized
    turnover_notional: float = 0.0  # executable |delta|; flip includes close + open
    material_effect: str = "none"  # none | entry | flip | increase | reduction
    expected_price_edge_frac: float = 0.0
    edge_horizon_hours: int = 24
    expected_price_edge_usd: float = 0.0
    expected_price_edge_usd_per_8h: float = 0.0
    market_conservative_funding_8h_bps: float = 0.0
    selected_side_carry_bps_8h: float = 0.0
    expected_carry_usd_per_8h: float = 0.0
    expected_total_edge_usd_per_8h: float = 0.0
    changed_slice_friction_usd: float | None = None
    changed_slice_expected_total_edge_usd_per_8h: float = 0.0
    changed_slice_expected_edge_through_horizon_usd: float | None = None
    # Explicit v2 name; the preceding field remains a compatibility alias with the same value.
    changed_slice_expected_edge_through_horizon_pre_friction_usd: float | None = None
    changed_slice_net_edge_through_horizon_after_friction_usd: float | None = None
    zero_price_edge_payback_intervals: float | None = None
    required_price_edge_frac_for_max_payback: float | None = None
    residual_vol_annualized: float | None = None
    standalone_vol_usd: float | None = None
    standalone_risk_share: float | None = None
    variance_contribution_frac: float | None = None


class ChangeCostMetric(BaseModel):
    """Strict, per-economic-change execution-cost and payback observability."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    symbol: str
    action: Literal["entry", "flip", "increase", "reduction", "drop", "role_change"]
    seat_role: SeatRole
    prior_side: str | None = None
    final_side: str | None = None
    prior_seat_role: SeatRole | None = None
    final_seat_role: SeatRole | None = None
    # ``decision_turnover_usd`` is the PM delta at the evidence mark.  Executable turnover is the
    # exact fixed quantity valued at the same L2 midpoint that denominates the selected curve.
    decision_turnover_usd: float = 0.0
    executable_turnover_usd: float | None
    future_exit_executable_notional_usd: float | None = None
    conservative_curve_lookup_turnover_usd: float | None = None
    conservative_future_exit_curve_lookup_usd: float | None = None
    estimated_min_fill_fraction: float | None = None
    partial_fill_expected: bool = False
    liquidity_mid: float | None = None
    friction_usd: float | None = None
    friction_priced: bool
    adverse_selection_reserve_bps: float = 0.0
    legging_reserve_bps: float = 0.0
    b12_insurance_exempt: bool = False
    expected_edge_through_horizon_pre_friction_usd: float | None = None
    expected_net_edge_through_horizon_after_friction_usd: float | None = None
    payback_intervals: float = 9999.0
    payback_evaluated: bool = True


class BoundCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    bound_id: str  # B1..B12
    description: str
    value: float
    limit: str
    ok: bool


class PrecheckMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: int = Field(default=1, ge=1)
    cycle: int
    cash: float
    gross: float
    deploy_frac: float
    longs_usd: float
    shorts_usd: float
    alpha_gross: float = 0.0
    alpha_deploy_frac: float = 0.0
    alpha_longs_usd: float = 0.0
    alpha_shorts_usd: float = 0.0
    hedge_gross: float = 0.0
    hedge_deploy_frac: float = 0.0
    dollar_residual_frac: float
    beta_net_usd: float
    beta_residual: float  # beta_net_usd / cash
    max_leg_symbol: str = ""
    max_leg_frac_gross: float = 0.0
    hedge_notional: float = 0.0  # the BTC leg, if any
    hedge_frac_cash: float = 0.0
    alpha_beta_net_usd_before_hedge: float = 0.0
    hedge_beta_usd: float = 0.0
    hedge_beta_reduction_frac: float = 0.0
    hedge_counterfactual_beta_net_usd: float = 0.0
    hedge_change_risk_reducing: bool = False
    max_leg_beta_usd_symbol: str = ""
    max_leg_beta_usd: float = 0.0  # max per-leg |notional x beta|
    legs: list[LegMetric] = Field(default_factory=list)
    legs_added: list[str] = Field(default_factory=list)
    legs_dropped: list[str] = Field(default_factory=list)
    legs_flipped: list[str] = Field(default_factory=list)
    legs_resized: list[str] = Field(default_factory=list)
    turnover_legs_changed: int = 0  # added + dropped + flipped + executable resizes
    turnover_aggressive_legs_changed: int = 0  # added + flipped + same-side increases
    cold_start_reentry_eligible: bool = False
    b9_aggressive_change_limit: Literal[2, 4] = MAX_LEGS_CHANGED
    controlled_restart_origin_cycle: int | None = Field(default=None, ge=1)
    controlled_restart_phase: bool = False
    controlled_restart_initial_eligible: bool = False
    controlled_restart_continuation_eligible: bool = False
    controlled_restart_lineage_valid: bool = True
    controlled_restart_prior_cycle: int | None = Field(default=None, ge=1)
    controlled_restart_prior_book_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    controlled_restart_prior_origin_cycle: int | None = Field(default=None, ge=1)
    controlled_restart_prior_phase: bool = False
    binding_user_directive_present: bool = False
    binding_user_directive_controlled_restart_graduation: bool = False
    turnover_risk_reductions: int = 0  # dropped + same-side decreases
    turnover_usd: float = 0.0  # sum |proposed - held| notional deltas
    turnover_claim_errors: list[str] = Field(default_factory=list)
    unpriced_symbols: list[str] = Field(default_factory=list)
    duplicate_symbols: list[str] = Field(default_factory=list)
    hard_ban_violations: list[ObjectiveHardBanViolation] = Field(default_factory=list)
    # Legacy field name; the value is measured in normalized 8h funding intervals.
    worst_changed_leg_payback_cycles: float = 0.0
    worst_changed_leg_payback_symbol: str = ""
    change_costs: list[ChangeCostMetric] = Field(default_factory=list)
    total_action_friction_usd: float | None = None
    total_action_friction_fully_priced: bool = True
    total_aggressive_expected_edge_through_horizon_pre_friction_usd: float | None = None
    total_aggressive_expected_net_edge_through_horizon_after_friction_usd: float | None = None
    execution_policy_applied: bool = False
    execution_latency_ms: float = 0.0
    execution_displayed_depth_fraction: float = 1.0
    execution_adverse_selection_bps: float = 0.0
    execution_legging_bps_per_second: float = 0.0
    execution_allow_partial_fills: bool = False
    pretrade_legging_reserve_bps: float = 0.0
    partial_fill_risk_symbols: list[str] = Field(default_factory=list)
    hedge_risk_reducing: bool = False  # BTC leg lowers |beta-$| versus the non-BTC book
    input_meta_sha256: str = ""
    risk_model_available: bool = False
    risk_model_unavailable_reason: str | None = None
    portfolio_residual_vol_annualized_usd: float = 0.0
    portfolio_residual_vol_annualized_frac_cash: float = 0.0
    portfolio_expected_price_edge_usd_per_8h: float = 0.0
    portfolio_expected_carry_usd_per_8h: float = 0.0
    portfolio_expected_total_edge_usd_per_8h: float = 0.0
    max_alpha_standalone_risk_symbol: str = ""
    max_alpha_standalone_risk_share: float = 0.0
    long_short_standalone_risk_ratio: float | None = None
    held_high_correlation_pairs: list[dict] = Field(default_factory=list)
    same_side_high_correlation_clusters: list[dict] = Field(default_factory=list)
    max_same_side_high_correlation_cluster_risk_share: float = 0.0
    position_co_risk_clusters: list[dict] = Field(default_factory=list)
    max_position_co_risk_cluster_risk_share: float = 0.0
    bounds: list[BoundCheck] = Field(default_factory=list)
    sha256: str = ""


_REQUIRED_EXPLICIT_PRECHECK_FIELDS_BY_SCHEMA: tuple[tuple[int, frozenset[str]], ...] = (
    (
        7,
        frozenset(
            {
                "controlled_restart_origin_cycle",
                "controlled_restart_phase",
                "controlled_restart_initial_eligible",
                "controlled_restart_continuation_eligible",
                "controlled_restart_lineage_valid",
                "controlled_restart_prior_cycle",
                "controlled_restart_prior_book_sha256",
                "controlled_restart_prior_origin_cycle",
                "controlled_restart_prior_phase",
            }
        ),
    ),
    (8, frozenset({"binding_user_directive_present"})),
    (9, frozenset({"binding_user_directive_controlled_restart_graduation"})),
)


def missing_required_precheck_fields(metrics: PrecheckMetrics) -> tuple[str, ...]:
    """Return provenance fields omitted by an artifact claiming their schema version.

    This is deliberately separate from :func:`precheck_sha256`: historical schema-v1..v8 hashes
    remain a digest of their original raw field set, while each schema is still required to carry
    the provenance fields introduced by that schema.
    """

    required = set()
    for minimum_schema, fields in _REQUIRED_EXPLICIT_PRECHECK_FIELDS_BY_SCHEMA:
        if metrics.schema_version >= minimum_schema:
            required.update(fields)
    return tuple(sorted(required - metrics.model_fields_set))


def precheck_sha256(metrics: PrecheckMetrics) -> str:
    """Return the canonical content hash for a precheck, excluding its hash field."""
    # For historical schema-v1..v8 artifacts, recursively preserve exactly the keys that were
    # present in the immutable JSON. Pydantic supplies defaults for every field added later;
    # hashing those defaults would make an old content-addressed artifact change retroactively.
    payload = metrics.model_dump(mode="json", exclude_unset=metrics.schema_version < 9)
    payload.pop("sha256", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# Backward-compatible private alias for callers/tests written before the integrity verifier.
_sha = precheck_sha256


def compute_precheck(
    book: Book,
    evidence: list[dict],
    *,
    cash: float,
    cycle: int,
    current_book: list[dict] | None = None,
    btc_symbol: str = "BTC/USDT:USDT",
    risk_model: dict | None = None,
    meta_sha256: str = "",
    execution_realism: ExecutionRealism | None = None,
    prior_book_lineage: PriorBookLineage | None = None,
    binding_user_directive_present: bool = False,
    binding_user_directive_controlled_restart_graduation: bool = False,
) -> PrecheckMetrics:
    """Compute every auditable number on the PROPOSED book. Pure function of its inputs."""
    if prior_book_lineage is not None and prior_book_lineage.cycle >= cycle:
        raise ValueError("prior manifest-bound Book must precede the current cycle")
    if (
        binding_user_directive_controlled_restart_graduation
        and not binding_user_directive_present
    ):
        raise ValueError("controlled-restart graduation capability requires a bound directive")
    execution_policy_applied = execution_realism is not None
    execution_policy = execution_realism or _LEGACY_EXECUTION_REALISM
    marks = {e["symbol"]: float(e["mark"]) for e in evidence}
    betas = {e["symbol"]: float(e.get("beta_clamped", e.get("beta_btc", 1.0))) for e in evidence}
    slip_est = {e["symbol"]: e.get("est_slippage_bps_2k") for e in evidence}
    ev_by_sym = {e["symbol"]: e for e in evidence}
    # Carry economics use the conservative, history-qualified funding estimate.  The legacy key
    # remains a compatibility alias in evidence, but a last-settled rate is never promoted to a
    # forward expectation here.
    fund_bps = {
        e["symbol"]: float(
            e.get("conservative_funding_8h_bps")
            if e.get("conservative_funding_8h_bps") is not None
            else e.get("expected_funding_8h_bps") or 0.0
        )
        for e in evidence
    }

    seen: set[str] = set()
    duplicates: list[str] = []
    for lg in book.legs:
        if lg.symbol in seen:
            duplicates.append(lg.symbol)
        seen.add(lg.symbol)
    unpriced = sorted({lg.symbol for lg in book.legs if lg.symbol not in marks})

    # The cold-start allowance is fail-closed: ``None`` is unknown inventory, not proof of a flat
    # book. Production supplies an explicit list from the paper account, including any dust row.
    predecision_inventory_empty = current_book is not None and len(current_book) == 0
    held = {c["symbol"]: c for c in (current_book or [])}
    longs = sum(lg.target_notional for lg in book.legs if lg.side == "long")
    shorts = sum(lg.target_notional for lg in book.legs if lg.side == "short")
    gross = longs + shorts
    alpha_book_legs = [
        lg for lg in book.legs if not (lg.symbol == btc_symbol and lg.seat_role == "hedge")
    ]
    alpha_longs = sum(lg.target_notional for lg in alpha_book_legs if lg.side == "long")
    alpha_shorts = sum(lg.target_notional for lg in alpha_book_legs if lg.side == "short")
    alpha_gross = alpha_longs + alpha_shorts
    hedge_gross = gross - alpha_gross

    legs: list[LegMetric] = []
    beta_net = 0.0
    max_leg_sym, max_leg_frac = "", 0.0
    max_beta_sym, max_beta_usd = "", 0.0
    hedge_notional = 0.0
    added, flipped, resized = [], [], []
    increased, reduced = [], []
    alpha_role_entries: list[str] = []
    role_changes: set[str] = set()
    turnover_claim_errors: list[str] = []
    turnover_usd = 0.0
    for lg in book.legs:
        beta = betas.get(lg.symbol, 1.0)
        sign = 1.0 if lg.side == "long" else -1.0
        beta_usd = sign * lg.target_notional * beta
        beta_net += beta_usd
        frac = (lg.target_notional / gross) if gross > 0 else 0.0
        if frac > max_leg_frac:
            max_leg_sym, max_leg_frac = lg.symbol, frac
        if abs(beta_usd) > abs(max_beta_usd):
            max_beta_sym, max_beta_usd = lg.symbol, abs(beta_usd)
        # Legacy B5 caps any BTC seat because historical books predate seat_role. Alpha/hedge
        # economics below still exclude only an explicitly typed risk-reducing hedge.
        if lg.symbol == btc_symbol:
            hedge_notional = lg.target_notional
        prior = held.get(lg.symbol)
        prior_role: SeatRole = prior.get("seat_role", "alpha") if prior is not None else "alpha"
        if prior is None:
            change = "new"
            material_effect = "entry"
            added.append(lg.symbol)
            trade_notional = lg.target_notional
            turnover_usd += trade_notional
        elif prior["side"] != lg.side:
            change = "flipped"
            material_effect = "flip"
            flipped.append(lg.symbol)
            trade_notional = float(prior["target_notional"]) + lg.target_notional
            turnover_usd += trade_notional
        else:
            delta = abs(lg.target_notional - float(prior["target_notional"]))
            trade_notional = delta
            turnover_usd += delta
            role_changed = prior_role != lg.seat_role
            if role_changed:
                role_changes.add(lg.symbol)
            if not math.isclose(delta, 0.0, rel_tol=0.0, abs_tol=NOTIONAL_NOOP_ABS_TOL):
                change = "resized"
                resized.append(lg.symbol)
                if lg.target_notional > float(prior["target_notional"]):
                    material_effect = "increase"
                    increased.append(lg.symbol)
                else:
                    material_effect = "reduction"
                    reduced.append(lg.symbol)
            else:
                change = "role_changed" if role_changed else "held"
                material_effect = "none"
            # Reclassifying held hedge inventory as an alpha thesis consumes a fresh alpha seat
            # and B9 action even if no order is executable.  Keep trade_notional at the truthful
            # quantity delta (zero for a role-only transition).
            if role_changed and prior_role == "hedge" and lg.seat_role == "alpha":
                material_effect = "entry"
                alpha_role_entries.append(lg.symbol)
        expected_is_new = change in ("new", "flipped")
        if lg.is_new is not expected_is_new:
            turnover_claim_errors.append(
                f"{lg.symbol}: is_new={lg.is_new}, expected={expected_is_new}"
            )
        if expected_is_new and not lg.hold_breaking_reason.strip():
            turnover_claim_errors.append(f"{lg.symbol}: new/flipped leg lacks hold_breaking_reason")
        legs.append(
            LegMetric(
                symbol=lg.symbol,
                side=lg.side,
                seat_role=lg.seat_role,
                notional=lg.target_notional,
                beta=beta,
                beta_usd=beta_usd,
                frac_gross=frac,
                change=change,
                turnover_notional=trade_notional,
                material_effect=material_effect,
                expected_price_edge_frac=lg.expected_price_edge_frac,
                edge_horizon_hours=lg.edge_horizon_hours,
            )
        )
    proposed_syms = {lg.symbol for lg in book.legs}
    dropped = sorted(s for s in held if s not in proposed_syms)
    turnover_usd += sum(float(held[s]["target_notional"]) for s in dropped)
    changed_symbols = set(added) | set(flipped) | set(resized) | set(dropped) | role_changes
    # Both B11 and B12 fail closed if an executable or semantic change cannot be tied to evidence.
    unpriced = sorted(set(unpriced) | {s for s in changed_symbols if s not in ev_by_sym})

    # Publish full-book forward economics separately from changed-slice break-even.  These remain
    # PM forecasts plus descriptive carry arithmetic, never a deterministic sizing signal.  The
    # Adversary sees both the point forecast and the residual-risk packet and owns the judgment.
    portfolio_price_edge_per_8h = 0.0
    portfolio_carry_per_8h = 0.0
    for lm in legs:
        market_funding_bps = fund_bps.get(lm.symbol, 0.0)
        selected_carry_bps = market_funding_bps if lm.side == "short" else -market_funding_bps
        horizon_intervals = max(lm.edge_horizon_hours / 8.0, 1.0)
        lm.market_conservative_funding_8h_bps = market_funding_bps
        lm.selected_side_carry_bps_8h = selected_carry_bps
        lm.expected_carry_usd_per_8h = selected_carry_bps / 1e4 * lm.notional
        lm.expected_price_edge_usd = lm.expected_price_edge_frac * lm.notional
        lm.expected_price_edge_usd_per_8h = lm.expected_price_edge_usd / horizon_intervals
        # A typed hedge has no alpha forecast in portfolio expected edge.  Its actual carry cost
        # remains included because neutrality is not free.
        lm.expected_total_edge_usd_per_8h = lm.expected_carry_usd_per_8h
        if lm.seat_role == "alpha":
            portfolio_price_edge_per_8h += lm.expected_price_edge_usd_per_8h
            lm.expected_total_edge_usd_per_8h += lm.expected_price_edge_usd_per_8h
        portfolio_carry_per_8h += lm.expected_carry_usd_per_8h

    deploy = (gross / cash) if cash > 0 else 0.0
    dollar_resid = (abs(longs - shorts) / gross) if gross > 0 else 0.0
    beta_resid = (beta_net / cash) if cash > 0 else 0.0
    hedge_frac = (hedge_notional / cash) if cash > 0 else 0.0
    n_changed = len(added) + len(dropped) + len(flipped) + len(resized)
    n_aggressive = len(set(added) | set(flipped) | set(increased) | set(alpha_role_entries))
    n_risk_reductions = len(dropped) + len(reduced)
    # Reconciliation observes symbols sequentially. Their realized elapsed time cannot be known at
    # decision time, so reserve one configured latency interval for each possible gap through the
    # last executable change. This is deliberately a pre-trade reserve, not fabricated slippage.
    pretrade_legging_reserve_bps = (
        execution_policy.legging_bps_per_second
        * (execution_policy.latency_ms / 1000.0)
        * max(n_changed - 1, 0)
    )
    beta_without_hedge = sum(
        lm.beta_usd for lm in legs if not (lm.symbol == btc_symbol and lm.seat_role == "hedge")
    )
    hedge_beta_usd = beta_net - beta_without_hedge
    hedge_risk_reducing = any(
        lm.symbol == btc_symbol and lm.seat_role == "hedge" for lm in legs
    ) and abs(beta_net) < abs(beta_without_hedge)
    hedge_beta_reduction = (
        (abs(beta_without_hedge) - abs(beta_net)) / abs(beta_without_hedge)
        if hedge_risk_reducing and abs(beta_without_hedge) > 0.0
        else 0.0
    )
    proposed_hedge = next(
        (lm for lm in legs if lm.symbol == btc_symbol and lm.seat_role == "hedge"),
        None,
    )
    prior_btc = held.get(btc_symbol)
    prior_btc_beta_usd = 0.0
    if prior_btc is not None:
        prior_btc_beta_usd = (
            (1.0 if prior_btc["side"] == "long" else -1.0)
            * float(prior_btc["target_notional"])
            * betas.get(btc_symbol, 1.0)
        )
    hedge_counterfactual_beta_net_usd = beta_without_hedge + prior_btc_beta_usd
    hedge_exposure_unchanged = bool(
        proposed_hedge is not None
        and prior_btc is not None
        and prior_btc["side"] == proposed_hedge.side
        and math.isclose(
            float(prior_btc["target_notional"]),
            proposed_hedge.notional,
            rel_tol=0.0,
            abs_tol=NOTIONAL_NOOP_ABS_TOL,
        )
    )
    # A changed hedge earns the insurance exemption only when it improves absolute beta versus
    # carrying the held BTC exposure into the same proposed non-BTC alpha book. Comparing only
    # with "no hedge" lets a shrink/overshoot worsen a perfectly hedged incumbent while still
    # appearing risk-reducing. An unchanged BTC exposure (including an alpha->hedge lifecycle
    # relabel) is an economic no-op, but qualifies only when it actually reduces final-book beta.
    hedge_change_risk_reducing = bool(
        proposed_hedge is not None
        and (
            (hedge_exposure_unchanged and hedge_risk_reducing)
            or abs(beta_net) + 1e-9 < abs(hedge_counterfactual_beta_net_usd)
        )
    )
    hedge_drop_risk_reducing = bool(
        btc_symbol in dropped
        and prior_btc is not None
        and prior_btc.get("seat_role", "alpha") == "hedge"
        and abs(beta_net) + 1e-9 < abs(hedge_counterfactual_beta_net_usd)
    )

    # Only an explicitly-labelled BTC hedge is excluded. A directional BTC alpha leg cannot be
    # silently treated as riskless; because the residual model has no BTC alpha series, it makes
    # the risk packet unavailable and forces the agents to confront the missing measurement.
    alpha_legs = [lm for lm in legs if not (lm.symbol == btc_symbol and lm.seat_role == "hedge")]
    risk_available = bool(risk_model is not None and (risk_model or {}).get("available", True))
    risk_unavailable_reason = (
        None
        if risk_available
        else str((risk_model or {}).get("unavailable_reason") or "risk model unavailable")
    )
    covariance = (
        (risk_model or {}).get("covariance_ewma_shrunk_annualized")
        or (risk_model or {}).get("covariance_annualized")
        or {}
    )
    vols = (
        (risk_model or {}).get("residual_vol_ewma_shrunk_annualized")
        or (risk_model or {}).get("residual_vol_annualized")
        or {}
    )
    signed_exposure = {
        lm.symbol: (lm.notional if lm.side == "long" else -lm.notional) for lm in alpha_legs
    }
    if risk_available:
        for left in signed_exposure:
            if vols.get(left) is None or left not in covariance:
                risk_available = False
                risk_unavailable_reason = f"missing residual volatility/covariance for {left}"
                break
            if any(covariance[left].get(right) is None for right in signed_exposure):
                risk_available = False
                risk_unavailable_reason = f"missing covariance pair for {left}"
                break
    portfolio_risk_usd = 0.0
    max_risk_symbol = ""
    max_risk_share = 0.0
    risk_ratio = None
    held_high_pairs: list[dict] = []
    same_side_clusters: list[dict] = []
    max_same_side_cluster_risk_share = 0.0
    position_co_risk_clusters: list[dict] = []
    max_position_co_risk_cluster_risk_share = 0.0
    if risk_available:
        covariance_vector = {
            left: sum(
                signed_exposure[right] * float(covariance[left][right]) for right in signed_exposure
            )
            for left in signed_exposure
        }
        variance = sum(signed_exposure[left] * covariance_vector[left] for left in signed_exposure)
        if not math.isfinite(variance) or variance < -1e-8:
            risk_available = False
            risk_unavailable_reason = "proposed-book covariance produced negative variance"
        else:
            variance = max(variance, 0.0)
            portfolio_risk_usd = math.sqrt(variance)
        standalone = {lm.symbol: lm.notional * float(vols[lm.symbol]) for lm in alpha_legs}
        standalone_total = sum(standalone.values())
        if standalone:
            max_risk_symbol = max(standalone, key=standalone.get)
            max_risk_share = (
                standalone[max_risk_symbol] / standalone_total if standalone_total > 0.0 else 0.0
            )
        long_risk = sum(standalone[lm.symbol] for lm in alpha_legs if lm.side == "long")
        short_risk = sum(standalone[lm.symbol] for lm in alpha_legs if lm.side == "short")
        risk_ratio = long_risk / short_risk if short_risk > 0.0 else None
        side_by_symbol = {lm.symbol: lm.side for lm in alpha_legs}
        if risk_available:
            by_symbol_metric = {lm.symbol: lm for lm in alpha_legs}
            variance_contribution_by_symbol: dict[str, float] = {}
            for symbol, standalone_value in standalone.items():
                leg_metric = by_symbol_metric[symbol]
                leg_metric.residual_vol_annualized = float(vols[symbol])
                leg_metric.standalone_vol_usd = standalone_value
                leg_metric.standalone_risk_share = (
                    standalone_value / standalone_total if standalone_total > 0.0 else 0.0
                )
                contribution = signed_exposure[symbol] * covariance_vector[symbol]
                leg_metric.variance_contribution_frac = (
                    contribution / variance if variance > 0.0 else 0.0
                )
                variance_contribution_by_symbol[symbol] = float(
                    leg_metric.variance_contribution_frac or 0.0
                )
            correlation_context = position_correlation_context(
                (risk_model or {}).get("high_correlation_pairs", []),
                side_by_symbol=side_by_symbol,
                standalone_risk=standalone,
                variance_contribution_frac=variance_contribution_by_symbol,
            )
            held_high_pairs = correlation_context["held_high_correlation_pairs"]
            same_side_clusters = correlation_context["same_side_high_correlation_clusters"]
            max_same_side_cluster_risk_share = correlation_context[
                "max_same_side_high_correlation_cluster_risk_share"
            ]
            position_co_risk_clusters = correlation_context["position_co_risk_clusters"]
            max_position_co_risk_cluster_risk_share = correlation_context[
                "max_position_co_risk_cluster_risk_share"
            ]

    # Four fresh entries are permitted only for an explicitly flat paper account, an all-alpha
    # non-BTC proposal, and a complete proposed-book residual covariance packet. A missing or
    # invalid risk model is unknown risk (not measured zero) and retains the ordinary B9 cap.
    cold_start_reentry_eligible = bool(
        predecision_inventory_empty
        and book.legs
        and all(lg.seat_role == "alpha" and lg.symbol != btc_symbol for lg in book.legs)
        and risk_available
    )
    b9_aggressive_change_limit = (
        COLD_START_MAX_LEGS_CHANGED if cold_start_reentry_eligible else MAX_LEGS_CHANGED
    )

    # Controlled restart is a GPT-authored lifecycle assertion with deterministic provenance.
    # These facts do not create a new bound or accept/reject a Book. They let the Adversary decide
    # whether a B1 starter-risk override is genuinely initial, a valid continuation, or an
    # invented/changed/reactivated lineage. An active lineage ends only with an explicitly
    # false/null, fully flat proposed Book, so clearing metadata cannot launder a risk expansion.
    prior_restart_active = bool(
        prior_book_lineage is not None and prior_book_lineage.controlled_restart_phase
    )
    fully_flat_explicit_end = bool(
        not book.legs
        and not book.controlled_restart_phase
        and book.controlled_restart_origin_cycle is None
    )
    controlled_restart_initial_eligible = bool(
        book.controlled_restart_phase
        and book.controlled_restart_origin_cycle == cycle
        and cold_start_reentry_eligible
        and not binding_user_directive_present
        and not prior_restart_active
    )
    controlled_restart_continuation_eligible = bool(
        book.controlled_restart_phase
        and book.legs
        and current_book is not None
        and len(current_book) > 0
        and prior_restart_active
        and prior_book_lineage is not None
        and book.controlled_restart_origin_cycle
        == prior_book_lineage.controlled_restart_origin_cycle
    )
    if prior_restart_active:
        # An authenticated active lineage cannot be replaced from an empty account. With live
        # inventory it may continue exactly; from either state it may end only explicitly flat.
        if predecision_inventory_empty:
            controlled_restart_lineage_valid = fully_flat_explicit_end
        elif current_book is not None and len(current_book) > 0:
            controlled_restart_lineage_valid = bool(
                controlled_restart_continuation_eligible or fully_flat_explicit_end
            )
        else:
            controlled_restart_lineage_valid = fully_flat_explicit_end
    elif predecision_inventory_empty and binding_user_directive_present:
        # The distinct directive cold-start path may carry a non-empty 98--102% Book, but it is
        # ordinary false/null lineage and can never masquerade as a controlled seed.
        controlled_restart_lineage_valid = bool(
            not book.controlled_restart_phase
            and book.controlled_restart_origin_cycle is None
        )
    elif predecision_inventory_empty:
        controlled_restart_lineage_valid = bool(
            controlled_restart_initial_eligible or fully_flat_explicit_end
        )
    else:
        # Ordinary non-empty (or unknown) inventory with no active prior must remain false/null.
        controlled_restart_lineage_valid = bool(
            not book.controlled_restart_phase
            and book.controlled_restart_origin_cycle is None
        )

    # B10 has two deliberately different scopes. Every priced selected seat (and a dropped seat
    # whose exit still crosses the book) remains subject to the absolute 75bp ceiling. An
    # aggressive alpha mutation must additionally satisfy the tighter fresh-entry-quality 50bp
    # evidence screen. ``material_effect`` includes hedge->alpha semantic entry, even when that
    # role transfer has no executable quantity delta.
    worst_slip = 0.0
    slip_known = False
    for symbol in proposed_syms | set(dropped):
        est = slip_est.get(symbol)
        if est is not None:
            slip_known = True
            worst_slip = max(worst_slip, float(est))
    aggressive_alpha_symbols = {
        lm.symbol
        for lm in legs
        if lm.seat_role == "alpha" and lm.material_effect in {"entry", "flip", "increase"}
    }
    aggressive_alpha_slips = {
        symbol: float(slip_est[symbol])
        for symbol in aggressive_alpha_symbols
        if slip_est.get(symbol) is not None
    }
    aggressive_alpha_slippage_complete = (
        set(aggressive_alpha_slips) == aggressive_alpha_symbols
    )
    worst_aggressive_alpha_slip = max(aggressive_alpha_slips.values(), default=0.0)
    hard_ban_violations: list[ObjectiveHardBanViolation] = []
    for lm in sorted(
        (metric for metric in legs if metric.symbol in aggressive_alpha_symbols),
        key=lambda metric: metric.symbol,
    ):
        prior = held.get(lm.symbol)
        if (
            prior is not None
            and prior.get("seat_role", "alpha") == "hedge"
            and lm.seat_role == "alpha"
        ):
            action = "hedge_to_alpha"
        elif lm.change == "new":
            action = "new"
        elif lm.change == "flipped":
            action = "flip"
        else:
            action = "increase"
        row = ev_by_sym.get(lm.symbol) or {}
        momentum_raw = row.get("momentum_pct")
        momentum = float(momentum_raw) if momentum_raw is not None else None
        if (
            lm.side == "short"
            and lm.change in {"new", "flipped"}
            and momentum is not None
        ):
            if momentum < POST_CRASH_SHORT_MOMENTUM_PCT:
                hard_ban_violations.append(
                    ObjectiveHardBanViolation(
                        rule_id="post_crash_short",
                        symbol=lm.symbol,
                        action=action,
                        side=lm.side,
                        target_notional=lm.notional,
                        momentum_pct=momentum,
                    )
                )
            if momentum > FADE_SHORT_MOMENTUM_PCT:
                hard_ban_violations.append(
                    ObjectiveHardBanViolation(
                        rule_id="fade_short",
                        symbol=lm.symbol,
                        action=action,
                        side=lm.side,
                        target_notional=lm.notional,
                        momentum_pct=momentum,
                    )
                )
        slip = aggressive_alpha_slips.get(lm.symbol)
        if slip is not None and slip > AGGRESSIVE_ALPHA_EST_SLIPPAGE_BPS_MAX:
            hard_ban_violations.append(
                ObjectiveHardBanViolation(
                    rule_id="aggressive_alpha_2k_slippage",
                    symbol=lm.symbol,
                    action=action,
                    side=lm.side,
                    target_notional=lm.notional,
                    est_slippage_bps_2k=slip,
                )
            )
        bid_depth_raw = row.get("depth_usd_bid")
        ask_depth_raw = row.get("depth_usd_ask")
        if bid_depth_raw is not None and ask_depth_raw is not None:
            bid_depth = float(bid_depth_raw)
            ask_depth = float(ask_depth_raw)
            if (
                lm.notional > LOW_DEPTH_MAX_AGGRESSIVE_NOTIONAL
                and (bid_depth < LOW_DEPTH_USD or ask_depth < LOW_DEPTH_USD)
            ):
                hard_ban_violations.append(
                    ObjectiveHardBanViolation(
                        rule_id="aggressive_alpha_low_depth",
                        symbol=lm.symbol,
                        action=action,
                        side=lm.side,
                        target_notional=lm.notional,
                        depth_usd_bid=bid_depth,
                        depth_usd_ask=ask_depth,
                    )
                )
    hard_ban_violations.sort(key=lambda item: (item.symbol, item.rule_id, item.action))

    # B12 — SIZE-AWARE break-even on every ECONOMIC change (the cycle-11 lesson: the agents priced a
    # $5K WLD entry off a $2K slippage probe, so a 32-interval payback passed as 7.7). Price the
    # friction at the REAL executable clip and require repayment within ten 8h funding intervals.
    # PM/current notionals live at the decision mark; freeze quantity and revalue it at the exact
    # L2 midpoint that denominates each curve before tier selection and dollar-cost arithmetic.
    # New legs and increases pay entry + eventual exit (2x one-way). A flip also pays the
    # immediate close of the prior side. Their explicit,
    # PM-proposed beta-adjusted price edge is combined with funding over its stated horizon; the
    # Adversary must audit that forecast against the price evidence. A material decrease or drop
    # pays one-way friction and can repay only by avoiding adverse carry. A changed BTC hedge is
    # exempt only when it reduces |beta-$| versus carrying the held BTC exposure into the exact
    # proposed non-BTC book. Final improvement versus "no hedge" alone is insufficient.
    worst_payback = 0.0
    worst_payback_symbol = ""
    payback_checked = False
    change_costs: list[ChangeCostMetric] = []
    partial_fill_risk_symbols: set[str] = set()
    total_friction = 0.0
    all_friction_priced = True
    aggressive_pre_friction_total = 0.0
    aggressive_post_friction_total = 0.0
    all_aggressive_priced = True
    for lm in legs:
        if lm.change not in ("new", "flipped", "resized", "role_changed"):
            continue
        insurance_exempt = bool(
            lm.symbol == btc_symbol and lm.seat_role == "hedge" and hedge_change_risk_reducing
        )
        row = ev_by_sym.get(lm.symbol)
        payback_checked = True
        prior = held.get(lm.symbol)
        prior_role = prior.get("seat_role", "alpha") if prior else None
        role_changed = bool(prior and prior_role != lm.seat_role)
        semantic_alpha_entry = bool(
            role_changed and prior_role == "hedge" and lm.seat_role == "alpha"
        )
        if role_changed:
            action = "role_change"
        elif lm.change == "resized":
            action = "increase" if lm.material_effect == "increase" else "reduction"
        elif lm.change == "flipped":
            action = "flip"
        else:
            action = "entry"
        immediate_execution_clip = float("inf")
        future_exit_execution_clip: float | None = None
        immediate_curve_lookup_clip: float | None = None
        future_exit_curve_lookup_clip: float | None = None
        fill_fractions: list[float] = []
        if not row:
            friction = float("inf")
            clip = lm.turnover_notional
            increasing = lm.material_effect in ("entry", "flip", "increase")
        elif lm.change == "role_changed":
            clip = 0.0
            increasing = lm.material_effect == "entry"
            friction = 0.0
            immediate_execution_clip = 0.0
            immediate_curve_lookup_clip = 0.0
            fill_fractions.append(1.0)
        elif lm.change == "resized":
            clip = lm.turnover_notional
            increasing = bool(prior and lm.notional > float(prior["target_notional"]))
            if increasing:
                (
                    immediate_friction,
                    immediate_execution_clip,
                    immediate_curve_lookup_clip,
                    immediate_fill_fraction,
                ) = _one_way_friction_usd(
                    row,
                    clip,
                    _entry_execution_side(lm.side),
                    execution_realism=execution_policy,
                    legging_reserve_bps=pretrade_legging_reserve_bps,
                )
                (
                    future_exit_friction,
                    future_exit_execution_clip,
                    future_exit_curve_lookup_clip,
                    future_fill_fraction,
                ) = _one_way_friction_usd(
                    row,
                    clip,
                    _exit_execution_side(lm.side),
                    execution_realism=execution_policy,
                    legging_reserve_bps=pretrade_legging_reserve_bps,
                )
                fill_fractions.extend(
                    value
                    for value in (immediate_fill_fraction, future_fill_fraction)
                    if value is not None
                )
                friction = immediate_friction + future_exit_friction
            else:
                (
                    friction,
                    immediate_execution_clip,
                    immediate_curve_lookup_clip,
                    immediate_fill_fraction,
                ) = _one_way_friction_usd(
                    row,
                    clip,
                    _exit_execution_side(lm.side),
                    execution_realism=execution_policy,
                    legging_reserve_bps=pretrade_legging_reserve_bps,
                )
                if immediate_fill_fraction is not None:
                    fill_fractions.append(immediate_fill_fraction)
        elif lm.change == "flipped" and prior:
            # Execution sends one net delta through one book. Price the immediate close+open as
            # that combined convex clip, then add the eventual exit of the new target.
            clip = lm.notional
            increasing = True
            immediate_clip = lm.turnover_notional
            (
                immediate_friction,
                immediate_execution_clip,
                immediate_curve_lookup_clip,
                immediate_fill_fraction,
            ) = _one_way_friction_usd(
                row,
                immediate_clip,
                _entry_execution_side(lm.side),
                execution_realism=execution_policy,
                legging_reserve_bps=pretrade_legging_reserve_bps,
            )
            (
                future_exit_friction,
                future_exit_execution_clip,
                future_exit_curve_lookup_clip,
                future_fill_fraction,
            ) = _one_way_friction_usd(
                row,
                clip,
                _exit_execution_side(lm.side),
                execution_realism=execution_policy,
                legging_reserve_bps=pretrade_legging_reserve_bps,
            )
            fill_fractions.extend(
                value
                for value in (immediate_fill_fraction, future_fill_fraction)
                if value is not None
            )
            friction = immediate_friction + future_exit_friction
        else:
            clip = lm.notional
            increasing = True
            (
                immediate_friction,
                immediate_execution_clip,
                immediate_curve_lookup_clip,
                immediate_fill_fraction,
            ) = _one_way_friction_usd(
                row,
                clip,
                _entry_execution_side(lm.side),
                execution_realism=execution_policy,
                legging_reserve_bps=pretrade_legging_reserve_bps,
            )
            (
                future_exit_friction,
                future_exit_execution_clip,
                future_exit_curve_lookup_clip,
                future_fill_fraction,
            ) = _one_way_friction_usd(
                row,
                clip,
                _exit_execution_side(lm.side),
                execution_realism=execution_policy,
                legging_reserve_bps=pretrade_legging_reserve_bps,
            )
            fill_fractions.extend(
                value
                for value in (immediate_fill_fraction, future_fill_fraction)
                if value is not None
            )
            friction = immediate_friction + future_exit_friction
        # The execution cost remains the actual resize delta, while hedge->alpha reclassification
        # evaluates the full retained seat as a fresh alpha thesis even when that delta is a cut.
        if semantic_alpha_entry:
            increasing = True
        # Short earns +funding and long earns -funding. An increase repays from positive carry;
        # a decrease repays only when it stops negative carry on the reduced notional.
        seat_sign = 1.0 if lm.side == "short" else -1.0
        seat_carry = seat_sign * fund_bps.get(lm.symbol, 0.0) / 1e4 * clip
        friction_priced = math.isfinite(friction)
        min_fill_fraction = min(fill_fractions) if fill_fractions else None
        partial_fill_expected = bool(
            min_fill_fraction is not None and min_fill_fraction < 1.0 - 1e-12
        )
        if partial_fill_expected:
            partial_fill_risk_symbols.add(lm.symbol)
        if friction_priced:
            total_friction += friction
        else:
            all_friction_priced = False
        lm.changed_slice_friction_usd = friction if friction_priced else None
        edge_through_horizon: float | None = None
        net_edge_through_horizon: float | None = None
        if increasing:
            horizon_intervals = max(lm.edge_horizon_hours / 8.0, 1.0)
            # A role-only hedge->alpha transition exposes the whole held seat to the newly claimed
            # alpha thesis even though executable turnover remains zero.
            economic_clip = lm.notional if action == "role_change" else clip
            price_edge_usd = lm.expected_price_edge_frac * economic_clip
            seat_carry = seat_sign * fund_bps.get(lm.symbol, 0.0) / 1e4 * economic_clip
            total_edge_per_8h = seat_carry + price_edge_usd / horizon_intervals
            lm.changed_slice_expected_total_edge_usd_per_8h = total_edge_per_8h
            edge_through_horizon = price_edge_usd + seat_carry * horizon_intervals
            lm.changed_slice_expected_edge_through_horizon_usd = edge_through_horizon
            lm.changed_slice_expected_edge_through_horizon_pre_friction_usd = edge_through_horizon
            if friction_priced:
                net_edge_through_horizon = edge_through_horizon - friction
                lm.changed_slice_net_edge_through_horizon_after_friction_usd = (
                    net_edge_through_horizon
                )
                lm.zero_price_edge_payback_intervals = (
                    friction / seat_carry if seat_carry > 1e-9 else 9999.0
                )
                lm.required_price_edge_frac_for_max_payback = (
                    _required_price_edge_frac_for_max_payback(
                        friction, seat_carry, horizon_intervals, economic_clip
                    )
                )
            payback = _forecast_payback_intervals(
                friction,
                price_edge_usd,
                seat_carry,
                horizon_intervals,
            )
        else:
            repay_edge = -seat_carry
            lm.changed_slice_expected_total_edge_usd_per_8h = repay_edge
            horizon_intervals = max(lm.edge_horizon_hours / 8.0, 1.0)
            edge_through_horizon = repay_edge * horizon_intervals
            lm.changed_slice_expected_edge_through_horizon_usd = edge_through_horizon
            lm.changed_slice_expected_edge_through_horizon_pre_friction_usd = edge_through_horizon
            if friction_priced:
                net_edge_through_horizon = edge_through_horizon - friction
                lm.changed_slice_net_edge_through_horizon_after_friction_usd = (
                    net_edge_through_horizon
                )
                lm.zero_price_edge_payback_intervals = (
                    friction / repay_edge if repay_edge > 1e-9 else 9999.0
                )
            payback = (
                friction / repay_edge if friction_priced and repay_edge > 1e-9 else float("inf")
            )
        # A pure same-quantity role relabel executes no order and pays exactly zero friction.
        # B12 is an execution-cost payback check, so it is already repaid at t=0; the required
        # ExitAudit plus fresh Seat/Action/Hedge audits govern the semantic lifecycle transfer.
        if action == "role_change" and friction_priced and friction <= 1e-12:
            payback = 0.0
        normalized_payback = payback if math.isfinite(payback) else 9999.0
        insurance_exemption_applied = insurance_exempt and friction_priced
        if not insurance_exemption_applied and normalized_payback >= worst_payback:
            worst_payback = normalized_payback
            worst_payback_symbol = lm.symbol
        aggressive = lm.material_effect in ("entry", "flip", "increase")
        if aggressive:
            if friction_priced and edge_through_horizon is not None:
                aggressive_pre_friction_total += edge_through_horizon
                aggressive_post_friction_total += edge_through_horizon - friction
            else:
                all_aggressive_priced = False
        change_costs.append(
            ChangeCostMetric(
                symbol=lm.symbol,
                action=action,
                seat_role=lm.seat_role,
                prior_side=prior.get("side") if prior else None,
                final_side=lm.side,
                prior_seat_role=prior_role,
                final_seat_role=lm.seat_role,
                decision_turnover_usd=round(lm.turnover_notional, 6),
                executable_turnover_usd=(
                    round(immediate_execution_clip, 6)
                    if math.isfinite(immediate_execution_clip)
                    else None
                ),
                future_exit_executable_notional_usd=(
                    round(future_exit_execution_clip, 6)
                    if future_exit_execution_clip is not None
                    and math.isfinite(future_exit_execution_clip)
                    else None
                ),
                conservative_curve_lookup_turnover_usd=(
                    round(immediate_curve_lookup_clip, 6)
                    if immediate_curve_lookup_clip is not None
                    and math.isfinite(immediate_curve_lookup_clip)
                    else None
                ),
                conservative_future_exit_curve_lookup_usd=(
                    round(future_exit_curve_lookup_clip, 6)
                    if future_exit_curve_lookup_clip is not None
                    and math.isfinite(future_exit_curve_lookup_clip)
                    else None
                ),
                estimated_min_fill_fraction=min_fill_fraction,
                partial_fill_expected=partial_fill_expected,
                liquidity_mid=(
                    float(row.get("liquidity_mid"))
                    if row
                    and math.isfinite(float(row.get("liquidity_mid") or 0.0))
                    and float(row.get("liquidity_mid") or 0.0) > 0.0
                    else None
                ),
                friction_usd=round(friction, 6) if friction_priced else None,
                friction_priced=friction_priced,
                adverse_selection_reserve_bps=execution_policy.adverse_selection_bps,
                legging_reserve_bps=pretrade_legging_reserve_bps,
                b12_insurance_exempt=insurance_exemption_applied,
                expected_edge_through_horizon_pre_friction_usd=edge_through_horizon,
                expected_net_edge_through_horizon_after_friction_usd=net_edge_through_horizon,
                # Preserve the exact finite value used by B12. Exception binding later applies the
                # same strict >10 predicate to this persisted row; rounding here could turn a raw
                # 10.0000004 failure into 10.0 and silently erase the offending symbol.
                payback_intervals=normalized_payback,
            )
        )

    # A dropped seat has no final BookLeg, but its exit friction/economics are still material.
    for symbol in dropped:
        row = ev_by_sym.get(symbol)
        prior = held.get(symbol)
        if not prior:
            continue
        payback_checked = True
        clip = float(prior["target_notional"])
        friction = float("inf")
        execution_clip = float("inf")
        curve_lookup_clip: float | None = None
        fill_fraction: float | None = None
        if row:
            friction, execution_clip, curve_lookup_clip, fill_fraction = _one_way_friction_usd(
                row,
                clip,
                _exit_execution_side(str(prior["side"])),
                execution_realism=execution_policy,
                legging_reserve_bps=pretrade_legging_reserve_bps,
            )
        seat_sign = 1.0 if prior["side"] == "short" else -1.0
        seat_carry = seat_sign * fund_bps.get(symbol, 0.0) / 1e4 * clip
        avoided_adverse_carry = -seat_carry
        friction_priced = math.isfinite(friction)
        partial_fill_expected = bool(
            fill_fraction is not None and fill_fraction < 1.0 - 1e-12
        )
        if partial_fill_expected:
            partial_fill_risk_symbols.add(symbol)
        insurance_exemption_applied = bool(
            symbol == btc_symbol
            and prior.get("seat_role", "alpha") == "hedge"
            and hedge_drop_risk_reducing
            and friction_priced
        )
        if friction_priced:
            total_friction += friction
        else:
            all_friction_priced = False
        payback = (
            friction / avoided_adverse_carry
            if friction_priced and avoided_adverse_carry > 1e-9
            else float("inf")
        )
        normalized_payback = payback if math.isfinite(payback) else 9999.0
        if not insurance_exemption_applied and normalized_payback >= worst_payback:
            worst_payback = normalized_payback
            worst_payback_symbol = symbol
        horizon_intervals = max(float(prior.get("edge_horizon_hours", 24)) / 8.0, 1.0)
        edge_through_horizon = avoided_adverse_carry * horizon_intervals
        change_costs.append(
            ChangeCostMetric(
                symbol=symbol,
                action="drop",
                seat_role=prior.get("seat_role", "alpha"),
                prior_side=prior["side"],
                final_side=None,
                prior_seat_role=prior.get("seat_role", "alpha"),
                final_seat_role=None,
                decision_turnover_usd=round(clip, 6),
                executable_turnover_usd=(
                    round(execution_clip, 6) if math.isfinite(execution_clip) else None
                ),
                conservative_curve_lookup_turnover_usd=(
                    round(curve_lookup_clip, 6)
                    if curve_lookup_clip is not None and math.isfinite(curve_lookup_clip)
                    else None
                ),
                estimated_min_fill_fraction=fill_fraction,
                partial_fill_expected=partial_fill_expected,
                liquidity_mid=(
                    float(row.get("liquidity_mid"))
                    if row
                    and math.isfinite(float(row.get("liquidity_mid") or 0.0))
                    and float(row.get("liquidity_mid") or 0.0) > 0.0
                    else None
                ),
                friction_usd=round(friction, 6) if friction_priced else None,
                friction_priced=friction_priced,
                adverse_selection_reserve_bps=execution_policy.adverse_selection_bps,
                legging_reserve_bps=pretrade_legging_reserve_bps,
                b12_insurance_exempt=insurance_exemption_applied,
                expected_edge_through_horizon_pre_friction_usd=edge_through_horizon,
                expected_net_edge_through_horizon_after_friction_usd=(
                    edge_through_horizon - friction if friction_priced else None
                ),
                # Keep the exact finite B12 decision value for downstream exception-scope binding.
                payback_intervals=normalized_payback,
            )
        )

    def _b(bid: str, desc: str, value: float, limit: str, ok: bool) -> BoundCheck:
        # Preserve the exact decision value. A rounded 10.0000004 displayed as 10.0 while B12
        # truthfully failed was safe for exception binding but misleading to both reviewing GPTs.
        return BoundCheck(bound_id=bid, description=desc, value=value, limit=limit, ok=ok)

    bounds = [
        _b(
            "B1",
            "deploy gross/cash within band",
            deploy,
            f"[{DEPLOY_MIN}, {DEPLOY_MAX}]",
            DEPLOY_MIN <= deploy <= DEPLOY_MAX,
        ),
        _b(
            "B2",
            "dollar residual |L-S|/gross",
            dollar_resid,
            f"<= {DOLLAR_RESIDUAL_MAX}",
            dollar_resid <= DOLLAR_RESIDUAL_MAX,
        ),
        _b(
            "B3",
            "beta residual |net beta-$|/cash",
            abs(beta_resid),
            f"<= {BETA_RESIDUAL_MAX}",
            abs(beta_resid) <= BETA_RESIDUAL_MAX,
        ),
        _b(
            "B4",
            "max single-leg share of gross",
            max_leg_frac,
            f"<= {MAX_LEG_FRAC_GROSS}",
            max_leg_frac <= MAX_LEG_FRAC_GROSS,
        ),
        _b(
            "B5",
            "BTC hedge notional / cash",
            hedge_frac,
            f"<= {HEDGE_FRAC_CASH_MAX}",
            hedge_frac <= HEDGE_FRAC_CASH_MAX,
        ),
        _b(
            "B6",
            "max per-leg |notional x beta| / cash",
            (max_beta_usd / cash) if cash > 0 else 0.0,
            f"<= {LEG_BETA_USD_FRAC_MAX}",
            (max_beta_usd / cash if cash > 0 else 0.0) <= LEG_BETA_USD_FRAC_MAX,
        ),
        _b(
            "B7",
            "stated_* metrics match computed",
            max(
                abs(book.stated_deploy_frac - deploy),
                abs(book.stated_dollar_residual_frac - dollar_resid),
                abs(book.stated_beta_residual - beta_resid),
            ),
            f"<= {STATED_TOL}",
            abs(book.stated_deploy_frac - deploy) <= STATED_TOL
            and abs(book.stated_dollar_residual_frac - dollar_resid) <= STATED_TOL
            and abs(book.stated_beta_residual - beta_resid) <= STATED_TOL,
        ),
        _b(
            "B8",
            "turnover count, is_new, and hold-breaking claims are truthful",
            float(abs(book.turnover_legs_changed - n_changed) + len(turnover_claim_errors)),
            "== 0 claim errors",
            book.turnover_legs_changed == n_changed and not turnover_claim_errors,
        ),
        _b(
            "B9",
            (
                "aggressive changes (added+flipped+same-side increases); four only for an "
                "exact-flat, all-non-BTC-alpha re-entry with complete residual covariance"
            ),
            float(n_aggressive),
            f"<= {b9_aggressive_change_limit}",
            n_aggressive <= b9_aggressive_change_limit,
        ),
        _b(
            "B10",
            (
                "worst priced selected/exit est. slippage <=75bp and every aggressive-alpha "
                "new/flip/increase/hedge->alpha 2k screen <=50bp"
            ),
            worst_slip,
            (
                f"priced selected/exit <= {EST_SLIPPAGE_BPS_MAX}; aggressive alpha complete "
                f"and <= {AGGRESSIVE_ALPHA_EST_SLIPPAGE_BPS_MAX}"
            ),
            ((worst_slip <= EST_SLIPPAGE_BPS_MAX) if slip_known else True)
            and aggressive_alpha_slippage_complete
            and worst_aggressive_alpha_slip <= AGGRESSIVE_ALPHA_EST_SLIPPAGE_BPS_MAX,
        ),
        _b(
            "B11",
            "no duplicate or unpriced legs",
            float(len(duplicates) + len(unpriced)),
            "== 0",
            not duplicates and not unpriced,
        ),
        _b(
            "B12",
            (
                "worst entry/flip/resize/drop payback after displayed-depth haircut and "
                "implementation-shortfall reserves, in 8h edge intervals"
            ),
            (worst_payback if worst_payback != float("inf") else 9999.0),
            f"<= {MAX_PAYBACK_FUNDING_INTERVALS}",
            (worst_payback <= MAX_PAYBACK_FUNDING_INTERVALS) if payback_checked else True,
        ),
    ]

    metrics = PrecheckMetrics(
        schema_version=PRECHECK_SCHEMA_VERSION,
        cycle=cycle,
        cash=round(cash, 2),
        gross=round(gross, 2),
        deploy_frac=round(deploy, 6),
        longs_usd=round(longs, 2),
        shorts_usd=round(shorts, 2),
        alpha_gross=round(alpha_gross, 2),
        alpha_deploy_frac=round(alpha_gross / cash if cash > 0.0 else 0.0, 6),
        alpha_longs_usd=round(alpha_longs, 2),
        alpha_shorts_usd=round(alpha_shorts, 2),
        hedge_gross=round(hedge_gross, 2),
        hedge_deploy_frac=round(hedge_gross / cash if cash > 0.0 else 0.0, 6),
        dollar_residual_frac=round(dollar_resid, 6),
        beta_net_usd=round(beta_net, 2),
        beta_residual=round(beta_resid, 6),
        max_leg_symbol=max_leg_sym,
        max_leg_frac_gross=round(max_leg_frac, 6),
        hedge_notional=round(hedge_notional, 2),
        hedge_frac_cash=round(hedge_frac, 6),
        alpha_beta_net_usd_before_hedge=round(beta_without_hedge, 2),
        hedge_beta_usd=round(hedge_beta_usd, 2),
        hedge_beta_reduction_frac=round(hedge_beta_reduction, 6),
        hedge_counterfactual_beta_net_usd=round(hedge_counterfactual_beta_net_usd, 2),
        hedge_change_risk_reducing=hedge_change_risk_reducing,
        max_leg_beta_usd_symbol=max_beta_sym,
        max_leg_beta_usd=round(max_beta_usd, 2),
        legs=legs,
        legs_added=sorted(added),
        legs_dropped=dropped,
        legs_flipped=sorted(flipped),
        legs_resized=sorted(resized),
        turnover_legs_changed=n_changed,
        turnover_usd=round(turnover_usd, 2),
        turnover_aggressive_legs_changed=n_aggressive,
        cold_start_reentry_eligible=cold_start_reentry_eligible,
        b9_aggressive_change_limit=b9_aggressive_change_limit,
        controlled_restart_origin_cycle=book.controlled_restart_origin_cycle,
        controlled_restart_phase=book.controlled_restart_phase,
        controlled_restart_initial_eligible=controlled_restart_initial_eligible,
        controlled_restart_continuation_eligible=(
            controlled_restart_continuation_eligible
        ),
        controlled_restart_lineage_valid=controlled_restart_lineage_valid,
        controlled_restart_prior_cycle=(
            prior_book_lineage.cycle if prior_book_lineage is not None else None
        ),
        controlled_restart_prior_book_sha256=(
            prior_book_lineage.book_sha256 if prior_book_lineage is not None else None
        ),
        controlled_restart_prior_origin_cycle=(
            prior_book_lineage.controlled_restart_origin_cycle
            if prior_book_lineage is not None
            else None
        ),
        controlled_restart_prior_phase=(
            prior_book_lineage.controlled_restart_phase
            if prior_book_lineage is not None
            else False
        ),
        binding_user_directive_present=binding_user_directive_present,
        binding_user_directive_controlled_restart_graduation=(
            binding_user_directive_controlled_restart_graduation
        ),
        turnover_risk_reductions=n_risk_reductions,
        turnover_claim_errors=turnover_claim_errors,
        unpriced_symbols=unpriced,
        duplicate_symbols=sorted(set(duplicates)),
        hard_ban_violations=hard_ban_violations,
        worst_changed_leg_payback_cycles=(
            worst_payback if worst_payback != float("inf") else 9999.0
        ),
        worst_changed_leg_payback_symbol=worst_payback_symbol,
        change_costs=change_costs,
        total_action_friction_usd=(round(total_friction, 6) if all_friction_priced else None),
        total_action_friction_fully_priced=all_friction_priced,
        total_aggressive_expected_edge_through_horizon_pre_friction_usd=(
            round(aggressive_pre_friction_total, 6) if all_aggressive_priced else None
        ),
        total_aggressive_expected_net_edge_through_horizon_after_friction_usd=(
            round(aggressive_post_friction_total, 6) if all_aggressive_priced else None
        ),
        execution_policy_applied=execution_policy_applied,
        execution_latency_ms=execution_policy.latency_ms,
        execution_displayed_depth_fraction=execution_policy.displayed_depth_fraction,
        execution_adverse_selection_bps=execution_policy.adverse_selection_bps,
        execution_legging_bps_per_second=execution_policy.legging_bps_per_second,
        execution_allow_partial_fills=execution_policy.allow_partial_fills,
        pretrade_legging_reserve_bps=pretrade_legging_reserve_bps,
        partial_fill_risk_symbols=sorted(partial_fill_risk_symbols),
        hedge_risk_reducing=hedge_risk_reducing,
        input_meta_sha256=meta_sha256,
        risk_model_available=risk_available,
        risk_model_unavailable_reason=risk_unavailable_reason,
        portfolio_residual_vol_annualized_usd=round(portfolio_risk_usd, 2),
        portfolio_residual_vol_annualized_frac_cash=round(
            portfolio_risk_usd / cash if cash > 0.0 else 0.0, 6
        ),
        portfolio_expected_price_edge_usd_per_8h=round(portfolio_price_edge_per_8h, 6),
        portfolio_expected_carry_usd_per_8h=round(portfolio_carry_per_8h, 6),
        portfolio_expected_total_edge_usd_per_8h=round(
            portfolio_price_edge_per_8h + portfolio_carry_per_8h, 6
        ),
        max_alpha_standalone_risk_symbol=max_risk_symbol,
        max_alpha_standalone_risk_share=round(max_risk_share, 6),
        long_short_standalone_risk_ratio=(round(risk_ratio, 6) if risk_ratio is not None else None),
        held_high_correlation_pairs=held_high_pairs,
        same_side_high_correlation_clusters=same_side_clusters,
        max_same_side_high_correlation_cluster_risk_share=round(
            max_same_side_cluster_risk_share, 6
        ),
        position_co_risk_clusters=position_co_risk_clusters,
        max_position_co_risk_cluster_risk_share=round(max_position_co_risk_cluster_risk_share, 6),
        bounds=bounds,
    )
    metrics.sha256 = precheck_sha256(metrics)
    return metrics
