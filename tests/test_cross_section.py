from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from futures_fund.cross_section import (
    AllocationProposal,
    DailyAssetRow,
    WeightPacket,
    WeightRevisionConstraints,
    build_allocation_precheck,
    build_weekly_snapshot,
    funding_adjusted_return,
    market_history_metrics,
    packet_sha256,
    validate_allocation,
    validate_policy_binding,
    validate_revision_constraints,
)

NOW = datetime(2026, 9, 7, 0, 7, tzinfo=UTC)
DAY_MS = 86_400_000


def _daily_rows(days: int = 182, *, start_price: float = 100.0, daily_volume: float = 1e9):
    boundary = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    first = int(boundary.timestamp() * 1000) - (days - 1) * DAY_MS
    rows = []
    for index in range(days):
        price = start_price + index
        open_ms = first + index * DAY_MS
        rows.append(
            [
                open_ms,
                str(price),
                str(price + 1),
                str(price - 1),
                str(price + 0.5),
                "10",
                open_ms + DAY_MS - 1,
                str(daily_volume),
            ]
        )
    return rows


def _funding_events(start: datetime, end: datetime, *, rate: float = 0.0001):
    events = []
    cursor = start + timedelta(hours=8)
    while cursor <= end:
        events.append({"timestamp": cursor, "rate": rate, "mark": 100.0})
        cursor += timedelta(hours=8)
    return events


def test_market_history_uses_completed_quote_volume_and_price_boundaries():
    metrics = market_history_metrics(
        "A/USDT:USDT",
        _daily_rows(),
        as_of_boundary=NOW,
        volume_lookback_days=180,
        performance_lookback_days=7,
    )
    assert metrics["quote_volume_180d_usd"] == 180e9
    assert metrics["average_daily_quote_volume_usd"] == 1e9
    assert metrics["end_price_7d"] > metrics["start_price_7d"]


def test_funding_adjusted_weekly_return_uses_settlement_marks():
    end = NOW.replace(minute=0)
    start = end - timedelta(days=7)
    funding, total, count = funding_adjusted_return(
        symbol="A/USDT:USDT",
        start_price=100.0,
        price_return=0.10,
        events=_funding_events(start, end),
        start=start,
        end=end,
    )
    assert count == 21
    assert funding == pytest.approx(0.0021)
    assert total == pytest.approx(0.0979)


def test_funding_adjusted_return_rejects_incomplete_week():
    end = NOW.replace(minute=0)
    start = end - timedelta(days=7)
    events = _funding_events(start, end)
    del events[4]
    with pytest.raises(ValueError, match="gap above eight hours"):
        funding_adjusted_return(
            symbol="A/USDT:USDT",
            start_price=100.0,
            price_return=0.10,
            events=events,
            start=start,
            end=end,
        )


def test_weekly_snapshot_selects_exact_top_and_bottom_ten():
    boundary = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    start = boundary - timedelta(days=7)
    metrics = []
    funding = {}
    for index in range(50):
        symbol = f"S{index:02d}/USDT:USDT"
        metrics.append(
            {
                "symbol": symbol,
                "quote_volume_180d_usd": float(1000 - index),
                "average_daily_quote_volume_usd": float(1000 - index) / 180.0,
                "start_price_7d": 100.0,
                "end_price_7d": 100.0 + index,
                "price_return_7d": index / 100.0,
            }
        )
        funding[symbol] = _funding_events(start, boundary, rate=0.0)
    snapshot = build_weekly_snapshot(
        metrics,
        funding,
        now=NOW,
        universe_size=50,
        sleeve_size=10,
        volume_lookback_days=180,
        performance_lookback_days=7,
        candle_audit={"source": "binance-proxy", "all_fresh": True, "request_count": 50},
    )
    assert snapshot.long_symbols == [f"S{index:02d}/USDT:USDT" for index in range(49, 39, -1)]
    assert snapshot.short_symbols == [f"S{index:02d}/USDT:USDT" for index in range(9, -1, -1)]


def _packet() -> WeightPacket:
    assets = []
    for side, offset in (("long", 0), ("short", 10)):
        for index in range(10):
            assets.append(
                DailyAssetRow(
                    symbol=f"S{offset + index:02d}/USDT:USDT",
                    side=side,
                    liquidity_rank=offset + index + 1,
                    performance_rank=offset + index + 1,
                    weekly_total_return=(10 - index) / 100.0,
                    weekly_price_return=(10 - index) / 100.0,
                    weekly_funding_return_long=0.0,
                    average_daily_quote_volume_usd=1e8,
                    mark=1.0,
                    return_24h=0.0,
                    return_72h=0.0,
                    return_168h=0.0,
                    realized_vol_annualized=0.5,
                    beta_btc=1.0,
                    current_funding_rate=0.0001,
                    funding_interval_hours=8,
                    current_notional_signed=0.0,
                    current_sleeve_weight=0.0,
                    unrealized_pnl=0.0,
                )
            )
    return WeightPacket(
        cycle=55,
        decision_ts=NOW,
        week_id="2026-W37",
        weekly_snapshot_sha256="a" * 64,
        equity=20_000.0,
        gross_target_frac=1.0,
        sleeve_target_usd=10_000.0,
        min_sleeve_weight=0.02,
        max_sleeve_weight=0.18,
        assets=assets,
        risk_pairs=[],
        performance={},
        selection_rule="fixed",
        agent_mandate="weights only",
    )


def _allocation(packet: WeightPacket, *, role: str = "alpha_allocator"):
    return AllocationProposal(
        cycle=packet.cycle,
        role=role,
        packet_sha256=packet_sha256(packet),
        weights=[
            {"symbol": row.symbol, "side": row.side, "weight": 0.1}
            for row in packet.assets
        ],
        rationale="Equal risk starting point.",
    )


def test_allocation_validation_forces_all_twenty_and_dollar_neutral_precheck():
    packet = _packet()
    allocation = _allocation(packet)
    validate_allocation(allocation, packet, expected_role="alpha_allocator")
    precheck = build_allocation_precheck(allocation, packet)
    assert precheck["all_bounds_ok"] is True
    assert precheck["gross_target_usd"] == 20_000.0
    assert precheck["long_target_usd"] == 10_000.0
    assert precheck["short_target_usd"] == 10_000.0
    assert precheck["dollar_residual_frac"] == 0.0
    assert precheck["turnover_frac_equity"] == 1.0


def test_precheck_is_stable_across_python_hash_seeds(tmp_path):
    packet = _packet()
    packet.equity = 18_601.919050774355
    packet.sleeve_target_usd = packet.equity / 2.0
    allocation = _allocation(packet)
    sleeve_weights = [0.02, 0.04, 0.06, 0.08, 0.09, 0.10, 0.11, 0.14, 0.18, 0.18]
    for side in ("long", "short"):
        side_rows = [row for row in allocation.weights if row.side == side]
        for row, weight in zip(side_rows, sleeve_weights, strict=True):
            row.weight = weight
    packet_path = tmp_path / "packet.json"
    allocation_path = tmp_path / "allocation.json"
    packet_path.write_text(packet.model_dump_json())
    allocation_path.write_text(allocation.model_dump_json())
    program = (
        "import json,sys; "
        "from pathlib import Path; "
        "from futures_fund.cross_section import AllocationProposal,WeightPacket,"
        "build_allocation_precheck; "
        "p=WeightPacket.model_validate_json(Path(sys.argv[1]).read_text()); "
        "a=AllocationProposal.model_validate_json(Path(sys.argv[2]).read_text()); "
        "print(json.dumps(build_allocation_precheck(a,p),sort_keys=True))"
    )
    outputs = set()
    for seed in range(1, 9):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = str(seed)
        outputs.add(
            subprocess.run(  # noqa: S603
                [sys.executable, "-c", program, str(packet_path), str(allocation_path)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout
        )
    assert len(outputs) == 1
    result = json.loads(outputs.pop())
    assert result["turnover_usd"] == packet.equity
    assert result["turnover_frac_equity"] == 1.0


def test_allocation_cannot_drop_a_symbol_or_break_a_sleeve_sum():
    packet = _packet()
    allocation = _allocation(packet)
    allocation.weights.pop()
    with pytest.raises(ValueError, match="cover every fixed"):
        validate_allocation(allocation, packet)

    allocation = _allocation(packet)
    allocation.weights[0].weight = 0.11
    with pytest.raises(ValueError, match="sum to one"):
        validate_allocation(allocation, packet)


def test_adversary_revision_caps_are_machine_bound():
    packet = _packet()
    revision = _allocation(packet, role="pm_revision")
    constraints = WeightRevisionConstraints(
        max_weight_by_symbol={revision.weights[0].symbol: 0.09},
        instruction="Reduce the first name.",
    )
    with pytest.raises(ValueError, match="violates adversary symbol caps"):
        validate_revision_constraints(revision, packet, constraints)


def test_policy_binding_rejects_a_self_consistent_but_wrong_sleeve_policy():
    boundary = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    start = boundary - timedelta(days=7)
    metrics = []
    funding = {}
    for index in range(50):
        symbol = f"S{index:02d}/USDT:USDT"
        metrics.append(
            {
                "symbol": symbol,
                "quote_volume_180d_usd": float(1000 - index),
                "average_daily_quote_volume_usd": float(1000 - index) / 180.0,
                "start_price_7d": 100.0,
                "end_price_7d": 100.0 + index,
                "price_return_7d": index / 100.0,
            }
        )
        funding[symbol] = _funding_events(start, boundary, rate=0.0)
    snapshot = build_weekly_snapshot(
        metrics,
        funding,
        now=NOW,
        universe_size=50,
        sleeve_size=10,
        volume_lookback_days=180,
        performance_lookback_days=7,
        candle_audit={"source": "binance-proxy", "all_fresh": True, "request_count": 50},
    )
    packet = _packet()
    packet.week_id = snapshot.week_id
    packet.weekly_snapshot_sha256 = "b" * 64
    expected_sides = {
        **dict.fromkeys(snapshot.long_symbols, "long"),
        **dict.fromkeys(snapshot.short_symbols, "short"),
    }
    for row, (symbol, side) in zip(packet.assets, expected_sides.items(), strict=True):
        row.symbol = symbol
        row.side = side
    with pytest.raises(ValueError, match="configured sleeve sizes"):
        validate_policy_binding(
            snapshot,
            packet,
            universe_size=50,
            sleeve_size=9,
            volume_lookback_days=180,
            performance_lookback_days=7,
            gross_target_frac=1.0,
            min_sleeve_weight=0.02,
            max_sleeve_weight=0.18,
        )
