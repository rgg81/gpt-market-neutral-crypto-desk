"""The 24h decision cycle: evidence -> specialists -> PM -> adversary -> reconcile -> report.
Deterministic ORCHESTRATION only; every trading judgement is delegated to the AgentRunner."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from hashlib import sha256
from types import MappingProxyType

from futures_fund.account import (
    CostInputs,
    PaperAccount,
    _signed_qty,
    load_account,
    save_account,
)
from futures_fund.agent_runner import AgentRunner, StubAgentRunner
from futures_fund.costs import trade_fee, vwap_fill
from futures_fund.cycle_io import save_output
from futures_fund.desk_contracts import (
    AdversaryVerdict,
    Book,
    CycleReport,
    SpecialistRead,
)
from futures_fund.equity_log import record_equity
from futures_fund.evidence import EvidencePack, build_evidence
from futures_fund.exchange import quantize_order_quantity
from futures_fund.precheck import (
    EST_SLIPPAGE_BPS_MAX,
    MAX_PAYBACK_FUNDING_INTERVALS,
    PrecheckMetrics,
    _forecast_payback_intervals,
)
from futures_fund.slippage import ExecutionRealism, estimate_slippage, haircut_depth

_SPECIALISTS = ("sentiment", "technical", "futures")
_OFFLINE_STUB_RUN_IMPLEMENTATION = StubAgentRunner.run
_MAX_MARKET_ORDER_CLIPS_PER_SYMBOL = 100


@dataclass(frozen=True, slots=True)
class _OfflineInjectedRunCapability:
    """Opaque, runner-bound authority for the legacy offline integration harness."""

    runner: StubAgentRunner
    canned: Mapping[str, object]


def _is_untampered_exact_stub(runner: AgentRunner) -> bool:
    """Reject subclasses and any process-local replacement of the canned dispatch method."""
    return (
        type(runner) is StubAgentRunner
        and type(runner).run is _OFFLINE_STUB_RUN_IMPLEMENTATION
        and not hasattr(runner, "__dict__")
        and type(runner._canned) is MappingProxyType
    )


def issue_offline_injected_run_capability(runner: AgentRunner) -> object:
    """Issue an opaque capability only for the exact deterministic canned test runner.

    Production subscription orchestration never calls this function or ``run_cycle``. Requiring
    both an exact ``StubAgentRunner`` (subclasses may override ``run``) and a runner-bound opaque
    token keeps the old combined driver useful for integration tests without leaving a plausible
    alternate production entry point.
    """
    if not _is_untampered_exact_stub(runner):
        raise RuntimeError(
            "offline run capability requires an exact StubAgentRunner; production must use "
            "scripts/desk_reconcile.py"
        )
    return _OfflineInjectedRunCapability(runner, runner._canned)


def _verify_offline_injected_run_capability(
    runner: AgentRunner, capability: object | None
) -> None:
    if (
        not _is_untampered_exact_stub(runner)
        or not isinstance(capability, _OfflineInjectedRunCapability)
        or capability.runner is not runner
        or capability.canned is not runner._canned
    ):
        raise RuntimeError(
            "run_cycle is an offline/injected-only test harness and requires a runner-bound "
            "capability from issue_offline_injected_run_capability; production must use "
            "scripts/desk_reconcile.py"
        )


def _evidence_json(evidence: list[EvidencePack]) -> str:
    return json.dumps([e.model_dump(mode="json") for e in evidence], default=str)


def run_specialists(
    runner: AgentRunner, evidence: list[EvidencePack], *, roles: tuple[str, ...] = _SPECIALISTS
) -> dict[str, list[SpecialistRead]]:
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


def run_pm(
    runner: AgentRunner,
    reads: dict[str, list[SpecialistRead]],
    evidence: list[EvidencePack],
    *,
    cash: float,
    current_book: list[dict] | None = None,
) -> Book:
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


def run_adversary(
    runner: AgentRunner,
    book: Book,
    reads: dict[str, list[SpecialistRead]],
    evidence: list[EvidencePack],
    *,
    cash: float,
    current_book: list[dict] | None = None,
) -> tuple[AdversaryVerdict, Book]:
    """Challenge the book once. On reject, ask the PM for ONE revision; else keep the book.

    `current_book` (optional): passed through to the PM revision so it can prefer holding existing
    positions even when addressing adversary objections."""
    challenge_payload = {
        "book": book.model_dump(mode="json"),
        "reads": {r: [x.model_dump(mode="json") for x in v] for r, v in reads.items()},
    }
    if current_book:
        challenge_payload["current_book"] = current_book
    challenge = json.dumps(challenge_payload, default=str)
    verdict = runner.run("adversary", challenge, AdversaryVerdict)
    if not isinstance(verdict, AdversaryVerdict):
        verdict = AdversaryVerdict.model_validate(verdict)
    if verdict.accept:
        return verdict, book
    revise_payload = {
        "original": book.model_dump(mode="json"),
        "objections": verdict.objections,
        "demanded_changes": verdict.demanded_changes,
        "revision_constraints": [
            item.model_dump(mode="json") for item in verdict.revision_constraints
        ],
        "revision_allowed_failing_bounds": (verdict.revision_allowed_failing_bounds),
        "cash": cash,
    }
    if current_book:
        revise_payload["current_book"] = current_book
    revised = runner.run("pm_revise", json.dumps(revise_payload, default=str), Book)
    return verdict, (revised if isinstance(revised, Book) else Book.model_validate(revised))


def _decision_anchored_fills(
    book: Book,
    decision_marks: dict[str, float],
    execution_marks: dict[str, float],
    *,
    cycle: int | None = None,
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
    book_sha256 = sha256(
        json.dumps(
            book.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
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
                "seat_role": leg.seat_role,
                "thesis_cycle": cycle,
                "thesis_book_sha256": book_sha256,
                "expected_price_edge_frac": leg.expected_price_edge_frac,
                "edge_horizon_hours": leg.edge_horizon_hours,
                "edge_calibration_basis": leg.edge_calibration_basis,
                "invalidation_condition": leg.invalidation_condition,
            }
        )
    return fills


def _execution_target_audit(
    account: PaperAccount,
    book: Book,
    decision_marks: dict[str, float],
    execution_marks: dict[str, float],
    execution_audit: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """Describe requested and executable decision-anchored quantity transitions.

    With an execution audit, the PM's raw quantity delta is rounded toward zero to the exchange lot
    step, checked against minimum order notional, and capped at conservatively available displayed
    depth. The resulting target remains mechanically derived from the PM order; code does not pick
    a symbol, side, or discretionary size.
    """
    signed_decision_notional: dict[str, float] = {}
    target_role_by_symbol: dict[str, str] = {}
    for leg in book.legs:
        sign = 1.0 if leg.side == "long" else -1.0
        signed_decision_notional[leg.symbol] = signed_decision_notional.get(
            leg.symbol, 0.0
        ) + sign * float(leg.target_notional)
        # Match PaperAccount consolidation: an alpha component may never be hidden by a
        # same-symbol hedge label.
        if leg.seat_role == "alpha" or leg.symbol not in target_role_by_symbol:
            target_role_by_symbol[leg.symbol] = leg.seat_role

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

        detail = {
            "symbol": symbol,
            "quantity_source": quantity_source,
            "execution_mark": execution_mark,
            "decision_target_notional_signed": target_notional,
            "decision_target_qty_signed": target_qty,
            "execution_target_notional_signed": target_qty * execution_mark,
            "current_qty_signed": current_qty,
            "delta_qty_signed": delta_qty,
            "planned_turnover_usd": abs(delta_qty) * execution_mark,
        }
        execution = (execution_audit or {}).get(symbol)
        if execution is not None:
            step_size = float(execution.get("step_size") or 0.0)
            min_notional = float(execution.get("min_notional") or 0.0)
            min_order_qty_raw = execution.get("min_order_qty")
            min_order_qty = (
                float(min_order_qty_raw) if min_order_qty_raw is not None else 0.0
            )
            max_order_qty_raw = execution.get("max_order_qty")
            max_order_qty = (
                float(max_order_qty_raw) if max_order_qty_raw is not None else math.inf
            )
            if step_size <= 0.0 or min_notional < 0.0 or min_order_qty < 0.0:
                raise RuntimeError(f"invalid execution filters for {symbol}")
            quantized_delta = quantize_order_quantity(delta_qty, step_size)
            quantized_notional = abs(quantized_delta) * execution_mark
            lot_step_rejected = abs(delta_qty) > 1e-12 and abs(quantized_delta) <= 1e-12
            min_notional_pass = (
                abs(quantized_delta) <= 1e-12 or quantized_notional + 1e-12 >= min_notional
            )
            requested_delta = quantized_delta if min_notional_pass else 0.0
            min_qty_pass = (
                abs(quantized_delta) <= 1e-12
                or abs(quantized_delta) + 1e-12 >= min_order_qty
            )
            try:
                order_clips = _market_order_clip_plan(
                    quantized_delta,
                    step_size=step_size,
                    min_order_qty=min_order_qty,
                    max_order_qty=(
                        max_order_qty if math.isfinite(max_order_qty) else None
                    ),
                    min_notional=min_notional,
                    execution_mark=execution_mark,
                )
            except RuntimeError as exc:
                order_clips = []
                max_qty_pass = False
                order_clip_error = str(exc)
            else:
                max_qty_pass = True
                order_clip_error = None
            side_key = (
                "effective_depth_qty_ask" if requested_delta > 0.0 else "effective_depth_qty_bid"
            )
            available_qty = float(execution.get(side_key) or 0.0)
            allow_partial = bool(execution.get("allow_partial_fills", True))
            existing = account.positions.get(symbol)
            target_role = target_role_by_symbol.get(
                symbol, existing.seat_role if existing is not None else "alpha"
            )
            reduction_or_side_change = bool(
                existing is not None
                and (
                    target_qty * current_qty <= 0.0
                    or (
                        target_qty * current_qty > 0.0
                        and abs(target_qty) < abs(current_qty) - 1e-12
                    )
                )
            )
            lifecycle_requires_full_fill = bool(
                existing is not None
                and (
                    reduction_or_side_change
                    or target_role != existing.seat_role
                )
            )
            detail.update(
                {
                    "step_size": step_size,
                    "min_notional": min_notional,
                    "quantized_order_delta_qty_signed": quantized_delta,
                    "quantized_order_notional_usd": quantized_notional,
                    "lot_step_rejected": lot_step_rejected,
                    "min_notional_pass": min_notional_pass,
                    "min_order_qty": min_order_qty_raw,
                    "min_qty_pass": min_qty_pass,
                    "max_order_qty": (
                        max_order_qty if math.isfinite(max_order_qty) else None
                    ),
                    "max_qty_pass": max_qty_pass,
                    "market_order_clip_count": len(order_clips),
                    "market_order_clips_qty_signed": order_clips,
                    "market_order_clip_error": order_clip_error,
                    "effective_crossing_depth_qty": available_qty,
                    "allow_partial_fills": allow_partial,
                    "requested_delta_qty_signed": requested_delta,
                    "target_seat_role": target_role,
                    "current_seat_role": existing.seat_role if existing is not None else None,
                    "lifecycle_requires_full_fill": lifecycle_requires_full_fill,
                }
            )
        details[symbol] = detail

    # A market-neutral book is one basket. Independent per-leg partial fills can turn a requested
    # +$9k/-$9k book into a directional +$9k/-$500 exposure. Use the worst available participation
    # across every changed order, then apply that single fraction to every order. This is execution
    # mechanics, not deterministic trade selection: no symbol is preferred and PM delta ratios are
    # preserved up to exchange lot steps.
    executable = [
        detail
        for detail in details.values()
        if detail.get("step_size") is not None
        and float(detail.get("planned_turnover_usd") or 0.0) > 0.01
    ]
    partial_modes = {bool(detail["allow_partial_fills"]) for detail in executable}
    if len(partial_modes) > 1:
        raise RuntimeError("inconsistent partial-fill policy within one execution basket")
    allow_basket_partial = partial_modes == {True}
    basket_fill_ratio = 1.0
    if allow_basket_partial and executable:
        capacity_ratios: list[float] = []
        for detail in executable:
            requested_qty = abs(float(detail["requested_delta_qty_signed"]))
            if requested_qty <= 1e-12:
                capacity_ratios.append(0.0)
            else:
                available_qty = max(0.0, float(detail["effective_crossing_depth_qty"]))
                capacity_ratios.append(min(1.0, available_qty / requested_qty))
        basket_fill_ratio = min(capacity_ratios, default=1.0)

    for detail in details.values():
        if detail.get("step_size") is None:
            continue
        requested_delta = float(detail["requested_delta_qty_signed"])
        step_size = float(detail["step_size"])
        if abs(requested_delta) <= 1e-12:
            executed_delta = 0.0
        elif allow_basket_partial:
            scaled_qty = abs(requested_delta) * basket_fill_ratio
            executed_delta = math.copysign(
                abs(quantize_order_quantity(scaled_qty, step_size)), requested_delta
            )
        else:
            executed_delta = requested_delta
        current_qty = float(detail["current_qty_signed"])
        executed_target = current_qty + executed_delta
        quantized_delta = float(detail["quantized_order_delta_qty_signed"])
        detail.update(
            {
                "basket_fill_ratio": basket_fill_ratio,
                "basket_partial_fill": basket_fill_ratio < 1.0 - 1e-12,
                "executed_delta_qty_signed": executed_delta,
                "executed_target_qty_signed": executed_target,
                "executed_target_notional_signed": (
                    executed_target * float(detail["execution_mark"])
                ),
                "executed_turnover_usd": abs(executed_delta)
                * float(detail["execution_mark"]),
                "unfilled_order_qty": max(
                    0.0, abs(quantized_delta) - abs(executed_delta)
                ),
                "partial_fill": (
                    abs(executed_delta) + 1e-12 < abs(quantized_delta)
                    and abs(quantized_delta) > 1e-12
                ),
                "fill_ratio": (
                    abs(executed_delta) / abs(quantized_delta)
                    if abs(quantized_delta) > 1e-12
                    else 1.0
                ),
            }
        )
    return details


def _market_order_clip_plan(
    quantity: float,
    *,
    step_size: float,
    min_order_qty: float,
    max_order_qty: float | None,
    min_notional: float,
    execution_mark: float,
) -> list[float]:
    """Split one aggregate change into deterministic exchange-valid market-order clips.

    Binance's ``MARKET_LOT_SIZE.maxQty`` limits each submitted order, not the aggregate position
    change. The full quantity is still priced cumulatively against one conservative L2 snapshot;
    clipping cannot manufacture depth or reduce modeled market impact.
    """
    values = (quantity, step_size, min_order_qty, min_notional, execution_mark)
    if not all(math.isfinite(float(value)) for value in values):
        raise RuntimeError("order clip inputs must be finite")
    if step_size <= 0.0 or min_order_qty < 0.0 or min_notional < 0.0 or execution_mark <= 0.0:
        raise RuntimeError("order clip inputs violate exchange filter domains")
    if abs(quantity) <= 1e-12:
        return []

    step = Decimal(str(step_size))
    absolute = Decimal(str(abs(quantity)))
    units_decimal = absolute / step
    units = int(units_decimal.to_integral_value(rounding=ROUND_HALF_EVEN))
    rebuilt_quantity = float(Decimal(units) * step)
    if units <= 0 or not math.isclose(
        rebuilt_quantity,
        abs(quantity),
        rel_tol=1e-12,
        abs_tol=max(1e-12, step_size * 1e-9),
    ):
        raise RuntimeError("aggregate order quantity is not lot-step aligned")

    if max_order_qty is None:
        max_units = units
    else:
        if not math.isfinite(max_order_qty) or max_order_qty <= 0.0:
            raise RuntimeError("market maxQty is invalid")
        max_units = int(
            (Decimal(str(max_order_qty)) / step).to_integral_value(rounding=ROUND_FLOOR)
        )
    if max_units <= 0:
        raise RuntimeError("market maxQty is below one lot step")

    min_qty_units = int(
        (Decimal(str(min_order_qty)) / step).to_integral_value(rounding=ROUND_CEILING)
    )
    min_notional_units = int(
        (
            Decimal(str(min_notional))
            / (Decimal(str(execution_mark)) * step)
        ).to_integral_value(rounding=ROUND_CEILING)
    )
    min_units = max(1, min_qty_units, min_notional_units)
    clip_count = (units + max_units - 1) // max_units
    if clip_count > _MAX_MARKET_ORDER_CLIPS_PER_SYMBOL:
        raise RuntimeError(
            f"aggregate change needs {clip_count} market-order clips; "
            f"limit is {_MAX_MARKET_ORDER_CLIPS_PER_SYMBOL}"
        )
    if units < clip_count * min_units:
        raise RuntimeError("market maxQty cannot be reconciled with minimum order filters")

    base_units, extra = divmod(units, clip_count)
    sign = 1.0 if quantity > 0.0 else -1.0
    clips = [
        sign * float(Decimal(base_units + (index < extra)) * step)
        for index in range(clip_count)
    ]
    if any(
        abs(clip) + 1e-12 < min_order_qty
        or abs(clip) > (max_order_qty if max_order_qty is not None else math.inf) + 1e-12
        or abs(clip) * execution_mark + 1e-12 < min_notional
        for clip in clips
    ):
        raise RuntimeError("constructed market-order clip violates exchange filters")
    if not math.isclose(
        math.fsum(clips),
        quantity,
        rel_tol=1e-12,
        abs_tol=max(1e-12, step_size * 1e-9),
    ):
        raise RuntimeError("market-order clips do not conserve aggregate quantity")
    return clips


def _verify_execution_liquidity(
    target_audit: dict[str, dict], execution_audit: dict[str, dict]
) -> dict[str, float]:
    """Validate basket execution mechanics and return simulated post-fill signed targets.

    Reductions, drops, flips, and role transfers are fill-or-halt. A single Position cannot
    truthfully represent both an unfilled legacy sleeve and a new target thesis/role, so applying
    partial lifecycle transitions would corrupt attribution even when aggregate quantity is known.
    """
    executed_targets: dict[str, float] = {}
    basket_ratios = {
        round(float(target["basket_fill_ratio"]), 15)
        for target in target_audit.values()
        if target.get("basket_fill_ratio") is not None
        and float(target.get("planned_turnover_usd") or 0.0) > 0.01
    }
    if len(basket_ratios) > 1:
        raise RuntimeError("execution legs do not share one basket participation ratio")
    for symbol, target in target_audit.items():
        turnover = float(target.get("planned_turnover_usd") or 0.0)
        execution = execution_audit.get(symbol) or {}
        if turnover <= 0.01:
            executed_targets[symbol] = float(
                target.get(
                    "executed_target_qty_signed", target.get("decision_target_qty_signed", 0.0)
                )
            )
            continue
        if bool(target.get("lot_step_rejected")):
            raise RuntimeError(f"changed order for {symbol} rounds to zero at exchange lot step")
        if not bool(target.get("min_notional_pass", True)):
            raise RuntimeError(f"changed order for {symbol} is below exchange minimum notional")
        if not bool(target.get("min_qty_pass", True)):
            raise RuntimeError(f"changed order for {symbol} is below exchange market minQty")
        if not bool(target.get("max_qty_pass", True)):
            reason = target.get("market_order_clip_error") or "unknown clip failure"
            raise RuntimeError(
                f"changed order for {symbol} cannot be split within market maxQty: {reason}"
            )
        delta_qty = float(
            target.get("quantized_order_delta_qty_signed", target["delta_qty_signed"])
        )
        side_key = "effective_depth_qty_ask" if delta_qty > 0.0 else "effective_depth_qty_bid"
        visible_qty = float(execution.get(side_key) or 0.0)
        required_qty = abs(delta_qty)
        if execution.get("price_source") != "book_mid":
            raise RuntimeError(
                f"fresh two-sided execution book unavailable for changed symbol {symbol}"
            )
        if (
            not bool(target.get("allow_partial_fills", False))
            and visible_qty + 1e-12 < required_qty
        ):
            raise RuntimeError(
                f"conservative visible {side_key} for changed symbol {symbol} is "
                f"{visible_qty:.12g}, below exchange-valid order quantity {required_qty:.12g}"
            )
        executed_delta = float(target.get("executed_delta_qty_signed", delta_qty))
        if abs(executed_delta) <= 1e-12:
            raise RuntimeError(
                f"common basket participation leaves no exchange-valid fill for {symbol}"
            )
        if abs(executed_delta) > min(required_qty, visible_qty) + 1e-12:
            raise RuntimeError(f"simulated execution exceeds available depth for {symbol}")
        if bool(target.get("lifecycle_requires_full_fill")) and bool(
            target.get("partial_fill")
        ):
            raise RuntimeError(
                f"partial fill would split the held lifecycle for {symbol}; "
                "reductions, drops, flips, and role changes are fill-or-halt"
            )
        step_size = float(target.get("step_size") or 0.0)
        if step_size > 0.0 and not math.isclose(
            executed_delta,
            quantize_order_quantity(executed_delta, step_size),
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise RuntimeError(f"simulated execution is not lot-step aligned for {symbol}")
        executed_notional = abs(executed_delta) * float(target["execution_mark"])
        if executed_notional + 1e-12 < float(target.get("min_notional") or 0.0):
            raise RuntimeError(
                f"common basket partial fill for {symbol} is below exchange minimum notional"
            )
        min_order_qty = float(target.get("min_order_qty") or 0.0)
        if abs(executed_delta) + 1e-12 < min_order_qty:
            raise RuntimeError(
                f"common basket partial fill for {symbol} is below exchange market minQty"
            )
        executed_clips = _market_order_clip_plan(
            executed_delta,
            step_size=step_size,
            min_order_qty=min_order_qty,
            max_order_qty=(
                float(target["max_order_qty"])
                if target.get("max_order_qty") is not None
                else None
            ),
            min_notional=float(target.get("min_notional") or 0.0),
            execution_mark=float(target["execution_mark"]),
        )
        target["executed_market_order_clip_count"] = len(executed_clips)
        target["executed_market_order_clips_qty_signed"] = executed_clips
        executed_targets[symbol] = float(target["executed_target_qty_signed"])
    return executed_targets


def _fresh_one_way_cost(
    symbol: str,
    signed_qty: float,
    execution_mark: float,
    costs: CostInputs,
) -> dict[str, float]:
    """Price one PAPER market order against the exact fresh execution snapshot.

    The reference is the same-book midpoint. Consequently the result contains only crossing/book
    walk plus configured implementation-shortfall reserves; decision-to-execution price movement
    is deliberately absent and remains a separate audit field.
    """
    qty = abs(float(signed_qty))
    mark = float(execution_mark)
    if not math.isfinite(qty) or not math.isfinite(mark) or mark <= 0.0:
        raise RuntimeError(f"invalid fresh execution quantity/mark for {symbol}")
    if qty <= 1e-12:
        return {
            "midpoint_notional_usd": 0.0,
            "book_walk_slippage_usd": 0.0,
            "book_walk_slippage_bps": 0.0,
            "adverse_selection_reserve_usd": 0.0,
            "legging_reserve_usd": 0.0,
            "total_slippage_usd": 0.0,
            "total_slippage_bps": 0.0,
            "fee_usd": 0.0,
            "fee_bps": 0.0,
            "friction_usd": 0.0,
            "friction_bps": 0.0,
        }
    if costs.depth is not None:
        depth = costs.depth
    elif signed_qty > 0.0:
        depth = costs.depth_asks
    else:
        depth = costs.depth_bids
    if not depth:
        raise RuntimeError(f"fresh crossing depth unavailable for {symbol}")
    filled_qty, _vwap = vwap_fill(depth, qty)
    if filled_qty + 1e-12 < qty:
        raise RuntimeError(
            f"fresh crossing depth cannot fully price {qty:.12g} contracts for {symbol}"
        )

    midpoint_notional = qty * mark
    total_slippage = estimate_slippage(
        symbol,
        qty,
        mark,
        depth=depth,
        adv_usd=costs.adv_usd,
        half_spread_bps=costs.half_spread_bps,
        adverse_selection_bps=costs.adverse_selection_bps,
        legging_bps=costs.legging_bps,
    )
    adverse_reserve = midpoint_notional * costs.adverse_selection_bps / 1e4
    legging_reserve = midpoint_notional * costs.legging_bps / 1e4
    book_walk = total_slippage - adverse_reserve - legging_reserve
    quote_notional = midpoint_notional + math.copysign(total_slippage, signed_qty)
    if (
        not all(
            math.isfinite(value)
            for value in (
                midpoint_notional,
                total_slippage,
                adverse_reserve,
                legging_reserve,
                book_walk,
                quote_notional,
            )
        )
        or min(total_slippage, adverse_reserve, legging_reserve, book_walk) < -1e-9
        or quote_notional <= 0.0
    ):
        raise RuntimeError(f"invalid fresh execution-cost result for {symbol}")
    book_walk = max(book_walk, 0.0)
    fee = trade_fee(quote_notional, maker=costs.maker)
    friction = total_slippage + fee
    return {
        "midpoint_notional_usd": midpoint_notional,
        "book_walk_slippage_usd": book_walk,
        "book_walk_slippage_bps": book_walk / midpoint_notional * 1e4,
        "adverse_selection_reserve_usd": adverse_reserve,
        "legging_reserve_usd": legging_reserve,
        "total_slippage_usd": total_slippage,
        "total_slippage_bps": total_slippage / midpoint_notional * 1e4,
        "fee_usd": fee,
        "fee_bps": fee / midpoint_notional * 1e4,
        "friction_usd": friction,
        "friction_bps": friction / midpoint_notional * 1e4,
    }


def _verify_fresh_execution_economics(
    book: Book,
    precheck: PrecheckMetrics,
    target_audit: dict[str, dict],
    execution_audit: dict[str, dict],
    costs: dict[str, CostInputs],
    betas: dict[str, float],
    *,
    decision_ts: datetime,
    execution_ts: datetime,
    cadence_tf_minutes: int,
    b12_failure_authorized: bool = False,
) -> dict[str, object]:
    """Revalidate the agents' B10/B12 authorization against fresh L2 before PAPER mutation.

    This never chooses, resizes, or substitutes a trade. It prices the exchange-valid basket that
    mechanical execution produced and halts when new liquidity makes the already-authorized
    economics stale. B10's 75bp implementation-shortfall ceiling is absolute. B12 normally keeps
    its ten-interval ceiling; an explicitly bound Adversary exception may retain, but never worsen,
    the exact per-change payback that was reviewed.
    """
    if not isinstance(decision_ts, datetime) or decision_ts.tzinfo is None:
        raise RuntimeError("fresh execution economics requires an aware decision timestamp")
    if not isinstance(execution_ts, datetime) or execution_ts.tzinfo is None:
        raise RuntimeError("fresh execution economics requires an aware execution timestamp")
    decision_utc = decision_ts.astimezone(UTC)
    execution_utc = execution_ts.astimezone(UTC)
    try:
        cadence_minutes = float(cadence_tf_minutes)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("fresh execution economics cadence TTL is invalid") from exc
    if (
        not math.isfinite(cadence_minutes)
        or cadence_minutes <= 0.0
        or not cadence_minutes.is_integer()
    ):
        raise RuntimeError("fresh execution economics cadence TTL is invalid")
    cadence_hours = cadence_minutes / 60.0
    basket_decision_age_hours = (execution_utc - decision_utc).total_seconds() / 3600.0
    if not math.isfinite(basket_decision_age_hours) or basket_decision_age_hours < 0.0:
        raise RuntimeError("fresh basket execution precedes its decision timestamp")
    if basket_decision_age_hours >= cadence_hours:
        raise RuntimeError(
            "fresh basket decision cadence TTL expired: "
            f"age={basket_decision_age_hours:.12g}h limit={cadence_hours:.12g}h"
        )

    b12_bound = next((bound for bound in precheck.bounds if bound.bound_id == "B12"), None)
    if b12_bound is None:
        raise RuntimeError("final precheck lacks B12")
    if not b12_bound.ok and not b12_failure_authorized:
        raise RuntimeError("failed final B12 lacks explicit Adversary authorization")

    rows = {row.symbol: row for row in precheck.change_costs}
    if len(rows) != len(precheck.change_costs):
        raise RuntimeError("final precheck contains duplicate change-cost symbols")
    leg_metrics = {leg.symbol: leg for leg in precheck.legs}
    changed_symbols = {
        symbol
        for symbol, target in target_audit.items()
        if abs(float(target.get("executed_delta_qty_signed") or 0.0)) > 1e-12
    }
    missing_rows = sorted(changed_symbols - set(rows))
    if missing_rows:
        raise RuntimeError(
            f"fresh executable changes lack B12 authorization rows: {missing_rows}"
        )
    missing_betas = sorted(set(target_audit) - set(betas))
    invalid_betas = {
        symbol: betas.get(symbol)
        for symbol in target_audit
        if symbol in betas and not math.isfinite(float(betas[symbol]))
    }
    if missing_betas or invalid_betas:
        raise RuntimeError(
            "fresh execution economics lacks finite bound betas: "
            f"missing={missing_betas} invalid={invalid_betas}"
        )

    total_friction = 0.0
    worst_payback = 0.0
    for symbol, row in rows.items():
        target = target_audit.get(symbol)
        execution = execution_audit.get(symbol)
        ci = costs.get(symbol)
        if target is None or execution is None or ci is None:
            raise RuntimeError(f"fresh execution inputs incomplete for B12 change {symbol}")
        mark = float(target.get("execution_mark") or 0.0)
        delta_qty = float(target.get("executed_delta_qty_signed") or 0.0)
        target_qty = float(target.get("executed_target_qty_signed") or 0.0)
        current_qty = float(target.get("current_qty_signed") or 0.0)
        metric = leg_metrics.get(symbol)

        raw_execution_ts = execution.get("execution_ts")
        try:
            leg_execution_ts = datetime.fromisoformat(str(raw_execution_ts))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"fresh execution economics lacks a valid execution timestamp for {symbol}"
            ) from exc
        if leg_execution_ts.tzinfo is None:
            raise RuntimeError(
                f"fresh execution economics execution timestamp is naive for {symbol}"
            )
        leg_execution_utc = leg_execution_ts.astimezone(UTC)
        if leg_execution_utc > execution_utc:
            raise RuntimeError(
                f"per-leg execution timestamp exceeds basket execution timestamp for {symbol}"
            )
        decision_age_hours = (leg_execution_utc - decision_utc).total_seconds() / 3600.0
        if not math.isfinite(decision_age_hours) or decision_age_hours < 0.0:
            raise RuntimeError(
                f"fresh execution precedes its decision timestamp for {symbol}"
            )

        immediate = _fresh_one_way_cost(symbol, delta_qty, mark, ci)
        future_exit_qty = 0.0
        if metric is not None:
            if metric.change in {"new", "flipped"}:
                future_exit_qty = -target_qty
            elif metric.change == "resized" and metric.material_effect == "increase":
                future_exit_qty = -math.copysign(abs(delta_qty), target_qty)
        future = _fresh_one_way_cost(symbol, future_exit_qty, mark, ci)

        hedge_exemption_review: dict[str, object] | None = None
        effective_insurance_exempt = False
        if row.b12_insurance_exempt:
            if symbol != "BTC/USDT:USDT" or row.seat_role != "hedge":
                raise RuntimeError(
                    f"non-BTC change carries a B12 hedge insurance exemption: {symbol}"
                )
            alpha_beta_net = sum(
                float(detail.get("executed_target_qty_signed") or 0.0)
                * float(detail.get("execution_mark") or 0.0)
                * float(betas[target_symbol])
                for target_symbol, detail in target_audit.items()
                if target_symbol != symbol
            )
            btc_beta = float(betas[symbol])
            proposed_hedge_beta = target_qty * mark * btc_beta
            carried_hedge_beta = current_qty * mark * btc_beta
            final_beta_net = alpha_beta_net + proposed_hedge_beta
            carried_beta_net = alpha_beta_net + carried_hedge_beta
            improves_vs_alpha = abs(final_beta_net) + 1e-9 < abs(alpha_beta_net)
            improves_vs_carried = abs(final_beta_net) + 1e-9 < abs(carried_beta_net)
            no_exposure_change = math.isclose(
                target_qty, current_qty, rel_tol=1e-12, abs_tol=1e-12
            )
            if row.action == "drop":
                effective_insurance_exempt = improves_vs_carried
            else:
                effective_insurance_exempt = improves_vs_alpha and (
                    improves_vs_carried or no_exposure_change
                )
            hedge_exemption_review = {
                "action": row.action,
                "bound_beta": btc_beta,
                "alpha_only_beta_net_usd": alpha_beta_net,
                "proposed_hedge_beta_usd": proposed_hedge_beta,
                "carried_hedge_beta_usd": carried_hedge_beta,
                "final_beta_net_usd": final_beta_net,
                "carried_counterfactual_beta_net_usd": carried_beta_net,
                "risk_reducing_vs_alpha_only": improves_vs_alpha,
                "risk_reducing_vs_carried_hedge": improves_vs_carried,
                "no_exposure_change": no_exposure_change,
                "fresh_insurance_exemption_valid": effective_insurance_exempt,
            }
            if not effective_insurance_exempt:
                raise RuntimeError(
                    f"fresh BTC hedge exemption is no longer risk-reducing for {row.action}: "
                    f"alpha_only={alpha_beta_net:.12g}, final={final_beta_net:.12g}, "
                    f"carried={carried_beta_net:.12g}"
                )

        worst_slippage_bps = max(
            immediate["total_slippage_bps"], future["total_slippage_bps"]
        )
        if worst_slippage_bps > EST_SLIPPAGE_BPS_MAX + 1e-12:
            raise RuntimeError(
                f"fresh B10 slippage for {symbol} is {worst_slippage_bps:.6g}bp, "
                f"above {EST_SLIPPAGE_BPS_MAX:.6g}bp"
            )

        friction = immediate["friction_usd"] + future["friction_usd"]
        decision_mark = float(execution.get("decision_mark") or 0.0)
        drift_bps = float(execution.get("decision_to_execution_bps") or 0.0)
        final_side_sign = (
            1.0
            if metric is not None and metric.side == "long"
            else -1.0
        )
        favorable_drift_frac = (
            max(final_side_sign * (mark / decision_mark - 1.0), 0.0)
            if decision_mark > 0.0 and metric is not None
            else 0.0
        )
        remaining_price_edge_frac: float | None = None
        decision_economic_clip = 0.0
        original_forecast_price_edge = 0.0
        time_adjusted_forecast_price_edge = 0.0
        price_target_remaining_forecast_edge = 0.0
        price_edge = 0.0
        forecast_time_decay = 0.0
        consumed_forecast_price_edge = 0.0
        original_forecast_horizon_hours: float | None = None
        remaining_forecast_horizon_hours: float | None = None
        forecast_time_remaining_frac: float | None = None
        if (
            row.action == "role_change"
            and abs(delta_qty) <= 1e-12
            and (metric is None or metric.material_effect not in {"entry", "flip", "increase"})
        ):
            economic_clip = abs(target_qty) * mark
            horizon_intervals = (
                max(metric.edge_horizon_hours / 8.0, 1.0) if metric is not None else 1.0
            )
            expected_edge = float(
                row.expected_edge_through_horizon_pre_friction_usd or 0.0
            )
            payback = 0.0
        elif metric is not None and metric.material_effect in {"entry", "flip", "increase"}:
            semantic_alpha_entry = bool(
                row.action == "role_change"
                and row.prior_seat_role == "hedge"
                and row.final_seat_role == "alpha"
            )
            if semantic_alpha_entry:
                economic_clip = abs(target_qty) * mark
                decision_economic_clip = abs(target_qty) * decision_mark
            elif metric.material_effect in {"entry", "flip"}:
                economic_clip = abs(target_qty) * mark
                decision_economic_clip = abs(target_qty) * decision_mark
            else:
                economic_clip = abs(delta_qty) * mark
                decision_economic_clip = abs(delta_qty) * decision_mark
            original_forecast_horizon_hours = float(metric.edge_horizon_hours)
            remaining_forecast_horizon_hours = (
                original_forecast_horizon_hours - decision_age_hours
            )
            if remaining_forecast_horizon_hours <= 0.0:
                raise RuntimeError(
                    f"fresh alpha forecast expired for {symbol}: "
                    f"age={decision_age_hours:.12g}h "
                    f"horizon={original_forecast_horizon_hours:.12g}h"
                )
            forecast_time_remaining_frac = (
                remaining_forecast_horizon_hours / original_forecast_horizon_hours
            )
            horizon_intervals = remaining_forecast_horizon_hours / 8.0
            # A favorable decision-to-execution move is not slippage, but it can consume the
            # finite return forecast the PM authorized from the decision mark. Do not assume an
            # adverse move creates extra edge. Elapsed time also cannot renew the PM forecast.
            # Time and favorable price are competing observations of the same forecast, so use
            # their more conservative residual rather than double-consuming it. All dollar
            # arithmetic stays anchored to the decision quantity and mark, so a favorable long
            # repricing cannot inflate its residual edge.
            original_forecast_price_edge = (
                metric.expected_price_edge_frac * decision_economic_clip
            )
            time_adjusted_forecast_price_edge = (
                original_forecast_price_edge * forecast_time_remaining_frac
            )
            forecast_time_decay = (
                original_forecast_price_edge - time_adjusted_forecast_price_edge
            )
            consumed_forecast_price_edge = (
                favorable_drift_frac * decision_economic_clip
            )
            price_target_remaining_forecast_edge = (
                original_forecast_price_edge - consumed_forecast_price_edge
            )
            price_edge = min(
                time_adjusted_forecast_price_edge,
                price_target_remaining_forecast_edge,
            )
            remaining_price_edge_frac = (
                price_edge / decision_economic_clip
                if decision_economic_clip > 1e-12
                else metric.expected_price_edge_frac
            )
            carry_per_interval = (
                metric.selected_side_carry_bps_8h / 1e4 * economic_clip
            )
            payback = _forecast_payback_intervals(
                friction,
                price_edge,
                carry_per_interval,
                horizon_intervals,
            )
            expected_edge = price_edge + carry_per_interval * horizon_intervals
        elif metric is not None and metric.material_effect == "reduction":
            economic_clip = abs(delta_qty) * mark
            horizon_intervals = max(metric.edge_horizon_hours / 8.0, 1.0)
            carry_per_interval = (
                metric.selected_side_carry_bps_8h / 1e4 * economic_clip
            )
            avoided_adverse_carry = -carry_per_interval
            payback = (
                friction / avoided_adverse_carry
                if avoided_adverse_carry > 1e-9
                else float("inf")
            )
            expected_edge = avoided_adverse_carry * horizon_intervals
        elif row.action == "drop":
            economic_clip = abs(current_qty) * mark
            horizon_intervals = 3.0
            authorized_clip = float(row.executable_turnover_usd or 0.0)
            authorized_edge = float(
                row.expected_edge_through_horizon_pre_friction_usd or 0.0
            )
            expected_edge = (
                authorized_edge * economic_clip / authorized_clip
                if authorized_clip > 1e-12
                else 0.0
            )
            avoided_adverse_carry = expected_edge / horizon_intervals
            payback = (
                friction / avoided_adverse_carry
                if avoided_adverse_carry > 1e-9
                else float("inf")
            )
        else:
            raise RuntimeError(f"cannot reproduce fresh B12 economics for {symbol}")

        normalized_payback = payback if math.isfinite(payback) else 9999.0
        authorized_limit = MAX_PAYBACK_FUNDING_INTERVALS
        if not b12_bound.ok and b12_failure_authorized:
            authorized_limit = max(authorized_limit, float(row.payback_intervals))
        b12_passed = bool(
            effective_insurance_exempt
            or normalized_payback <= authorized_limit + 1e-12
        )
        if not b12_passed:
            raise RuntimeError(
                f"fresh B12 payback for {symbol} is {normalized_payback:.6g} intervals, "
                f"above authorized {authorized_limit:.6g}"
            )

        execution.update(
            {
                "fresh_market_drift_bps": drift_bps,
                "fresh_market_drift_excluded_from_slippage": True,
                "fresh_decision_ts": decision_utc.isoformat(),
                "fresh_execution_ts": leg_execution_utc.isoformat(),
                "fresh_decision_age_hours": decision_age_hours,
                "fresh_cadence_ttl_minutes": int(cadence_minutes),
                "fresh_cadence_ttl_hours": cadence_hours,
                "fresh_cadence_ttl_passed": True,
                "fresh_favorable_drift_consumed_price_edge_frac": favorable_drift_frac,
                "fresh_remaining_expected_price_edge_frac": remaining_price_edge_frac,
                "fresh_decision_anchored_economic_clip_usd": decision_economic_clip,
                "fresh_original_forecast_horizon_hours": (
                    original_forecast_horizon_hours
                ),
                "fresh_remaining_forecast_horizon_hours": (
                    remaining_forecast_horizon_hours
                ),
                "fresh_forecast_time_remaining_frac": forecast_time_remaining_frac,
                "fresh_original_forecast_price_edge_usd": original_forecast_price_edge,
                "fresh_time_adjusted_forecast_price_edge_usd": (
                    time_adjusted_forecast_price_edge
                ),
                "fresh_forecast_time_decay_usd": forecast_time_decay,
                "fresh_favorable_drift_consumed_price_edge_usd": (
                    consumed_forecast_price_edge
                ),
                "fresh_price_target_remaining_forecast_price_edge_usd": (
                    price_target_remaining_forecast_edge
                ),
                "fresh_remaining_forecast_price_edge_usd": price_edge,
                "fresh_execution_midpoint_notional_usd": immediate[
                    "midpoint_notional_usd"
                ],
                "fresh_execution_book_walk_slippage_usd": immediate[
                    "book_walk_slippage_usd"
                ],
                "fresh_execution_book_walk_slippage_bps": immediate[
                    "book_walk_slippage_bps"
                ],
                "fresh_execution_adverse_selection_reserve_usd": immediate[
                    "adverse_selection_reserve_usd"
                ],
                "fresh_execution_adverse_selection_reserve_bps": (
                    ci.adverse_selection_bps
                ),
                "fresh_execution_legging_reserve_usd": immediate[
                    "legging_reserve_usd"
                ],
                "fresh_execution_legging_reserve_bps": ci.legging_bps,
                "fresh_execution_total_slippage_usd": immediate[
                    "total_slippage_usd"
                ],
                "fresh_execution_total_slippage_bps": immediate[
                    "total_slippage_bps"
                ],
                "fresh_execution_fee_usd": immediate["fee_usd"],
                "fresh_execution_fee_bps": immediate["fee_bps"],
                "fresh_execution_one_way_friction_usd": immediate["friction_usd"],
                "fresh_execution_one_way_friction_bps": immediate["friction_bps"],
                "fresh_b10_limit_bps": EST_SLIPPAGE_BPS_MAX,
                "fresh_b10_worst_one_way_slippage_bps": worst_slippage_bps,
                "fresh_b10_passed": True,
                "fresh_b12_future_exit_qty_signed": future_exit_qty,
                "fresh_b12_future_exit_midpoint_notional_usd": future[
                    "midpoint_notional_usd"
                ],
                "fresh_b12_future_exit_slippage_usd": future[
                    "total_slippage_usd"
                ],
                "fresh_b12_future_exit_slippage_bps": future[
                    "total_slippage_bps"
                ],
                "fresh_b12_future_exit_fee_usd": future["fee_usd"],
                "fresh_b12_future_exit_friction_usd": future["friction_usd"],
                "fresh_b12_future_exit_friction_bps": future["friction_bps"],
                "fresh_b12_total_friction_usd": friction,
                "fresh_b12_precheck_friction_usd": row.friction_usd,
                "fresh_b12_precheck_payback_intervals": row.payback_intervals,
                "fresh_b12_expected_edge_through_horizon_usd": expected_edge,
                "fresh_b12_payback_intervals": normalized_payback,
                "fresh_b12_authorized_payback_limit": authorized_limit,
                "fresh_b12_precheck_insurance_exempt": row.b12_insurance_exempt,
                "fresh_b12_insurance_exempt": effective_insurance_exempt,
                "fresh_b12_hedge_exemption_review": hedge_exemption_review,
                "fresh_b12_passed": True,
            }
        )
        total_friction += friction
        if not effective_insurance_exempt:
            worst_payback = max(worst_payback, normalized_payback)

    return {
        "fresh_decision_ts": decision_utc.isoformat(),
        "fresh_execution_ts": execution_utc.isoformat(),
        "fresh_basket_decision_age_hours": basket_decision_age_hours,
        "fresh_cadence_ttl_minutes": int(cadence_minutes),
        "fresh_cadence_ttl_hours": cadence_hours,
        "fresh_cadence_ttl_passed": True,
        "fresh_total_action_friction_usd": total_friction,
        "fresh_worst_b12_payback_intervals": worst_payback,
        "fresh_execution_economics_passed": True,
    }


def _achieved_execution_safety(
    account: PaperAccount,
    marks: dict[str, float],
    betas: dict[str, float],
    *,
    minimum_deploy_frac: float = 0.75,
) -> dict[str, object]:
    """Compute hard post-execution B1-B6 safety from held PAPER quantities.

    Lower deployment remains an agent-approved B1 exception. Upper leverage, dollar/beta
    neutrality, concentration, hedge size, and per-leg beta-dollar limits never are. These are
    achieved execution facts, not a second trade decision.
    """
    missing_marks = sorted(set(account.positions) - set(marks))
    if missing_marks:
        raise RuntimeError(f"held symbols lack achieved execution marks: {missing_marks}")
    invalid_marks = {
        symbol: marks[symbol]
        for symbol in account.positions
        if not math.isfinite(float(marks[symbol])) or float(marks[symbol]) <= 0.0
    }
    if invalid_marks:
        raise RuntimeError(f"held symbols have invalid achieved execution marks: {invalid_marks}")
    invalid_betas = {
        symbol: betas.get(symbol)
        for symbol in account.positions
        if symbol in betas and not math.isfinite(float(betas[symbol]))
    }
    if invalid_betas:
        raise RuntimeError(f"held symbols have invalid achieved betas: {invalid_betas}")
    equity = account.equity(marks)
    signed_notionals = {
        symbol: _signed_qty(position) * marks[symbol]
        for symbol, position in account.positions.items()
        if symbol in marks
    }
    gross = sum(abs(value) for value in signed_notionals.values())
    long_usd = sum(max(value, 0.0) for value in signed_notionals.values())
    short_usd = sum(max(-value, 0.0) for value in signed_notionals.values())
    deploy = gross / equity if equity > 0.0 else math.inf
    dollar_residual = abs(long_usd - short_usd) / gross if gross > 0.0 else 0.0
    beta_dollars = {
        symbol: value * float(betas.get(symbol, 1.0))
        for symbol, value in signed_notionals.items()
    }
    beta_residual = sum(beta_dollars.values()) / equity if equity > 0.0 else math.inf
    max_leg_frac = (
        max((abs(value) for value in signed_notionals.values()), default=0.0) / gross
        if gross > 0.0
        else 0.0
    )
    btc_hedge_usd = sum(
        abs(signed_notionals.get(symbol, 0.0))
        for symbol, position in account.positions.items()
        if symbol == "BTC/USDT:USDT" and position.seat_role == "hedge"
    )
    btc_hedge_frac = btc_hedge_usd / equity if equity > 0.0 else math.inf
    max_leg_beta_frac = (
        max((abs(value) for value in beta_dollars.values()), default=0.0) / equity
        if equity > 0.0
        else math.inf
    )
    if not math.isfinite(minimum_deploy_frac) or not 0.0 <= minimum_deploy_frac <= 0.75:
        raise ValueError("minimum achieved deployment must be in [0, 0.75]")
    violations: list[str] = []
    if not all(
        math.isfinite(value)
        for value in (
            equity,
            gross,
            deploy,
            dollar_residual,
            beta_residual,
            max_leg_frac,
            btc_hedge_frac,
            max_leg_beta_frac,
        )
    ):
        violations.append("non_finite_achieved_metric")
    if equity <= 0.0:
        violations.append("equity_non_positive")
    if deploy > 1.15 + 1e-12:
        violations.append("B1_upper")
    if deploy + 1e-12 < minimum_deploy_frac:
        violations.append("B1_lower_unapproved")
    if dollar_residual > 0.10 + 1e-12:
        violations.append("B2_dollar_residual")
    if abs(beta_residual) > 0.15 + 1e-12:
        violations.append("B3_beta_residual")
    if max_leg_frac > 0.35 + 1e-12:
        violations.append("B4_concentration")
    if btc_hedge_frac > 0.50 + 1e-12:
        violations.append("B5_btc_hedge")
    if max_leg_beta_frac > 0.60 + 1e-12:
        violations.append("B6_leg_beta")
    return {
        "equity": equity,
        "gross_usd": gross,
        "achieved_deploy_frac": deploy,
        "achieved_dollar_residual_frac": dollar_residual,
        "achieved_beta_residual": beta_residual,
        "achieved_max_leg_frac": max_leg_frac,
        "achieved_btc_hedge_frac": btc_hedge_frac,
        "achieved_max_leg_beta_frac": max_leg_beta_frac,
        "achieved_safety_passed": not violations,
        "achieved_safety_violations": violations,
        "minimum_authorized_deploy_frac": minimum_deploy_frac,
    }


def reconcile_book(
    account: PaperAccount,
    book: Book,
    *,
    marks: dict[str, float],
    costs: dict[str, CostInputs],
    betas: dict[str, float],
    now: datetime,
    cycle: int,
    cadence: str,
    decision_marks: dict[str, float] | None = None,
    execution_ts: datetime | None = None,
    funding_by_symbol: dict[str, float] | None = None,
    funding_intervals: dict[str, int] | None = None,
    funding_events_by_symbol: dict[str, list[dict]] | None = None,
    specialist_failed: list[str] | None = None,
    target_signed_quantities: dict[str, float] | None = None,
    execution_ts_by_symbol: dict[str, datetime] | None = None,
    execution_completed_by_symbol: dict[str, bool] | None = None,
    enforce_achieved_safety: bool = True,
    minimum_achieved_deploy_frac: float = 0.75,
) -> CycleReport:
    """Reconcile the paper account to the PM's final book (fills only priced symbols), then compute
    the ACHIEVED deploy% and dollar/beta residual from the resulting held book. Records, never
    vetoes.

    PM target notionals are converted to quantities with `decision_marks`; the fresh `marks` are
    used only to value and fill those quantities. Thus an explicitly unchanged held leg remains an
    exact no-op even if the market moves while agents reason.

    Settles FUNDING on the held-going-in book first (perp desk — carry must cash), then fills.
    Captures this cycle's frictions (fees/slippage deltas + turnover) so no cost is ever invisible
    again (the 2026-07 review found 77.5% of losses were frictions absent from every report)."""
    # Stage funding, fills, lifecycle changes, and frictions on a deep copy. A failed achieved-book
    # safety check must leave the prior account byte-for-byte intact; otherwise "fail closed" could
    # still publish half a basket or advance the funding clock before raising.
    working = account.model_copy(deep=True)
    fill_ts = execution_ts or now
    funding_before = working.funding_received - working.funding_paid
    if funding_events_by_symbol is not None:
        working.settle_funding_events(
            funding_events_by_symbol,
            now=fill_ts,
            observed_intervals=funding_intervals,
        )
    elif funding_by_symbol:
        if working.positions and working.last_funding_ts is None:
            raise RuntimeError(
                "held account has no funding clock; explicit audited migration is required"
            )
        prev_ts = working.last_funding_ts or fill_ts
        working.settle_funding(
            prev_ts,
            fill_ts,
            funding_by_symbol,
            funding_intervals or {},
            marks,
        )
    elif not working.positions and working.last_funding_ts is None:
        # A new account has no historical exposure to settle. Establishing its clock immediately
        # before the first fill is exact; doing the same for a held account would erase history.
        working.last_funding_ts = fill_ts
    fees_before, slip_before = working.fees_paid, working.slippage_paid
    held_before = {
        s: _signed_qty(p) * marks[s] for s, p in working.positions.items() if s in marks
    }

    anchored = _decision_anchored_fills(book, decision_marks or marks, marks, cycle=cycle)
    fills = [fill for fill in anchored if marks.get(fill["symbol"], 0.0) > 0.0]
    unpriced = sorted({lg.symbol for lg in book.legs if marks.get(lg.symbol, 0.0) <= 0.0})
    working.apply_fills(
        fills,
        marks,
        costs,
        opened_ts=fill_ts,
        opened_cycle=cycle,
        opened_cadence=cadence,
        target_signed_quantities=target_signed_quantities,
        execution_ts_by_symbol=execution_ts_by_symbol,
        execution_completed_by_symbol=execution_completed_by_symbol,
    )
    if funding_intervals is not None:
        working.funding_intervals_observed = {
            symbol: int(funding_intervals[symbol])
            for symbol in working.positions
            if symbol in funding_intervals
        }
    # Mutating BaseModel fields does not re-run Pydantic validation. Re-parse the fully staged
    # generation before evaluating safety or copying a single byte back to the live account.
    working._validated_for_persistence()

    held_after = {
        s: _signed_qty(p) * marks[s] for s, p in working.positions.items() if s in marks
    }
    turnover = sum(
        abs(held_after.get(s, 0.0) - held_before.get(s, 0.0))
        for s in set(held_before) | set(held_after)
    )
    safety = _achieved_execution_safety(
        working,
        marks,
        betas,
        minimum_deploy_frac=minimum_achieved_deploy_frac,
    )
    equity = float(safety["equity"])
    dollar_resid = float(safety["achieved_dollar_residual_frac"])
    beta_resid = float(safety["achieved_beta_residual"])
    deploy = float(safety["achieved_deploy_frac"])
    if enforce_achieved_safety:
        violations = list(safety["achieved_safety_violations"])
        if violations:
            raise RuntimeError(
                "post-execution basket violates achieved B1-B6 safety: "
                + ", ".join(violations)
            )

    report = CycleReport(
        cycle=cycle,
        achieved_deploy_frac=deploy,
        achieved_dollar_residual_frac=dollar_resid,
        achieved_beta_residual=beta_resid,
        equity=equity,
        n_legs=len(working.positions),
        ran_at=datetime.now(tz=UTC).isoformat(),
        decision_ts=now.isoformat(),
        execution_ts=fill_ts.isoformat(),
        decision_age_seconds=(fill_ts - now).total_seconds(),
        turnover_usd=turnover,
        fees_paid_cycle=working.fees_paid - fees_before,
        slippage_paid_cycle=working.slippage_paid - slip_before,
        funding_settled_cycle=(working.funding_received - working.funding_paid) - funding_before,
        stated_deploy_frac=book.stated_deploy_frac,
        stated_dollar_residual_frac=book.stated_dollar_residual_frac,
        stated_beta_residual=book.stated_beta_residual,
        specialist_failed=sorted(specialist_failed or []),
        unpriced_legs=unpriced,
    )
    for field_name in PaperAccount.model_fields:
        setattr(account, field_name, getattr(working, field_name))
    return report


def _cost_inputs(exchange, symbol: str) -> CostInputs:
    """Backward-compatible single-symbol cost helper.

    New reconciliation code must use `_execution_inputs` so the fill mark and depth are captured
    together. This wrapper remains for older callers that only need CostInputs.
    """
    _, costs, _, _ = _execution_inputs(exchange, {symbol}, {})
    return costs[symbol]


def _validate_l2_side(
    symbol: str,
    side: str,
    raw_levels: object,
) -> list[tuple[float, float]]:
    """Normalize one raw L2 side while preserving its exact order for replay.

    Binance market depth is aggregated by price and arrives in crossing order: bids strictly
    decrease from best to worst and asks strictly increase. Silently sorting, deduplicating, or
    dropping malformed rows would invent a different book, so every shape/value/order defect is a
    hard data-integrity failure.
    """
    if side not in {"bids", "asks"}:
        raise ValueError(f"invalid L2 side: {side}")
    if not isinstance(raw_levels, (list, tuple)):
        raise RuntimeError(f"invalid fresh L2 {side} container for {symbol}")

    descending = side == "bids"
    normalized: list[tuple[float, float]] = []
    prior_price: float | None = None
    for index, raw_level in enumerate(raw_levels):
        if not isinstance(raw_level, (list, tuple)) or len(raw_level) != 2:
            raise RuntimeError(
                f"invalid fresh L2 {side} level shape for {symbol} at index {index}"
            )
        raw_price, raw_qty = raw_level
        if isinstance(raw_price, bool) or isinstance(raw_qty, bool):
            raise RuntimeError(
                f"invalid fresh L2 {side} boolean level for {symbol} at index {index}"
            )
        try:
            price = float(raw_price)
            qty = float(raw_qty)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"invalid fresh L2 {side} numeric level for {symbol} at index {index}"
            ) from exc
        if not math.isfinite(price) or not math.isfinite(qty) or price <= 0.0 or qty <= 0.0:
            raise RuntimeError(
                f"fresh L2 {side} requires positive finite price/qty for {symbol} "
                f"at index {index}"
            )
        if prior_price is not None:
            correctly_ordered = price < prior_price if descending else price > prior_price
            if not correctly_ordered:
                relation = "strictly descending" if descending else "strictly ascending"
                raise RuntimeError(
                    f"fresh L2 {side} must be unique and {relation} for {symbol} "
                    f"at index {index}"
                )
        normalized.append((price, qty))
        prior_price = price
    return normalized


def _execution_inputs(
    exchange,
    symbols: Iterable[str],
    decision_marks: dict[str, float],
    *,
    required_depth_symbols: set[str] | None = None,
    execution_realism: ExecutionRealism | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] | None = None,
) -> tuple[dict[str, float], dict[str, CostInputs], dict[str, dict], datetime]:
    """Capture internally consistent execution marks and depth for paper fills.

    Agent provenance and precheck validation continue to use `decision_marks`. At reconcile time,
    each complete two-sided L2 book supplies BOTH the execution reference (its top-of-book midpoint)
    and the depth walked by `apply_fills`. This prevents a delayed cycle from comparing a fresh book
    with an old decision mark and misclassifying intervening market movement as slippage.

    A symbol with an executable delta must have a fresh two-sided L2 book; there is no synthetic
    ADV or stale decision-mark fill fallback. A no-op symbol may use a fresh mark for valuation
    when its L2 is unavailable, but failure of both sources still HALTs. The returned audit block
    is persisted beside the cycle artifacts.
    """
    policy = execution_realism or ExecutionRealism()
    clock = now_fn or (lambda: datetime.now(tz=UTC))
    execution_marks = dict(decision_marks)
    costs: dict[str, CostInputs] = {}
    audit: dict[str, dict] = {}

    ordered_symbols = sorted(set(symbols))
    depth_required = (
        set(ordered_symbols) if required_depth_symbols is None else required_depth_symbols
    )
    specs = {}
    for symbol in ordered_symbols:
        try:
            spec = exchange.symbol_spec(symbol)
        except Exception as exc:  # noqa: BLE001 — exchange filters are execution truth
            raise RuntimeError(f"exchange execution filters unavailable for {symbol}") from exc
        max_qty = getattr(spec, "max_qty", None)
        min_qty = getattr(spec, "min_qty", None)
        if (
            spec.step_size <= 0.0
            or spec.tick_size <= 0.0
            or spec.min_notional < 0.0
            or (min_qty is not None and (not math.isfinite(float(min_qty)) or min_qty <= 0.0))
            or (max_qty is not None and (not math.isfinite(float(max_qty)) or max_qty <= 0.0))
        ):
            raise RuntimeError(f"invalid exchange execution filters for {symbol}")
        specs[symbol] = spec

    submitted_at = clock()
    if ordered_symbols and policy.latency_ms > 0.0:
        sleep_fn(policy.latency_ms / 1000.0)
    first_execution_at: datetime | None = None
    execution_times: list[datetime] = []
    for sequence, symbol in enumerate(ordered_symbols, start=1):
        observation_started_at = clock()
        decision_mark = float(decision_marks.get(symbol, 0.0) or 0.0)
        raw_bids: list[tuple[float, float]] = []
        raw_asks: list[tuple[float, float]] = []
        depth_fetch_error: str | None = None
        try:
            depth = exchange.depth(symbol)
        except Exception as exc:  # noqa: BLE001 — transport failure may fall back for a no-op
            depth_fetch_error = f"{type(exc).__name__}: {exc}"
        else:
            if not isinstance(depth, dict) or "bids" not in depth or "asks" not in depth:
                raise RuntimeError(f"invalid fresh L2 mapping for {symbol}")
            raw_bids = _validate_l2_side(symbol, "bids", depth["bids"])
            raw_asks = _validate_l2_side(symbol, "asks", depth["asks"])

        raw_best_bid = raw_bids[0][0] if raw_bids else 0.0
        raw_best_ask = raw_asks[0][0] if raw_asks else 0.0
        if raw_bids and raw_asks and raw_best_bid >= raw_best_ask:
            raise RuntimeError(
                f"fresh L2 top is locked or inverted for {symbol}: "
                f"bid={raw_best_bid:.12g} ask={raw_best_ask:.12g}"
            )
        two_sided = bool(raw_bids and raw_asks)
        bids = raw_bids if two_sided else []
        asks = raw_asks if two_sided else []
        price_source = "book_mid"

        if two_sided:
            execution_mark = (raw_best_bid + raw_best_ask) / 2.0
            spread_bps = (
                (raw_best_ask - raw_best_bid) / execution_mark * 1e4
                if execution_mark > 0.0
                else 0.0
            )
            half_spread_bps = spread_bps / 2.0
        else:
            if symbol in depth_required:
                detail = f"; fetch_error={depth_fetch_error}" if depth_fetch_error else ""
                raise RuntimeError(
                    f"fresh two-sided execution depth unavailable for changed symbol {symbol}"
                    f"{detail}"
                )
            try:
                execution_mark = float(exchange.mark_price(symbol))
            except Exception as exc:  # noqa: BLE001 — stale valuation would falsify equity
                raise RuntimeError(
                    f"fresh execution valuation unavailable for no-op symbol {symbol}"
                ) from exc
            else:
                price_source = "mark_price"
            if execution_mark <= 0.0:
                raise RuntimeError(
                    f"non-positive fresh execution valuation for no-op symbol {symbol}"
                )
            spread_bps = 0.0
            half_spread_bps = 0.0

        observed_at = clock()
        if first_execution_at is None:
            first_execution_at = observed_at
        legging_delay_seconds = max(0.0, (observed_at - first_execution_at).total_seconds())
        legging_bps = policy.legging_bps_per_second * legging_delay_seconds
        effective_bids = haircut_depth(bids, policy.displayed_depth_fraction) if bids else []
        effective_asks = haircut_depth(asks, policy.displayed_depth_fraction) if asks else []
        cost = CostInputs(
            adv_usd=0.0,
            half_spread_bps=half_spread_bps,
            depth_bids=effective_bids,
            depth_asks=effective_asks,
            adverse_selection_bps=policy.adverse_selection_bps,
            legging_bps=legging_bps,
        )

        execution_marks[symbol] = execution_mark
        costs[symbol] = cost
        execution_times.append(observed_at)
        spec = specs[symbol]
        audit[symbol] = {
            "symbol": symbol,
            "execution_sequence": sequence,
            "submission_at": submitted_at.isoformat(),
            "observation_started_at": observation_started_at.isoformat(),
            "observed_at": observed_at.isoformat(),
            "execution_ts": observed_at.isoformat(),
            "captured_at": observed_at.isoformat(),
            "requested_latency_ms": policy.latency_ms,
            "observed_latency_ms": max(0.0, (observed_at - submitted_at).total_seconds() * 1000.0),
            "legging_delay_seconds": legging_delay_seconds,
            "adverse_selection_bps": policy.adverse_selection_bps,
            "legging_bps": legging_bps,
            "displayed_depth_fraction": policy.displayed_depth_fraction,
            "allow_partial_fills": policy.allow_partial_fills,
            "price_source": price_source,
            "l2_snapshot_schema_version": 1,
            "l2_validation": "positive_finite_unique_strictly_ordered_uncrossed",
            "l2_validation_passed": depth_fetch_error is None,
            "l2_fetch_error": depth_fetch_error,
            "raw_l2_used_for_execution": two_sided,
            "raw_bid_ladder": [[price, qty] for price, qty in raw_bids],
            "raw_ask_ladder": [[price, qty] for price, qty in raw_asks],
            "effective_bid_ladder": [[price, qty] for price, qty in effective_bids],
            "effective_ask_ladder": [[price, qty] for price, qty in effective_asks],
            "decision_mark": decision_mark,
            "execution_mark": execution_mark,
            "decision_to_execution_bps": (
                (execution_mark / decision_mark - 1.0) * 1e4 if decision_mark > 0.0 else 0.0
            ),
            "best_bid": raw_best_bid if two_sided else 0.0,
            "best_ask": raw_best_ask if two_sided else 0.0,
            "spread_bps": spread_bps,
            "half_spread_bps": half_spread_bps,
            "bid_levels": len(bids),
            "ask_levels": len(asks),
            "depth_usd_bid": sum(price * qty for price, qty in bids),
            "depth_usd_ask": sum(price * qty for price, qty in asks),
            "depth_qty_bid": sum(qty for _price, qty in bids),
            "depth_qty_ask": sum(qty for _price, qty in asks),
            "effective_depth_usd_bid": sum(price * qty for price, qty in effective_bids),
            "effective_depth_usd_ask": sum(price * qty for price, qty in effective_asks),
            "effective_depth_qty_bid": sum(qty for _price, qty in effective_bids),
            "effective_depth_qty_ask": sum(qty for _price, qty in effective_asks),
            "tick_size": float(spec.tick_size),
            "step_size": float(spec.step_size),
            "min_notional": float(spec.min_notional),
            "min_order_qty": float(min_qty) if min_qty is not None else None,
            "max_order_qty": float(max_qty) if max_qty is not None else None,
        }

    group_execution_ts = max(execution_times) if execution_times else submitted_at
    return execution_marks, costs, audit, group_execution_ts


def run_cycle(
    state_dir,
    *,
    now: datetime,
    exchange,
    runner: AgentRunner,
    symbols: list[str],
    cash: float,
    cycle: int,
    btc_symbol: str = "BTC/USDT:USDT",
    cadence: str = "rebal",
    enforce_achieved_safety: bool = True,
    offline_injected_capability: object | None = None,
) -> CycleReport:
    """Run the legacy OFFLINE/INJECTED integration harness with canned agent outputs.

    This is not a production orchestration path. The caller must supply the exact opaque
    capability issued for the same exact ``StubAgentRunner`` instance; the check runs before
    evidence, exchange, state, or filesystem access. Production uses ``scripts/desk_reconcile.py``
    after real agents.

    The harness builds evidence + reconciles the paper account, but every canned trading judgement
    still comes from the injected runner.

    Steps: build per-coin evidence (BTC always included for the hedge mark + beta ref) -> the three
    specialists rank in parallel -> the PM synthesizes a book against the LIVE account equity -> the
    adversary challenges (one PM revision) -> reconcile the paper account to the final book at real
    marks and compute the ACHIEVED deploy/neutrality (per-name beta from the evidence) -> persist
    reads/book/adversary/report and record equity. `symbols` is this cycle's universe.

    Fail-safe (spec §6): if there is no evidence, or ALL specialists dropped, the cycle HOLDS the
    prior book (no reconcile — no decision on no evidence)."""
    _verify_offline_injected_run_capability(runner, offline_injected_capability)
    evidence = build_evidence(exchange, symbols, now=now, btc_symbol=btc_symbol)
    marks = {e.symbol: e.mark for e in evidence}
    betas = {e.symbol: e.beta_clamped for e in evidence}
    account = load_account(state_dir, default_cash=cash)

    reads = run_specialists(runner, evidence)
    reads_json = {r: [x.model_dump(mode="json") for x in v] for r, v in reads.items()}
    if not evidence or not any(reads.values()):
        equity = account.equity(marks) if marks else account.cash
        report = CycleReport(
            cycle=cycle,
            achieved_deploy_frac=0.0,
            achieved_dollar_residual_frac=0.0,
            achieved_beta_residual=0.0,
            equity=equity,
            n_legs=len(account.positions),
        )
        save_output(state_dir, cycle, "reads", reads_json, cadence=cadence)
        save_output(state_dir, cycle, "report", report.model_dump(mode="json"), cadence=cadence)
        record_equity(state_dir, now, equity, cycle)
        return report

    equity = account.equity(marks)  # the LIVE cash to deploy (cold account -> == cash)
    # Build current_book from held positions so PM can prefer holding them (reduce turnover)
    current_book = [
        {
            "symbol": s,
            "side": p.direction,
            "target_notional": abs(p.qty) * marks.get(s, p.entry_price),
        }
        for s, p in account.positions.items()
        if s in marks
    ]
    book = run_pm(runner, reads, evidence, cash=equity, current_book=current_book)
    verdict, final = run_adversary(
        runner, book, reads, evidence, cash=equity, current_book=current_book
    )

    fill_syms = ({leg.symbol for leg in final.legs} | set(account.positions)) & set(marks)
    decision_target_audit = _execution_target_audit(account, final, marks, marks)
    changed_symbols = {
        symbol
        for symbol, detail in decision_target_audit.items()
        if float(detail["planned_turnover_usd"]) > 0.01
    }
    execution_marks, costs, execution_audit, execution_ts = _execution_inputs(
        exchange, fill_syms, marks, required_depth_symbols=changed_symbols
    )
    target_audit = _execution_target_audit(account, final, marks, execution_marks, execution_audit)
    executed_targets = _verify_execution_liquidity(target_audit, execution_audit)
    for symbol, detail in target_audit.items():
        execution_audit.setdefault(symbol, {}).update(detail)
    execution_ts_by_symbol = {
        symbol: datetime.fromisoformat(detail["execution_ts"])
        for symbol, detail in execution_audit.items()
        if detail.get("execution_ts")
    }
    execution_completed_by_symbol = {
        symbol: not bool(detail.get("partial_fill", False))
        for symbol, detail in target_audit.items()
    }
    report = reconcile_book(
        account,
        final,
        marks=execution_marks,
        decision_marks=marks,
        costs=costs,
        betas=betas,
        now=now,
        execution_ts=execution_ts,
        execution_ts_by_symbol=execution_ts_by_symbol,
        execution_completed_by_symbol=execution_completed_by_symbol,
        target_signed_quantities=executed_targets,
        cycle=cycle,
        cadence=cadence,
        enforce_achieved_safety=enforce_achieved_safety,
    )
    achieved_safety = _achieved_execution_safety(account, execution_marks, betas)
    for symbol, detail in execution_audit.items():
        post_qty = _signed_qty(account.positions.get(symbol))
        detail["post_execution_qty_signed"] = post_qty
        detail["post_execution_notional_signed"] = post_qty * execution_marks[symbol]
        detail["execution_target_attained"] = math.isclose(
            post_qty,
            float(detail.get("executed_target_qty_signed", post_qty)),
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        detail.update(achieved_safety)
    save_account(state_dir, account)

    save_output(state_dir, cycle, "reads", reads_json, cadence=cadence)
    save_output(state_dir, cycle, "book", final.model_dump(mode="json"), cadence=cadence)
    save_output(state_dir, cycle, "adversary", verdict.model_dump(mode="json"), cadence=cadence)
    save_output(state_dir, cycle, "report", report.model_dump(mode="json"), cadence=cadence)
    save_output(state_dir, cycle, "execution", execution_audit, cadence=cadence)
    record_equity(state_dir, execution_ts, report.equity, cycle)
    return report
