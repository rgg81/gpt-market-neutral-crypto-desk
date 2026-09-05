"""Auditably migrate historical normal score rows to the current provenance schema."""

from __future__ import annotations

import argparse
import json

from futures_fund.reflection import CANONICAL_BTC_SYMBOL
from futures_fund.scorecard_migration import migrate_scorecard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--memory-dir", default="live_memory")
    parser.add_argument("--cadence", default="rebal")
    parser.add_argument("--btc-symbol", default=CANONICAL_BTC_SYMBOL)
    args = parser.parse_args(argv)
    result = migrate_scorecard(
        args.state_dir,
        args.memory_dir,
        cadence=args.cadence,
        btc_symbol=args.btc_symbol,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
