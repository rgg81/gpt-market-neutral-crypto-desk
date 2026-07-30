"""Cycle step 3 (deterministic): reconcile the paper account to the agents' final book and persist.

    uv run python scripts/desk_reconcile.py --state-dir live_state --memory-dir live_memory

Reads `<memory>/pending/` — the evidence + meta from `desk_evidence.py` and the agent outputs the
orchestrator saved there (`sentiment_reads.json`, `technical_reads.json`, `futures_reads.json`,
`pm_book.json`, `adversary.json`). Decision-chain validation uses the exact evidence marks the
agents saw. Paper fills then use a fresh execution snapshot whose reference price and L2 depth come
from the same book, so delayed market movement is never mislabeled as slippage. PM target notionals
become quantities at the evidence marks before that fresh snapshot is applied, so unchanged held
legs remain no-ops. It computes the ACHIEVED deploy/neutrality and persists
reads/book/adversary/report/execution under
`state/rebal/cycle/<cycle>/` + records equity. Before any fill, it proves the book, precheck, and
Adversary verdict belong to the same cycle and decision chain. This is workflow validation, never
a deterministic trading veto. PAPER.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.account import load_account, save_account
from futures_fund.adversary_binding import (
    AdversaryBindingError,
    verify_precheck_artifact,
    verify_verdict_binding,
)
from futures_fund.config import load_settings
from futures_fund.cycle_io import save_output
from futures_fund.desk_contracts import AdversaryVerdict, Book, SpecialistRead
from futures_fund.desk_cycle import (
    _execution_inputs,
    _execution_target_audit,
    reconcile_book,
)
from futures_fund.equity_log import record_equity
from futures_fund.exchange import FuturesExchange
from futures_fund.pending_io import resolve_pending
from futures_fund.pnl_attribution import append_ledger, build_cycle_pnl
from futures_fund.precheck import PrecheckMetrics, compute_precheck
from futures_fund.reflection import persist_decision_snapshot


def _halt(reason: str) -> int:
    print(json.dumps({"halt": f"{reason} — prior book stands"}))
    return 1


def _current_book(account, marks: dict[str, float]) -> list[dict]:
    """Represent held positions exactly as desk_precheck.py did when the artifacts were built."""
    return [
        {
            "symbol": symbol,
            "side": position.direction,
            "target_notional": abs(position.qty) * marks.get(symbol, position.entry_price),
        }
        for symbol, position in account.positions.items()
    ]


def _parse_specialist_reads(raw, expected_symbols: list[str]) -> list[SpecialistRead]:
    """Validate schema plus complete, duplicate-free coverage of the evidence universe."""
    if not isinstance(raw, list):
        raise ValueError("specialist output must be a JSON array")
    reads = [SpecialistRead.model_validate(item) for item in raw]
    actual = [read.symbol for read in reads]
    expected = set(expected_symbols)
    if len(expected) != len(expected_symbols):
        raise ValueError("evidence contains duplicate symbols")
    if len(actual) != len(set(actual)) or set(actual) != expected:
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        raise ValueError(
            f"specialist coverage mismatch: missing={missing}, extra={extra}, "
            f"duplicates={len(actual) - len(set(actual))}"
        )
    return reads


def _verify_decision_chain(
    pending,
    *,
    cycle: int,
    meta: dict,
    evidence: list[dict],
    current_book: list[dict],
    book: Book,
    verdict: AdversaryVerdict,
    reads: dict[str, list[SpecialistRead]] | None = None,
) -> None:
    """Validate accepted and once-revised artifact chains before the paper account is mutated."""
    compute_args = {
        "cash": float(meta["cash"]),
        "cycle": cycle,
        "current_book": current_book,
        "btc_symbol": meta.get("btc_symbol", "BTC/USDT:USDT"),
    }
    final_precheck = PrecheckMetrics.model_validate_json((pending / "precheck.json").read_text())
    expected_final = compute_precheck(book, evidence, **compute_args)
    verify_precheck_artifact(final_precheck, expected_final, cycle=cycle)

    reviewed_precheck = final_precheck
    reviewed_book = book
    if not verdict.accept:
        original_book_path = pending / "pm_book_original.json"
        original_precheck_path = pending / "precheck_original.json"
        if not original_book_path.is_file() or not original_precheck_path.is_file():
            raise AdversaryBindingError(
                "rejected verdict lacks pm_book_original.json and precheck_original.json"
            )
        original_book = Book.model_validate_json(original_book_path.read_text())
        original_precheck = PrecheckMetrics.model_validate_json(original_precheck_path.read_text())
        expected_original = compute_precheck(original_book, evidence, **compute_args)
        verify_precheck_artifact(
            original_precheck, expected_original, cycle=cycle, label="precheck_original"
        )
        reviewed_precheck = original_precheck
        reviewed_book = original_book

    verify_verdict_binding(
        verdict,
        reviewed_precheck,
        cycle=cycle,
        sentiment_reads=(reads or {}).get("sentiment", []),
        book=reviewed_book,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Reconcile the paper book from the agents' outputs.")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    args = ap.parse_args(argv)

    pending, meta = resolve_pending(args.memory_dir)
    cycle = int(meta["cycle"])
    decision_ts = datetime.fromisoformat(meta["now"])
    cadence = "rebal"

    evidence = json.loads((pending / "evidence.json").read_text())
    if not evidence:
        return _halt("evidence pack is empty")
    marks = {e["symbol"]: float(e["mark"]) for e in evidence}
    if len(marks) != len(evidence):
        return _halt("evidence pack contains duplicate symbols")
    evidence_symbols = list(marks)
    betas = {e["symbol"]: float(e.get("beta_btc", 1.0)) for e in evidence}
    funding_rates = {e["symbol"]: float(e.get("funding_rate", 0.0)) for e in evidence}
    funding_ivals = {e["symbol"]: int(float(e.get("funding_interval_h", 8) or 8))
                     for e in evidence}

    # A specialist whose reads file is missing/malformed is fail-soft ([]) but RECORDED — never
    # silently absent (2026-07 review: stale/missing files must be named, not papered over).
    reads: dict[str, list[SpecialistRead]] = {}
    specialist_failed: list[str] = []
    for role in ("sentiment", "technical", "futures"):
        try:
            raw = json.loads((pending / f"{role}_reads.json").read_text())
            reads[role] = _parse_specialist_reads(raw, evidence_symbols)
        except Exception:  # noqa: BLE001 — fail-soft per charter; PM proceeded on the others
            reads[role] = []
            specialist_failed.append(role)
    if len(specialist_failed) == 3:
        return _halt("all three specialists failed")

    try:
        book = Book.model_validate_json((pending / "pm_book.json").read_text())
        verdict = AdversaryVerdict.model_validate_json((pending / "adversary.json").read_text())
    except Exception as exc:  # noqa: BLE001 — invalid agent output must halt before any fill
        return _halt(f"invalid PM/Adversary output: {exc}")

    settings = load_settings()
    account = load_account(args.state_dir, default_cash=float(meta["cash"]))
    try:
        _verify_decision_chain(
            pending,
            cycle=cycle,
            meta=meta,
            evidence=evidence,
            current_book=_current_book(account, marks),
            book=book,
            verdict=verdict,
            reads=reads,
        )
    except Exception as exc:  # noqa: BLE001 — any unbound artifact means no authorized decision
        return _halt(f"decision-chain validation failed: {exc}")

    exchange = FuturesExchange.from_settings(settings)
    # Cost inputs for every symbol the fill path may touch: the book's legs AND any held symbol
    # about to be flattened because it left the book (ends the flat-1bp undercharge on drops).
    fill_syms = ({lg.symbol for lg in book.legs} | set(account.positions)) & set(marks)
    execution_marks, costs, execution_audit, execution_ts = _execution_inputs(
        exchange, fill_syms, marks
    )
    target_audit = _execution_target_audit(account, book, marks, execution_marks)
    for symbol, detail in target_audit.items():
        execution_audit.setdefault(symbol, {}).update(detail)

    opening_equity = account.equity(execution_marks)
    report = reconcile_book(account, book, marks=execution_marks, decision_marks=marks,
                            costs=costs, betas=betas, now=decision_ts,
                            execution_ts=execution_ts,
                            cycle=cycle, cadence=cadence,
                            funding_by_symbol=funding_rates, funding_intervals=funding_ivals,
                            specialist_failed=specialist_failed)
    save_account(args.state_dir, account)

    reads_json = {r: [x.model_dump(mode="json") for x in v] for r, v in reads.items()}
    save_output(args.state_dir, cycle, "reads", reads_json, cadence=cadence)
    save_output(args.state_dir, cycle, "book", book.model_dump(mode="json"), cadence=cadence)
    save_output(args.state_dir, cycle, "adversary", verdict.model_dump(mode="json"),
                cadence=cadence)
    save_output(args.state_dir, cycle, "report", report.model_dump(mode="json"), cadence=cadence)
    save_output(args.state_dir, cycle, "execution", execution_audit, cadence=cadence)
    for name in ("precheck.json", "precheck_original.json", "pm_book_original.json"):
        src = pending / name
        if src.exists():
            save_output(args.state_dir, cycle, name.removesuffix(".json"),
                        json.loads(src.read_text()), cadence=cadence)
    # Equity keyed to the REAL wall clock (the review found manufactured/misordered stamps).
    record_equity(args.state_dir, datetime.now(UTC), report.equity, cycle)
    pnl = build_cycle_pnl(account, opening_equity=opening_equity, marks=execution_marks,
                          turnover_usd=report.turnover_usd, cycle=cycle, cadence=cadence,
                          now=datetime.now(UTC))
    append_ledger(args.state_dir, pnl)
    persist_decision_snapshot(args.state_dir, cycle, evidence=evidence,
                              pending_dir=pending, cadence=cadence)

    print(json.dumps({
        "cycle": cycle, "n_legs": report.n_legs,
        "achieved_deploy_frac": round(report.achieved_deploy_frac, 4),
        "achieved_dollar_residual_frac": round(report.achieved_dollar_residual_frac, 4),
        "achieved_beta_residual": round(report.achieved_beta_residual, 4),
        "equity": round(report.equity, 2), "adversary_accepted": verdict.accept,
        "turnover_usd": round(report.turnover_usd, 2),
        "fees_paid_cycle": round(report.fees_paid_cycle, 2),
        "slippage_paid_cycle": round(report.slippage_paid_cycle, 2),
        "funding_settled_cycle": round(report.funding_settled_cycle, 4),
        "decision_age_seconds": round(report.decision_age_seconds, 1),
        "specialist_failed": report.specialist_failed,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
