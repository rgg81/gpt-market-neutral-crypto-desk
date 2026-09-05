"""Cycle step 1 (deterministic): fetch the top-N-by-volume universe + build the evidence packs the
LLM specialist subagents read, and pick a FRESH cycle number.

    uv run python scripts/desk_evidence.py --state-dir live_state --memory-dir live_memory

Writes `<memory>/pending/<cycle>/evidence.json` + `meta.json` ({cycle, symbols, now}) into a
PER-CYCLE pending subdirectory, and updates the `<memory>/pending/current.json` pointer. The
per-cycle isolation is load-bearing (2026-07 review, Incident B): a flat pending/ let a slow
cycle-N-1 specialist's stale file pass validation for cycle N. Old cycle dirs are pruned keep-3.
Loose legacy files at the pending/ ROOT from the flat layout are removed so no consumer can
accidentally read them.

Universe hygiene (2026-07 review): the vol-ranked scan is routed through `quality_filter`
(age >= 30d, |24h chg| <= 25%, book depth >= $250K, ADV floor) so LAB-class names — crashed,
thin, 300-1000bps-to-exit — never reach the agents. Currently-HELD symbols are always unioned in
(bypassing the gates) so every held position gets a mark and can be truthfully closed. PAPER ONLY.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.account import load_account
from futures_fund.candle_proxy import BinanceCandleProxy
from futures_fund.config import load_settings
from futures_fund.cycle_io import cycle_dir
from futures_fund.evidence import build_evidence
from futures_fund.exchange import FuturesExchange, build_ccxt
from futures_fund.market_data import quality_filter, scan_universe
from futures_fund.performance import canonical_sha256
from futures_fund.reconcile_commit import completed_cycle_numbers
from futures_fund.reflection import (
    learning_origin_is_bound,
    read_forecast_scorecard,
    scored_cycles,
)
from futures_fund.runtime_provenance import (
    DECISION_START_PROVENANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT,
)
from scripts.desk_watchdog import build_watchdog_receipt

_KEEP_PENDING_DIRS = 3
# Legacy flat-layout filenames: remove from pending/ ROOT so stale copies can't be consumed.
_LEGACY_FILES = (
    "evidence.json", "meta.json", "recurrences.json", "recurrences.sha256", "reflection.json",
    "sentiment_reads.json", "technical_reads.json", "futures_reads.json",
    "pm_book.json", "pm_book_original.json", "adversary.json",
    "precheck.json", "precheck_original.json",
    "performance_snapshot.json", "performance_snapshot.sha256",
    "specialist_reads.sha256",
    "risk_model.json", "scoring_marks.json",
    DECISION_START_PROVENANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT,
    "binding_user_directive.md",
)


def _next_cycle(state_dir: str, cadence: str = "rebal") -> int:
    """Allocate from completed generations only; retry (never skip) an incomplete cycle."""
    root = Path(state_dir) / cadence / "cycle"
    completed = completed_cycle_numbers(state_dir, cadence=cadence)
    cycle = (max(completed) + 1) if completed else 1
    if root.exists():
        present = sorted(
            int(path.name) for path in root.iterdir()
            if path.is_dir() and path.name.isdigit()
        )
        incomplete = sorted(set(present) - set(completed))
        stranded = [number for number in incomplete if number < cycle]
        if stranded:
            raise RuntimeError(
                "completed cycle exists beyond an incomplete generation; manual audit required: "
                f"incomplete={stranded}, next_completed_allocator={cycle}"
            )
        future = sorted(
            number for number in present if number > cycle
        )
        if future:
            raise RuntimeError(
                f"state has cycle directories beyond incomplete cycle {cycle}: {future}"
            )
    return cycle


def _build_data_clients(settings):
    """Share public point-data reads; route every kline exclusively through the local proxy."""
    client = build_ccxt(settings)
    client.load_markets()
    return client, FuturesExchange(
        client,
        keyless=True,
        kline_proxy=BinanceCandleProxy.from_settings(settings),
    )


def _unscored_specialist_symbols(
    state_dir: str, memory_dir: str, cadence: str = "rebal"
) -> list[str]:
    """Return every unscored dispatched universe for token-free catch-up marks."""
    completed = completed_cycle_numbers(state_dir, cadence=cadence)
    if not completed:
        return []
    already_scored = scored_cycles(
        memory_dir, state_dir=state_dir, cadence=cadence
    )
    symbols: set[str] = set()
    for cycle in completed:
        if cycle in already_scored or not learning_origin_is_bound(
            state_dir, cycle, cadence=cadence
        ):
            continue
        path = cycle_dir(state_dir, cycle, cadence=cadence) / "reads.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text())
        symbols.update(
            str(row["symbol"])
            for role in ("sentiment", "technical", "futures")
            for row in raw.get(role, [])
            if isinstance(row, dict) and row.get("symbol")
        )
    return sorted(symbols)


def _pending_forecast_symbols(
    state_dir: str, memory_dir: str, cadence: str = "rebal"
) -> list[str]:
    """Keep every explicit unscored PM forecast marked through its declared maturity."""
    score_path = Path(memory_dir) / "forecast-scorecard.jsonl"
    seen = {
        (int(row["origin_cycle"]), str(row["symbol"]))
        for row in read_forecast_scorecard(score_path, state_dir=state_dir, cadence=cadence)
    }
    symbols: set[str] = set()
    for cycle in completed_cycle_numbers(state_dir, cadence=cadence):
        path = cycle_dir(state_dir, cycle, cadence=cadence) / "book.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text())
        for leg in raw.get("legs", []):
            if not isinstance(leg, dict):
                continue
            symbol = str(leg.get("symbol") or "")
            explicit = all(
                field in leg
                for field in ("seat_role", "expected_price_edge_frac", "edge_horizon_hours")
            )
            if explicit and leg.get("seat_role") == "alpha" and (cycle, symbol) not in seen:
                symbols.add(symbol)
    return sorted(symbols)


def _prune_pending(pending_root: Path, keep: int = _KEEP_PENDING_DIRS) -> None:
    dirs = sorted((p for p in pending_root.glob("*") if p.is_dir() and p.name.isdigit()),
                  key=lambda p: int(p.name))
    for stale in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(stale, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch universe + build evidence for one desk cycle.")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    ap.add_argument("--directive-path", default="ops/next-cycle-directive.md")
    args = ap.parse_args(argv)

    directive_path = Path(args.directive_path)
    directive_text: str | None = None
    if directive_path.exists():
        if not directive_path.is_file():
            raise RuntimeError(f"binding directive path is not a file: {directive_path}")
        directive_text = directive_path.read_text()
        if not directive_text.strip():
            raise RuntimeError(f"binding directive is empty: {directive_path}")

    settings = load_settings()
    now = datetime.now(UTC)
    cycle = _next_cycle(args.state_dir)
    watchdog_receipt = build_watchdog_receipt(args.state_dir, now=now)
    if int(watchdog_receipt["next_cycle"]) != cycle:
        raise RuntimeError(
            "watchdog/non-next cycle mismatch: "
            f"receipt={watchdog_receipt['next_cycle']}, allocator={cycle}"
        )
    schedule_status = str(watchdog_receipt["schedule_status"])
    if schedule_status in {"EARLY", "FUTURE_CLOCK", "UNKNOWN_LAST_TIMESTAMP"}:
        raise RuntimeError(f"watchdog requires stand-down before evidence: {schedule_status}")
    client, exchange = _build_data_clients(settings)
    exchange.require_candle_proxy()
    rows = scan_universe(client, top_n=settings.universe_top_n)
    uni = settings.universe
    kept, drops = quality_filter(
        rows, now=now, exchange=exchange,
        min_adv_usd=uni.min_adv_usd, min_age_days=uni.min_age_days,
        max_abs_chg_24h_pct=uni.max_abs_chg_24h_pct, min_depth_usd=uni.min_depth_usd,
        depth_ref_usd=uni.depth_ref_usd, symbol_count=uni.symbol_count)
    symbols = [r["symbol"] for r in kept]

    # Union in currently-HELD symbols (even if the gates dropped them): a held position must
    # always have a mark so it can be truthfully valued and closed (no entry-price fabrication).
    account = load_account(args.state_dir, default_cash=settings.account_size_usdt)
    held_extra = [s for s in account.positions if s not in symbols]
    symbols = symbols + held_extra

    prior_symbols = sorted(set(
        _unscored_specialist_symbols(args.state_dir, args.memory_dir)
        + _pending_forecast_symbols(args.state_dir, args.memory_dir)
    ))
    scoring_symbols = [
        symbol for symbol in prior_symbols
        if symbol not in set(symbols) and symbol != settings.btc_symbol
    ]
    agent_symbols = set([*symbols, settings.btc_symbol])
    fetch_symbols = [*symbols, *scoring_symbols]
    risk_model: dict = {}
    all_evidence = build_evidence(
        exchange,
        fetch_symbols,
        now=now,
        btc_symbol=settings.btc_symbol,
        risk_model_out=risk_model,
        risk_symbols=agent_symbols,
    )
    evidence = [pack for pack in all_evidence if pack.symbol in agent_symbols]
    scoring_marks = {
        "as_of_ts": now.isoformat(),
        "marks": {pack.symbol: float(pack.mark) for pack in all_evidence},
    }
    candle_audit = exchange.candle_audit()
    expected_candle_requests = [
        {
            "unified_symbol": symbol,
            "symbol": client.market(symbol)["id"],
            "timeframe": timeframe,
            "requested_limit": 200 if timeframe == "1h" else 60,
        }
        for symbol in dict.fromkeys([*fetch_symbols, settings.btc_symbol])
        for timeframe in ("1h", "1d")
    ]
    expected_candles = {
        (row["symbol"], row["timeframe"])
        for row in expected_candle_requests
    }
    observed_candles = {
        (row["symbol"], row["timeframe"])
        for row in candle_audit["requests"]
        if row["timeframe"] in {"1h", "1d"}
    }
    missing_candles = sorted(expected_candles - observed_candles)
    extra_candles = sorted(observed_candles - expected_candles)
    if (
        candle_audit.get("source") != "binance-proxy"
        or candle_audit.get("all_fresh") is not True
        or missing_candles
        or extra_candles
        or int(candle_audit.get("request_count", -1)) != len(expected_candle_requests)
    ):
        raise RuntimeError(
            "mandatory candle provenance/freshness check failed: "
            f"source={candle_audit.get('source')}, "
            f"all_fresh={candle_audit.get('all_fresh')}, missing={missing_candles}, "
            f"extra={extra_candles}"
        )

    # PM sizes against LIVE equity, not the static seed: on cycle 1 the account is fresh
    # (equity == account_size_usdt); from cycle 2+ this reflects funding/PnL drift.
    marks = {e.symbol: float(e.mark) for e in evidence}
    cash = round(account.equity(marks), 2)

    pending_root = Path(args.memory_dir) / "pending"
    pending = pending_root / str(cycle)
    # A failed attempt retries the same completed+1 identity. Remove every old agent artifact
    # before publishing new evidence so reads/book/verdict from that attempt cannot be reused.
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(parents=True, exist_ok=True)
    for name in _LEGACY_FILES:                     # scrub flat-layout leftovers at the root
        legacy = pending_root / name
        if legacy.exists():
            legacy.unlink()
    (pending / "risk_model.json").write_text(json.dumps(risk_model, indent=2) + "\n")
    (pending / "scoring_marks.json").write_text(json.dumps(scoring_marks, indent=2) + "\n")
    evidence_json = [e.model_dump(mode="json") for e in evidence]
    (pending / "evidence.json").write_text(json.dumps(evidence_json, default=str, indent=2))
    meta = {
        "cycle": cycle, "symbols": symbols, "now": now.isoformat(),
        "btc_symbol": settings.btc_symbol, "cash": cash,
        "held_extra": held_extra, "universe_drops": drops,
        "scoring_symbols": scoring_symbols,
        "evidence_sha256": canonical_sha256(evidence_json),
        "risk_model_sha256": canonical_sha256(risk_model),
        "scoring_marks_sha256": canonical_sha256(scoring_marks),
        "watchdog_receipt": watchdog_receipt,
        "watchdog_receipt_sha256": canonical_sha256(watchdog_receipt),
        "expected_candle_requests": expected_candle_requests,
        "candle_data": candle_audit,
    }
    if directive_text is not None:
        (pending / "binding_user_directive.md").write_text(directive_text)
        meta["binding_user_directive_sha256"] = canonical_sha256(directive_text)
    (pending / "meta.json").write_text(json.dumps(meta, indent=2))
    (pending_root / "current.json").write_text(json.dumps(
        {"cycle": cycle, "dir": str(pending.resolve()), "created": now.isoformat()}, indent=2))
    _prune_pending(pending_root)

    print(json.dumps({
        "cycle": cycle, "now": now.isoformat(), "universe": symbols,
        "held_extra": held_extra, "universe_drops": drops,
        "evidence_packs": len(evidence), "cash": cash,
        "candle_source": candle_audit["source"],
        "candle_requests": candle_audit["request_count"],
        "candles_all_fresh": candle_audit["all_fresh"],
        "risk_model_symbols": len(risk_model.get("symbols", [])),
        "scoring_only_symbols": scoring_symbols,
        "pending_dir": str(pending),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
