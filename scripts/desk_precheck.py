"""Cycle step 3b (deterministic DATA FEED): compute precheck metrics on the PROPOSED book.

    uv run python scripts/desk_precheck.py --state-dir live_state --memory-dir live_memory

Reads `<memory>/pending/` (meta.json, evidence.json, pm_book.json) plus the live account's held
positions, computes `PrecheckMetrics` (gross/deploy/residuals/concentration/hedge/beta-$/turnover
+ bounds B1-B12), and writes `<memory>/pending/precheck.json`. The orchestrator injects this into
the Adversary dispatch (and into the PM revision, if any). It PRINTS and RECORDS — it never
vetoes; the Adversary owns the verdict (charter).
"""
from __future__ import annotations

import argparse
import json
import sys

from futures_fund.account import load_account
from futures_fund.desk_contracts import Book
from futures_fund.pending_io import resolve_pending
from futures_fund.precheck import compute_precheck


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Precheck the proposed book (data feed, no veto).")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    ap.add_argument("--book", default="pm_book.json",
                    help="pending book filename (pm_book.json)")
    ap.add_argument("--out", default="precheck.json",
                    help="pending output filename (precheck.json / precheck_original.json)")
    args = ap.parse_args(argv)

    pending, meta = resolve_pending(args.memory_dir)
    evidence = json.loads((pending / "evidence.json").read_text())
    book = Book.model_validate(json.loads((pending / args.book).read_text()))

    marks = {e["symbol"]: float(e["mark"]) for e in evidence}
    account = load_account(args.state_dir, default_cash=float(meta["cash"]))
    current_book = [
        {"symbol": s, "side": p.direction,
         "target_notional": abs(p.qty) * marks.get(s, p.entry_price)}
        for s, p in account.positions.items()
    ]

    metrics = compute_precheck(
        book, evidence, cash=float(meta["cash"]), cycle=int(meta["cycle"]),
        current_book=current_book, btc_symbol=meta.get("btc_symbol", "BTC/USDT:USDT"))
    (pending / args.out).write_text(json.dumps(metrics.model_dump(mode="json"), indent=2))

    failing = [b.bound_id for b in metrics.bounds if not b.ok]
    print(json.dumps({
        "cycle": metrics.cycle, "gross": metrics.gross, "deploy_frac": metrics.deploy_frac,
        "dollar_residual_frac": metrics.dollar_residual_frac,
        "beta_residual": metrics.beta_residual,
        "max_leg": f"{metrics.max_leg_symbol} {metrics.max_leg_frac_gross:.3f}",
        "hedge_frac_cash": metrics.hedge_frac_cash,
        "turnover_legs_changed": metrics.turnover_legs_changed,
        "turnover_usd": metrics.turnover_usd,
        "bounds_failing": failing, "sha256": metrics.sha256,
        "out": str(pending / args.out),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
