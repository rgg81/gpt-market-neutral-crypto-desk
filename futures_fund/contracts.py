from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from futures_fund.models import (
    Cadence,
    Direction,
    PairTestMethod,
    SentimentLevel,
    SleeveName,
    SpreadState,
    SymbolSpec,
)

# --- analyst-roster type aliases (Phase 4; adapted from the weekly desk's contracts) ---
Lean = Literal["long", "short", "watch"]      # Universe Scout candidate lean
Stance = Literal["bullish", "bearish", "neutral"]   # analyst read direction
# The Research Manager's five-tier verdict ladder (judge of the Bull/Bear debate). `strong_*`
# requires confluent analysts AND a decisively defeated opponent; `flat` = no trade flows.
Rating = Literal["strong_long", "long", "flat", "short", "strong_short"]
# A lesson's lifecycle (Reflector mints `candidate`; the eval harness promotes to `validated`).
LessonState = Literal["candidate", "validated", "retired"]
# A lesson's directional pull: `restrictive` = a brake (do NOT / cut / avoid); `enabling` = an
# accelerator (DO take / size when X); `process` = neutral discipline. The retrieval quota keeps
# the injected set two-sided so a losing record can't flood every debate with prohibitions.
Polarity = Literal["restrictive", "enabling", "process"]


class Candidate(BaseModel):
    """One symbol the Universe Scout nominates for deeper analysis. Adapted from the weekly
    `Candidate`: a triage lean + score, never a sized trade."""
    symbol: str                                   # ccxt unified symbol, e.g. BTC/USDT:USDT
    lean: Lean
    rationale: str = ""
    score: float = Field(ge=0.0, le=1.0)          # triage priority, NOT a probability of profit
    correlation_group: str | None = None          # e.g. "majors", "alt-l1"; null = stands alone


class WatcherOutput(BaseModel):
    """The Universe Scout's bundle: a two-sided shortlist of candidates."""
    candidates: list[Candidate] = Field(default_factory=list)


class AnalystReport(BaseModel):
    """One analyst's per-symbol read for the market-neutral desk. Adapted from the weekly
    `AnalystReport`, but on the desk's field set (`stance/conviction/thesis/signals/horizon`).
    `extra="allow"` so each analyst can attach its own structured `signals` keys (e.g. the Pair
    researcher's `hedge_ratio`/`adf_pvalue`, the Carry desk's `signed_funding`/`funding_interval_h`)
    while the shared envelope stays validated."""
    model_config = ConfigDict(extra="allow")
    symbol: str                                   # ccxt unified id, or a pair_id for the Pair desk
    stance: Stance                                # the READ direction (both sides co-equal)
    conviction: float = Field(ge=0.0, le=1.0)     # how strongly the evidence backs the stance
    thesis: str = ""                              # one-paragraph rationale citing the signals
    signals: dict = Field(default_factory=dict)   # the computed evidence (analyst-specific keys)
    horizon: str = ""                             # intended hold horizon, e.g. "weekly", "1-3 days"


class ResearchPlan(BaseModel):
    """The Research Manager's verdict for one symbol (or relative-value leg): a five-tier rating
    plus a falsifiable prediction the Reflector grades later. Ported from the weekly desk's
    `ResearchPlan`. The RM does NOT size — `rating` sets only direction/conviction; `flat` means
    no trade flows to the Trader."""
    symbol: str                                   # ccxt unified id, or a pair_id for a pair leg
    rating: Rating                                # one of the five tiers (judge of the debate)
    confidence: float = Field(ge=0.0, le=1.0)     # how decisively the debate resolved
    thesis: str                                   # why this side won the debate, in this regime
    falsifiable_prediction: str                   # concrete claim + horizon + explicit invalidation


class SentimentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")  # strict-by-default (canonical contract PART 1)
    url: str
    published_ts: datetime          # MUST be < owning report's as_of_ts (point-in-time)
    title: str = ""
    feed: str = ""                  # "news_rss" | "reddit" | "fear_greed" | "media"


class SentimentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")  # strict-by-default (canonical contract PART 1)
    symbol: str                     # ccxt unified id, or "MARKET" for the market-wide read
    level: SentimentLevel
    s: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[SentimentSource] = Field(default_factory=list)
    rationale: str = ""
    as_of_ts: datetime              # decision-time anchor; all sources must precede this
    decayed_s: float | None = None  # s after half-life decay toward 0 (filled by ingest)


class SentimentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # strict-by-default (canonical contract PART 1)
    reports: list[SentimentReport] = Field(default_factory=list)


class DiscretionaryView(BaseModel):
    """One out-of-band agent conviction for the 8h-redesign `discretionary` sleeve (agent agency).

    The deterministic loop invokes NO in-loop LLM, so a discretionary agent proposes OUT OF BAND and
    persists a `DiscretionaryBatch`; the loop reads it fail-soft + point-in-time (see
    `selection_cache`). A view is a signed conviction on ONE symbol — never a size or an order (the
    optimizer sizes + neutralizes it, capped to the discretionary risk budget). `as_of_ts` is the
    decision anchor; a view at/after the cycle's `now` is dropped (no look-ahead)."""
    model_config = ConfigDict(extra="forbid")  # strict-by-default (canonical contract PART 1)
    symbol: str
    direction: Direction
    conviction: float = Field(ge=0.0, le=1.0)   # strength in [0,1]; age-decays toward 0
    rationale: str = ""
    as_of_ts: datetime


class DiscretionaryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")  # strict-by-default (canonical contract PART 1)
    views: list[DiscretionaryView] = Field(default_factory=list)


class CoinGeometry(BaseModel):
    symbol: str
    mark: float
    # momentum / vol / beta
    momentum_20: float = 0.0
    realized_vol: float = 0.0
    beta_btc: float = 1.0
    beta_lookback_days: int = 45
    # carry
    funding_rate: float = 0.0
    funding_interval_hours: float = 8.0
    funding_apr: float = 0.0
    funding_cap: float = 0.02
    # cointegration state
    in_pair: bool = False
    pair_id: str | None = None
    # sentiment (first-class)
    sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    sentiment_conf: float = Field(default=0.0, ge=0.0, le=1.0)
    # liquidity / filters
    adv_usd: float = 0.0
    chg_24h_pct: float = 0.0            # 24h % change carried from the universe row (audit/filter)
    onboard_date: int | None = None    # Binance onboardDate, ms-epoch (None when unavailable)
    # Phase 10 depth-aware slippage: the two crossing sides of the live L2 book at build time.
    # `depth_asks` is the crossing side for a BUY (delta>0), `depth_bids` for a SELL (delta<0).
    # Empty lists (not None) when depth was unavailable -> estimate_slippage uses the ADV fallback.
    depth_bids: list[tuple[float, float]] = Field(default_factory=list)
    depth_asks: list[tuple[float, float]] = Field(default_factory=list)
    spec: SymbolSpec | None = None
    # crypto-only universe audit: the exchange `market["info"]` (carries `underlyingType` /
    # `contractType`) the reviewer feeds to `market_data.is_crypto_perp` to reject TradFi-wrapper
    # perps (tokenized stocks / commodities / indices). None => no metadata (treated as crypto).
    market_info: dict | None = None


class GeometryBundle(BaseModel):
    geometries: list[CoinGeometry] = Field(default_factory=list)
    as_of_ts: datetime


class SleeveTilt(BaseModel):
    symbol: str
    direction: Direction
    target_weight: float
    raw_score: float = 0.0
    pair_id: str | None = None


class SleeveSignal(BaseModel):
    sleeve: SleeveName
    tilts: list[SleeveTilt] = Field(default_factory=list)
    risk_budget_frac: float = Field(default=0.0, ge=0.0, le=1.0)
    # Optional per-sleeve budget CEILING honored by `neutrality.risk_parity_budgets`: when the
    # risk-parity split would hand this sleeve MORE than `budget_cap`, the excess is redistributed
    # to the uncapped sleeves. The 8h-redesign discretionary (agent-agency) sleeve uses it to start
    # at a small budget (~10-15%) and graduate up. None => uncapped (the default for the systematic
    # sleeves — no behaviour change).
    budget_cap: float | None = Field(default=None, ge=0.0, le=1.0)
    diagnostics: dict = Field(default_factory=dict)
    as_of_ts: datetime


class WeightLeg(BaseModel):
    symbol: str
    direction: Direction
    weight: float
    target_notional: float
    beta_btc: float
    sleeve: SleeveName | Literal["hedge"]
    pair_id: str | None = None


class TargetWeights(BaseModel):
    legs: list[WeightLeg] = Field(default_factory=list)
    btc_hedge_notional: float = 0.0
    # neutrality residuals
    dollar_residual: float
    dollar_residual_frac: float
    beta_residual: float
    # deployment per side. gross_long/short are leg-level |notional| sums (a BTC alpha leg and the
    # BTC hedge counted separately). deploy_long/short_frac are NET-BY-SYMBOL (deploy-net-retarget):
    # the BTC alpha leg + hedge are consolidated into one held position, so the fraction is the
    # economically-HELD deployment the >=90%/side floor binds on — not the gross sum that
    # double-counts an offsetting BTC overlap. gross can exceed the target by that netted overlap.
    gross_long: float
    gross_short: float
    deploy_long_frac: float
    deploy_short_frac: float
    gross_notional: float
    turnover_l1: float = 0.0
    feasible: bool = True
    notes: list[str] = Field(default_factory=list)
    as_of_ts: datetime


class Pair(BaseModel):
    pair_id: str                                  # canonical slash-free id, e.g. "BTCUSDT__ETHUSDT"
    symbol_y: str                                 # dependent leg (ccxt unified id)
    symbol_x: str                                 # independent / hedge leg (ccxt unified id)
    hedge_ratio: float                            # spread = y - hedge_ratio*x
    method: PairTestMethod
    adf_pvalue: float                             # Engle-Granger ADF p (info when johansen)
    adf_pvalue_adj: float | None = None           # FDR/Bonferroni-corrected p
    johansen_trace_stat: float | None = None
    johansen_crit_95: float | None = None
    half_life: float                              # OU half-life in CYCLES (ln2/theta)
    theta: float                                  # OU mean-reversion speed
    mu: float                                     # OU long-run spread mean
    sigma_eq: float                               # OU equilibrium stdev of the spread
    formed_cycle: int
    cointegrated: bool = True                     # rolling re-test result


class Spread(BaseModel):
    pair_id: str
    spread_value: float                           # y - hedge_ratio*x at current marks
    zscore: float                                 # (spread_value - mu) / sigma_eq
    state: SpreadState
    entry_z: float = 2.0
    exit_z: float = 0.0
    stop_z: float = 3.0
    qty_y: float = 0.0
    qty_x: float = 0.0
    notional_y: float = 0.0
    notional_x: float = 0.0
    realized_pnl: float = 0.0                     # attributed at pair level


class AgentProposal(BaseModel):
    """One gate-ready per-leg order the Trader emits from a `TargetWeights` leg. The Trader does NO
    sizing — notional comes from the optimizer — so this is a pure entry/stop/TP envelope. Field
    names are reused verbatim by the `trader.json` conformance fixture."""
    symbol: str                                   # ccxt unified id, e.g. BTC/USDT:USDT
    direction: Direction
    entry: float
    stop: float
    take_profit: float
    rationale: str = ""
    trigger_type: Literal["market", "limit", "stop"] = "market"

    @model_validator(mode="after")
    def _check_stop_tp_side(self) -> AgentProposal:
        # Mirror TradeProposal._check_stop_side: stop is always on the loss side of entry,
        # take_profit on the gain side. A malformed gate-ready order must NOT validate.
        if self.direction == "long":
            if self.stop >= self.entry:
                raise ValueError("long stop must be below entry")
            if self.take_profit <= self.entry:
                raise ValueError("long take_profit must be above entry")
        else:  # short
            if self.stop <= self.entry:
                raise ValueError("short stop must be above entry")
            if self.take_profit >= self.entry:
                raise ValueError("short take_profit must be below entry")
        return self


class TraderOutput(BaseModel):
    """The Trader/Execution planner's bundle: gate-ready opens + management + triggers. Mirrors
    the weekly `ScalperOutput`; an explicit empty `management` list is the stand-down contract."""
    proposals: list[AgentProposal] = Field(default_factory=list)
    management: list[dict] = Field(default_factory=list)
    triggers: list[dict] = Field(default_factory=list)
    cancel_triggers: list[dict] = Field(default_factory=list)


class Lesson(BaseModel):
    """One contrastive, actionable lesson the Reflector distills post-trade, keyed on alpha-vs-beta
    (§10) — never raw return. Ported from the weekly desk's `lessons.Lesson`. The Reflector mints
    `candidate` lessons in BOTH polarities so the corpus self-heals symmetrically (a losing record
    must not ratchet the desk into an all-`restrictive` never-trade state)."""
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime
    text: str                                     # the contrastive, actionable lesson
    regime: str | None = None                     # quadrant it applies to; None = all regimes
    symbol: str | None = None
    tags: list[str] = Field(default_factory=list)
    # neutral failure mode (§10): cointegration_break | carry_thesis_miss | neutrality_breach
    # | sentiment_detract; read by the lesson retrieval filter (Task 6.2).
    dimension: str | None = None
    importance: int = Field(default=5, ge=1, le=10)
    polarity: Polarity = "restrictive"            # restrictive | enabling | process
    state: LessonState = "candidate"              # Reflector proposes; eval harness promotes
    confirmations: int = 0
    provenance: list[str] = Field(default_factory=list)  # source journal decision id(s)


class ReviewerCheck(BaseModel):
    """One re-derived adversarial code/calc check (§10 Guardian, §12). The reviewer NEVER trusts
    an artifact's stated number — it recomputes `expected` from ground truth and compares it to
    the `actual` it found in the artifact, within `tolerance`. `name` is one of the canonical,
    verbatim check ids the gate keys off."""
    name: str                                     # canonical check id (see the canonical set)
    ok: bool
    expected: float | str | None = None           # reviewer's ground-truth re-derivation
    actual: float | str | None = None             # value found in the artifact under review
    tolerance: float = 1e-6
    detail: str = ""


class ReviewerVerdict(BaseModel):
    """Every-cycle reviewer verdict. `passed` is the AND of every canonical check and is the
    DETERMINISTIC flag `reviewer_gate_ok` reads; the execute step HALTs (`SystemExit(2)`) if it is
    absent or false (§10 mandatory non-skippable stage). `mismatches` is exactly the names of the
    failed checks (`[c.name for c in checks if not c.ok]`)."""
    passed: bool
    checks: list[ReviewerCheck] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)
    cycle: int
    cadence: Cadence
    reviewed_at: datetime
