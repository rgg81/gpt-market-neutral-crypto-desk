"""Attribution + recurrence detection for the self-learning loop.

Pure functions and pydantic models — NO I/O. The deterministic 'measure' half of
the reflector: score each agent's decisions against realized forward returns, then
(Task 2) surface RECURRENT misbehaviours for the Reflector agent to act on.
Records, never decides."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from futures_fund.desk_contracts import Book, CandidateReview, SpecialistRead

K_DEFAULT = 3
WINDOW_DEFAULT = 6
HI_CONV = 0.6
LAX_NET_EDGE_FRAC = -0.005  # accepted book whose strategy net edge < -0.5% of gross
# => adversary too lax
PM_NET_EDGE_FRAC = -0.0025  # only material (< -25bps) strategy losses tune the carry PM
_SCHEDULED_HORIZON_TOLERANCE_HOURS = 5.0 / 60.0
CURRENT_SCORE_SCHEMA_VERSION = 2
SCORECARD_MIGRATION_WAL_FILE = "scorecard-migration-v2.wal.json"


def forward_returns(marks_prev: dict[str, float], marks_now: dict[str, float]) -> dict[str, float]:
    """Per-symbol return marks_prev -> marks_now, for symbols present (and
    non-zero) in BOTH."""
    out: dict[str, float] = {}
    for sym, mp in marks_prev.items():
        mn = marks_now.get(sym)
        if mp and mn:
            out[sym] = mn / mp - 1.0
    return out


class SpecialistScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    n_available: int = 0
    n_scored: int = 0
    abstention_rate: float = 0.0
    hit_rate: float = 0.0
    conv_weighted_edge: float = 0.0
    hi_n: int = 0
    hi_conv_hit_rate: float = 0.0


def score_specialist(
    role: str, reads: list[SpecialistRead], rets: dict[str, float]
) -> SpecialistScore:
    hits = n = hi_hits = hi_n = 0
    available = sum(read.symbol in rets for read in reads)
    edge = 0.0
    for r in reads:
        if r.lean == "flat":
            continue
        ret = rets.get(r.symbol)
        if ret is None:
            continue
        n += 1
        signed = 1.0 if r.lean == "long" else -1.0
        hit = (signed * ret) > 0
        hits += 1 if hit else 0
        edge += signed * ret * r.conviction
        if r.conviction > HI_CONV:
            hi_n += 1
            hi_hits += 1 if hit else 0
    return SpecialistScore(
        role=role,
        n_available=available,
        n_scored=n,
        abstention_rate=(1.0 - n / available) if available else 0.0,
        hit_rate=(hits / n) if n else 0.0,
        conv_weighted_edge=(edge / n) if n else 0.0,
        hi_n=hi_n,
        hi_conv_hit_rate=(hi_hits / hi_n) if hi_n else 0.0,
    )


class BookScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_legs: int = 0
    gross_notional: float = 0.0
    alpha_n_legs: int = 0
    alpha_gross_notional: float = 0.0
    hedge_gross_notional: float = 0.0
    gross_pnl: float = 0.0
    alpha_gross_pnl: float = 0.0
    hedge_gross_pnl: float = 0.0
    return_frac: float = 0.0
    beta_dollar: float = 0.0
    alpha_net_beta: float = 0.0
    alpha_frac: float = 0.0
    # ``alpha_frac`` above retains its historical whole-book-gross denominator. These explicit
    # fields stop a BTC insurance leg from diluting the alpha-seat return used for learning.
    alpha_return_frac_on_alpha_gross: float | None = None
    projected_funding_pnl: float = 0.0
    entry_friction: float = 0.0
    # This is the realized forward, beta-adjusted price edge less the entry friction recorded by
    # the paper account. Funding is deliberately excluded because decision-time expected funding
    # is a forecast, not realized P&L. Exact cumulative funding is reported in the performance
    # packet until it can be attributed to a particular decision generation without guessing.
    realized_edge_ex_funding: float | None = None
    realized_edge_ex_funding_frac: float | None = None
    realized_alpha_edge_ex_funding_frac: float | None = None
    # None distinguishes legacy price-only score rows from strategy-aligned rows. Old rows must
    # never tune the carry PM after this contract was introduced.
    strategy_net_edge: float | None = None
    strategy_net_frac: float | None = None
    strategy_net_is_forecast: bool = False
    actual_realized_funding_pnl: float | None = None
    actual_strategy_net_edge: float | None = None
    actual_strategy_net_frac_on_whole_book_gross: float | None = None
    # Pre-release compatibility only: exact funding is observed for the whole account and cannot
    # truthfully be allocated between alpha and hedge seats without a per-seat settlement ledger.
    actual_strategy_net_frac_on_alpha_gross: float | None = None
    actual_funding_attribution_status: str = "unavailable"
    decision_kind: Literal["deployed", "exit_to_cash", "cash_hold", "hedge_only"] = "cash_hold"
    decision_evaluation_gross: float = 0.0
    no_change_counterfactual_edge_ex_funding: float | None = None
    incremental_edge_vs_no_change: float | None = None
    incremental_edge_vs_no_change_frac: float | None = None


class CandidateOpportunityScore(BaseModel):
    """One immutable PM-declared candidate measured at its scheduled outcome.

    The causal label is never inferred. ``gate_causal_claim`` is true only when the bound PM
    explicitly wrote ``exclusion_reason='entry_gate'`` before the outcome was known.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_sha256: str
    symbol: str
    side: Literal["long", "short"]
    status: Literal["selected", "rejected", "deferred"]
    exclusion_reason: str
    gate_causal_claim: bool = False
    forecast_horizon_hours: int
    evaluation_horizon_hours: float
    horizon_label_eligible: bool
    expected_price_edge_frac: float
    counterfactual_notional: float
    realized_beta_adjusted_return_frac: float | None = None
    realized_selected_edge_frac: float | None = None
    counterfactual_price_pnl: float | None = None
    forecast_error_frac: float | None = None
    selected_side_profitable: bool | None = None
    book_sha256: str = ""
    specialist_reads_sha256: str = ""
    entry_gate_policy_sha256: str = ""


def score_candidate_opportunities(
    candidates: list[CandidateReview],
    rets: dict[str, float],
    betas: dict[str, float],
    btc_ret: float,
    *,
    evaluation_horizon_hours: float,
    book_sha256: str,
    specialist_reads_sha256: str,
    entry_gate_policy_sha256: str,
) -> list[CandidateOpportunityScore]:
    """Measure PM-declared candidates without inferring a trade or exclusion reason."""
    rows: list[CandidateOpportunityScore] = []
    for candidate in candidates:
        payload = candidate.model_dump(mode="json")
        candidate_sha256 = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        raw_return = rets.get(candidate.symbol)
        relative_return = (
            raw_return - betas.get(candidate.symbol, 1.0) * btc_ret
            if raw_return is not None
            else None
        )
        side_sign = 1.0 if candidate.side == "long" else -1.0
        selected_edge = side_sign * relative_return if relative_return is not None else None
        on_horizon = (
            abs(evaluation_horizon_hours - float(candidate.edge_horizon_hours))
            <= _SCHEDULED_HORIZON_TOLERANCE_HOURS + 1e-12
        )
        rows.append(
            CandidateOpportunityScore(
                candidate_sha256=candidate_sha256,
                symbol=candidate.symbol,
                side=candidate.side,
                status=candidate.status,
                exclusion_reason=candidate.exclusion_reason,
                gate_causal_claim=candidate.exclusion_reason == "entry_gate",
                forecast_horizon_hours=candidate.edge_horizon_hours,
                evaluation_horizon_hours=evaluation_horizon_hours,
                horizon_label_eligible=on_horizon,
                expected_price_edge_frac=candidate.expected_price_edge_frac,
                counterfactual_notional=candidate.counterfactual_notional,
                realized_beta_adjusted_return_frac=relative_return,
                realized_selected_edge_frac=selected_edge,
                counterfactual_price_pnl=(
                    selected_edge * candidate.counterfactual_notional
                    if selected_edge is not None
                    else None
                ),
                forecast_error_frac=(
                    selected_edge - candidate.expected_price_edge_frac
                    if selected_edge is not None
                    else None
                ),
                selected_side_profitable=(selected_edge > 0.0)
                if selected_edge is not None
                else None,
                book_sha256=book_sha256,
                specialist_reads_sha256=specialist_reads_sha256,
                entry_gate_policy_sha256=entry_gate_policy_sha256,
            )
        )
    return rows


def score_book(
    book: Book,
    rets: dict[str, float],
    betas: dict[str, float],
    btc_ret: float,
    *,
    projected_funding_pnl: float | None = None,
    entry_friction: float = 0.0,
    actual_realized_funding_pnl: float | None = None,
    actual_funding_attribution_status: str = "unavailable",
    previous_book: Book | None = None,
) -> BookScore:
    def components(candidate: Book) -> dict[str, float | int]:
        gross = pnl = beta_d = 0.0
        alpha_gross = alpha_pnl = alpha_beta_d = hedge_gross = hedge_pnl = 0.0
        n = alpha_n = 0
        for lg in candidate.legs:
            ret = rets.get(lg.symbol)
            if ret is None:
                continue
            signed = lg.target_notional if lg.side == "long" else -lg.target_notional
            leg_pnl = signed * ret
            pnl += leg_pnl
            beta_d += signed * betas.get(lg.symbol, 1.0)
            gross += lg.target_notional
            n += 1
            if lg.seat_role == "hedge":
                hedge_gross += lg.target_notional
                hedge_pnl += leg_pnl
            else:
                alpha_gross += lg.target_notional
                alpha_pnl += leg_pnl
                alpha_beta_d += signed * betas.get(lg.symbol, 1.0)
                alpha_n += 1
        return {
            "gross": gross,
            "pnl": pnl,
            "beta_d": beta_d,
            "n": n,
            "alpha_gross": alpha_gross,
            "alpha_pnl": alpha_pnl,
            "alpha_beta_d": alpha_beta_d,
            "alpha_n": alpha_n,
            "hedge_gross": hedge_gross,
            "hedge_pnl": hedge_pnl,
        }

    values = components(book)
    gross = float(values["gross"])
    pnl = float(values["pnl"])
    beta_d = float(values["beta_d"])
    n = int(values["n"])
    alpha_gross = float(values["alpha_gross"])
    alpha_pnl = float(values["alpha_pnl"])
    alpha_beta_d = float(values["alpha_beta_d"])
    hedge_gross = float(values["hedge_gross"])
    hedge_pnl = float(values["hedge_pnl"])
    alpha_n = int(values["alpha_n"])
    alpha = alpha_pnl - alpha_beta_d * btc_ret
    # Keep the legacy whole-book beta-adjusted result. A correctly typed BTC hedge contributes
    # approximately zero residual alpha, but its notional must not enter the new alpha denominator.
    whole_book_residual = pnl - beta_d * btc_ret
    realized_edge = whole_book_residual - entry_friction
    strategy_net = (
        whole_book_residual + projected_funding_pnl - entry_friction
        if projected_funding_pnl is not None
        else None
    )
    actual_strategy_net = (
        whole_book_residual + actual_realized_funding_pnl - entry_friction
        if actual_realized_funding_pnl is not None
        else None
    )
    prior_values = components(previous_book) if previous_book is not None else None
    prior_alpha_gross = float(prior_values["alpha_gross"]) if prior_values is not None else 0.0
    no_change_edge = None
    incremental_vs_no_change = None
    if previous_book is not None:
        prior_edge = (
            float(prior_values["alpha_pnl"]) - float(prior_values["alpha_beta_d"]) * btc_ret
        )
        no_change_edge = prior_edge
        incremental_vs_no_change = whole_book_residual - entry_friction - prior_edge
    if alpha_gross > 0.0:
        decision_kind = "deployed"
        decision_gross = alpha_gross
    elif gross > 0.0:
        decision_kind = "hedge_only"
        decision_gross = gross
    elif prior_alpha_gross > 0.0:
        decision_kind = "exit_to_cash"
        decision_gross = prior_alpha_gross
    else:
        decision_kind = "cash_hold"
        decision_gross = 0.0
    return BookScore(
        n_legs=n,
        gross_notional=gross,
        alpha_n_legs=alpha_n,
        alpha_gross_notional=alpha_gross,
        hedge_gross_notional=hedge_gross,
        gross_pnl=pnl,
        alpha_gross_pnl=alpha_pnl,
        hedge_gross_pnl=hedge_pnl,
        return_frac=(pnl / gross) if gross else 0.0,
        beta_dollar=beta_d,
        alpha_net_beta=whole_book_residual,
        alpha_frac=(whole_book_residual / gross) if gross else 0.0,
        alpha_return_frac_on_alpha_gross=(alpha / alpha_gross) if alpha_gross else None,
        projected_funding_pnl=projected_funding_pnl or 0.0,
        entry_friction=entry_friction,
        realized_edge_ex_funding=realized_edge,
        realized_edge_ex_funding_frac=(realized_edge / gross) if gross else None,
        realized_alpha_edge_ex_funding_frac=(
            (alpha - entry_friction) / alpha_gross if alpha_gross else None
        ),
        strategy_net_edge=strategy_net,
        strategy_net_frac=(strategy_net / gross) if strategy_net is not None and gross else None,
        strategy_net_is_forecast=projected_funding_pnl is not None,
        actual_realized_funding_pnl=actual_realized_funding_pnl,
        actual_strategy_net_edge=actual_strategy_net,
        actual_strategy_net_frac_on_whole_book_gross=(
            actual_strategy_net / gross if actual_strategy_net is not None and gross else None
        ),
        actual_strategy_net_frac_on_alpha_gross=(
            None
            if actual_realized_funding_pnl is not None
            else (
                actual_strategy_net / alpha_gross
                if actual_strategy_net is not None and alpha_gross
                else None
            )
        ),
        actual_funding_attribution_status=actual_funding_attribution_status,
        decision_kind=decision_kind,
        decision_evaluation_gross=decision_gross,
        no_change_counterfactual_edge_ex_funding=no_change_edge,
        incremental_edge_vs_no_change=incremental_vs_no_change,
        incremental_edge_vs_no_change_frac=(
            incremental_vs_no_change / decision_gross
            if incremental_vs_no_change is not None and decision_gross
            else None
        ),
    )


_TAGS: dict[str, tuple[str, ...]] = {
    "concentration": ("concentrat", "single-name", "single name", "dominates"),
    "hallucination": (
        "hallucinat",
        "unverifiab",
        "invented",
        "fabricat",
        "unsourced",
        "cannot verify",
        "couldn't verify",
        "could not verify",
    ),
    "under_deployment": (
        "under-deploy",
        "underdeploy",
        "under deployed",
        "sits under",
        "deploy to ~",
    ),
    "tilt": ("tilt", "directional"),
    "beta": ("beta",),
    "crowded": ("crowded", "low-conviction", "low conviction"),
}


def classify_objections(objections: list[str]) -> list[str]:
    """Deterministic keyword -> reason-tag map over adversary objection
    strings."""
    tags: list[str] = []
    for text in objections:
        low = text.lower()
        for tag, kws in _TAGS.items():
            if tag not in tags and any(kw in low for kw in kws):
                tags.append(tag)
    if objections and not tags:
        tags.append("other")
    return tags


class ScoreRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Rows written before this discriminator existed intentionally parse as ``None`` so they can
    # be inspected by the one-time audited migration. They are never accepted as current learning
    # evidence merely because Pydantic supplied newer fields at their defaults.
    score_schema_version: Literal[CURRENT_SCORE_SCHEMA_VERSION] | None = None
    cycle: int
    btc_symbol: str = "BTC/USDT:USDT"
    scored_at: str = ""
    evaluation_horizon_hours: float = 0.0
    outcome_marks_sha256: str = ""
    outcome_observation_cycle: int | None = None
    outcome_scoring_marks_sha256: str = ""
    outcome_provenance: Literal["legacy_unverified", "manifest_bound"] = "legacy_unverified"
    n_symbols: int = 0
    specialist_return_label: Literal["legacy_raw", "btc_beta_adjusted"] = "legacy_raw"
    specialists: dict[str, SpecialistScore] = Field(default_factory=dict)
    book: BookScore = Field(default_factory=BookScore)
    decision_book_sha256: str = ""
    decision_reads_sha256: str = ""
    entry_gate_policy_sha256: str = ""
    candidate_opportunities: list[CandidateOpportunityScore] = Field(default_factory=list)
    adv_accepted: bool = True
    adv_revised: bool = False
    adv_reason_tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_outcome_identity(self):
        """A verified label must name a structurally possible committed observation.

        This is only the schema half of the provenance check. Consumers that use a row for
        calibration also rebind these fields to the state completion manifest and packet content.
        """
        if self.outcome_provenance != "manifest_bound":
            return self
        if self.cycle < 1:
            raise ValueError("manifest-bound score cycle must be positive")
        if self.outcome_observation_cycle is None or self.outcome_observation_cycle <= self.cycle:
            raise ValueError("manifest-bound score requires a later observation cycle")
        for field, value in (
            ("outcome_marks_sha256", self.outcome_marks_sha256),
            ("outcome_scoring_marks_sha256", self.outcome_scoring_marks_sha256),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{field} must be a lowercase SHA-256 digest")
        if not self.scored_at:
            raise ValueError("manifest-bound score requires scored_at")
        if not self.btc_symbol:
            raise ValueError("manifest-bound score requires btc_symbol")
        return self


def _reject_duplicate_score_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate score JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_score_constant(value: str) -> None:
    raise ValueError(f"non-finite score JSON number: {value}")


def _assert_finite_score_json(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite score JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _assert_finite_score_json(child)
    elif isinstance(value, list):
        for child in value:
            _assert_finite_score_json(child)


def parse_score_record_json(value: str) -> ScoreRecord:
    """Parse one score row without ambiguous duplicate/non-finite JSON semantics."""
    try:
        raw = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_score_pairs,
            parse_constant=_reject_nonfinite_score_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("malformed score JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("score JSON must be an object")
    _assert_finite_score_json(raw)
    if raw.get("score_schema_version") is not None and type(
        raw["score_schema_version"]
    ) is not int:
        raise ValueError("score_schema_version must be an integer")
    return ScoreRecord.model_validate(raw, strict=True)


class Recurrence(BaseModel):
    kind: str
    role: str
    count: int
    window: int
    evidence: list[str] = Field(default_factory=list)
    suggestion: str = ""


_SPECIALIST_ROLES = ("sentiment", "technical", "futures")


def realized_edge_frac(book: BookScore) -> float | None:
    """Return the comparable realized edge for new and prior strategy-aware score rows."""
    if book.realized_alpha_edge_ex_funding_frac is not None:
        return book.realized_alpha_edge_ex_funding_frac
    if book.realized_edge_ex_funding_frac is not None:
        return book.realized_edge_ex_funding_frac
    # Rows written by the first performance-aware implementation have strategy_net_frac but no
    # explicit realized field. Recover their price/friction component without counting forecast
    # funding. Truly legacy price-only rows remain ineligible for PM/Adversary calibration.
    if book.strategy_net_frac is None or book.gross_notional <= 0.0:
        return None
    return book.alpha_frac - book.entry_friction / book.gross_notional


def pm_decision_edge_frac(book: BookScore) -> float | None:
    """Comparable PM decision value, including an exit's avoided no-change outcome.

    A cash hold with no declared/shadow alternative has no denominator and remains unscored. It
    must not be promoted to a profitable zero. Entry starvation is surfaced separately.
    """
    if book.decision_kind == "exit_to_cash":
        return book.incremental_edge_vs_no_change_frac
    if book.decision_kind in {"deployed", "hedge_only"} or book.n_legs > 0:
        return realized_edge_frac(book)
    return None


def _specialist_available_count(record: ScoreRecord, role: str) -> int:
    """Return actual scoreable coverage, with legacy fallback only for legacy labels.

    In a beta-adjusted row, ``n_available=0`` means that role supplied no scoreable reads (for
    example, a failed specialist emitted ``[]``). Falling back to the desk universe would falsely
    call that disciplined abstention/inactivity and tune a role that produced no evidence.
    """
    score = record.specialists[role]
    if record.specialist_return_label == "btc_beta_adjusted":
        return score.n_available
    return score.n_available or record.n_symbols


def detect_recurrences(
    records: list[ScoreRecord], *, k: int = K_DEFAULT, window: int = WINDOW_DEFAULT
) -> list[Recurrence]:
    """Emit a Recurrence for each pattern true in >=k of the last `window`
    scored records."""
    recent = records[-window:]
    w = len(recent)
    if w < k:
        return []
    out: list[Recurrence] = []

    for role in _SPECIALIST_ROLES:
        # Historical specialist rows were scored on raw coin direction. They are not comparable
        # with the desk's market-neutral objective and must never tune a specialist after the
        # beta-adjusted label migration. PM/Adversary book scores were already beta-adjusted and
        # continue to use the ordinary trailing record window below.
        role_recent = [r for r in records if r.specialist_return_label == "btc_beta_adjusted"][
            -window:
        ]
        role_window = len(role_recent)
        if role_window < k:
            continue
        bad = [
            r
            for r in role_recent
            if role in r.specialists
            and r.specialists[role].n_scored > 0
            and r.specialists[role].conv_weighted_edge < 0
        ]
        if len(bad) >= k:
            out.append(
                Recurrence(
                    kind="specialist_miscalibrated",
                    role=role,
                    count=len(bad),
                    window=role_window,
                    evidence=[
                        f"c{r.cycle}: edge={r.specialists[role].conv_weighted_edge:+.4f} "
                        f"hit={r.specialists[role].hit_rate:.2f}"
                        for r in bad
                    ],
                    suggestion=(
                        f"{role}'s beta-adjusted conviction-weighted calls lost money in "
                        f"{len(bad)}/{role_window} "
                        "cycles; tighten conviction discipline / demand stronger evidence."
                    ),
                )
            )
        oc = [
            r
            for r in role_recent
            if role in r.specialists
            and r.specialists[role].hi_n > 0
            and r.specialists[role].hi_conv_hit_rate < 0.5
        ]
        if len(oc) >= k:
            out.append(
                Recurrence(
                    kind="specialist_overconviction",
                    role=role,
                    count=len(oc),
                    window=role_window,
                    evidence=[
                        f"c{r.cycle}: hi_conv_hit={r.specialists[role].hi_conv_hit_rate:.2f} "
                        f"(n={r.specialists[role].hi_n})"
                        for r in oc
                    ],
                    suggestion=(
                        f"{role}'s high-conviction (>0.6) calls were wrong more often than not "
                        f"in {len(oc)}/{role_window} cycles; raise the bar for high conviction."
                    ),
                )
            )

        # All-flat output can be disciplined abstention, but persistent total inactivity means a
        # role is no longer contributing candidates or generating measurable feedback. Surface it
        # for calibration; deterministic code does not force a call or choose a side.
        inactive = [
            r
            for r in role_recent
            if role in r.specialists
            and _specialist_available_count(r, role) > 0
            and r.specialists[role].n_scored == 0
        ]
        inactivity_threshold = max(k, (2 * role_window + 2) // 3)
        if len(inactive) >= inactivity_threshold:
            out.append(
                Recurrence(
                    kind="specialist_inactive",
                    role=role,
                    count=len(inactive),
                    window=role_window,
                    evidence=[
                        f"c{r.cycle}: calls=0/{_specialist_available_count(r, role)}"
                        for r in inactive
                    ],
                    suggestion=(
                        f"{role} made no directional call in {len(inactive)}/{role_window} cycles; "
                        "inspect "
                        "whether an active calibration note has made the role inert. Relax only "
                        "performance-calibration gates supported by this recurrence, never "
                        "evidence quality, anti-hallucination, liquidity, neutrality, or "
                        "PAPER-only rules."
                    ),
                )
            )

        # A clean recent streak is positive evidence too. It lets the Reflector retire a stale
        # performance-calibration note once its own retire condition has actually been observed.
        recovered = role_recent[-k:]
        if len(recovered) == k and all(
            role in r.specialists and r.specialists[role].n_scored > 0 for r in recovered
        ):
            n_calls = sum(r.specialists[role].n_scored for r in recovered)
            wins = sum(
                r.specialists[role].hit_rate * r.specialists[role].n_scored for r in recovered
            )
            edge = (
                sum(
                    r.specialists[role].conv_weighted_edge * r.specialists[role].n_scored
                    for r in recovered
                )
                / n_calls
            )
            hit_rate = wins / n_calls
            if edge >= 0.0 and hit_rate >= 0.5:
                out.append(
                    Recurrence(
                        kind="specialist_recovered",
                        role=role,
                        count=k,
                        window=role_window,
                        evidence=[
                            f"c{r.cycle}: edge={r.specialists[role].conv_weighted_edge:+.4f} "
                            f"hit={r.specialists[role].hit_rate:.2f} "
                            f"n={r.specialists[role].n_scored}"
                            for r in recovered
                        ],
                        suggestion=(
                            f"{role}'s last {k} active cycles recovered to aggregate "
                            f"edge={edge:+.4f} and hit={hit_rate:.2f}; retire or relax only a "
                            "performance-calibration note whose stated recovery condition is "
                            "satisfied."
                        ),
                    )
                )

    # The PM is a carry/low-churn allocator, not a one-cycle directional forecaster. Only scores
    # produced under a strategy-aware contract are eligible. Adaptation uses realized
    # beta-adjusted forward price edge less actual entry friction; projected funding is disclosed
    # separately and never used as a realized profitability label.
    neg = [
        r
        for r in recent
        if pm_decision_edge_frac(r.book) is not None
        and pm_decision_edge_frac(r.book) < PM_NET_EDGE_FRAC
    ]
    if len(neg) >= k:
        out.append(
            Recurrence(
                kind="pm_negative_net_edge",
                role="pm",
                count=len(neg),
                window=w,
                evidence=[
                    f"c{r.cycle}: decision_kind={r.book.decision_kind} "
                    f"pm_decision_edge_frac={pm_decision_edge_frac(r.book):+.4f} "
                    f"forecast_funding=${r.book.projected_funding_pnl:+.2f} "
                    f"friction=${r.book.entry_friction:.2f}"
                    for r in neg
                ],
                suggestion=(
                    f"the PM decision's realized beta-adjusted forward value versus cash/no-change "
                    f"less entry friction "
                    f"lost more than {abs(PM_NET_EDGE_FRAC):.2%} of gross in "
                    f"{len(neg)}/{w} cycles; reconsider seat selection without forcing churn."
                ),
            )
        )

    pm_recovered = [
        r
        for r in recent[-k:]
        if r.book.n_legs > 0
        and realized_edge_frac(r.book) is not None
        and realized_edge_frac(r.book) >= 0.0
    ]
    if len(pm_recovered) == k:
        out.append(
            Recurrence(
                kind="pm_recovered",
                role="pm",
                count=k,
                window=w,
                evidence=[
                    f"c{r.cycle}: realized_edge_ex_funding_frac={realized_edge_frac(r.book):+.4f}"
                    for r in pm_recovered
                ],
                suggestion=(
                    f"the PM recorded non-negative realized beta-adjusted price edge after entry "
                    f"friction in each "
                    f"of its last {k} scored cycles; retire or relax only an active performance "
                    "calibration note whose stated recovery condition is satisfied."
                ),
            )
        )

    tag_cycles: dict[str, list[int]] = {}
    for r in recent:
        if not r.adv_accepted:
            for t in r.adv_reason_tags:
                tag_cycles.setdefault(t, []).append(r.cycle)
    for tag, cyc in tag_cycles.items():
        if len(cyc) >= k:
            out.append(
                Recurrence(
                    kind="pm_rejected_same_reason",
                    role="pm",
                    count=len(cyc),
                    window=w,
                    evidence=[f"rejected for '{tag}' in cycles {cyc}"],
                    suggestion=(
                        f"the adversary rejected the PM for '{tag}' in {len(cyc)}/{w} cycles; "
                        "bake the fix into the PM's construction rules."
                    ),
                )
            )

    lax = [
        r
        for r in recent
        if r.adv_accepted
        and not r.adv_revised
        and r.book.n_legs > 0
        and realized_edge_frac(r.book) is not None
        and realized_edge_frac(r.book) < LAX_NET_EDGE_FRAC
    ]
    if len(lax) >= k:
        out.append(
            Recurrence(
                kind="adversary_too_lax",
                role="adversary",
                count=len(lax),
                window=w,
                evidence=[
                    f"c{r.cycle}: accepted, realized_edge_ex_funding_frac="
                    f"{realized_edge_frac(r.book):+.4f}"
                    for r in lax
                ],
                suggestion=(
                    f"the adversary accepted books that then lost on realized beta-adjusted price "
                    f"edge after entry friction (< "
                    f"{LAX_NET_EDGE_FRAC:+.3f}) in {len(lax)}/{w} cycles; sharpen its risk "
                    "scrutiny."
                ),
            )
        )
    return out
