from datetime import UTC, datetime, timedelta

import pytest

from futures_fund.config import Settings
from futures_fund.performance import canonical_sha256
from scripts import desk_evidence
from scripts.desk_reconcile import _verify_watchdog_receipt
from scripts.desk_watchdog import build_watchdog_receipt, last_cycle_ts


class _Client:
    def __init__(self):
        self.load_markets_calls = 0

    def load_markets(self):
        self.load_markets_calls += 1


def test_universe_scan_and_evidence_share_one_public_client(monkeypatch):
    client = _Client()
    monkeypatch.setattr(desk_evidence, "build_ccxt", lambda settings: client)

    scan_client, exchange = desk_evidence._build_data_clients(Settings())

    assert scan_client is client
    assert exchange.client is client
    assert exchange.keyless is True
    assert client.load_markets_calls == 1


def test_next_cycle_retries_incomplete_state_dir_instead_of_skipping(tmp_path):
    state = tmp_path / "state"
    complete = state / "rebal" / "cycle" / "3"
    incomplete = state / "rebal" / "cycle" / "4"
    complete.mkdir(parents=True)
    incomplete.mkdir(parents=True)
    (complete / "report.json").write_text('{"cycle": 3, "ran_at": "2026-08-18T00:00:00Z"}')
    (incomplete / "book.json").write_text('{"legs": []}')
    assert desk_evidence._next_cycle(str(state)) == 4


def test_next_cycle_refuses_completed_generation_beyond_an_incomplete_one(tmp_path):
    state = tmp_path / "state"
    for cycle in (3, 4, 5):
        (state / "rebal" / "cycle" / str(cycle)).mkdir(parents=True)
    for cycle in (3, 5):
        (state / "rebal" / "cycle" / str(cycle) / "report.json").write_text(
            f'{{"cycle": {cycle}, "ran_at": "2026-08-18T00:00:00+00:00"}}'
        )
    with pytest.raises(RuntimeError, match="incomplete generation"):
        desk_evidence._next_cycle(str(state))


def test_watchdog_uses_latest_complete_cycle_not_partial_directory(tmp_path):
    state = tmp_path / "state"
    complete = state / "rebal" / "cycle" / "3"
    partial = state / "rebal" / "cycle" / "4"
    complete.mkdir(parents=True)
    partial.mkdir(parents=True)
    (complete / "report.json").write_text(
        '{"cycle": 3, "ran_at": "2026-08-18T00:00:00+00:00"}'
    )
    (partial / "report.json.tmp").write_text('{"cycle": 4}')
    cycle, timestamp = last_cycle_ts(str(state))
    assert cycle == 3
    assert timestamp.isoformat() == "2026-08-18T00:00:00+00:00"


def test_watchdog_receipt_binds_exact_first_cycle_and_reproduces_at_reconcile(tmp_path):
    now = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)
    receipt = build_watchdog_receipt(str(tmp_path / "state"), now=now)
    meta = {
        "cycle": 1,
        "now": now.isoformat(),
        "watchdog_receipt": receipt,
        "watchdog_receipt_sha256": canonical_sha256(receipt),
    }

    assert receipt["schedule_status"] == "FIRST"
    assert receipt["next_cycle"] == 1
    assert _verify_watchdog_receipt(str(tmp_path / "state"), meta) == receipt


def test_watchdog_receipt_rejects_hash_tamper_and_non_next_cycle(tmp_path):
    now = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)
    receipt = build_watchdog_receipt(str(tmp_path / "state"), now=now)
    meta = {
        "cycle": 2,
        "now": now.isoformat(),
        "watchdog_receipt": receipt,
        "watchdog_receipt_sha256": canonical_sha256(receipt),
    }
    with pytest.raises(ValueError, match="not bound to the next cycle"):
        _verify_watchdog_receipt(str(tmp_path / "state"), meta)

    meta["cycle"] = 1
    meta["watchdog_receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="hash mismatch"):
        _verify_watchdog_receipt(str(tmp_path / "state"), meta)


def test_evidence_stands_down_early_before_any_market_data_request(tmp_path, monkeypatch):
    state = tmp_path / "state"
    cycle = state / "rebal" / "cycle" / "1"
    cycle.mkdir(parents=True)
    (cycle / "report.json").write_text(
        '{"cycle":1,"ran_at":"' + datetime.now(UTC).isoformat() + '"}'
    )
    monkeypatch.setattr(
        desk_evidence,
        "_build_data_clients",
        lambda _settings: (_ for _ in ()).throw(
            AssertionError("EARLY evidence must not fetch market data")
        ),
    )

    with pytest.raises(RuntimeError, match="watchdog requires stand-down.*EARLY"):
        desk_evidence.main(
            [
                "--state-dir",
                str(state),
                "--memory-dir",
                str(tmp_path / "memory"),
                "--directive-path",
                str(tmp_path / "no-directive"),
            ]
        )


def test_watchdog_future_clock_is_a_fail_closed_receipt(tmp_path):
    state = tmp_path / "state"
    cycle = state / "rebal" / "cycle" / "1"
    cycle.mkdir(parents=True)
    now = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)
    future = now + timedelta(hours=1)
    (cycle / "report.json").write_text(
        f'{{"cycle":1,"ran_at":"{future.isoformat()}"}}'
    )

    receipt = build_watchdog_receipt(str(state), now=now)

    assert receipt["schedule_status"] == "FUTURE_CLOCK"
