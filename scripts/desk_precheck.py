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
from pathlib import Path

from futures_fund.account import load_account
from futures_fund.adversary_binding import specialist_reads_sha256
from futures_fund.candle_proxy import validate_candle_audit
from futures_fund.config import load_settings
from futures_fund.desk_contracts import Book, SpecialistRead
from futures_fund.pending_io import resolve_pending
from futures_fund.performance import canonical_sha256
from futures_fund.precheck import compute_precheck
from futures_fund.prompt_guard import split_managed
from futures_fund.slippage import ExecutionRealism
from scripts.desk_reconcile import _parse_specialist_reads


def _verify_specialist_digest(
    pending: Path, evidence: list[dict], book: Book
) -> dict[str, list[SpecialistRead]]:
    reads_sha256 = (pending / "specialist_reads.sha256").read_text().strip()
    if book.specialist_reads_sha256 != reads_sha256:
        raise ValueError(
            "PM book specialist_reads_sha256 does not match the immutable read digest"
        )
    expected_symbols = [str(row["symbol"]) for row in evidence]
    specialist_reads = {}
    for role in ("sentiment", "technical", "futures"):
        raw = json.loads((pending / f"{role}_reads.json").read_text())
        specialist_reads[role] = (
            [] if raw == [] else _parse_specialist_reads(raw, expected_symbols)
        )
    if specialist_reads_sha256(specialist_reads) != reads_sha256:
        raise ValueError("specialist files changed after the immutable read digest")
    return specialist_reads


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    ap = argparse.ArgumentParser(description="Precheck the proposed book (data feed, no veto).")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    ap.add_argument("--agents-dir", default="agents")
    ap.add_argument("--book", default="pm_book.json",
                    help="pending book filename (pm_book.json)")
    ap.add_argument("--out", default="precheck.json",
                    help="pending output filename (precheck.json / precheck_original.json)")
    args = ap.parse_args(argv)

    pending, meta = resolve_pending(args.memory_dir)
    evidence = json.loads((pending / "evidence.json").read_text())
    risk_model = json.loads((pending / "risk_model.json").read_text())
    if (
        meta.get("evidence_sha256") != canonical_sha256(evidence)
        or meta.get("risk_model_sha256") != canonical_sha256(risk_model)
    ):
        raise ValueError("cycle meta evidence/risk hash mismatch")
    validate_candle_audit(meta, evidence)
    book = Book.model_validate(
        json.loads((pending / args.book).read_text())
    ).validate_production_contract()
    specialist_reads = _verify_specialist_digest(pending, evidence, book)
    book.validate_candidate_review_coverage(specialist_reads)

    marks = {e["symbol"]: float(e["mark"]) for e in evidence}
    account = load_account(args.state_dir, default_cash=float(meta["cash"]))
    current_book = [
        {"symbol": s, "side": p.direction,
         "target_notional": abs(p.qty) * marks.get(s, p.entry_price),
         "seat_role": p.seat_role}
        for s, p in account.positions.items()
    ]

    metrics = compute_precheck(
        book,
        evidence,
        cash=float(meta["cash"]),
        cycle=int(meta["cycle"]),
        current_book=current_book,
        btc_symbol=meta.get("btc_symbol", "BTC/USDT:USDT"),
        risk_model=risk_model,
        meta_sha256=canonical_sha256(meta),
        execution_realism=ExecutionRealism(
            latency_ms=settings.execution.latency_ms,
            displayed_depth_fraction=settings.execution.displayed_depth_fraction,
            adverse_selection_bps=settings.execution.adverse_selection_bps,
            legging_bps_per_second=settings.execution.legging_bps_per_second,
            allow_partial_fills=settings.execution.allow_partial_fills,
        ),
    )
    pm_region = split_managed((Path(args.agents_dir) / "pm.md").read_text())[1]
    entry_gate_policy = {
        "source": str(Path(args.agents_dir) / "pm.md"),
        "managed_region": pm_region,
        "sha256": canonical_sha256(pm_region),
    }
    (pending / "entry_gate_policy.json").write_text(
        json.dumps(entry_gate_policy, indent=2) + "\n"
    )
    (pending / args.out).write_text(json.dumps(metrics.model_dump(mode="json"), indent=2))

    failing = [b.bound_id for b in metrics.bounds if not b.ok]
    print(json.dumps({
        "cycle": metrics.cycle, "gross": metrics.gross, "deploy_frac": metrics.deploy_frac,
        "alpha_gross": metrics.alpha_gross, "alpha_deploy_frac": metrics.alpha_deploy_frac,
        "hedge_gross": metrics.hedge_gross, "hedge_deploy_frac": metrics.hedge_deploy_frac,
        "dollar_residual_frac": metrics.dollar_residual_frac,
        "beta_residual": metrics.beta_residual,
        "max_leg": f"{metrics.max_leg_symbol} {metrics.max_leg_frac_gross:.3f}",
        "hedge_frac_cash": metrics.hedge_frac_cash,
        "turnover_legs_changed": metrics.turnover_legs_changed,
        "turnover_aggressive_legs_changed": metrics.turnover_aggressive_legs_changed,
        "turnover_risk_reductions": metrics.turnover_risk_reductions,
        "turnover_usd": metrics.turnover_usd,
        "portfolio_residual_vol_annualized_frac_cash": (
            metrics.portfolio_residual_vol_annualized_frac_cash
        ),
        "max_alpha_standalone_risk": (
            f"{metrics.max_alpha_standalone_risk_symbol} "
            f"{metrics.max_alpha_standalone_risk_share:.3f}"
        ),
        "max_same_side_high_correlation_cluster_risk_share": (
            metrics.max_same_side_high_correlation_cluster_risk_share
        ),
        "max_position_co_risk_cluster_risk_share": (
            metrics.max_position_co_risk_cluster_risk_share
        ),
        "portfolio_expected_total_edge_usd_per_8h": (
            metrics.portfolio_expected_total_edge_usd_per_8h
        ),
        "bounds_failing": failing, "sha256": metrics.sha256,
        "out": str(pending / args.out),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
