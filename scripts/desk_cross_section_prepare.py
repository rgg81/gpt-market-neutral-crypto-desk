#!/usr/bin/env python3
"""Build the frozen weekly selection and compact daily weight packet."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from futures_fund.account import PaperAccount
from futures_fund.config import load_settings
from futures_fund.cross_section import (
    DailyAssetRow,
    WeeklyUniverseSnapshot,
    WeightPacket,
    build_weekly_snapshot,
    compact_risk_pairs,
    iso_week_id,
    market_history_metrics,
    packet_sha256,
    utc_day_boundary,
    validate_policy_binding,
)
from futures_fund.durable_io import canonical_json_sha256, durable_write_json
from futures_fund.exchange import FuturesExchange
from futures_fund.market_data import is_crypto_perp, parse_onboard_date_ms
from futures_fund.reconcile_commit import completed_cycle_numbers
from futures_fund.runtime_provenance import default_runtime_provenance
from futures_fund.state_transaction import load_account_with_sha256
from scripts.desk_watchdog import build_watchdog_receipt


def _eligible_symbols(exchange: FuturesExchange, *, oldest_onboard_ms: int) -> list[str]:
    markets = getattr(exchange.client, "markets", None) or {}
    selected: list[str] = []
    for symbol, market in markets.items():
        info = market.get("info") or {}
        onboard = parse_onboard_date_ms(market)
        if (
            not symbol.endswith("/USDT:USDT")
            or market.get("active") is False
            or market.get("swap") is False
            or market.get("linear") is False
            or str(market.get("settle") or "USDT").upper() != "USDT"
            or info.get("contractType") not in (None, "", "PERPETUAL")
            or not is_crypto_perp({**market, "symbol": symbol})
            or onboard is None
            or onboard > oldest_onboard_ms
        ):
            continue
        selected.append(symbol)
    return sorted(selected)


def _read_weekly_snapshot(path: Path, week_id: str) -> WeeklyUniverseSnapshot | None:
    if not path.is_file():
        return None
    snapshot = WeeklyUniverseSnapshot.model_validate_json(path.read_text())
    if snapshot.week_id != week_id:
        return None
    pointer_path = path.parent / "current.json"
    if not pointer_path.is_file():
        return None
    pointer = json.loads(pointer_path.read_text())
    if (
        pointer.get("week_id") != week_id
        or pointer.get("snapshot") != path.name
        or pointer.get("sha256")
        != canonical_json_sha256(snapshot.model_dump(mode="json"))
    ):
        raise RuntimeError("current weekly snapshot pointer/hash is inconsistent")
    return snapshot


def _write_weekly_snapshot(
    state_dir: Path,
    snapshot: WeeklyUniverseSnapshot,
) -> None:
    root = state_dir / "cross-section" / "weekly"
    target = root / f"{snapshot.week_id}.json"
    body = snapshot.model_dump(mode="json")
    if target.exists():
        existing = WeeklyUniverseSnapshot.model_validate_json(target.read_text())
        if existing.model_dump(mode="json") != body:
            raise RuntimeError(f"conflicting frozen weekly snapshot already exists: {target}")
    else:
        durable_write_json(target, body)
    durable_write_json(
        root / "current.json",
        {
            "schema_version": 1,
            "paper_only": True,
            "week_id": snapshot.week_id,
            "snapshot": target.name,
            "sha256": canonical_json_sha256(body),
        },
    )


def _build_or_load_weekly(
    exchange: FuturesExchange,
    *,
    state_dir: Path,
    now: datetime,
    settings,
) -> tuple[WeeklyUniverseSnapshot, bool, int]:
    policy = settings.cross_section
    week = iso_week_id(now)
    weekly_path = state_dir / "cross-section" / "weekly" / f"{week}.json"
    existing = _read_weekly_snapshot(weekly_path, week)
    if existing is not None:
        return existing, False, 0

    boundary = utc_day_boundary(now)
    oldest_onboard_ms = int(
        (boundary - timedelta(days=policy.volume_lookback_days + 1)).timestamp() * 1000
    )
    symbols = _eligible_symbols(exchange, oldest_onboard_ms=oldest_onboard_ms)
    if len(symbols) < policy.universe_size:
        raise RuntimeError(
            f"only {len(symbols)} eligible six-month crypto perpetuals; "
            f"need {policy.universe_size}"
        )
    request_limit = policy.volume_lookback_days + 2
    metrics = [
        market_history_metrics(
            symbol,
            exchange.klines(symbol, "1d", request_limit),
            as_of_boundary=boundary,
            volume_lookback_days=policy.volume_lookback_days,
            performance_lookback_days=policy.performance_lookback_days,
        )
        for symbol in symbols
    ]
    liquid = sorted(
        metrics,
        key=lambda row: (-float(row["quote_volume_180d_usd"]), str(row["symbol"])),
    )[: policy.universe_size]
    start = boundary - timedelta(days=policy.performance_lookback_days)
    funding_events = {
        row["symbol"]: exchange.funding_history(
            row["symbol"],
            since_ms=int(start.timestamp() * 1000) + 1,
            limit=1000,
        )
        for row in liquid
    }
    weekly_requests = len(exchange.candle_audit().get("requests", []))
    snapshot = build_weekly_snapshot(
        metrics,
        funding_events,
        now=now,
        universe_size=policy.universe_size,
        sleeve_size=policy.sleeve_size,
        volume_lookback_days=policy.volume_lookback_days,
        performance_lookback_days=policy.performance_lookback_days,
        candle_audit=exchange.candle_audit(),
    )
    _write_weekly_snapshot(state_dir, snapshot)
    return snapshot, True, weekly_requests


def _hourly_features(frame, *, now: datetime, symbol: str) -> tuple[dict, list[float]]:
    if frame.empty or "timestamp" not in frame or "close" not in frame:
        raise ValueError(f"{symbol}: empty hourly candle frame")
    timestamps = frame["timestamp"]
    current_boundary = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if timestamps.iloc[-1].to_pydatetime().astimezone(UTC) != current_boundary:
        raise ValueError(f"{symbol}: hourly candles lack currently-forming UTC candle")
    completed = frame.iloc[:-1]
    closes = completed["close"].astype(float).tolist()
    if len(closes) < 169 or any(not math.isfinite(value) or value <= 0.0 for value in closes):
        raise ValueError(f"{symbol}: insufficient valid completed hourly candles")

    def ret(hours: int) -> float:
        return closes[-1] / closes[-1 - hours] - 1.0

    log_returns = np.diff(np.log(np.asarray(closes[-169:], dtype=float)))
    vol = float(np.std(log_returns, ddof=0) * math.sqrt(24.0 * 365.0))
    return {
        "return_24h": ret(24),
        "return_72h": ret(72),
        "return_168h": ret(168),
        "realized_vol_annualized": vol,
    }, closes[-169:]


def _performance_summary(
    account: PaperAccount,
    equity: float,
    state_dir: Path,
    *,
    as_of: datetime,
    starting_capital: float,
) -> dict:
    ledger_path = state_dir / "ledger.jsonl"
    rows: list[dict] = []
    if ledger_path.is_file():
        rows = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
    closes = [float(row["closing_equity"]) for row in rows]
    peak = max([starting_capital, *closes, equity])
    cutoff = as_of.astimezone(UTC) - timedelta(days=7)
    recent = [
        row
        for row in rows
        if datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00")) >= cutoff
    ]
    start_7d = (
        float(recent[0]["prior_closing_equity"] or recent[0]["opening_equity"])
        if recent
        else equity
    )
    return {
        "equity": equity,
        "lifetime_net_pnl": equity - starting_capital,
        "lifetime_return": equity / starting_capital - 1.0,
        "current_drawdown": equity / peak - 1.0,
        "seven_day_return": equity / start_7d - 1.0 if start_7d > 0.0 else 0.0,
        "fees_paid": account.fees_paid,
        "slippage_paid": account.slippage_paid,
        "funding_net": account.funding_received - account.funding_paid,
        "recent_turnover_usd": sum(float(row.get("turnover_usd") or 0.0) for row in recent),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--memory-dir", default="live_memory")
    args = parser.parse_args(argv)

    settings = load_settings()
    now = datetime.now(UTC)
    state_dir = Path(args.state_dir)
    completed = completed_cycle_numbers(state_dir, cadence="rebal")
    cycle = (max(completed) if completed else 0) + 1
    watchdog = build_watchdog_receipt(str(state_dir), now=now)
    if int(watchdog["next_cycle"]) != cycle:
        raise RuntimeError("watchdog and completed-cycle allocator disagree")
    if watchdog["schedule_status"] in {"EARLY", "FUTURE_CLOCK", "UNKNOWN_LAST_TIMESTAMP"}:
        raise RuntimeError(f"watchdog requires stand-down: {watchdog['schedule_status']}")

    exchange = FuturesExchange.from_settings(settings)
    exchange.require_candle_proxy()
    snapshot, refreshed, weekly_request_count = _build_or_load_weekly(
        exchange,
        state_dir=state_dir,
        now=now,
        settings=settings,
    )
    selected_side = {
        **dict.fromkeys(snapshot.long_symbols, "long"),
        **dict.fromkeys(snapshot.short_symbols, "short"),
    }
    account, account_sha256 = load_account_with_sha256(
        state_dir,
        default_cash=settings.account_size_usdt,
    )
    held_extra = sorted(set(account.positions) - set(selected_side))
    market_symbols = sorted(set(selected_side) | set(account.positions))
    analytics_symbols = sorted(set(market_symbols) | {settings.btc_symbol})
    hourly: dict[str, dict] = {}
    closes: dict[str, list[float]] = {}
    market_state: dict[str, dict] = {}
    for symbol in analytics_symbols:
        features, close_values = _hourly_features(
            exchange.ohlcv(symbol, "1h", 170), now=now, symbol=symbol
        )
        hourly[symbol] = features
        closes[symbol] = close_values
    btc_returns = np.diff(np.log(np.asarray(closes[settings.btc_symbol], dtype=float)))
    btc_variance = float(np.var(btc_returns, ddof=0))
    if not math.isfinite(btc_variance) or btc_variance <= 0.0:
        raise ValueError("BTC hourly variance is unavailable for beta measurement")
    betas: dict[str, float] = {}
    for symbol in market_symbols:
        asset_returns = np.diff(np.log(np.asarray(closes[symbol], dtype=float)))
        beta = (
            1.0
            if symbol == settings.btc_symbol
            else float(np.cov(asset_returns, btc_returns, ddof=0)[0, 1] / btc_variance)
        )
        if not math.isfinite(beta):
            raise ValueError(f"non-finite BTC beta for {symbol}")
        betas[symbol] = max(-3.0, min(3.0, beta))
        funding = exchange.funding(symbol)
        raw_interval = float(funding.interval_hours)
        interval = int(raw_interval)
        if raw_interval != interval or interval not in {1, 2, 4, 8}:
            raise ValueError(
                f"unsupported current funding interval for {symbol}: {raw_interval}"
            )
        market_state[symbol] = {
            "mark": float(funding.mark_price),
            "funding_rate": float(funding.last_settled_rate),
            "funding_interval_hours": interval,
            "beta_btc": betas[symbol],
        }

    marks = {symbol: row["mark"] for symbol, row in market_state.items()}
    equity = account.equity(marks)
    if not math.isfinite(equity) or equity <= 0.0:
        raise RuntimeError("paper account equity is non-positive or non-finite")
    sleeve_target = equity * settings.cross_section.gross_target_frac / 2.0
    upnl = account.mark_to_market(marks)
    weekly_by_symbol = {row.symbol: row for row in snapshot.markets}
    assets: list[DailyAssetRow] = []
    for symbol, side in selected_side.items():
        position = account.positions.get(symbol)
        current_signed = 0.0
        if position is not None:
            current_signed = position.qty * marks[symbol]
            if position.direction == "short":
                current_signed = -current_signed
        weekly = weekly_by_symbol[symbol]
        assets.append(
            DailyAssetRow(
                symbol=symbol,
                side=side,
                liquidity_rank=weekly.liquidity_rank,
                performance_rank=weekly.performance_rank,
                weekly_total_return=weekly.long_total_return_7d,
                weekly_price_return=weekly.price_return_7d,
                weekly_funding_return_long=weekly.long_funding_return_7d,
                average_daily_quote_volume_usd=weekly.average_daily_quote_volume_usd,
                mark=marks[symbol],
                **hourly[symbol],
                beta_btc=betas[symbol],
                current_funding_rate=market_state[symbol]["funding_rate"],
                funding_interval_hours=market_state[symbol]["funding_interval_hours"],
                current_notional_signed=current_signed,
                current_sleeve_weight=abs(current_signed) / sleeve_target,
                unrealized_pnl=float(upnl.get(symbol, 0.0)),
            )
        )
    held_outside = [
        {
            "symbol": symbol,
            "side": account.positions[symbol].direction,
            "current_notional_signed": (
                account.positions[symbol].qty
                * marks[symbol]
                * (1.0 if account.positions[symbol].direction == "long" else -1.0)
            ),
            "unrealized_pnl": float(upnl.get(symbol, 0.0)),
        }
        for symbol in held_extra
    ]
    packet = WeightPacket(
        cycle=cycle,
        decision_ts=now,
        week_id=snapshot.week_id,
        weekly_snapshot_sha256=canonical_json_sha256(snapshot.model_dump(mode="json")),
        equity=equity,
        gross_target_frac=settings.cross_section.gross_target_frac,
        sleeve_target_usd=sleeve_target,
        min_sleeve_weight=settings.cross_section.min_sleeve_weight,
        max_sleeve_weight=settings.cross_section.max_sleeve_weight,
        assets=sorted(assets, key=lambda row: (row.side, row.performance_rank)),
        risk_pairs=compact_risk_pairs(
            {symbol: closes[symbol] for symbol in selected_side},
            selected_side,
            limit=settings.cross_section.risk_pair_count,
        ),
        held_outside_selection=held_outside,
        performance=_performance_summary(
            account,
            equity,
            state_dir,
            as_of=now,
            starting_capital=settings.account_size_usdt,
        ),
        selection_rule=(
            "symbols and sides are frozen for the ISO week: top-10 funding-adjusted weekly "
            "performers long, bottom-10 short from the trailing-180d-volume Top 50"
        ),
        agent_mandate=(
            "choose weights only; hold all 20 names; each sleeve sums to 1; dollar-neutral "
            "100% gross; prefer net risk-adjusted return after funding and turnover"
        ),
    )
    validate_policy_binding(
        snapshot,
        packet,
        universe_size=settings.cross_section.universe_size,
        sleeve_size=settings.cross_section.sleeve_size,
        volume_lookback_days=settings.cross_section.volume_lookback_days,
        performance_lookback_days=settings.cross_section.performance_lookback_days,
        gross_target_frac=settings.cross_section.gross_target_frac,
        min_sleeve_weight=settings.cross_section.min_sleeve_weight,
        max_sleeve_weight=settings.cross_section.max_sleeve_weight,
    )

    all_audit = exchange.candle_audit()
    daily_requests = all_audit.get("requests", [])[weekly_request_count:]
    daily_audit = {
        "source": "binance-proxy",
        "base_url": all_audit.get("base_url"),
        "all_fresh": bool(daily_requests)
        and all(row.get("current_candle_present") is True for row in daily_requests),
        "request_count": len(daily_requests),
        "requests": daily_requests,
    }
    if not daily_audit["all_fresh"] or len(daily_requests) != len(analytics_symbols):
        raise RuntimeError("daily selected/held candle audit is incomplete")
    market_packet = {
        "schema_version": 1,
        "paper_only": True,
        "cycle": cycle,
        "decision_ts": now.isoformat(),
        "account_sha256": account_sha256,
        "symbols": market_state,
        "daily_candle_audit": daily_audit,
    }
    provenance = default_runtime_provenance(captured_at=now)

    pending_root = Path(args.memory_dir) / "pending"
    pending = pending_root / str(cycle)
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True)
    snapshot_json = snapshot.model_dump(mode="json")
    packet_json = packet.model_dump(mode="json")
    durable_write_json(pending / "weekly_universe.json", snapshot_json)
    durable_write_json(pending / "weight_packet.json", packet_json)
    durable_write_json(pending / "market_state.json", market_packet)
    durable_write_json(pending / "runtime_provenance.json", provenance)
    meta = {
        "schema_version": 1,
        "paper_only": True,
        "design": "weekly_top50_cross_section_v1",
        "cycle": cycle,
        "now": now.isoformat(),
        "watchdog_receipt": watchdog,
        "weekly_refreshed": refreshed,
        "weekly_universe_sha256": canonical_json_sha256(snapshot_json),
        "weight_packet_sha256": packet_sha256(packet),
        "market_state_sha256": canonical_json_sha256(market_packet),
        "runtime_provenance_sha256": canonical_json_sha256(provenance),
    }
    durable_write_json(pending / "meta.json", meta)
    durable_write_json(
        pending_root / "current.json",
        {"cycle": cycle, "dir": str(pending.resolve()), "created": now.isoformat()},
    )
    print(
        json.dumps(
            {
                "cycle": cycle,
                "week_id": snapshot.week_id,
                "weekly_refreshed": refreshed,
                "longs": snapshot.long_symbols,
                "shorts": snapshot.short_symbols,
                "equity": round(equity, 2),
                "gross_target": round(2.0 * sleeve_target, 2),
                "daily_candle_requests": daily_audit["request_count"],
                "packet_sha256": packet_sha256(packet),
                "pending_dir": str(pending),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
