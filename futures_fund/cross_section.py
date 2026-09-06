"""Deterministic weekly cross-section and compact GPT weight contracts.

Symbols and sides are data decisions: the 50 most-traded eligible USD-M crypto perpetuals over
180 completed UTC days are ranked by seven-day price return net of realized long funding.  GPT
agents may choose only the weights of the fixed top-10 long and bottom-10 short sleeves.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from futures_fund.durable_io import canonical_json_sha256

Side = Literal["long", "short"]
AllocatorRole = Literal["alpha_allocator", "risk_allocator", "pm", "pm_revision"]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def utc_day_boundary(now: datetime) -> datetime:
    value = _utc(now)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def iso_week_id(now: datetime) -> str:
    iso = _utc(now).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


class WeeklyMarketRow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str = Field(min_length=1)
    liquidity_rank: int = Field(ge=1)
    quote_volume_180d_usd: float = Field(gt=0.0)
    average_daily_quote_volume_usd: float = Field(gt=0.0)
    start_price_7d: float = Field(gt=0.0)
    end_price_7d: float = Field(gt=0.0)
    price_return_7d: float
    long_funding_return_7d: float
    long_total_return_7d: float
    performance_rank: int = Field(ge=1)
    funding_events_7d: int = Field(ge=1)


class WeeklyUniverseSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    paper_only: Literal[True] = True
    week_id: str = Field(pattern=r"^\d{4}-W\d{2}$")
    created_at: datetime
    as_of_boundary: datetime
    volume_lookback_days: int = Field(ge=1)
    performance_lookback_days: int = Field(ge=1)
    ranking_definition: str = Field(min_length=1)
    markets: list[WeeklyMarketRow]
    long_symbols: list[str]
    short_symbols: list[str]
    candle_audit: dict

    @model_validator(mode="after")
    def validate_selection(self) -> WeeklyUniverseSnapshot:
        if self.created_at.tzinfo is None or self.as_of_boundary.tzinfo is None:
            raise ValueError("weekly snapshot timestamps must be timezone-aware")
        if self.week_id != iso_week_id(self.created_at):
            raise ValueError("weekly snapshot week does not match its creation timestamp")
        if self.as_of_boundary != utc_day_boundary(self.created_at):
            raise ValueError("weekly snapshot boundary does not match its creation day")
        if (
            self.candle_audit.get("source") != "binance-proxy"
            or self.candle_audit.get("all_fresh") is not True
            or int(self.candle_audit.get("request_count", -1)) <= 0
        ):
            raise ValueError("weekly snapshot lacks a complete proxy candle audit")
        symbols = [row.symbol for row in self.markets]
        if len(symbols) != len(set(symbols)):
            raise ValueError("weekly markets contain duplicate symbols")
        if [row.liquidity_rank for row in self.markets] != list(
            range(1, len(self.markets) + 1)
        ):
            raise ValueError("weekly markets are not in exact liquidity-rank order")
        performance = sorted(
            self.markets,
            key=lambda row: (-row.long_total_return_7d, row.symbol),
        )
        expected_rank = {row.symbol: rank for rank, row in enumerate(performance, start=1)}
        if any(row.performance_rank != expected_rank[row.symbol] for row in self.markets):
            raise ValueError("weekly performance ranks are inconsistent")
        sleeve_size = len(self.long_symbols)
        if sleeve_size == 0 or len(self.short_symbols) != sleeve_size:
            raise ValueError("weekly sleeves must be non-empty and equal-sized")
        if len(self.markets) < 2 * sleeve_size:
            raise ValueError("weekly universe is too small for disjoint sleeves")
        expected_longs = [row.symbol for row in performance[:sleeve_size]]
        expected_shorts = [row.symbol for row in performance[-sleeve_size:]]
        if self.long_symbols != expected_longs or self.short_symbols != expected_shorts:
            raise ValueError("weekly sleeves do not match deterministic performance ranks")
        if set(self.long_symbols) & set(self.short_symbols):
            raise ValueError("weekly long and short sleeves overlap")
        return self


class DailyAssetRow(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    side: Side
    liquidity_rank: int = Field(ge=1)
    performance_rank: int = Field(ge=1)
    weekly_total_return: float
    weekly_price_return: float
    weekly_funding_return_long: float
    average_daily_quote_volume_usd: float = Field(gt=0.0)
    mark: float = Field(gt=0.0)
    return_24h: float
    return_72h: float
    return_168h: float
    realized_vol_annualized: float = Field(ge=0.0)
    beta_btc: float
    current_funding_rate: float
    funding_interval_hours: int
    current_notional_signed: float
    current_sleeve_weight: float = Field(ge=0.0)
    unrealized_pnl: float


class RiskPair(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    left: str
    right: str
    asset_return_correlation: float = Field(ge=-1.0, le=1.0)
    position_pnl_correlation: float = Field(ge=-1.0, le=1.0)


class WeightPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    paper_only: Literal[True] = True
    cycle: int = Field(ge=1)
    decision_ts: datetime
    week_id: str = Field(pattern=r"^\d{4}-W\d{2}$")
    weekly_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    equity: float = Field(gt=0.0)
    gross_target_frac: float = Field(gt=0.0)
    sleeve_target_usd: float = Field(gt=0.0)
    min_sleeve_weight: float = Field(ge=0.0)
    max_sleeve_weight: float = Field(gt=0.0)
    assets: list[DailyAssetRow]
    risk_pairs: list[RiskPair]
    held_outside_selection: list[dict] = Field(default_factory=list)
    performance: dict
    selection_rule: str
    agent_mandate: str

    @model_validator(mode="after")
    def validate_packet(self) -> WeightPacket:
        if self.decision_ts.tzinfo is None:
            raise ValueError("decision timestamp must be timezone-aware")
        keys = [(row.symbol, row.side) for row in self.assets]
        if len(keys) != len(set(keys)):
            raise ValueError("weight packet contains duplicate assets")
        longs = [row for row in self.assets if row.side == "long"]
        shorts = [row for row in self.assets if row.side == "short"]
        if len(longs) != len(shorts) or not longs:
            raise ValueError("weight packet sleeves must be non-empty and equal-sized")
        if self.min_sleeve_weight * len(longs) > 1.0 + 1e-12:
            raise ValueError("minimum sleeve weight makes allocation impossible")
        if self.max_sleeve_weight * len(longs) < 1.0 - 1e-12:
            raise ValueError("maximum sleeve weight makes allocation impossible")
        expected_sleeve = self.equity * self.gross_target_frac / 2.0
        if not math.isclose(self.sleeve_target_usd, expected_sleeve, abs_tol=0.01):
            raise ValueError("packet sleeve target does not match equity and gross target")
        selected = {row.symbol for row in self.assets}
        for pair in self.risk_pairs:
            if pair.left not in selected or pair.right not in selected or pair.left >= pair.right:
                raise ValueError("risk pair is not a canonical selected-symbol pair")
        return self


class AllocationWeight(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    side: Side
    weight: float = Field(gt=0.0, lt=1.0)


class AllocationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    cycle: int = Field(ge=1)
    role: AllocatorRole
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_proposal_sha256: dict[str, str] = Field(default_factory=dict)
    weights: list[AllocationWeight]
    rationale: str = Field(min_length=1, max_length=2000)
    disagreements_resolved: list[str] = Field(default_factory=list, max_length=10)


class WeightRevisionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    max_weight_by_symbol: dict[str, float] = Field(default_factory=dict)
    max_turnover_frac_equity: float | None = Field(default=None, ge=0.0, le=2.0)
    instruction: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_caps(self) -> WeightRevisionConstraints:
        if any(
            not math.isfinite(float(value)) or not 0.0 < float(value) < 1.0
            for value in self.max_weight_by_symbol.values()
        ):
            raise ValueError("revision symbol caps must be finite values in (0, 1)")
        return self


class AllocationAdversary(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    schema_version: Literal[1] = 1
    cycle: int = Field(ge=1)
    packet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allocation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    precheck_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accept: bool
    objections: list[str] = Field(default_factory=list, max_length=10)
    revision_constraints: WeightRevisionConstraints | None = None
    rationale: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_verdict(self) -> AllocationAdversary:
        if self.accept and (self.objections or self.revision_constraints is not None):
            raise ValueError("accepted allocation cannot carry objections or revision constraints")
        if not self.accept and (not self.objections or self.revision_constraints is None):
            raise ValueError("rejected allocation requires objections and revision constraints")
        return self


def market_history_metrics(
    symbol: str,
    rows: list[list],
    *,
    as_of_boundary: datetime,
    volume_lookback_days: int,
    performance_lookback_days: int,
) -> dict:
    """Compute exact completed-day volume and price metrics from full Binance klines."""
    boundary = utc_day_boundary(as_of_boundary)
    boundary_ms = int(boundary.timestamp() * 1000)
    if not rows or int(rows[-1][0]) != boundary_ms:
        raise ValueError(f"{symbol}: daily series lacks current boundary")
    completed = rows[:-1]
    required = max(volume_lookback_days, performance_lookback_days) + 1
    if len(completed) < required:
        raise ValueError(f"{symbol}: insufficient completed daily history")
    start_price = float(completed[-performance_lookback_days - 1][4])
    end_price = float(completed[-1][4])
    volume_rows = completed[-volume_lookback_days:]
    quote_volume = sum(float(row[7]) for row in volume_rows)
    values = (start_price, end_price, quote_volume)
    if not all(math.isfinite(value) for value in values) or min(values) <= 0.0:
        raise ValueError(f"{symbol}: invalid price or quote-volume history")
    return {
        "symbol": symbol,
        "quote_volume_180d_usd": quote_volume,
        "average_daily_quote_volume_usd": quote_volume / volume_lookback_days,
        "start_price_7d": start_price,
        "end_price_7d": end_price,
        "price_return_7d": end_price / start_price - 1.0,
    }


def funding_adjusted_return(
    *,
    symbol: str,
    start_price: float,
    price_return: float,
    events: list[dict],
    start: datetime,
    end: datetime,
) -> tuple[float, float, int]:
    """Return long funding drag and total return over a fully covered weekly window.

    Funding is normalized to the initial notional using each event's actual settlement mark:
    ``sum(rate * settlement_mark / start_price)``. Positive rates are paid by a long.
    """
    start_utc, end_utc = _utc(start), _utc(end)
    selected: list[tuple[datetime, float, float]] = []
    for event in events:
        timestamp = event.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if not isinstance(timestamp, datetime):
            raise ValueError(f"{symbol}: funding event lacks timestamp")
        timestamp = _utc(timestamp)
        if start_utc < timestamp <= end_utc:
            rate, mark = float(event["rate"]), float(event["mark"])
            if not math.isfinite(rate) or not math.isfinite(mark) or mark <= 0.0:
                raise ValueError(f"{symbol}: invalid funding event")
            selected.append((timestamp, rate, mark))
    selected.sort()
    timestamps = [row[0] for row in selected]
    if not timestamps or len(timestamps) != len(set(timestamps)):
        raise ValueError(f"{symbol}: empty or duplicate weekly funding history")
    # Every USD-M funding schedule is at most eight hours. This coverage proof admits adaptive
    # 1/2/4h schedules while detecting an omitted boundary without assuming today's interval
    # governed the entire week.
    coverage = [start_utc, *timestamps, end_utc]
    if any(
        later - earlier > timedelta(hours=8, seconds=1)
        for earlier, later in zip(coverage, coverage[1:], strict=False)
    ):
        raise ValueError(f"{symbol}: weekly funding history has a gap above eight hours")
    funding_return = sum(rate * mark / start_price for _ts, rate, mark in selected)
    total = price_return - funding_return
    if not math.isfinite(funding_return) or not math.isfinite(total):
        raise ValueError(f"{symbol}: non-finite funding-adjusted return")
    return funding_return, total, len(selected)


def build_weekly_snapshot(
    market_metrics: list[dict],
    funding_events: dict[str, list[dict]],
    *,
    now: datetime,
    universe_size: int,
    sleeve_size: int,
    volume_lookback_days: int,
    performance_lookback_days: int,
    candle_audit: dict,
) -> WeeklyUniverseSnapshot:
    """Rank liquidity first, then deterministic funding-adjusted weekly performance."""
    if len({str(row["symbol"]) for row in market_metrics}) != len(market_metrics):
        raise ValueError("duplicate market metrics")
    liquid = sorted(
        market_metrics,
        key=lambda row: (-float(row["quote_volume_180d_usd"]), str(row["symbol"])),
    )[:universe_size]
    if len(liquid) != universe_size:
        raise ValueError(f"eligible history produced {len(liquid)} markets, need {universe_size}")
    boundary = utc_day_boundary(now)
    start = boundary - timedelta(days=performance_lookback_days)
    scored: list[dict] = []
    for liquidity_rank, raw in enumerate(liquid, start=1):
        symbol = str(raw["symbol"])
        funding_return, total_return, count = funding_adjusted_return(
            symbol=symbol,
            start_price=float(raw["start_price_7d"]),
            price_return=float(raw["price_return_7d"]),
            events=funding_events.get(symbol, []),
            start=start,
            end=boundary,
        )
        scored.append(
            {
                **raw,
                "liquidity_rank": liquidity_rank,
                "long_funding_return_7d": funding_return,
                "long_total_return_7d": total_return,
                "funding_events_7d": count,
            }
        )
    performance = sorted(
        scored,
        key=lambda row: (-float(row["long_total_return_7d"]), str(row["symbol"])),
    )
    rank_by_symbol = {
        str(row["symbol"]): rank for rank, row in enumerate(performance, start=1)
    }
    markets = [
        WeeklyMarketRow(**row, performance_rank=rank_by_symbol[str(row["symbol"])])
        for row in scored
    ]
    return WeeklyUniverseSnapshot(
        week_id=iso_week_id(now),
        created_at=_utc(now),
        as_of_boundary=boundary,
        volume_lookback_days=volume_lookback_days,
        performance_lookback_days=performance_lookback_days,
        ranking_definition=(
            "top liquidity by trailing completed-day Binance quote volume; performance is "
            "seven-day close return minus actual long funding normalized by settlement mark"
        ),
        markets=markets,
        long_symbols=[str(row["symbol"]) for row in performance[:sleeve_size]],
        short_symbols=[str(row["symbol"]) for row in performance[-sleeve_size:]],
        candle_audit=candle_audit,
    )


def allocation_sha256(allocation: AllocationProposal) -> str:
    return canonical_json_sha256(allocation.model_dump(mode="json"))


def packet_sha256(packet: WeightPacket) -> str:
    return canonical_json_sha256(packet.model_dump(mode="json"))


def validate_policy_binding(
    snapshot: WeeklyUniverseSnapshot,
    packet: WeightPacket,
    *,
    universe_size: int,
    sleeve_size: int,
    volume_lookback_days: int,
    performance_lookback_days: int,
    gross_target_frac: float,
    min_sleeve_weight: float,
    max_sleeve_weight: float,
) -> None:
    """Bind persisted weekly/daily artifacts to the configured strategy, not just themselves."""
    if snapshot.week_id != packet.week_id or packet.week_id != iso_week_id(packet.decision_ts):
        raise ValueError("weekly snapshot and packet are not in the decision ISO week")
    if len(snapshot.markets) != universe_size:
        raise ValueError("weekly snapshot does not contain the configured universe size")
    if len(snapshot.long_symbols) != sleeve_size or len(snapshot.short_symbols) != sleeve_size:
        raise ValueError("weekly snapshot does not contain the configured sleeve sizes")
    if (
        snapshot.volume_lookback_days != volume_lookback_days
        or snapshot.performance_lookback_days != performance_lookback_days
    ):
        raise ValueError("weekly snapshot lookbacks differ from configured policy")
    if len(packet.assets) != 2 * sleeve_size:
        raise ValueError("weight packet does not contain the configured 10/10 selection")
    expected_sides = {
        **dict.fromkeys(snapshot.long_symbols, "long"),
        **dict.fromkeys(snapshot.short_symbols, "short"),
    }
    if {row.symbol: row.side for row in packet.assets} != expected_sides:
        raise ValueError("weight packet symbols/sides differ from the frozen weekly selection")
    if not all(
        math.isclose(observed, expected, abs_tol=1e-12)
        for observed, expected in (
            (packet.gross_target_frac, gross_target_frac),
            (packet.min_sleeve_weight, min_sleeve_weight),
            (packet.max_sleeve_weight, max_sleeve_weight),
        )
    ):
        raise ValueError("weight packet sizing policy differs from configured policy")


def validate_allocation(
    allocation: AllocationProposal,
    packet: WeightPacket,
    *,
    expected_role: AllocatorRole | None = None,
) -> dict:
    """Bind an agent's weights to the exact fixed weekly symbols, sides, and risk envelope."""
    digest = packet_sha256(packet)
    if allocation.cycle != packet.cycle or allocation.packet_sha256 != digest:
        raise ValueError("allocation is not bound to this cycle's weight packet")
    if expected_role is not None and allocation.role != expected_role:
        raise ValueError(f"expected {expected_role}, got {allocation.role}")
    expected = {(row.symbol, row.side) for row in packet.assets}
    actual = [(row.symbol, row.side) for row in allocation.weights]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("allocation must cover every fixed symbol/side exactly once")
    by_side = {
        side: sum(row.weight for row in allocation.weights if row.side == side)
        for side in ("long", "short")
    }
    if any(not math.isclose(value, 1.0, abs_tol=1e-6) for value in by_side.values()):
        raise ValueError(f"allocation sleeve weights must each sum to one: {by_side}")
    outside = [
        row.symbol
        for row in allocation.weights
        if row.weight < packet.min_sleeve_weight - 1e-12
        or row.weight > packet.max_sleeve_weight + 1e-12
    ]
    if outside:
        raise ValueError(f"allocation weights outside bounds: {sorted(outside)}")
    return {
        "long_weight_sum": by_side["long"],
        "short_weight_sum": by_side["short"],
        "min_weight": min(row.weight for row in allocation.weights),
        "max_weight": max(row.weight for row in allocation.weights),
    }


def build_allocation_precheck(
    allocation: AllocationProposal,
    packet: WeightPacket,
) -> dict:
    """Compute transparent sizing, turnover, and deterministic structural bounds."""
    summary = validate_allocation(allocation, packet)
    asset_by_key = {(row.symbol, row.side): row for row in packet.assets}
    targets: list[dict] = []
    target_signed: dict[str, float] = {}
    for weight in allocation.weights:
        target = weight.weight * packet.sleeve_target_usd
        signed = target if weight.side == "long" else -target
        target_signed[weight.symbol] = signed
        asset = asset_by_key[(weight.symbol, weight.side)]
        targets.append(
            {
                "symbol": weight.symbol,
                "side": weight.side,
                "sleeve_weight": weight.weight,
                "target_notional_usd": target,
                "weekly_total_return": asset.weekly_total_return,
            }
        )
    current = {row.symbol: row.current_notional_signed for row in packet.assets}
    for row in packet.held_outside_selection:
        current[str(row["symbol"])] = float(row["current_notional_signed"])
    turnover = sum(
        abs(target_signed.get(symbol, 0.0) - current.get(symbol, 0.0))
        for symbol in set(target_signed) | set(current)
    )
    long_usd = sum(max(value, 0.0) for value in target_signed.values())
    short_usd = sum(max(-value, 0.0) for value in target_signed.values())
    gross = long_usd + short_usd
    dollar_residual = abs(long_usd - short_usd) / gross if gross else math.inf
    performance_edge = sum(
        row.weight
        * (
            asset_by_key[(row.symbol, row.side)].weekly_total_return
            if row.side == "long"
            else -asset_by_key[(row.symbol, row.side)].weekly_total_return
        )
        for row in allocation.weights
    ) / 2.0
    bounds = [
        {"id": "W1", "description": "fixed 10/10 symbol-side coverage", "ok": True},
        {
            "id": "W2",
            "description": "each sleeve weight sum equals one",
            "value": summary,
            "ok": True,
        },
        {
            "id": "W3",
            "description": "all weights inside configured min/max",
            "value": [packet.min_sleeve_weight, packet.max_sleeve_weight],
            "ok": True,
        },
        {
            "id": "W4",
            "description": "target gross equals configured fraction of equity",
            "value": gross / packet.equity,
            "limit": packet.gross_target_frac,
            "ok": math.isclose(gross / packet.equity, packet.gross_target_frac, abs_tol=1e-6),
        },
        {
            "id": "W5",
            "description": "dollar residual",
            "value": dollar_residual,
            "limit": 1e-6,
            "ok": dollar_residual <= 1e-6,
        },
    ]
    result = {
        "schema_version": 1,
        "paper_only": True,
        "cycle": packet.cycle,
        "packet_sha256": packet_sha256(packet),
        "allocation_sha256": allocation_sha256(allocation),
        "equity": packet.equity,
        "gross_target_usd": gross,
        "long_target_usd": long_usd,
        "short_target_usd": short_usd,
        "dollar_residual_frac": dollar_residual,
        "turnover_usd": turnover,
        "turnover_frac_equity": turnover / packet.equity,
        "weighted_weekly_long_short_edge": performance_edge,
        "targets": sorted(targets, key=lambda row: (row["side"], row["symbol"])),
        "bounds": bounds,
        "all_bounds_ok": all(row["ok"] for row in bounds),
    }
    result["sha256"] = canonical_json_sha256(result)
    return result


def validate_revision_constraints(
    revised: AllocationProposal,
    packet: WeightPacket,
    constraints: WeightRevisionConstraints,
) -> None:
    validate_allocation(revised, packet, expected_role="pm_revision")
    by_symbol = {row.symbol: row.weight for row in revised.weights}
    unknown = sorted(set(constraints.max_weight_by_symbol) - set(by_symbol))
    if unknown:
        raise ValueError(f"revision constraint names unknown symbols: {unknown}")
    violations = {
        symbol: {"weight": by_symbol[symbol], "cap": cap}
        for symbol, cap in constraints.max_weight_by_symbol.items()
        if by_symbol[symbol] > float(cap) + 1e-12
    }
    if violations:
        raise ValueError(f"PM revision violates adversary symbol caps: {violations}")
    if constraints.max_turnover_frac_equity is not None:
        precheck = build_allocation_precheck(revised, packet)
        if precheck["turnover_frac_equity"] > constraints.max_turnover_frac_equity + 1e-12:
            raise ValueError("PM revision violates adversary turnover cap")


def compact_risk_pairs(
    closes: dict[str, list[float]],
    side_by_symbol: dict[str, Side],
    *,
    limit: int,
) -> list[RiskPair]:
    """Return only the largest position-PnL correlations, avoiding a 20x20 prompt matrix."""
    rows: list[RiskPair] = []
    symbols = sorted(side_by_symbol)
    for left_index, left in enumerate(symbols):
        for right in symbols[left_index + 1 :]:
            left_values = np.asarray(closes[left], dtype=float)
            right_values = np.asarray(closes[right], dtype=float)
            n = min(len(left_values), len(right_values))
            if n < 3:
                continue
            left_returns = np.diff(np.log(left_values[-n:]))
            right_returns = np.diff(np.log(right_values[-n:]))
            if np.std(left_returns) <= 0.0 or np.std(right_returns) <= 0.0:
                correlation = 0.0
            else:
                correlation = float(np.corrcoef(left_returns, right_returns)[0, 1])
            if not math.isfinite(correlation):
                correlation = 0.0
            sign = 1.0 if side_by_symbol[left] == side_by_symbol[right] else -1.0
            rows.append(
                RiskPair(
                    left=left,
                    right=right,
                    asset_return_correlation=correlation,
                    position_pnl_correlation=correlation * sign,
                )
            )
    rows.sort(key=lambda row: (-row.position_pnl_correlation, row.left, row.right))
    return rows[:limit]
