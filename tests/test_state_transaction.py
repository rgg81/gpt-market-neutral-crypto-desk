from __future__ import annotations

import json

import pytest

from futures_fund.state_transaction import (
    account_history_established,
    load_account_with_sha256,
)


def test_true_empty_state_can_open_an_unpersisted_cold_start(tmp_path):
    account, digest = load_account_with_sha256(tmp_path, default_cash=20_000.0)

    assert account.cash == 20_000.0
    assert digest is None
    assert account_history_established(tmp_path) is False
    assert not (tmp_path / "account.json").exists()


def test_missing_root_account_cannot_reseed_a_legacy_completed_desk(tmp_path):
    generation = tmp_path / "rebal" / "cycle" / "7"
    generation.mkdir(parents=True)
    (generation / "account_state.json").write_text(json.dumps({"cash": 18_750.0}))
    (generation / "complete.json").write_text(
        json.dumps({"cycle": 7, "paper_only": True, "manifest": {"version": 2}})
    )
    (tmp_path / "ledger.jsonl").write_text('{"cycle":7}\n')

    assert account_history_established(tmp_path) is True
    with pytest.raises(RuntimeError, match="recover an audited account snapshot"):
        load_account_with_sha256(tmp_path, default_cash=20_000.0)
    assert not (tmp_path / "account.json").exists()
