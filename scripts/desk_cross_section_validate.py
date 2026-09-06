#!/usr/bin/env python3
"""Validate and bind compact allocator, PM, precheck, and Adversary artifacts."""

from __future__ import annotations

import argparse
import json
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
from futures_fund.durable_io import canonical_json_sha256, durable_write_json
from futures_fund.pending_io import resolve_pending


def _load_packet(memory_dir: str) -> tuple[Path, dict, WeightPacket]:
    pending, meta = resolve_pending(memory_dir)
    packet = WeightPacket.model_validate_json((pending / "weight_packet.json").read_text())
    weekly = WeeklyUniverseSnapshot.model_validate_json(
        (pending / "weekly_universe.json").read_text()
    )
    if (
        meta.get("design") != "weekly_top50_cross_section_v1"
        or int(meta["cycle"]) != packet.cycle
        or meta.get("weight_packet_sha256") != packet_sha256(packet)
    ):
        raise ValueError("pending meta does not bind the weight packet")
    if meta.get("weekly_universe_sha256") != canonical_json_sha256(
        weekly.model_dump(mode="json")
    ) or packet.weekly_snapshot_sha256 != meta.get("weekly_universe_sha256"):
        raise ValueError("pending meta does not bind the weekly universe")
    settings = load_settings()
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
    return pending, meta, packet


def _proposal(path: Path) -> AllocationProposal:
    return AllocationProposal.model_validate_json(path.read_text())


def _validate_precheck(
    precheck: dict,
    allocation: AllocationProposal,
    packet: WeightPacket,
) -> None:
    expected = build_allocation_precheck(allocation, packet)
    if precheck != expected:
        raise ValueError("allocation precheck does not equal deterministic rebuild")
    body = dict(precheck)
    claimed = body.pop("sha256", None)
    if claimed != canonical_json_sha256(body):
        raise ValueError("allocation precheck internal hash is invalid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "kind",
        choices=(
            "alpha",
            "risk",
            "consensus-input",
            "pm",
            "precheck",
            "adversary",
            "finalize",
        ),
    )
    parser.add_argument("--memory-dir", default="live_memory")
    args = parser.parse_args(argv)
    pending, _meta, packet = _load_packet(args.memory_dir)

    if args.kind in {"alpha", "risk"}:
        role = "alpha_allocator" if args.kind == "alpha" else "risk_allocator"
        path = pending / f"allocator_{args.kind}.json"
        proposal = _proposal(path)
        summary = validate_allocation(proposal, packet, expected_role=role)
        if proposal.source_proposal_sha256:
            raise ValueError("independent allocator cannot claim source proposals")
        print(json.dumps({"status": "VALID", "role": role, **summary}, indent=2))
        return 0

    alpha = _proposal(pending / "allocator_alpha.json")
    risk = _proposal(pending / "allocator_risk.json")
    validate_allocation(alpha, packet, expected_role="alpha_allocator")
    validate_allocation(risk, packet, expected_role="risk_allocator")
    proposal_digests = {
        "alpha_allocator": allocation_sha256(alpha),
        "risk_allocator": allocation_sha256(risk),
    }

    if args.kind == "consensus-input":
        value = {
            "schema_version": 1,
            "cycle": packet.cycle,
            "packet_sha256": packet_sha256(packet),
            "proposal_sha256": proposal_digests,
        }
        durable_write_json(pending / "allocator_digest.json", value)
        print(json.dumps(value, indent=2))
        return 0

    pm = _proposal(pending / "pm_weights.json")
    validate_allocation(pm, packet, expected_role="pm")
    if pm.source_proposal_sha256 != proposal_digests:
        raise ValueError("PM allocation does not bind both validated allocator proposals")

    if args.kind == "pm":
        print(
            json.dumps(
                {"status": "VALID", "role": "pm", "sha256": allocation_sha256(pm)},
                indent=2,
            )
        )
        return 0

    if args.kind == "precheck":
        precheck = build_allocation_precheck(pm, packet)
        durable_write_json(pending / "allocation_precheck.json", precheck)
        print(json.dumps(precheck, indent=2))
        return 0

    precheck = json.loads((pending / "allocation_precheck.json").read_text())
    _validate_precheck(precheck, pm, packet)
    verdict = AllocationAdversary.model_validate_json(
        (pending / "allocation_adversary.json").read_text()
    )
    if (
        verdict.cycle != packet.cycle
        or verdict.packet_sha256 != packet_sha256(packet)
        or verdict.allocation_sha256 != allocation_sha256(pm)
        or verdict.precheck_sha256 != precheck["sha256"]
    ):
        raise ValueError("Adversary verdict is not bound to PM allocation and precheck")

    if args.kind == "adversary":
        if not verdict.accept:
            durable_write_json(
                pending / "revision_digest.json",
                {
                    "schema_version": 1,
                    "cycle": packet.cycle,
                    "source_proposal_sha256": {
                        "pm_original": allocation_sha256(pm),
                        "adversary": canonical_json_sha256(
                            verdict.model_dump(mode="json")
                        ),
                    },
                },
            )
        print(
            json.dumps(
                {
                    "status": "ACCEPT" if verdict.accept else "REVISION_REQUIRED",
                    "objections": verdict.objections,
                },
                indent=2,
            )
        )
        return 0 if verdict.accept else 2

    if verdict.accept:
        final = pm
        final_precheck = precheck
    else:
        revision = _proposal(pending / "pm_weights_revision.json")
        constraints = verdict.revision_constraints
        if constraints is None:
            raise ValueError("rejected verdict lacks revision constraints")
        validate_revision_constraints(revision, packet, constraints)
        expected_sources = {
            "pm_original": allocation_sha256(pm),
            "adversary": canonical_json_sha256(verdict.model_dump(mode="json")),
        }
        if revision.source_proposal_sha256 != expected_sources:
            raise ValueError("PM revision does not bind original PM allocation and Adversary")
        final = revision
        final_precheck = build_allocation_precheck(revision, packet)
    durable_write_json(pending / "allocation_final.json", final.model_dump(mode="json"))
    durable_write_json(pending / "allocation_precheck_final.json", final_precheck)
    print(
        json.dumps(
            {
                "status": "FINALIZED",
                "cycle": packet.cycle,
                "revised": not verdict.accept,
                "allocation_sha256": allocation_sha256(final),
                "precheck_sha256": final_precheck["sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
