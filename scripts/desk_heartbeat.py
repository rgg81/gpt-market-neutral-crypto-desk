"""Token-free 8h PAPER funding/portfolio heartbeat.

This is deliberately not an agent cycle: it fetches public evidence only for held symbols,
settles the paper funding clock, marks the unchanged book, and appends portfolio statistics.
It never proposes or records a fill.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.config import load_settings
from futures_fund.exchange import FuturesExchange
from futures_fund.funding_history import collect_funding_events, resolve_previous_intervals
from futures_fund.heartbeat import (
    heartbeat_for_schedule_slot,
    recover_heartbeat_transaction,
    settle_funding_heartbeat,
    stage_heartbeat_transaction,
)
from futures_fund.reconcile_commit import completed_cycle_numbers, recover_reconcile_transaction
from futures_fund.state_transaction import load_account_with_sha256


def _latest_betas(state_dir: str, symbols: set[str]) -> dict[str, float]:
    if not symbols:
        return {}
    root = Path(state_dir) / "rebal" / "cycle"
    cycle_dirs = [root / str(cycle) for cycle in reversed(completed_cycle_numbers(state_dir))]
    for cycle_dir in cycle_dirs:
        path = cycle_dir / "evidence.json"
        if not path.exists():
            continue
        rows = json.loads(path.read_text())
        betas = {
            str(row["symbol"]): float(row.get("beta_clamped", row.get("beta_btc", 1.0)))
            for row in rows
            if row.get("symbol") in symbols
        }
        if set(betas) == symbols:
            return betas
    raise ValueError(
        f"no completed evidence snapshot covers held symbols for beta reporting: {sorted(symbols)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--schedule-slot", help="claimed UTC slot; makes retry a true no-op")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        recover_reconcile_transaction(args.state_dir)
        recover_heartbeat_transaction(args.state_dir)
        schedule_slot = None
        if args.schedule_slot:
            parsed_slot = datetime.fromisoformat(args.schedule_slot.replace("Z", "+00:00"))
            if parsed_slot.tzinfo is None:
                raise ValueError("schedule slot must include a timezone")
            schedule_slot = parsed_slot.astimezone(UTC).isoformat()
            completed = heartbeat_for_schedule_slot(args.state_dir, schedule_slot)
            if completed is not None:
                print(
                    json.dumps(
                        {
                            "already_complete": True,
                            "schedule_slot": schedule_slot,
                            "heartbeat_ts": completed["ts"],
                        },
                        indent=2,
                    )
                )
                return 0
        account, base_account_sha256 = load_account_with_sha256(
            args.state_dir, default_cash=settings.account_size_usdt
        )
        now = datetime.now(UTC)
        exchange = FuturesExchange.from_settings(settings)
        symbols = set(account.positions)
        current = {symbol: exchange.funding(symbol) for symbol in sorted(symbols)}
        intervals: dict[str, int] = {}
        for symbol, info in current.items():
            raw_interval = float(info.interval_hours)
            interval = int(raw_interval)
            if raw_interval != interval or interval not in {1, 2, 4, 8}:
                raise ValueError(
                    f"unsupported current funding interval for {symbol}: {raw_interval}"
                )
            intervals[symbol] = interval
        previous_ts = account.last_funding_ts or now
        previous_intervals = resolve_previous_intervals(args.state_dir, account)
        funding_interval_proofs: dict[str, dict] = {}
        events = collect_funding_events(
            exchange,
            symbols,
            previous_ts=previous_ts,
            now=now,
            intervals=intervals,
            previous_intervals=previous_intervals,
            proof_out=funding_interval_proofs,
        )
        betas = _latest_betas(args.state_dir, symbols)
        evidence = [
            {
                "symbol": symbol,
                "mark": info.mark_price,
                "funding_rate": info.current_rate,
                "funding_interval_h": intervals[symbol],
                "beta_clamped": betas[symbol],
                "funding_events": events[symbol],
            }
            for symbol, info in current.items()
        ]
        record = settle_funding_heartbeat(
            account,
            evidence,
            now=now,
        )
        record["funding_interval_proofs"] = funding_interval_proofs
        if schedule_slot is not None:
            record["schedule_slot"] = schedule_slot
        record = stage_heartbeat_transaction(
            args.state_dir,
            account,
            record,
            expected_base_account_sha256=base_account_sha256,
        )
        recover_heartbeat_transaction(args.state_dir)
    except Exception as exc:  # noqa: BLE001 — fail closed; prior position quantities stand
        print(json.dumps({"halt": f"token-free heartbeat failed: {exc}"}), file=sys.stderr)
        return 1

    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
