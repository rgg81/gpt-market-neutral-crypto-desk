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
from futures_fund.config import load_settings
from futures_fund.evidence import build_evidence
from futures_fund.exchange import FuturesExchange, build_ccxt
from futures_fund.market_data import quality_filter, scan_universe

_KEEP_PENDING_DIRS = 3
# Legacy flat-layout filenames: remove from pending/ ROOT so stale copies can't be consumed.
_LEGACY_FILES = (
    "evidence.json", "meta.json", "recurrences.json", "reflection.json",
    "sentiment_reads.json", "technical_reads.json", "futures_reads.json",
    "pm_book.json", "pm_book_original.json", "adversary.json",
    "precheck.json", "precheck_original.json",
)


def _next_cycle(state_dir: str, cadence: str = "rebal") -> int:
    root = Path(state_dir) / cadence / "cycle"
    if not root.exists():
        return 1
    nums = [int(p.name) for p in root.glob("*") if p.is_dir() and p.name.isdigit()]
    return (max(nums) + 1) if nums else 1


def _build_data_clients(settings):
    """Build one public client and share it between universe scan and evidence collection."""
    client = build_ccxt(settings)
    client.load_markets()
    return client, FuturesExchange(client, keyless=True)


def _prune_pending(pending_root: Path, keep: int = _KEEP_PENDING_DIRS) -> None:
    dirs = sorted((p for p in pending_root.glob("*") if p.is_dir() and p.name.isdigit()),
                  key=lambda p: int(p.name))
    for stale in dirs[:-keep] if len(dirs) > keep else []:
        shutil.rmtree(stale, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch universe + build evidence for one desk cycle.")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    args = ap.parse_args(argv)

    settings = load_settings()
    now = datetime.now(UTC)
    client, exchange = _build_data_clients(settings)
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

    evidence = build_evidence(exchange, symbols, now=now, btc_symbol=settings.btc_symbol)

    # PM sizes against LIVE equity, not the static seed: on cycle 1 the account is fresh
    # (equity == account_size_usdt); from cycle 2+ this reflects funding/PnL drift.
    marks = {e.symbol: float(e.mark) for e in evidence}
    cash = round(account.equity(marks), 2)

    cycle = _next_cycle(args.state_dir)
    pending_root = Path(args.memory_dir) / "pending"
    pending = pending_root / str(cycle)
    pending.mkdir(parents=True, exist_ok=True)
    for name in _LEGACY_FILES:                     # scrub flat-layout leftovers at the root
        legacy = pending_root / name
        if legacy.exists():
            legacy.unlink()
    (pending / "evidence.json").write_text(
        json.dumps([e.model_dump(mode="json") for e in evidence], default=str, indent=2))
    (pending / "meta.json").write_text(json.dumps(
        {"cycle": cycle, "symbols": symbols, "now": now.isoformat(),
         "btc_symbol": settings.btc_symbol, "cash": cash,
         "held_extra": held_extra, "universe_drops": drops}, indent=2))
    (pending_root / "current.json").write_text(json.dumps(
        {"cycle": cycle, "dir": str(pending.resolve()), "created": now.isoformat()}, indent=2))
    _prune_pending(pending_root)

    print(json.dumps({
        "cycle": cycle, "now": now.isoformat(), "universe": symbols,
        "held_extra": held_extra, "universe_drops": drops,
        "evidence_packs": len(evidence), "cash": cash,
        "pending_dir": str(pending),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
