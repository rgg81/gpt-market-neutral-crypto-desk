import json
from datetime import UTC, datetime

from futures_fund.account import PaperAccount
from futures_fund.reconcile_commit import (
    recover_reconcile_transaction,
    stage_reconcile_transaction,
)
from futures_fund.seat_role_migration import migrate_legacy_position_seat_roles


def test_legacy_roles_are_recovered_only_from_a_bound_completed_book(tmp_path):
    state = tmp_path / "state"
    now = datetime(2026, 8, 29, 0, 7, tzinfo=UTC)
    book = {
        "legs": [
            {"symbol": "A", "side": "long", "seat_role": "alpha", "target_notional": 1000},
            {
                "symbol": "BTC/USDT:USDT",
                "side": "short",
                "seat_role": "hedge",
                "target_notional": 1000,
            },
        ]
    }
    stage_reconcile_transaction(
        state,
        expected_base_account_sha256=None,
        cycle=7,
        cadence="rebal",
        account=PaperAccount(cash=20_000.0),
        artifacts={
            "book": book,
            "evidence": [],
            "report": {"cycle": 7, "decision_ts": now.isoformat()},
        },
        equity_ts=now,
        equity=20_000.0,
        ledger={"cycle": 7, "closing_equity": 20_000.0},
    )
    recover_reconcile_transaction(state)
    legacy = {
        "cash": 20_000.0,
        "last_funding_ts": now.isoformat(),
        "positions": {
            "A": {
                "symbol": "A", "direction": "long", "qty": 10.0,
                "entry_price": 100.0, "opened_ts": now.isoformat(),
            },
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT", "direction": "short", "qty": 0.01,
                "entry_price": 100_000.0, "opened_ts": now.isoformat(),
            },
        },
    }
    (state / "account.json").write_text(json.dumps(legacy))

    result = migrate_legacy_position_seat_roles(state)

    assert result["source_cycle"] == 7
    migrated = json.loads((state / "account.json").read_text())
    assert migrated["positions"]["A"]["seat_role"] == "alpha"
    assert migrated["positions"]["BTC/USDT:USDT"]["seat_role"] == "hedge"
    assert migrated["positions"]["BTC/USDT:USDT"]["qty"] == 0.01
    assert migrate_legacy_position_seat_roles(state)["already_complete"] is True
