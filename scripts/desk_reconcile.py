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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.adversary_binding import (
    AdversaryBindingError,
    specialist_reads_sha256,
    verify_precheck_artifact,
    verify_revision_binding,
    verify_revision_citation_safety,
    verify_verdict_binding,
)
from futures_fund.candle_proxy import validate_candle_audit
from futures_fund.config import load_settings
from futures_fund.desk_contracts import AdversaryVerdict, Book, SpecialistRead
from futures_fund.desk_cycle import (
    _achieved_execution_safety,
    _execution_inputs,
    _execution_target_audit,
    _verify_execution_liquidity,
    _verify_fresh_execution_economics,
    reconcile_book,
)
from futures_fund.exchange import FuturesExchange
from futures_fund.funding_history import (
    collect_funding_events,
    resolve_previous_intervals,
    verify_execution_window_funding_safety,
)
from futures_fund.heartbeat import recover_heartbeat_transaction
from futures_fund.pending_io import resolve_pending
from futures_fund.performance import (
    PERFORMANCE_SCHEMA_VERSION,
    build_performance_snapshot,
    canonical_sha256,
)
from futures_fund.pnl_attribution import build_cycle_pnl, latest_closing_equity
from futures_fund.precheck import PrecheckMetrics, compute_precheck
from futures_fund.prompt_guard import split_managed
from futures_fund.reconcile_commit import (
    recover_reconcile_transaction,
    stage_reconcile_transaction,
)
from futures_fund.runtime_provenance import (
    load_bound_decision_start_provenance,
    load_bound_pre_reflection_performance,
)
from futures_fund.slippage import ExecutionRealism
from futures_fund.state_transaction import load_account_with_sha256
from scripts.desk_watchdog import build_watchdog_receipt


def _halt(reason: str) -> int:
    print(json.dumps({"halt": f"{reason} — prior book stands"}))
    return 1


def _verify_watchdog_receipt(state_dir: str, meta: dict) -> dict:
    """Reproduce the deterministic pre-agent cadence receipt from committed state."""
    cycle = int(meta["cycle"])
    decision_ts = datetime.fromisoformat(str(meta["now"]))
    receipt = meta.get("watchdog_receipt")
    if not isinstance(receipt, dict):
        raise ValueError("watchdog receipt is missing or not an object")
    if meta.get("watchdog_receipt_sha256") != canonical_sha256(receipt):
        raise ValueError("watchdog receipt hash mismatch")
    if receipt != build_watchdog_receipt(state_dir, now=decision_ts):
        raise ValueError("watchdog receipt does not reproduce from committed state")
    if int(receipt.get("next_cycle", 0)) != cycle:
        raise ValueError("watchdog receipt is not bound to the next cycle")
    if str(receipt.get("schedule_status")) in {
        "EARLY",
        "FUTURE_CLOCK",
        "UNKNOWN_LAST_TIMESTAMP",
    }:
        raise ValueError(f"watchdog forbids cycle: {receipt.get('schedule_status')}")
    return receipt


def _current_book(account, marks: dict[str, float]) -> list[dict]:
    """Represent held positions exactly as desk_precheck.py did when the artifacts were built."""
    return [
        {
            "symbol": symbol,
            "side": position.direction,
            "target_notional": abs(position.qty) * marks.get(symbol, position.entry_price),
            "seat_role": position.seat_role,
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


def _revision_dispatch_sha256(
    verdict: AdversaryVerdict,
    original_book: Book,
    original_precheck: PrecheckMetrics,
) -> str:
    return canonical_sha256({
        "cycle": verdict.cycle,
        "original_book_sha256": canonical_sha256(original_book.model_dump(mode="json")),
        "original_precheck_sha256": original_precheck.sha256,
        "adversary_sha256": canonical_sha256(verdict.model_dump(mode="json")),
        "revision_constraints": [
            row.model_dump(mode="json") for row in verdict.revision_constraints
        ],
        "revision_allowed_failing_bounds": verdict.revision_allowed_failing_bounds,
    })


@dataclass(frozen=True)
class VerifiedDecisionChain:
    """One parse of every PM/Adversary decision artifact, verified before network I/O."""

    book: Book
    verdict: AdversaryVerdict
    final_precheck: PrecheckMetrics
    original_book: Book | None = None
    original_precheck: PrecheckMetrics | None = None
    revision_dispatch_receipt: dict | None = None
    revision_output_receipt: dict | None = None


def _verify_revision_receipt(
    pending: Path,
    verdict: AdversaryVerdict,
    original_book: Book,
    original_precheck: PrecheckMetrics,
    final_book: Book,
) -> tuple[dict, dict]:
    """Require one explicit, input/output-bound PM revision attempt receipt."""
    dispatch_path = pending / "revision_dispatch_receipt.json"
    output_path = pending / "revision_output_receipt.json"
    if not dispatch_path.is_file() or not output_path.is_file():
        raise AdversaryBindingError(
            "rejected decision lacks immutable revision dispatch/output receipts"
        )
    dispatch = json.loads(dispatch_path.read_text())
    output = json.loads(output_path.read_text())
    if (
        not isinstance(dispatch, dict)
        or set(dispatch) != {"schema_version", "cycle", "attempt", "dispatch_sha256"}
        or not isinstance(output, dict)
        or set(output) != {
            "schema_version", "cycle", "attempt", "dispatch_sha256", "output_book_sha256"
        }
    ):
        raise AdversaryBindingError("revision receipts have a malformed schema")
    expected_dispatch_sha256 = _revision_dispatch_sha256(
        verdict, original_book, original_precheck
    )
    if (
        dispatch.get("schema_version") != 1
        or dispatch.get("cycle") != verdict.cycle
        or dispatch.get("attempt") != 1
        or dispatch.get("dispatch_sha256") != expected_dispatch_sha256
        or output.get("schema_version") != 1
        or output.get("cycle") != verdict.cycle
        or output.get("attempt") != 1
        or output.get("dispatch_sha256") != expected_dispatch_sha256
        or output.get("output_book_sha256")
        != canonical_sha256(final_book.model_dump(mode="json"))
    ):
        raise AdversaryBindingError(
            "revision receipt does not bind exactly one attempt to the original/verdict/final book"
        )
    return dispatch, output


def _verify_decision_chain(
    pending,
    *,
    cycle: int,
    meta: dict,
    evidence: list[dict],
    current_book: list[dict],
    book: Book,
    verdict: AdversaryVerdict,
    risk_model: dict | None = None,
    reads: dict[str, list[SpecialistRead]] | None = None,
    performance_snapshot: dict | None = None,
    entry_gate_policy_sha256: str | None = None,
    execution_realism: ExecutionRealism | None = None,
) -> VerifiedDecisionChain:
    """Validate accepted and once-revised artifact chains before the paper account is mutated."""
    book.validate_production_contract()
    if reads is not None:
        book.validate_candidate_review_coverage(reads)
    compute_args = {
        "cash": float(meta["cash"]),
        "cycle": cycle,
        "current_book": current_book,
        "btc_symbol": meta.get("btc_symbol", "BTC/USDT:USDT"),
        "risk_model": risk_model,
        "meta_sha256": canonical_sha256(meta),
        "execution_realism": execution_realism,
    }
    final_precheck = PrecheckMetrics.model_validate_json((pending / "precheck.json").read_text())
    expected_final = compute_precheck(book, evidence, **compute_args)
    verify_precheck_artifact(final_precheck, expected_final, cycle=cycle)

    reviewed_precheck = final_precheck
    reviewed_book = book
    original_book = None
    original_precheck = None
    revision_dispatch_receipt = None
    revision_output_receipt = None
    if not verdict.accept:
        original_book_path = pending / "pm_book_original.json"
        original_precheck_path = pending / "precheck_original.json"
        if not original_book_path.is_file() or not original_precheck_path.is_file():
            raise AdversaryBindingError(
                "rejected verdict lacks pm_book_original.json and precheck_original.json"
            )
        original_book = Book.model_validate_json(
            original_book_path.read_text()
        ).validate_production_contract()
        if reads is not None:
            original_book.validate_candidate_review_coverage(reads)
        original_precheck = PrecheckMetrics.model_validate_json(original_precheck_path.read_text())
        expected_original = compute_precheck(original_book, evidence, **compute_args)
        verify_precheck_artifact(
            original_precheck, expected_original, cycle=cycle, label="precheck_original"
        )
        reviewed_precheck = original_precheck
        reviewed_book = original_book
        revision_dispatch_receipt, revision_output_receipt = _verify_revision_receipt(
            pending,
            verdict,
            original_book,
            original_precheck,
            book,
        )
    elif any(
        (pending / name).exists()
        for name in (
            "pm_book_original.json",
            "precheck_original.json",
            "revision_dispatch_receipt.json",
            "revision_output_receipt.json",
        )
    ):
        raise AdversaryBindingError("accepted decision carries unexpected revision artifacts")

    verify_verdict_binding(
        verdict,
        reviewed_precheck,
        cycle=cycle,
        sentiment_reads=(reads or {}).get("sentiment", []),
        specialist_reads=reads,
        performance_snapshot=performance_snapshot,
        book=reviewed_book,
        entry_gate_policy_sha256=entry_gate_policy_sha256,
        binding_user_directive_sha256=meta.get("binding_user_directive_sha256"),
    )
    if not verdict.accept:
        verify_revision_binding(
            verdict,
            original_book,
            original_precheck,
            book,
            final_precheck,
            binding_user_directive_sha256=meta.get(
                "binding_user_directive_sha256"
            ),
        )
        verify_revision_citation_safety(
            verdict, (reads or {}).get("sentiment", []), book
        )
    return VerifiedDecisionChain(
        book=book,
        verdict=verdict,
        final_precheck=final_precheck,
        original_book=original_book,
        original_precheck=original_precheck,
        revision_dispatch_receipt=revision_dispatch_receipt,
        revision_output_receipt=revision_output_receipt,
    )


def main(argv: list[str] | None = None) -> int:
    settings = load_settings()
    execution_defaults = ExecutionRealism(
        latency_ms=settings.execution.latency_ms,
        displayed_depth_fraction=settings.execution.displayed_depth_fraction,
        adverse_selection_bps=settings.execution.adverse_selection_bps,
        legging_bps_per_second=settings.execution.legging_bps_per_second,
        allow_partial_fills=settings.execution.allow_partial_fills,
    )
    ap = argparse.ArgumentParser(description="Reconcile the paper book from the agents' outputs.")
    ap.add_argument("--state-dir", default="live_state")
    ap.add_argument("--memory-dir", default="live_memory")
    ap.add_argument("--agents-dir", default="agents")
    ap.add_argument(
        "--execution-latency-ms", type=float, default=execution_defaults.latency_ms
    )
    ap.add_argument(
        "--displayed-depth-fraction",
        type=float,
        default=execution_defaults.displayed_depth_fraction,
    )
    ap.add_argument(
        "--adverse-selection-bps",
        type=float,
        default=execution_defaults.adverse_selection_bps,
    )
    ap.add_argument(
        "--legging-bps-per-second",
        type=float,
        default=execution_defaults.legging_bps_per_second,
    )
    ap.add_argument(
        "--no-partial-fills",
        action="store_false",
        dest="allow_partial_fills",
        default=execution_defaults.allow_partial_fills,
    )
    args = ap.parse_args(argv)
    try:
        execution_realism = ExecutionRealism(
            latency_ms=args.execution_latency_ms,
            displayed_depth_fraction=args.displayed_depth_fraction,
            adverse_selection_bps=args.adverse_selection_bps,
            legging_bps_per_second=args.legging_bps_per_second,
            allow_partial_fills=args.allow_partial_fills,
        )
    except ValueError as exc:
        return _halt(f"invalid execution-realism configuration: {exc}")

    try:
        recovery = recover_reconcile_transaction(args.state_dir)
    except Exception as exc:  # noqa: BLE001 — never proceed past an ambiguous PAPER generation
        return _halt(f"unfinished reconcile recovery failed: {exc}")
    if recovery.get("recovered"):
        print(json.dumps(recovery, indent=2))
        return 0
    try:
        recover_heartbeat_transaction(args.state_dir)
    except Exception as exc:  # noqa: BLE001 — account/audit halves must recover together
        return _halt(f"unfinished heartbeat recovery failed: {exc}")

    pending, meta = resolve_pending(args.memory_dir)
    cycle = int(meta["cycle"])
    decision_ts = datetime.fromisoformat(meta["now"])
    cadence = "rebal"
    try:
        _verify_watchdog_receipt(args.state_dir, meta)
    except Exception as exc:  # noqa: BLE001 - cadence provenance must fail closed
        return _halt(f"invalid or missing watchdog cadence receipt: {exc}")
    evidence = json.loads((pending / "evidence.json").read_text())
    if not evidence:
        return _halt("evidence pack is empty")
    marks = {e["symbol"]: float(e["mark"]) for e in evidence}
    if len(marks) != len(evidence):
        return _halt("evidence pack contains duplicate symbols")
    try:
        risk_model = json.loads((pending / "risk_model.json").read_text())
        scoring_marks = json.loads((pending / "scoring_marks.json").read_text())
        if (
            meta.get("evidence_sha256") != canonical_sha256(evidence)
            or meta.get("risk_model_sha256") != canonical_sha256(risk_model)
            or meta.get("scoring_marks_sha256") != canonical_sha256(scoring_marks)
        ):
            raise ValueError("evidence/risk/scoring meta hash mismatch")
        directive_path = pending / "binding_user_directive.md"
        directive_sha256 = meta.get("binding_user_directive_sha256")
        if directive_sha256 is not None:
            if not directive_path.is_file():
                raise ValueError("meta binds a missing user directive artifact")
            if directive_sha256 != canonical_sha256(directive_path.read_text()):
                raise ValueError("binding user directive hash mismatch")
        elif directive_path.exists():
            raise ValueError("unbound user directive artifact is present")
        validate_candle_audit(meta, evidence)
        decision_start_provenance = load_bound_decision_start_provenance(
            pending,
            meta,
            require_current_match=True,
        )
        pre_reflection_performance = load_bound_pre_reflection_performance(pending, meta)
    except Exception as exc:  # noqa: BLE001 — proposal inputs must be one immutable fresh packet
        return _halt(f"invalid or missing cycle evidence provenance: {exc}")
    try:
        performance_snapshot = json.loads((pending / "performance_snapshot.json").read_text())
        if (
            int(performance_snapshot["cycle"]) != cycle
            or performance_snapshot.get("paper_only") is not True
            or int(performance_snapshot.get("schema_version", 0))
            != PERFORMANCE_SCHEMA_VERSION
            or datetime.fromisoformat(performance_snapshot["as_of_ts"]) != decision_ts
            or performance_snapshot.get("bindings", {}).get("evidence_sha256")
            != canonical_sha256(evidence)
            or performance_snapshot.get("bindings", {}).get("risk_model_sha256")
            != canonical_sha256(risk_model)
            or performance_snapshot.get("bindings", {}).get("meta_sha256")
            != canonical_sha256(meta)
        ):
            raise ValueError("cycle/time/evidence/paper/schema binding mismatch")
        rebuilt_performance = build_performance_snapshot(
            args.state_dir,
            args.memory_dir,
            pending,
            cycle=cycle,
            as_of_ts=decision_ts,
            starting_capital=settings.account_size_usdt,
            require_cycle_meta=True,
        )
        if canonical_sha256(performance_snapshot) != canonical_sha256(rebuilt_performance):
            raise ValueError("performance packet does not equal deterministic rebuild")
        expected_performance_sha256 = canonical_sha256(performance_snapshot)
        sidecar_sha256 = (pending / "performance_snapshot.sha256").read_text().strip()
        if sidecar_sha256 != expected_performance_sha256:
            raise ValueError("performance snapshot SHA-256 sidecar mismatch")
    except Exception as exc:  # noqa: BLE001 — every decision role must share the same packet
        return _halt(f"invalid or missing performance snapshot: {exc}")
    evidence_symbols = list(marks)
    betas = {
        e["symbol"]: float(e.get("beta_clamped", e.get("beta_btc", 1.0)))
        for e in evidence
    }
    funding_rates = {e["symbol"]: float(e.get("funding_rate", 0.0)) for e in evidence}

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
    reads_sha256 = specialist_reads_sha256(reads)
    try:
        if (pending / "specialist_reads.sha256").read_text().strip() != reads_sha256:
            raise ValueError("specialist read SHA-256 sidecar mismatch")
    except Exception as exc:  # noqa: BLE001 — all downstream roles must share one read packet
        return _halt(f"invalid or missing specialist read digest: {exc}")

    try:
        book = Book.model_validate_json(
            (pending / "pm_book.json").read_text()
        ).validate_production_contract()
        book.validate_candidate_review_coverage(reads)
        verdict = AdversaryVerdict.model_validate_json((pending / "adversary.json").read_text())
        entry_gate_policy = json.loads((pending / "entry_gate_policy.json").read_text())
        current_pm_region = split_managed(
            (Path(args.agents_dir) / "pm.md").read_text()
        )[1]
        if (
            entry_gate_policy.get("managed_region") != current_pm_region
            or entry_gate_policy.get("sha256") != canonical_sha256(current_pm_region)
        ):
            raise ValueError("PM managed entry-gate policy changed after precheck")
    except Exception as exc:  # noqa: BLE001 — invalid agent output must halt before any fill
        return _halt(f"invalid PM/Adversary output: {exc}")

    try:
        account, base_account_sha256 = load_account_with_sha256(
            args.state_dir, default_cash=float(meta["cash"])
        )
    except Exception as exc:  # noqa: BLE001 — malformed persisted state must be a named HALT
        return _halt(f"invalid persisted PAPER account: {exc}")
    if (
        performance_snapshot.get("bindings", {}).get("account_sha256")
        != canonical_sha256(account.to_dict())
    ):
        return _halt("performance snapshot account binding mismatch")
    try:
        decision_chain = _verify_decision_chain(
            pending,
            cycle=cycle,
            meta=meta,
            evidence=evidence,
            current_book=_current_book(account, marks),
            book=book,
            verdict=verdict,
            risk_model=risk_model,
            reads=reads,
            performance_snapshot=performance_snapshot,
            entry_gate_policy_sha256=entry_gate_policy["sha256"],
            execution_realism=execution_realism,
        )
    except Exception as exc:  # noqa: BLE001 — any unbound artifact means no authorized decision
        return _halt(f"decision-chain validation failed: {exc}")
    # From this point onward, never re-read a mutable decision file. Fresh exchange capture may
    # take long enough for an operator or concurrent process to change pending bytes; execution and
    # the durable audit must use this exact already-verified in-memory generation.
    book = decision_chain.book
    verdict = decision_chain.verdict
    final_precheck = decision_chain.final_precheck

    exchange = FuturesExchange.from_settings(settings)
    # Cost inputs for every symbol the fill path may touch: the book's legs AND any held symbol
    # about to be flattened because it left the book (ends the flat-1bp undercharge on drops).
    fill_syms = ({lg.symbol for lg in book.legs} | set(account.positions)) & set(marks)
    decision_target_audit = _execution_target_audit(account, book, marks, marks)
    changed_symbols = {
        symbol for symbol, detail in decision_target_audit.items()
        if float(detail["planned_turnover_usd"]) > 0.01
    }
    try:
        execution_marks, costs, execution_audit, execution_ts = _execution_inputs(
            exchange,
            fill_syms,
            marks,
            required_depth_symbols=changed_symbols,
            execution_realism=execution_realism,
        )
    except Exception as exc:  # noqa: BLE001 — no stale/synthetic PAPER execution
        return _halt(f"fresh execution-book capture failed: {exc}")
    target_audit = _execution_target_audit(
        account, book, marks, execution_marks, execution_audit
    )
    try:
        executed_targets = _verify_execution_liquidity(target_audit, execution_audit)
    except Exception as exc:  # noqa: BLE001 — execution mechanics must remain auditable
        return _halt(f"fresh execution liquidity failed: {exc}")
    for symbol, detail in target_audit.items():
        execution_audit.setdefault(symbol, {}).update(detail)
    b12_precheck = next(
        (bound for bound in final_precheck.bounds if bound.bound_id == "B12"), None
    )
    b12_failure_authorized = bool(
        b12_precheck is not None
        and not b12_precheck.ok
        and (
            verdict.accept
            or "B12" in verdict.revision_allowed_failing_bounds
        )
    )
    try:
        execution_economics = _verify_fresh_execution_economics(
            book,
            final_precheck,
            target_audit,
            execution_audit,
            costs,
            betas,
            decision_ts=decision_ts,
            execution_ts=execution_ts,
            cadence_tf_minutes=settings.cadence_tf_minutes,
            b12_failure_authorized=b12_failure_authorized,
        )
    except Exception as exc:  # noqa: BLE001 — changed liquidity invalidates old authorization
        return _halt(f"fresh execution economics failed: {exc}")
    execution_ts_by_symbol = {
        symbol: datetime.fromisoformat(detail["execution_ts"])
        for symbol, detail in execution_audit.items()
        if detail.get("execution_ts")
    }
    execution_completed_by_symbol = {
        symbol: not bool(detail.get("partial_fill", False))
        for symbol, detail in target_audit.items()
    }

    # The evidence interval is a decision-time signal. Re-fetch authoritative execution-time
    # metadata for every held or proposed symbol; a transport/shape failure must halt rather than
    # silently settle a held adaptive contract on an invented 8h schedule.
    try:
        funding_intervals = {
            symbol: int(exchange.funding_interval_hours(symbol))
            for symbol in sorted(fill_syms)
        }
    except Exception as exc:  # noqa: BLE001 — exact settlement metadata is mandatory
        return _halt(f"current funding interval metadata failed: {exc}")

    previous_funding_ts = account.last_funding_ts or execution_ts
    try:
        previous_intervals = resolve_previous_intervals(args.state_dir, account)
        funding_interval_proofs: dict[str, dict] = {}
        funding_events = collect_funding_events(
            exchange,
            set(account.positions),
            previous_ts=previous_funding_ts,
            now=execution_ts,
            intervals={symbol: funding_intervals[symbol] for symbol in account.positions},
            previous_intervals=previous_intervals,
            proof_out=funding_interval_proofs,
        )
        funding_execution_window_proof = verify_execution_window_funding_safety(
            execution_ts_by_symbol,
            fill_syms,
            current_intervals=funding_intervals,
            decision_intervals={
                str(row["symbol"]): row["funding_interval_h"] for row in evidence
            },
            held_interval_proofs=funding_interval_proofs,
        )
    except Exception as exc:  # noqa: BLE001 — incomplete history would fabricate funding P&L
        return _halt(f"historical funding settlement failed: {exc}")

    opening_equity = account.equity(execution_marks)
    try:
        prior_closing_equity = latest_closing_equity(args.state_dir)
    except Exception as exc:  # noqa: BLE001 — never build a return over corrupted history
        return _halt(f"prior ledger close validation failed: {exc}")
    b1_precheck = next(bound for bound in final_precheck.bounds if bound.bound_id == "B1")
    minimum_achieved_deploy_frac = 0.75 if b1_precheck.ok else 0.0
    try:
        report = reconcile_book(account, book, marks=execution_marks, decision_marks=marks,
                                costs=costs, betas=betas, now=decision_ts,
                                execution_ts=execution_ts,
                                cycle=cycle, cadence=cadence,
                                funding_by_symbol=funding_rates,
                                funding_intervals=funding_intervals,
                                funding_events_by_symbol=funding_events,
                                specialist_failed=specialist_failed,
                                target_signed_quantities=executed_targets,
                                execution_ts_by_symbol=execution_ts_by_symbol,
                                execution_completed_by_symbol=execution_completed_by_symbol,
                                minimum_achieved_deploy_frac=minimum_achieved_deploy_frac)
    except Exception as exc:  # noqa: BLE001 — staged execution must fail closed before mutation
        return _halt(f"paper execution validation failed: {exc}")
    achieved_safety = _achieved_execution_safety(
        account,
        execution_marks,
        betas,
        minimum_deploy_frac=minimum_achieved_deploy_frac,
    )
    for symbol, detail in execution_audit.items():
        position = account.positions.get(symbol)
        post_qty = 0.0 if position is None else (
            position.qty if position.direction == "long" else -position.qty
        )
        detail["post_execution_qty_signed"] = post_qty
        detail["post_execution_notional_signed"] = post_qty * execution_marks[symbol]
        detail["execution_target_attained"] = abs(
            post_qty - float(detail.get("executed_target_qty_signed", post_qty))
        ) <= 1e-12
        detail.update(achieved_safety)
    reads_json = {r: [x.model_dump(mode="json") for x in v] for r, v in reads.items()}
    artifacts = {
        "reads": reads_json,
        "book": book.model_dump(mode="json"),
        "adversary": verdict.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "execution": execution_audit,
        "execution_economics": execution_economics,
        "performance_snapshot": performance_snapshot,
        "performance_snapshot_pre_reflection": pre_reflection_performance,
        "performance_snapshot_pre_reflection_digest": {
            "sha256": canonical_sha256(pre_reflection_performance)
        },
        "evidence": evidence,
        "funding_interval_proofs": funding_interval_proofs,
        "funding_execution_window_proof": funding_execution_window_proof,
        "specialist_reads_digest": {"sha256": reads_sha256},
    }
    # Closed lifecycle carriers are committed beside the account, then removed from it in the
    # same durable transaction. This makes exact price/funding/fee/slippage outcomes available to
    # future agents without re-recording them after a crash/retry.
    artifacts["closed_legs"] = [
        leg.model_dump(mode="json") for leg in account.drain_closed_legs()
    ]
    artifacts["risk_model"] = risk_model
    artifacts["scoring_marks"] = scoring_marks
    artifacts["meta"] = meta
    artifacts["entry_gate_policy"] = entry_gate_policy
    artifacts["precheck"] = final_precheck.model_dump(mode="json")
    if decision_chain.original_precheck is not None:
        artifacts["precheck_original"] = decision_chain.original_precheck.model_dump(
            mode="json"
        )
    if decision_chain.original_book is not None:
        original_book_json = decision_chain.original_book.model_dump(mode="json")
        artifacts["pm_book_original"] = original_book_json
        artifacts["book_original"] = original_book_json
    if decision_chain.revision_dispatch_receipt is not None:
        artifacts["revision_dispatch_receipt"] = dict(
            decision_chain.revision_dispatch_receipt
        )
    if decision_chain.revision_output_receipt is not None:
        artifacts["revision_output_receipt"] = dict(
            decision_chain.revision_output_receipt
        )
    commit_ts = datetime.now(UTC)
    pnl = build_cycle_pnl(account, opening_equity=opening_equity, marks=execution_marks,
                          turnover_usd=report.turnover_usd, cycle=cycle, cadence=cadence,
                          now=commit_ts, prior_closing_equity=prior_closing_equity)
    try:
        stage_reconcile_transaction(
            args.state_dir,
            expected_base_account_sha256=base_account_sha256,
            cycle=cycle,
            cadence=cadence,
            account=account,
            artifacts=artifacts,
            equity_ts=commit_ts,
            equity=report.equity,
            ledger=pnl,
            runtime_provenance=decision_start_provenance,
        )
        recover_reconcile_transaction(args.state_dir)
    except Exception as exc:  # noqa: BLE001 — durable intent remains replayable after a crash
        print(json.dumps({
            "halt": f"durable reconcile commit interrupted: {exc}; run desk_recover.py",
        }))
        return 1

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
