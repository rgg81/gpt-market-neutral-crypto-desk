"""Recover any durable PAPER reconcile intent before watchdog/evidence/heartbeat work."""
from __future__ import annotations

import argparse
import json
import sys

from futures_fund.heartbeat import recover_heartbeat_transaction
from futures_fund.reconcile_commit import recover_reconcile_transaction


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    args = parser.parse_args(argv)
    try:
        result = {
            "reconcile": recover_reconcile_transaction(args.state_dir),
            "heartbeat": recover_heartbeat_transaction(args.state_dir),
        }
    except Exception as exc:  # noqa: BLE001 — recovery must fail closed before another task
        print(json.dumps({"halt": f"durable state recovery failed: {exc}"}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
