#!/usr/bin/env python3
"""Print deterministic PAPER desk health plus monthly and accumulated performance."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.status_performance import build_status_performance_report, format_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--now", help="aware ISO-8601 forensic timestamp")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="exit nonzero on CRITICAL health")
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        except ValueError as exc:
            parser.error(f"invalid --now: {exc}")
        if now.tzinfo is None:
            parser.error("--now must be timezone-aware")
        now = now.astimezone(UTC)
    try:
        settings = load_settings(args.config)
        report = build_status_performance_report(
            args.state_dir,
            args.log_dir,
            starting_capital=settings.account_size_usdt,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - malformed status state must fail closed
        failure = {
            "schema_version": 1,
            "paper_only": True,
            "generated_at": now.isoformat(),
            "status": "ERROR",
            "error": str(exc),
        }
        print(json.dumps(failure, indent=2) if args.json else f"STATUS REPORT ERROR: {exc}")
        return 2
    print(json.dumps(report, indent=2) if args.json else format_text(report))
    return 1 if args.strict and report["health"]["status"] == "CRITICAL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
