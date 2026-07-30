"""LLM Market-Neutral Desk — the 8h decision driver.

    uv run python scripts/run_desk_cli.py
    uv run python scripts/run_desk_cli.py --now 2026-07-07T08:00:00+00:00   # pinned (offline)

Offline/injected combined driver for exercising one 8h decision cycle under a single run lock.
Production subscription orchestration uses SKILL.md and docs/desk-cycle-runbook.md instead. PAPER.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.desk_cycle import run_cycle
from futures_fund.runlock import single_flight
from futures_fund.scheduling import cycle_due

_STATE_DIR = "state"


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _build_runner(settings):
    """Injection seam for offline tests; raw-API LLM execution is intentionally unsupported."""
    raise RuntimeError(
        "No raw-API agent runner exists. Use SKILL.md / docs/desk-cycle-runbook.md "
        "for subscription orchestration, or inject a StubAgentRunner in an offline test."
    )


def _fetch_universe(settings) -> list[str]:
    """The top-N-by-24h-volume symbols (seam — monkeypatched in tests)."""
    from futures_fund.exchange import build_ccxt
    from futures_fund.market_data import scan_universe
    client = build_ccxt(settings)
    client.load_markets()
    return [r["symbol"] for r in scan_universe(client, top_n=settings.universe_top_n)]


def _build_exchange(settings):
    """The exchange used for evidence/marks/depth (seam — monkeypatched in tests)."""
    from futures_fund.exchange import FuturesExchange
    return FuturesExchange.from_settings(settings)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Run one 8h LLM-desk decision cycle (paper).")
    ap.add_argument("--now", default=None, help="ISO-8601 run instant (UTC); default wall-clock.")
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--memory-dir", default="memory")
    args = ap.parse_args(argv)

    now = _parse_now(args.now)
    settings = load_settings()
    with single_flight(args.state_dir, now, owner="llm-desk") as ok:
        if not ok:
            print("STAND DOWN: another desk run holds the lock; skipping this fire.")
            return
        mode, cycle, reason = cycle_due(
            args.state_dir, now, tf_minutes=settings.cadence_tf_minutes, loop="rebal")
        if mode == "SKIP":
            print(f"SKIP: {reason}")
            return
        exchange = _build_exchange(settings)
        runner = _build_runner(settings)
        symbols = _fetch_universe(settings)
        report = run_cycle(
            args.state_dir, now=now, exchange=exchange, runner=runner, symbols=symbols,
            cash=settings.account_size_usdt, cycle=cycle, btc_symbol=settings.btc_symbol)
        print(json.dumps({
            "cycle": report.cycle, "n_legs": report.n_legs,
            "achieved_deploy_frac": round(report.achieved_deploy_frac, 4),
            "achieved_dollar_residual_frac": round(report.achieved_dollar_residual_frac, 4),
            "achieved_beta_residual": round(report.achieved_beta_residual, 4),
            "equity": round(report.equity, 2),
        }, indent=2))


if __name__ == "__main__":
    sys.exit(main())
