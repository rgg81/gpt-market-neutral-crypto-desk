"""Print the read-only, token-free PAPER desk health/SLO report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.desk_health import build_health_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--now", help="ISO UTC test/forensic timestamp")
    parser.add_argument("--strict", action="store_true", help="exit nonzero on CRITICAL")
    args = parser.parse_args(argv)
    now = None
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    try:
        report = build_health_report(args.state_dir, args.log_dir, now=now)
    except Exception as exc:  # noqa: BLE001 - a malformed health input is itself critical
        print(json.dumps({"status": "CRITICAL", "error": str(exc)}))
        return 1
    print(json.dumps(report, indent=2))
    return 1 if args.strict and report["status"] == "CRITICAL" else 0


if __name__ == "__main__":
    sys.exit(main())
