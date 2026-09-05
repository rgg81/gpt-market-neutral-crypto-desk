from datetime import UTC, datetime, timedelta

import pytest

from futures_fund.config import Settings
from futures_fund.directives import (
    CONTROLLED_RESTART_GRADUATION,
    claim_next_directive,
    parse_directive_capabilities,
)
from futures_fund.performance import canonical_sha256
from scripts import desk_evidence
from scripts.desk_reconcile import _load_bound_directive_claim, _verify_watchdog_receipt
from scripts.desk_watchdog import build_watchdog_receipt, last_cycle_ts


class _Client:
    def __init__(self):
        self.load_markets_calls = 0

    def load_markets(self):
        self.load_markets_calls += 1


def test_directive_capability_requires_exact_first_line_typed_header():
    unrelated = "exclude XRP; discuss controlled_restart_graduation only in prose"
    assert parse_directive_capabilities(unrelated) == ()
    scoped = (
        '<!-- desk-directive-capabilities: ["controlled_restart_graduation"] -->\n'
        "Explicitly authorize graduation for this cycle.\n"
    )
    assert parse_directive_capabilities(scoped) == (CONTROLLED_RESTART_GRADUATION,)

    with pytest.raises(ValueError, match="unknown"):
        parse_directive_capabilities(
            '<!-- desk-directive-capabilities: ["unknown_scope"] -->\nInstruction'
        )
    with pytest.raises(ValueError, match="malformed"):
        parse_directive_capabilities(
            '<!-- desk-directive-capabilities: controlled_restart_graduation -->\nInstruction'
        )


def _directive_meta(claim: dict) -> dict:
    return {
        "cycle": claim["cycle"],
        "binding_user_directive_claim_id": claim["claim_id"],
        "binding_user_directive_claim_intent_sha256": claim["claim_intent_sha256"],
        "binding_user_directive_payload_sha256": claim["payload_sha256"],
        "binding_user_directive_sha256": claim["directive_sha256"],
        "binding_user_directive_source_relpath": claim["source_relpath"],
        "binding_user_directive_capabilities": claim["capabilities"],
        "binding_user_directive_capabilities_sha256": claim["capabilities_sha256"],
    }


def test_pending_directive_binds_the_exact_active_claim_and_all_fields(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "ops" / "next-cycle-directive.md"
    source.parent.mkdir()
    payload = b"Keep this exact claimed instance.\r\n"
    source.write_bytes(payload)
    claim = claim_next_directive(state, cycle=1, source_path=source)
    assert claim is not None
    pending = tmp_path / "memory" / "pending" / "1"
    pending.mkdir(parents=True)
    (pending / "binding_user_directive.md").write_bytes(payload)
    meta = _directive_meta(claim)

    # A new source is a queued second instance and does not invalidate or replace this claim.
    queued = "Queue this for the next cycle, even if its contents later repeat.\n"
    source.write_text(queued)
    loaded = _load_bound_directive_claim(state, pending, meta)
    assert loaded is not None and loaded["claim_id"] == claim["claim_id"]
    assert source.read_text() == queued

    incomplete = dict(meta)
    incomplete.pop("binding_user_directive_claim_intent_sha256")
    with pytest.raises(ValueError, match="claim field set mismatch"):
        _load_bound_directive_claim(state, pending, incomplete)


def test_cycle_meta_cannot_omit_an_existing_active_claim(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "ops" / "next-cycle-directive.md"
    source.parent.mkdir()
    source.write_text("This claimed instruction must not disappear from the decision packet.\n")
    assert claim_next_directive(state, cycle=1, source_path=source) is not None
    pending = tmp_path / "memory" / "pending" / "1"
    pending.mkdir(parents=True)

    with pytest.raises(ValueError, match="omits the active directive claim"):
        _load_bound_directive_claim(state, pending, {"cycle": 1})


def test_post_evidence_queued_source_does_not_retroactively_bind_current_cycle(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "ops" / "next-cycle-directive.md"
    source.parent.mkdir()
    source.write_text("This arrived only after the no-directive evidence packet was sealed.\n")
    pending = tmp_path / "memory" / "pending" / "1"
    pending.mkdir(parents=True)

    assert _load_bound_directive_claim(state, pending, {"cycle": 1}) is None
    assert source.is_file()


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
    monkeypatch.setattr(
        desk_evidence,
        "claim_next_directive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("EARLY evidence must not claim a directive")
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
