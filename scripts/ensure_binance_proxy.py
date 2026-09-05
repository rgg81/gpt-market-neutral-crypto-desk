"""Host-side self-healing gate for the exact proxy at ~/binance-proxy."""

from __future__ import annotations

import argparse
import json
import sys

from futures_fund.config import load_settings
from futures_fund.proxy_process import ensure_binance_proxy, probe_binance_proxy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="read-only health/process probe; never restart the proxy or write monitor state",
    )
    parser.add_argument("--log-dir", default="logs")
    args = parser.parse_args(argv)
    result = (
        probe_binance_proxy(load_settings())
        if args.probe_only
        else ensure_binance_proxy(load_settings(), log_dir=args.log_dir)
    )
    print(json.dumps(result))
    return 0 if result.get("status") == "HEALTHY" else 1


if __name__ == "__main__":
    sys.exit(main())
