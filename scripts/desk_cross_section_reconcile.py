#!/usr/bin/env python3
"""Reconcile one accepted weekly-cross-section weight decision to the PAPER account."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.config import load_settings
from futures_fund.cross_section import (
    AllocationAdversary,
    AllocationProposal,
    WeeklyUniverseSnapshot,
    WeightPacket,
    allocation_sha256,
    build_allocation_precheck,
    packet_sha256,
    validate_allocation,
    validate_policy_binding,
    validate_revision_constraints,
)
from futures_fund.desk_contracts import Book, BookLeg
from futures_fund.desk_cycle import (
    _execution_inputs,
    _execution_target_audit,
    _fresh_one_way_cost,
    _verify_execution_liquidity,
    reconcile_book,
)
from futures_fund.directives import build_directive_commit_expectation
from futures_fund.durable_io import canonical_json_sha256
from futures_fund.exchange import FuturesExchange
from futures_fund.funding_history import (
    collect_funding_events,
    resolve_previous_intervals,
    verify_execution_window_funding_safety,
)
from futures_fund.pending_io import resolve_pending
from futures_fund.pnl_attribution import build_cycle_pnl, latest_closing_equity
from futures_fund.reconcile_commit import recover_reconcile_transaction, stage_reconcile_transaction
from futures_fund.runtime_provenance import default_runtime_provenance, verify_runtime_provenance
from futures_fund.slippage import ExecutionRealism
from futures_fund.state_transaction import load_account_with_sha256


def _halt(message: str) -> int:
    print(json.dumps({"halt": f"{message} — prior PAPER book stands"}))
    return 1


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _verified_final(pending: Path, packet: WeightPacket) -> tuple[AllocationProposal, dict, dict]:
    alpha = AllocationProposal.model_validate_json((pending / "allocator_alpha.json").read_text())
    risk = AllocationProposal.model_validate_json((pending / "allocator_risk.json").read_text())
    validate_allocation(alpha, packet, expected_role="alpha_allocator")
    validate_allocation(risk, packet, expected_role="risk_allocator")
    proposal_digests = {
        "alpha_allocator": allocation_sha256(alpha),
        "risk_allocator": allocation_sha256(risk),
    }
    pm = AllocationProposal.model_validate_json((pending / "pm_weights.json").read_text())
    validate_allocation(pm, packet, expected_role="pm")
    if pm.source_proposal_sha256 != proposal_digests:
        raise ValueError("PM does not bind both allocator proposals")
    precheck = _load_json(pending / "allocation_precheck.json")
    if precheck != build_allocation_precheck(pm, packet):
        raise ValueError("original allocation precheck rebuild mismatch")
    verdict = AllocationAdversary.model_validate_json(
        (pending / "allocation_adversary.json").read_text()
    )
    if (
        verdict.cycle != packet.cycle
        or verdict.packet_sha256 != packet_sha256(packet)
        or verdict.allocation_sha256 != allocation_sha256(pm)
        or verdict.precheck_sha256 != precheck["sha256"]
    ):
        raise ValueError("Adversary does not bind PM allocation and precheck")
    if verdict.accept:
        final = pm
    else:
        final = AllocationProposal.model_validate_json(
            (pending / "pm_weights_revision.json").read_text()
        )
        if verdict.revision_constraints is None:
            raise ValueError("rejected verdict lacks revision constraints")
        validate_revision_constraints(final, packet, verdict.revision_constraints)
        expected_sources = {
            "pm_original": allocation_sha256(pm),
            "adversary": canonical_json_sha256(verdict.model_dump(mode="json")),
        }
        if final.source_proposal_sha256 != expected_sources:
            raise ValueError("PM revision does not bind original and Adversary")
    final_precheck = build_allocation_precheck(final, packet)
    durable_final = AllocationProposal.model_validate_json(
        (pending / "allocation_final.json").read_text()
    )
    durable_precheck = _load_json(pending / "allocation_precheck_final.json")
    if durable_final != final or durable_precheck != final_precheck:
        raise ValueError("final allocation artifacts differ from the verified decision chain")
    return final, final_precheck, verdict.model_dump(mode="json")


def _compatibility_book(
    allocation: AllocationProposal,
    packet: WeightPacket,
    precheck: dict,
) -> Book:
    current = {row.symbol: row.current_notional_signed for row in packet.assets}
    targets = {(row["symbol"], row["side"]): row for row in precheck["targets"]}
    legs = []
    changed = 0
    for weight in allocation.weights:
        target = float(targets[(weight.symbol, weight.side)]["target_notional_usd"])
        signed_target = target if weight.side == "long" else -target
        prior = current.get(weight.symbol, 0.0)
        if abs(signed_target - prior) > 0.01:
            changed += 1
        legs.append(
            BookLeg(
                symbol=weight.symbol,
                side=weight.side,
                target_notional=target,
                seat_role="alpha",
                expected_price_edge_frac=0.0,
                edge_horizon_hours=168,
                edge_calibration_basis=(
                    "Deterministic weekly funding-adjusted cross-sectional rank; agents size "
                    "the fixed side but do not author a price forecast."
                ),
                invalidation_condition="Side changes only at the next frozen weekly ranking.",
                rationale="Agent consensus weight inside the deterministic weekly sleeve.",
                is_new=abs(prior) <= 0.01,
                hold_breaking_reason=(
                    "Deterministic weekly top/bottom selection requires this fixed side."
                    if abs(prior) <= 0.01 or prior * signed_target < 0.0
                    else ""
                ),
            )
        )
    return Book(
        legs=legs,
        stated_deploy_frac=packet.gross_target_frac,
        stated_dollar_residual_frac=0.0,
        stated_beta_residual=0.0,
        turnover_legs_changed=changed + len(packet.held_outside_selection),
        turnover_justification=(
            "Symbols/sides are the frozen weekly deterministic selection; agents control only "
            "the bound within-sleeve weights."
        ),
        notes="weekly_top50_cross_section_v1; PAPER only",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--memory-dir", default="live_memory")
    args = parser.parse_args(argv)
    settings = load_settings()
    try:
        recovered = recover_reconcile_transaction(args.state_dir)
    except Exception as exc:  # noqa: BLE001
        return _halt(f"unfinished reconcile recovery failed: {exc}")
    if recovered.get("recovered"):
        print(json.dumps(recovered, indent=2))
        return 0

    try:
        pending, meta = resolve_pending(args.memory_dir)
        if meta.get("design") != "weekly_top50_cross_section_v1":
            raise ValueError("pending cycle is not the weekly cross-section design")
        packet = WeightPacket.model_validate_json((pending / "weight_packet.json").read_text())
        weekly = WeeklyUniverseSnapshot.model_validate_json(
            (pending / "weekly_universe.json").read_text()
        )
        market_state = _load_json(pending / "market_state.json")
        provenance = _load_json(pending / "runtime_provenance.json")
        if (
            int(meta["cycle"]) != packet.cycle
            or meta["weight_packet_sha256"] != packet_sha256(packet)
            or meta["weekly_universe_sha256"]
            != canonical_json_sha256(weekly.model_dump(mode="json"))
            or packet.weekly_snapshot_sha256 != meta["weekly_universe_sha256"]
            or meta["market_state_sha256"] != canonical_json_sha256(market_state)
            or meta["runtime_provenance_sha256"] != canonical_json_sha256(provenance)
            or not verify_runtime_provenance(provenance)
        ):
            raise ValueError("pending decision provenance hash mismatch")
        expected_side = {
            **dict.fromkeys(weekly.long_symbols, "long"),
            **dict.fromkeys(weekly.short_symbols, "short"),
        }
        packet_side = {row.symbol: row.side for row in packet.assets}
        if packet.week_id != weekly.week_id or packet_side != expected_side:
            raise ValueError("weight packet symbols/sides differ from frozen weekly selection")
        validate_policy_binding(
            weekly,
            packet,
            universe_size=settings.cross_section.universe_size,
            sleeve_size=settings.cross_section.sleeve_size,
            volume_lookback_days=settings.cross_section.volume_lookback_days,
            performance_lookback_days=settings.cross_section.performance_lookback_days,
            gross_target_frac=settings.cross_section.gross_target_frac,
            min_sleeve_weight=settings.cross_section.min_sleeve_weight,
            max_sleeve_weight=settings.cross_section.max_sleeve_weight,
        )
        if (
            int(market_state.get("cycle", -1)) != packet.cycle
            or datetime.fromisoformat(str(market_state.get("decision_ts"))) != packet.decision_ts
            or market_state.get("daily_candle_audit", {}).get("source") != "binance-proxy"
            or market_state.get("daily_candle_audit", {}).get("all_fresh") is not True
        ):
            raise ValueError("market state lacks exact cycle/time/proxy freshness binding")
        captured_at = datetime.fromisoformat(str(provenance["captured_at"]))
        if canonical_json_sha256(default_runtime_provenance(captured_at=captured_at)) != meta[
            "runtime_provenance_sha256"
        ]:
            raise ValueError("runtime source or prompt build changed after decision seal")
        allocation, precheck, verdict = _verified_final(pending, packet)
        decision_ts = packet.decision_ts.astimezone(UTC)
        age_minutes = (datetime.now(UTC) - decision_ts).total_seconds() / 60.0
        if age_minutes < 0.0 or age_minutes > settings.cross_section.max_decision_age_minutes:
            raise ValueError(
                f"weight decision age {age_minutes:.2f}m exceeds "
                f"{settings.cross_section.max_decision_age_minutes}m"
            )
        account, base_account_sha256 = load_account_with_sha256(
            args.state_dir,
            default_cash=settings.account_size_usdt,
        )
        if base_account_sha256 != market_state.get("account_sha256"):
            raise ValueError("paper account changed after weight packet was sealed")
    except Exception as exc:  # noqa: BLE001
        return _halt(f"decision-chain validation failed: {exc}")

    book = _compatibility_book(allocation, packet, precheck)
    symbols = set(market_state["symbols"])
    decision_marks = {
        symbol: float(row["mark"]) for symbol, row in market_state["symbols"].items()
    }
    if symbols != ({row.symbol for row in packet.assets} | set(account.positions)):
        return _halt("market-state symbols do not cover exact selected plus held book")
    decision_intervals = {
        symbol: int(row["funding_interval_hours"])
        for symbol, row in market_state["symbols"].items()
    }
    exchange = FuturesExchange.from_settings(settings)
    policy = ExecutionRealism(
        latency_ms=settings.execution.latency_ms,
        displayed_depth_fraction=settings.execution.displayed_depth_fraction,
        adverse_selection_bps=settings.execution.adverse_selection_bps,
        legging_bps_per_second=settings.execution.legging_bps_per_second,
        allow_partial_fills=settings.execution.allow_partial_fills,
    )
    decision_audit = _execution_target_audit(account, book, decision_marks, decision_marks)
    changed = {
        symbol
        for symbol, row in decision_audit.items()
        if float(row["planned_turnover_usd"]) > 0.01
    }
    try:
        execution_marks, costs, execution_audit, execution_ts = _execution_inputs(
            exchange,
            symbols,
            decision_marks,
            required_depth_symbols=changed,
            execution_realism=policy,
        )
        target_audit = _execution_target_audit(
            account, book, decision_marks, execution_marks, execution_audit
        )
        executed_targets = _verify_execution_liquidity(target_audit, execution_audit)
        max_slippage = 0.0
        total_estimated_friction = 0.0
        for symbol, detail in target_audit.items():
            cost = _fresh_one_way_cost(
                symbol,
                float(detail.get("executed_delta_qty_signed") or 0.0),
                float(detail["execution_mark"]),
                costs[symbol],
            )
            max_slippage = max(max_slippage, cost["total_slippage_bps"])
            total_estimated_friction += cost["friction_usd"]
            execution_audit[symbol].update(detail)
            execution_audit[symbol]["fresh_cost"] = cost
        if max_slippage > settings.cross_section.max_execution_slippage_bps + 1e-12:
            raise RuntimeError(
                f"fresh one-way slippage {max_slippage:.4f}bp exceeds "
                f"{settings.cross_section.max_execution_slippage_bps:.4f}bp"
            )
        execution_ts_by_symbol = {
            symbol: datetime.fromisoformat(str(row["execution_ts"]))
            for symbol, row in execution_audit.items()
        }
        intervals = {
            symbol: int(exchange.funding_interval_hours(symbol)) for symbol in sorted(symbols)
        }
        previous_intervals = resolve_previous_intervals(args.state_dir, account)
        funding_proofs: dict[str, dict] = {}
        funding_events = collect_funding_events(
            exchange,
            set(account.positions),
            previous_ts=account.last_funding_ts or execution_ts,
            now=execution_ts,
            intervals={symbol: intervals[symbol] for symbol in account.positions},
            previous_intervals=previous_intervals,
            proof_out=funding_proofs,
        )
        funding_window = verify_execution_window_funding_safety(
            execution_ts_by_symbol,
            symbols,
            current_intervals=intervals,
            decision_intervals=decision_intervals,
            held_interval_proofs=funding_proofs,
        )
    except Exception as exc:  # noqa: BLE001
        return _halt(f"fresh execution validation failed: {exc}")

    opening_equity = account.equity(execution_marks)
    try:
        prior_closing_equity = latest_closing_equity(args.state_dir)
        report = reconcile_book(
            account,
            book,
            marks=execution_marks,
            decision_marks=decision_marks,
            costs=costs,
            betas={
                symbol: float(row["beta_btc"])
                for symbol, row in market_state["symbols"].items()
            },
            now=decision_ts,
            execution_ts=execution_ts,
            cycle=packet.cycle,
            cadence="rebal",
            funding_intervals=intervals,
            funding_events_by_symbol=funding_events,
            target_signed_quantities=executed_targets,
            execution_ts_by_symbol=execution_ts_by_symbol,
            execution_completed_by_symbol={
                symbol: not bool(row.get("partial_fill")) for symbol, row in target_audit.items()
            },
            enforce_achieved_safety=False,
        )
        selected_side = {row.symbol: row.side for row in packet.assets}
        if set(account.positions) != set(selected_side):
            raise RuntimeError("achieved book does not contain exactly the frozen 20 symbols")
        wrong_side = {
            symbol: position.direction
            for symbol, position in account.positions.items()
            if position.direction != selected_side[symbol]
        }
        if wrong_side:
            raise RuntimeError(f"achieved book has wrong deterministic sides: {wrong_side}")
        if not 0.85 <= report.achieved_deploy_frac <= 1.15:
            raise RuntimeError("achieved gross deployment is outside [0.85, 1.15]")
        if report.achieved_dollar_residual_frac > 0.02:
            raise RuntimeError("achieved dollar residual exceeds 2%")
        signed = {
            symbol: position.qty
            * execution_marks[symbol]
            * (1.0 if position.direction == "long" else -1.0)
            for symbol, position in account.positions.items()
        }
        gross = sum(abs(value) for value in signed.values())
        if max(abs(value) for value in signed.values()) / gross > 0.11:
            raise RuntimeError("achieved single-name gross concentration exceeds 11%")
    except Exception as exc:  # noqa: BLE001
        return _halt(f"paper execution validation failed: {exc}")

    commit_ts = datetime.now(UTC)
    ledger = build_cycle_pnl(
        account,
        opening_equity=opening_equity,
        marks=execution_marks,
        turnover_usd=report.turnover_usd,
        cycle=packet.cycle,
        cadence="rebal",
        now=commit_ts,
        prior_closing_equity=prior_closing_equity,
    )
    artifacts = {
        "meta": meta,
        "weekly_universe": weekly.model_dump(mode="json"),
        "weight_packet": packet.model_dump(mode="json"),
        "market_state": market_state,
        "evidence": [
            {
                "symbol": symbol,
                "mark": execution_marks[symbol],
                "beta_clamped": float(market_state["symbols"][symbol]["beta_btc"]),
                "funding_rate": float(market_state["symbols"][symbol]["funding_rate"]),
                "funding_interval_h": int(
                    market_state["symbols"][symbol]["funding_interval_hours"]
                ),
                "as_of_ts": packet.decision_ts.isoformat(),
            }
            for symbol in sorted(set(account.positions))
        ],
        "allocator_alpha": _load_json(pending / "allocator_alpha.json"),
        "allocator_risk": _load_json(pending / "allocator_risk.json"),
        "allocator_digest": _load_json(pending / "allocator_digest.json"),
        "pm_weights": _load_json(pending / "pm_weights.json"),
        "allocation_precheck": _load_json(pending / "allocation_precheck.json"),
        "allocation_adversary": verdict,
        "allocation_final": allocation.model_dump(mode="json"),
        "allocation_precheck_final": precheck,
        "book": book.model_dump(mode="json"),
        "report": report.model_dump(mode="json"),
        "execution": execution_audit,
        "execution_summary": {
            "schema_version": 1,
            "fresh_execution_ts": execution_ts.isoformat(),
            "max_one_way_slippage_bps": max_slippage,
            "estimated_friction_usd": total_estimated_friction,
        },
        "funding_interval_proofs": funding_proofs,
        "funding_execution_window_proof": funding_window,
        "closed_legs": [leg.model_dump(mode="json") for leg in account.drain_closed_legs()],
    }
    revision_path = pending / "pm_weights_revision.json"
    if revision_path.is_file():
        artifacts["pm_weights_revision"] = _load_json(revision_path)
    try:
        stage_reconcile_transaction(
            args.state_dir,
            expected_base_account_sha256=base_account_sha256,
            cycle=packet.cycle,
            cadence="rebal",
            account=account,
            artifacts=artifacts,
            equity_ts=commit_ts,
            equity=report.equity,
            ledger=ledger,
            runtime_provenance=provenance,
            directive_expectation=build_directive_commit_expectation(None, cycle=packet.cycle),
        )
        recovered = recover_reconcile_transaction(args.state_dir)
    except Exception as exc:  # noqa: BLE001
        return _halt(f"durable reconcile commit interrupted: {exc}; run desk_recover.py")
    print(
        json.dumps(
            {
                "status": "COMPLETED",
                "cycle": packet.cycle,
                "week_id": packet.week_id,
                "positions": report.n_legs,
                "equity": round(report.equity, 2),
                "gross_deploy_frac": round(report.achieved_deploy_frac, 6),
                "dollar_residual_frac": round(report.achieved_dollar_residual_frac, 6),
                "turnover_usd": round(report.turnover_usd, 2),
                "fees_usd": round(report.fees_paid_cycle, 2),
                "slippage_usd": round(report.slippage_paid_cycle, 2),
                "funding_usd": round(report.funding_settled_cycle, 4),
                "recovered": recovered.get("recovered"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
