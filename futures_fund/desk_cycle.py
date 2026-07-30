"""The 8h decision cycle: evidence -> specialists -> PM -> adversary -> reconcile -> report.
Deterministic ORCHESTRATION only; every trading judgement is delegated to the AgentRunner."""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime

from futures_fund.account import (
    CostInputs,
    PaperAccount,
    _signed_qty,
    load_account,
    save_account,
)
from futures_fund.agent_runner import AgentRunner
from futures_fund.cycle_io import save_output
from futures_fund.desk_contracts import (
    AdversaryVerdict,
    Book,
    CycleReport,
    SpecialistRead,
)
from futures_fund.equity_log import record_equity
from futures_fund.evidence import EvidencePack, build_evidence

_SPECIALISTS = ("sentiment", "technical", "futures")


def _evidence_json(evidence: list[EvidencePack]) -> str:
    return json.dumps([e.model_dump(mode="json") for e in evidence], default=str)


def run_specialists(runner: AgentRunner, evidence: list[EvidencePack], *,
                    roles: tuple[str, ...] = _SPECIALISTS) -> dict[str, list[SpecialistRead]]:
    """Fan out each specialist role over the evidence. Fail-soft: a role that raises -> []."""
    ev_json = _evidence_json(evidence)
    out: dict[str, list[SpecialistRead]] = {}
    for role in roles:
        try:
            result = runner.run(role, ev_json, SpecialistRead)  # runner returns list per role
            out[role] = list(result) if isinstance(result, list) else [result]
        except Exception:  # noqa: BLE001 — a dropped specialist must not sink the cycle
            out[role] = []
    return out


def run_pm(runner: AgentRunner, reads: dict[str, list[SpecialistRead]],
           evidence: list[EvidencePack], *, cash: float,
           current_book: list[dict] | None = None) -> Book:
    """Ask the PM agent to synthesize the reads into a book. Returns the PM's Book verbatim.

    `current_book` (optional): the currently held legs, each a dict with symbol, side, and
    target_notional. Pass this so the PM can prefer holding existing positions to reduce turnover
    costs."""
    payload = {
        "cash": cash,
        "reads": {r: [x.model_dump(mode="json") for x in v] for r, v in reads.items()},
        "evidence": [e.model_dump(mode="json") for e in evidence],
    }
    if current_book:
        payload["current_book"] = current_book
    result = runner.run("pm", json.dumps(payload, default=str), Book)
    return result if isinstance(result, Book) else Book.model_validate(result)


def run_adversary(runner: AgentRunner, book: Book, reads: dict[str, list[SpecialistRead]],
                  evidence: list[EvidencePack], *, cash: float,
                  current_book: list[dict] | None = None) -> tuple[AdversaryVerdict, Book]:
    """Challenge the book once. On reject, ask the PM for ONE revision; else keep the book.

    `current_book` (optional): passed through to the PM revision so it can prefer holding existing
    positions even when addressing adversary objections."""
    challenge_payload = {"book": book.model_dump(mode="json"),
                         "reads": {r: [x.model_dump(mode="json") for x in v]
                                   for r, v in reads.items()}}
    if current_book:
        challenge_payload["current_book"] = current_book
    challenge = json.dumps(challenge_payload, default=str)
    verdict = runner.run("adversary", challenge, AdversaryVerdict)
    if not isinstance(verdict, AdversaryVerdict):
        verdict = AdversaryVerdict.model_validate(verdict)
    if verdict.accept:
        return verdict, book
    revise_payload = {"original": book.model_dump(mode="json"),
                      "objections": verdict.objections,
                      "demanded_changes": verdict.demanded_changes,
                      "cash": cash}
    if current_book:
        revise_payload["current_book"] = current_book
    revised = runner.run("pm_revise", json.dumps(revise_payload, default=str), Book)
    return verdict, (revised if isinstance(revised, Book) else Book.model_validate(revised))


def _decision_anchored_fills(
    book: Book,
    decision_marks: dict[str, float],
    execution_marks: dict[str, float],
) -> list[dict]:
    """Translate PM decision-time notionals into quantities, then value them at execution.

    A `target_notional` is a decision made against the evidence mark. Its executable quantity is
    therefore `target_notional / decision_mark`. Passing the numerically unchanged notional to
    `apply_fills` with a later execution mark would silently derive a different quantity and trade
    every nominally held leg. Scaling the notional by execution/decision preserves the quantity
    while still letting `apply_fills` use the fresh execution mark and same-snapshot L2 costs.

    The execution mark is the explicit fallback only when a decision mark is unavailable.
    """
    fills: list[dict] = []
    for leg in book.legs:
        decision_mark = float(decision_marks.get(leg.symbol, 0.0) or 0.0)
        execution_mark = float(execution_marks.get(leg.symbol, 0.0) or 0.0)
        execution_notional = float(leg.target_notional)
        if decision_mark > 0.0 and execution_mark > 0.0:
            execution_notional *= execution_mark / decision_mark
        fills.append(
            {
                "symbol": leg.symbol,
                "direction": leg.side,
                "target_notional": execution_notional,
            }
        )
    return fills


def _execution_target_audit(
    account: PaperAccount,
    book: Book,
    decision_marks: dict[str, float],
    execution_marks: dict[str, float],
) -> dict[str, dict]:
    """Describe the exact decision-anchored quantity transition before account mutation."""
    signed_decision_notional: dict[str, float] = {}
    for leg in book.legs:
        sign = 1.0 if leg.side == "long" else -1.0
        signed_decision_notional[leg.symbol] = (
            signed_decision_notional.get(leg.symbol, 0.0)
            + sign * float(leg.target_notional)
        )

    symbols = set(signed_decision_notional) | set(account.positions)
    details: dict[str, dict] = {}
    for symbol in sorted(symbols):
        decision_mark = float(decision_marks.get(symbol, 0.0) or 0.0)
        execution_mark = float(execution_marks.get(symbol, 0.0) or 0.0)
        target_notional = signed_decision_notional.get(symbol, 0.0)
        if decision_mark > 0.0:
            target_qty = target_notional / decision_mark
            quantity_source = "decision_mark"
        elif execution_mark > 0.0:
            target_qty = target_notional / execution_mark
            quantity_source = "execution_mark_fallback"
        else:
            target_qty = 0.0
            quantity_source = "unpriced"

        current_qty = _signed_qty(account.positions.get(symbol))
        delta_qty = target_qty - current_qty
        if math.isclose(target_qty, current_qty, rel_tol=1e-12, abs_tol=1e-12):
            delta_qty = 0.0

        details[symbol] = {
            "quantity_source": quantity_source,
            "decision_target_notional_signed": target_notional,
            "decision_target_qty_signed": target_qty,
            "execution_target_notional_signed": target_qty * execution_mark,
            "current_qty_signed": current_qty,
            "delta_qty_signed": delta_qty,
            "planned_turnover_usd": abs(delta_qty) * execution_mark,
        }
    return details


def reconcile_book(account: PaperAccount, book: Book, *, marks: dict[str, float],
                   costs: dict[str, CostInputs], betas: dict[str, float],
                   now: datetime, cycle: int, cadence: str,
                   decision_marks: dict[str, float] | None = None,
                   execution_ts: datetime | None = None,
                   funding_by_symbol: dict[str, float] | None = None,
                   funding_intervals: dict[str, int] | None = None,
                   specialist_failed: list[str] | None = None) -> CycleReport:
    """Reconcile the paper account to the PM's final book (fills only priced symbols), then compute
    the ACHIEVED deploy% and dollar/beta residual from the resulting held book. Records, never
    vetoes.

    PM target notionals are converted to quantities with `decision_marks`; the fresh `marks` are
    used only to value and fill those quantities. Thus an explicitly unchanged held leg remains an
    exact no-op even if the market moves while agents reason.

    Settles FUNDING on the held-going-in book first (perp desk — carry must cash), then fills.
    Captures this cycle's frictions (fees/slippage deltas + turnover) so no cost is ever invisible
    again (the 2026-07 review found 77.5% of losses were frictions absent from every report)."""
    fill_ts = execution_ts or now
    funding_before = account.funding_received - account.funding_paid
    if funding_by_symbol:
        prev_ts = account.last_funding_ts or fill_ts
        account.settle_funding(prev_ts, fill_ts, funding_by_symbol,
                               funding_intervals or {}, marks)
    fees_before, slip_before = account.fees_paid, account.slippage_paid
    held_before = {s: _signed_qty(p) * marks[s]
                   for s, p in account.positions.items() if s in marks}

    anchored = _decision_anchored_fills(book, decision_marks or marks, marks)
    fills = [fill for fill in anchored if marks.get(fill["symbol"], 0.0) > 0.0]
    unpriced = sorted({lg.symbol for lg in book.legs if marks.get(lg.symbol, 0.0) <= 0.0})
    account.apply_fills(fills, marks, costs, opened_ts=fill_ts, opened_cycle=cycle,
                        opened_cadence=cadence)

    held_after = {s: _signed_qty(p) * marks[s]
                  for s, p in account.positions.items() if s in marks}
    turnover = sum(abs(held_after.get(s, 0.0) - held_before.get(s, 0.0))
                   for s in set(held_before) | set(held_after))
    long_usd = sum(p.qty * marks[s] for s, p in account.positions.items()
                   if p.direction == "long" and s in marks)
    short_usd = sum(p.qty * marks[s] for s, p in account.positions.items()
                    if p.direction == "short" and s in marks)
    gross = long_usd + short_usd
    equity = account.equity(marks)
    dollar_resid = (abs(long_usd - short_usd) / gross) if gross > 0 else 0.0
    beta_net = sum((1 if p.direction == "long" else -1) * p.qty * marks[s] * betas.get(s, 1.0)
                   for s, p in account.positions.items() if s in marks)
    beta_resid = (beta_net / gross) if gross > 0 else 0.0
    deploy = (gross / equity) if equity > 0 else 0.0   # gross exposure / equity-at-these-marks
    return CycleReport(
        cycle=cycle, achieved_deploy_frac=deploy,
        achieved_dollar_residual_frac=dollar_resid,
        achieved_beta_residual=beta_resid,
        equity=equity, n_legs=len(fills),
        ran_at=datetime.now(tz=UTC).isoformat(), decision_ts=now.isoformat(),
        execution_ts=fill_ts.isoformat(),
        decision_age_seconds=(fill_ts - now).total_seconds(),
        turnover_usd=turnover,
        fees_paid_cycle=account.fees_paid - fees_before,
        slippage_paid_cycle=account.slippage_paid - slip_before,
        funding_settled_cycle=(account.funding_received - account.funding_paid) - funding_before,
        stated_deploy_frac=book.stated_deploy_frac,
        stated_dollar_residual_frac=book.stated_dollar_residual_frac,
        stated_beta_residual=book.stated_beta_residual,
        specialist_failed=sorted(specialist_failed or []),
        unpriced_legs=unpriced)


def _cost_inputs(exchange, symbol: str) -> CostInputs:
    """Backward-compatible single-symbol cost helper.

    New reconciliation code must use `_execution_inputs` so the fill mark and depth are captured
    together. This wrapper remains for older callers that only need CostInputs.
    """
    _, costs, _, _ = _execution_inputs(exchange, {symbol}, {})
    return costs[symbol]


def _execution_inputs(
    exchange,
    symbols: Iterable[str],
    decision_marks: dict[str, float],
) -> tuple[dict[str, float], dict[str, CostInputs], dict[str, dict], datetime]:
    """Capture internally consistent execution marks and depth for paper fills.

    Agent provenance and precheck validation continue to use `decision_marks`. At reconcile time,
    each complete two-sided L2 book supplies BOTH the execution reference (its top-of-book midpoint)
    and the depth walked by `apply_fills`. This prevents a delayed cycle from comparing a fresh book
    with an old decision mark and misclassifying intervening market movement as slippage.

    If a two-sided book is unavailable, discard partial/stale depth and use a fresh mark with the
    ADV/half-spread fallback. If that mark also fails, retain the decision mark and the fallback.
    The returned audit block is persisted beside the cycle artifacts.
    """
    execution_marks = dict(decision_marks)
    costs: dict[str, CostInputs] = {}
    audit: dict[str, dict] = {}

    for symbol in sorted(set(symbols)):
        decision_mark = float(decision_marks.get(symbol, 0.0) or 0.0)
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        try:
            depth = exchange.depth(symbol)
            bids = [
                (float(price), float(qty))
                for price, qty in (depth.get("bids") or [])
                if float(price) > 0.0 and float(qty) > 0.0
            ]
            asks = [
                (float(price), float(qty))
                for price, qty in (depth.get("asks") or [])
                if float(price) > 0.0 and float(qty) > 0.0
            ]
        except Exception:  # noqa: BLE001 — missing depth routes to the documented fallback
            bids, asks = [], []

        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        two_sided = bool(bids and asks and best_bid <= best_ask)
        price_source = "book_mid"

        if two_sided:
            execution_mark = (best_bid + best_ask) / 2.0
            spread_bps = (
                (best_ask - best_bid) / execution_mark * 1e4 if execution_mark > 0.0 else 0.0
            )
            half_spread_bps = spread_bps / 2.0
            cost = CostInputs(
                adv_usd=1e9,
                half_spread_bps=half_spread_bps,
                depth_bids=bids,
                depth_asks=asks,
            )
        else:
            # Never combine one side of a new book with an old decision mark. A fresh mark plus the
            # fallback model is less granular but internally coherent and cannot bill price drift.
            bids, asks = [], []
            try:
                execution_mark = float(exchange.mark_price(symbol))
            except Exception:  # noqa: BLE001 — last-resort paper fallback is explicitly audited
                execution_mark = decision_mark
                price_source = "decision_fallback"
            else:
                price_source = "mark_price"
            if execution_mark <= 0.0:
                execution_mark = decision_mark
                price_source = "decision_fallback"
            spread_bps = 0.0
            half_spread_bps = 1.0
            cost = CostInputs(adv_usd=1e9, half_spread_bps=half_spread_bps)

        execution_marks[symbol] = execution_mark
        costs[symbol] = cost
        audit[symbol] = {
            "symbol": symbol,
            "captured_at": datetime.now(tz=UTC).isoformat(),
            "price_source": price_source,
            "decision_mark": decision_mark,
            "execution_mark": execution_mark,
            "decision_to_execution_bps": (
                (execution_mark / decision_mark - 1.0) * 1e4 if decision_mark > 0.0 else 0.0
            ),
            "best_bid": best_bid if two_sided else 0.0,
            "best_ask": best_ask if two_sided else 0.0,
            "spread_bps": spread_bps,
            "half_spread_bps": half_spread_bps,
            "bid_levels": len(bids),
            "ask_levels": len(asks),
            "depth_usd_bid": sum(price * qty for price, qty in bids),
            "depth_usd_ask": sum(price * qty for price, qty in asks),
        }

    return execution_marks, costs, audit, datetime.now(tz=UTC)


def run_cycle(state_dir, *, now: datetime, exchange, runner: AgentRunner, symbols: list[str],
              cash: float, cycle: int, btc_symbol: str = "BTC/USDT:USDT",
              cadence: str = "rebal") -> CycleReport:
    """Run ONE 8h decision cycle end-to-end and persist its artifacts. Deterministic ORCHESTRATION:
    it builds evidence + reconciles the paper account, but every trading judgement is the agents'.

    Steps: build per-coin evidence (BTC always included for the hedge mark + beta ref) -> the three
    specialists rank in parallel -> the PM synthesizes a book against the LIVE account equity -> the
    adversary challenges (one PM revision) -> reconcile the paper account to the final book at real
    marks and compute the ACHIEVED deploy/neutrality (per-name beta from the evidence) -> persist
    reads/book/adversary/report and record equity. `symbols` is this cycle's universe.

    Fail-safe (spec §6): if there is no evidence, or ALL specialists dropped, the cycle HOLDS the
    prior book (no reconcile — no decision on no evidence)."""
    evidence = build_evidence(exchange, symbols, now=now, btc_symbol=btc_symbol)
    marks = {e.symbol: e.mark for e in evidence}
    betas = {e.symbol: e.beta_btc for e in evidence}
    account = load_account(state_dir, default_cash=cash)

    reads = run_specialists(runner, evidence)
    reads_json = {r: [x.model_dump(mode="json") for x in v] for r, v in reads.items()}
    if not evidence or not any(reads.values()):
        equity = account.equity(marks) if marks else account.cash
        report = CycleReport(cycle=cycle, achieved_deploy_frac=0.0,
                             achieved_dollar_residual_frac=0.0, achieved_beta_residual=0.0,
                             equity=equity, n_legs=len(account.positions))
        save_output(state_dir, cycle, "reads", reads_json, cadence=cadence)
        save_output(state_dir, cycle, "report", report.model_dump(mode="json"), cadence=cadence)
        record_equity(state_dir, now, equity, cycle)
        return report

    equity = account.equity(marks)          # the LIVE cash to deploy (cold account -> == cash)
    # Build current_book from held positions so PM can prefer holding them (reduce turnover)
    current_book = [
        {"symbol": s, "side": p.direction,
         "target_notional": abs(p.qty) * marks.get(s, p.entry_price)}
        for s, p in account.positions.items() if s in marks
    ]
    book = run_pm(runner, reads, evidence, cash=equity, current_book=current_book)
    verdict, final = run_adversary(
        runner, book, reads, evidence, cash=equity, current_book=current_book)

    fill_syms = ({leg.symbol for leg in final.legs} | set(account.positions)) & set(marks)
    execution_marks, costs, execution_audit, execution_ts = _execution_inputs(
        exchange, fill_syms, marks
    )
    target_audit = _execution_target_audit(account, final, marks, execution_marks)
    for symbol, detail in target_audit.items():
        execution_audit.setdefault(symbol, {}).update(detail)
    report = reconcile_book(
        account,
        final,
        marks=execution_marks,
        decision_marks=marks,
        costs=costs,
        betas=betas,
        now=now,
        execution_ts=execution_ts,
        cycle=cycle,
        cadence=cadence,
    )
    save_account(state_dir, account)

    save_output(state_dir, cycle, "reads", reads_json, cadence=cadence)
    save_output(state_dir, cycle, "book", final.model_dump(mode="json"), cadence=cadence)
    save_output(state_dir, cycle, "adversary", verdict.model_dump(mode="json"), cadence=cadence)
    save_output(state_dir, cycle, "report", report.model_dump(mode="json"), cadence=cadence)
    save_output(state_dir, cycle, "execution", execution_audit, cadence=cadence)
    record_equity(state_dir, execution_ts, report.equity, cycle)
    return report
