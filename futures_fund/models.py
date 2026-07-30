from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

__all__ = ["get_args"]

Direction = Literal["long", "short"]
RegimeQuadrant = Literal[
    "low_vol_trend", "high_vol_trend", "low_vol_range", "high_vol_range", "transition"
]
HealthTier = Literal["healthy", "caution", "stressed"]
Bias = Literal["normal", "reduce", "flat"]
Verdict = Literal["approve", "resize", "veto"]

# --- new shared aliases (CANONICAL CONTRACT §0) ---
# `discretionary` is the 8h-redesign agent-agency sleeve: out-of-band LLM proposals (fail-soft,
# point-in-time) enter the book as a budget-CAPPED sleeve alongside the four systematic sleeves.
SleeveName = Literal["carry", "pairs", "factor", "sentiment", "discretionary"]
SentimentLevel = Literal[
    "very_positive", "positive", "neutral", "negative", "very_negative"
]
SpreadState = Literal["flat", "long_spread", "short_spread", "stop"]
PairTestMethod = Literal["engle_granger", "johansen"]
Cadence = Literal["weekly", "daily", "rebal"]


class MmrBracket(BaseModel):
    notional_floor: float
    notional_cap: float
    mmr: float                      # maintenance margin rate
    maint_amount: float             # maintenance amount offset (cum)
    max_leverage: float


class SymbolSpec(BaseModel):
    symbol: str
    tick_size: float
    step_size: float
    min_notional: float
    mmr_brackets: list[MmrBracket]

    @property
    def sorted_brackets(self) -> list[MmrBracket]:
        return sorted(self.mmr_brackets, key=lambda b: b.notional_floor)


class TradeProposal(BaseModel):
    symbol: str
    direction: Direction
    entry: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    atr: float
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_hours: float = Field(gt=0)
    funding_rate: float             # current/predicted per-interval funding rate
    funding_interval_hours: float = Field(default=8.0, gt=0)
    risk_mult: float = 1.0

    @model_validator(mode="after")
    def _check_stop_side(self) -> TradeProposal:
        if self.direction == "long" and self.stop >= self.entry:
            raise ValueError("long stop must be below entry")
        if self.direction == "short" and self.stop <= self.entry:
            raise ValueError("short stop must be above entry")
        return self

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)


class CostEstimate(BaseModel):
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return self.entry_fee + self.exit_fee + self.funding + self.slippage


class SizedTrade(BaseModel):
    proposal: TradeProposal
    qty: float
    notional: float
    leverage: float
    margin: float
    liq_price: float
    cost: CostEstimate


class RegimeState(BaseModel):
    quadrant: RegimeQuadrant
    trend_direction: Literal["up", "down", "neutral"] = "neutral"
    hurst: float = 0.5


class PortfolioHealth(BaseModel):
    equity: float
    peak_equity: float
    open_heat: float = 0.0
    recent_hit_rate: float = 0.5

    @property
    def drawdown_from_peak(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def tier(self) -> HealthTier:
        dd = self.drawdown_from_peak
        if dd >= 0.40:
            return "stressed"
        if dd >= 0.20:
            return "caution"
        return "healthy"


class RiskCaps(BaseModel):
    max_leverage: float
    per_trade_risk_pct: float
    max_heat: float
    bias: Bias


class RiskDecision(BaseModel):
    verdict: Verdict
    reason: str
    sized_trade: SizedTrade | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_sized(self) -> RiskDecision:
        if self.verdict in ("approve", "resize") and self.sized_trade is None:
            raise ValueError(f"verdict '{self.verdict}' requires a sized_trade")
        return self
