from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from futures_fund.account import PaperAccount
from futures_fund.status_performance import (
    build_status_performance_report,
    format_text,
)


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _ledger_row(
    ts,
    cycle,
    equity,
    *,
    realized=0.0,
    unrealized=0.0,
    funding=0.0,
    fees=0.0,
    slippage=0.0,
    turnover=0.0,
):
    return {
        "ts": ts,
        "cycle": cycle,
        "cadence": "rebal",
        "closing_equity": equity,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "funding_net": funding,
        "fees_paid": fees,
        "slippage_paid": slippage,
        "turnover_usd": turnover,
        "positions": [],
    }


def _health(latest_cycle, heartbeat_ts):
    return {
        "status": "HEALTHY",
        "paper_only": True,
        "generated_at": "2026-03-05T16:08:00+00:00",
        "slo": {
            "cycle": {"status": "HEALTHY", "age_hours": 0.1},
            "heartbeat": {"status": "HEALTHY", "age_hours": 0.1},
            "funding_clock": {"status": "HEALTHY", "age_hours": 0.1},
            "flat_book": {"status": "HEALTHY", "age_hours": 0.1},
            "proxy_current": {
                "status": "HEALTHY",
                "http_healthy": True,
                "identity_bound": True,
            },
        },
        "state": {
            "latest_completed_cycle": latest_cycle,
            "latest_cycle_directory": latest_cycle,
            "invalid_published_cycles": [],
            "latest_heartbeat_ts": heartbeat_ts,
            "latest_settlement_source": "heartbeat",
            "heartbeat_chain_valid": True,
            "account_binding_source": "legacy_heartbeat_unbound",
            "account_manifest_consistent": None,
            "account_event_chain_valid": None,
            "account_event_count": 0,
            "positions": 0,
            "transactions": {"reconcile_pending": False, "heartbeat_pending": False},
            "directive_lifecycle": {"status": "idle", "queued_source_present": False},
        },
        "issues": [],
    }


def test_monthly_and_accumulated_performance_reconcile_with_heartbeat_marks(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    ledger = [
        _ledger_row("2026-01-31T00:07:00+00:00", 1, 100.0),
        _ledger_row(
            "2026-02-10T00:07:00+00:00",
            2,
            105.0,
            realized=6.0,
            funding=1.0,
            fees=1.0,
            slippage=1.0,
            turnover=20.0,
        ),
        _ledger_row(
            "2026-03-05T00:07:00+00:00",
            3,
            92.0,
            realized=-5.0,
            funding=2.0,
            fees=3.0,
            slippage=2.0,
            turnover=30.0,
        ),
    ]
    _write_jsonl(state / "ledger.jsonl", ledger)
    _write_jsonl(
        state / "equity-history.jsonl",
        [
            {"ts": row["ts"], "cycle": row["cycle"], "equity": row["closing_equity"]}
            for row in ledger
        ],
    )
    heartbeat_ts = "2026-02-28T16:07:02+00:00"
    _write_jsonl(
        state / "portfolio-heartbeats.jsonl",
        [
            {
                "ts": heartbeat_ts,
                "schedule_slot": "2026-02-28T16:07:00+00:00",
                "kind": "token_free_funding_heartbeat",
                "paper_only": True,
                "equity": 108.0,
                "funding_net_cumulative": 2.0,
                "gross": 0.0,
                "deploy_frac": 0.0,
                "longs_usd": 0.0,
                "shorts_usd": 0.0,
                "dollar_residual_frac": 0.0,
                "beta_residual": 0.0,
                "positions": [],
            }
        ],
    )
    (state / "account.json").write_text(
        json.dumps(
            PaperAccount(
                cash=92.0,
                realized_pnl=-5.0,
                funding_received=3.0,
                funding_paid=1.0,
                fees_paid=3.0,
                slippage_paid=2.0,
                last_funding_ts=datetime(2026, 3, 5, tzinfo=UTC),
            ).to_dict()
        )
    )

    report = build_status_performance_report(
        state,
        tmp_path / "logs",
        starting_capital=100.0,
        now=datetime(2026, 3, 5, 16, 8, tzinfo=UTC),
        health_report=_health(3, heartbeat_ts),
    )

    months = {row["month"]: row for row in report["performance"]["monthly"]}
    assert months["2026-01"]["net_pnl"] == pytest.approx(0.0)
    assert months["2026-02"]["end_equity"] == pytest.approx(108.0)
    assert months["2026-02"]["net_pnl"] == pytest.approx(8.0)
    assert months["2026-02"]["realized_price_pnl"] == pytest.approx(6.0)
    assert months["2026-02"]["unrealized_pnl_change"] == pytest.approx(2.0)
    assert months["2026-02"]["funding_net"] == pytest.approx(2.0)
    assert months["2026-02"]["fees_paid"] == pytest.approx(1.0)
    assert months["2026-02"]["slippage_paid"] == pytest.approx(1.0)
    assert months["2026-03"]["net_pnl"] == pytest.approx(-16.0)
    assert months["2026-03"]["turnover_usd"] == pytest.approx(30.0)
    accumulated = report["performance"]["accumulated"]
    assert accumulated["net_pnl"] == pytest.approx(-8.0)
    assert accumulated["return_frac"] == pytest.approx(-0.08)
    assert accumulated["realized_price_pnl"] == pytest.approx(-5.0)
    assert accumulated["funding_net"] == pytest.approx(2.0)
    assert "ACCUMULATED" in format_text(report)


def test_report_rejects_component_equity_mismatch(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    row = _ledger_row("2026-01-31T00:07:00+00:00", 1, 101.0)
    _write_jsonl(state / "ledger.jsonl", [row])
    _write_jsonl(
        state / "equity-history.jsonl",
        [{"ts": row["ts"], "cycle": 1, "equity": 101.0}],
    )
    _write_jsonl(
        state / "portfolio-heartbeats.jsonl",
        [
            {
                "ts": "2026-01-31T16:07:00+00:00",
                "kind": "token_free_funding_heartbeat",
                "paper_only": True,
                "equity": 101.0,
                "funding_net_cumulative": 0.0,
                "gross": 0.0,
                "deploy_frac": 0.0,
                "longs_usd": 0.0,
                "shorts_usd": 0.0,
                "dollar_residual_frac": 0.0,
                "beta_residual": 0.0,
                "positions": [],
            }
        ],
    )
    (state / "account.json").write_text(json.dumps(PaperAccount(cash=101.0).to_dict()))

    with pytest.raises(ValueError, match="does not reconcile"):
        build_status_performance_report(
            state,
            tmp_path / "logs",
            starting_capital=100.0,
            now=datetime(2026, 1, 31, 17, tzinfo=UTC),
            health_report=_health(1, "2026-01-31T16:07:00+00:00"),
        )
