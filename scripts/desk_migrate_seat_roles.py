"""Auditably recover missing legacy PAPER position seat roles from the latest completed Book."""
from __future__ import annotations

import argparse
import json

from futures_fund.seat_role_migration import migrate_legacy_position_seat_roles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--cadence", default="rebal")
    args = parser.parse_args(argv)
    result = migrate_legacy_position_seat_roles(args.state_dir, cadence=args.cadence)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
