from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.desk_schedule_gate import (
    claim_due_slot,
    due_slot,
    latest_slot,
    read_claims,
    release_claim,
    seed_current_slots,
)
from scripts.install_desk_cron import (
    BEGIN,
    END,
    managed_block,
    render_crontab,
    without_managed_block,
)


def test_managed_block_has_daily_gpt_and_token_free_heartbeats():
    block = managed_block(Path("/srv/desk"))
    assert block.count("7-57/10 * * * *") == 2
    assert block.count("2-52/10 * * * *") == 1
    assert "/usr/bin/bash /srv/desk/scripts/run_scheduled_cycle.sh --scheduled" in block
    assert "/usr/bin/bash /srv/desk/scripts/run_desk_heartbeat.sh --scheduled" in block
    assert "/srv/desk/.venv/bin/python /srv/desk/scripts/desk_health_alert.py" in block
    assert "--state-dir /srv/desk/live_state --log-dir /srv/desk/logs" in block
    assert "Daily full GPT cycle at 00:07 UTC" in block
    assert "Token-free PAPER funding/portfolio heartbeats" in block
    assert "Token-free read-only SLO check" in block
    assert "Offset from :07 task polling" in block
    assert "full slots claim once, failed heartbeats retry within six hours" in block


def test_optional_systemd_units_poll_gates_and_own_the_proxy_lifecycle():
    cycle_service = Path("ops/systemd-user/desk-cycle.service.in").read_text()
    cycle_timer = Path("ops/systemd-user/desk-cycle.timer").read_text()
    heartbeat_timer = Path("ops/systemd-user/desk-heartbeat.timer").read_text()
    health_timer = Path("ops/systemd-user/desk-health.timer").read_text()
    proxy_service = Path("ops/systemd-user/binance-proxy.service.in").read_text()

    assert "Requires=binance-proxy.service" in cycle_service
    assert "After=network-online.target binance-proxy.service" in cycle_service
    assert "Environment=BINANCE_PROXY_EXTERNAL_MANAGER=systemd" in cycle_service
    assert "*:07,17,27,37,47,57:00 UTC" in cycle_timer
    assert "*:07,17,27,37,47,57:00 UTC" in heartbeat_timer
    assert "*:02,12,22,32,42,52:00 UTC" in health_timer
    assert "@BINANCE_PROXY_ROOT@/.venv/bin/uvicorn" in proxy_service
    assert "binance_proxy.app:app" in proxy_service
    assert "--app-dir @BINANCE_PROXY_ROOT@/src" in proxy_service
    assert "Restart=on-failure" in proxy_service


def test_launcher_pins_sol_xhigh_and_supports_real_subagents():
    launcher = Path("scripts/run_scheduled_cycle.sh").read_text()
    assert 'readonly MODEL="gpt-5.6-sol"' in launcher
    assert 'readonly EFFORT="xhigh"' in launcher
    assert "BASH_SOURCE[0]" in launcher
    assert "CODEX_BIN_OVERRIDE" in launcher and "UV_BIN_OVERRIDE" in launcher
    assert "/home/" not in launcher
    assert "--enable multi_agent" in launcher
    assert "sandbox_workspace_write.network_access=true" in launcher
    assert "--ephemeral" not in launcher  # Codex CLI ephemeral threads cannot spawn subagents
    assert "unset OPENAI_API_KEY AZURE_OPENAI_API_KEY CODEX_API_KEY" in launcher
    assert "desk_schedule_gate.py" in launcher
    assert "desk_data_preflight.py" in launcher
    assert "ensure_binance_proxy.py" in launcher
    assert "desk_cycle_outcome.py" in launcher
    assert "fapi.binance.com/fapi/v1/time" not in launcher
    ensure_call = "run python scripts/ensure_binance_proxy.py"
    data_call = "run python scripts/desk_data_preflight.py"
    assert launcher.index(ensure_call) < launcher.index(data_call)
    assert launcher.index(data_call) < launcher.index("schedule_gate --claim")
    assert launcher.index("schedule_gate --check") < launcher.index('exec 9>"${LOCK_FILE}"')
    assert launcher.index('exec 9>"${LOCK_FILE}"') < launcher.index("schedule_gate --claim")
    assert "--attest --before-cycle" in launcher
    assert "exit=0 rejected by outcome attestation" in launcher
    assert '"${1:-}" == "--probe-only"' in launcher
    assert "scripts/ensure_binance_proxy.py --probe-only" in launcher
    assert "--memory-dir live_memory --agents-dir agents --probe-existing" in launcher
    assert "BINANCE_PROXY_EXTERNAL_MANAGER" in launcher
    assert "systemd-owned Binance proxy did not become ready" in launcher
    assert "PROBE_OK read_only=true" in launcher
    assert launcher.index("probe_preflight") < launcher.index('mkdir -p "${LOG_DIR}"')


def test_production_call_graph_excludes_the_offline_combined_driver():
    launcher = Path("scripts/run_scheduled_cycle.sh").read_text()
    prompt = Path("ops/desk-cycle-prompt.md").read_text()
    runbook = Path("docs/desk-cycle-runbook.md").read_text()
    reconcile = Path("scripts/desk_reconcile.py").read_text()

    assert "run_desk_cli.py" not in launcher
    assert "run_desk_cli.py" not in prompt
    assert "run_cycle" not in reconcile
    assert "docs/desk-cycle-runbook.md" in prompt
    assert "uv run python scripts/desk_reconcile.py" in runbook


def test_heartbeat_launcher_has_no_codex_or_agent_path():
    launcher = Path("scripts/run_desk_heartbeat.sh").read_text()
    assert "desk_heartbeat.py" in launcher
    assert "desk-cycle.lock" in launcher
    assert "desk_schedule_gate.py" in launcher
    assert "--kind heartbeat" in launcher
    assert "fapi.binance.com/fapi/v1/time" in launcher
    assert "CODEX_BIN" not in launcher
    assert "multi_agent" not in launcher
    assert "OPENAI_API_KEY" in launcher
    assert launcher.index("schedule_gate --check") < launcher.index('exec 9>"${LOCK_FILE}"')
    assert launcher.index('exec 9>"${LOCK_FILE}"') < launcher.index("schedule_gate --claim")


def test_render_preserves_unrelated_jobs_and_is_idempotent():
    old = "MAILTO=x@example.com\n0 * * * * /bin/existing\n"
    block = managed_block(Path("/srv/desk"))
    first = render_crontab(old, block)
    second = render_crontab(first, block)
    assert second == first
    assert "0 * * * * /bin/existing" in second
    assert second.count(BEGIN) == 1
    assert second.count(END) == 1


def test_unterminated_managed_block_is_refused():
    with pytest.raises(ValueError, match="unterminated"):
        without_managed_block(f"keep\n{BEGIN}\nstale")


def test_utc_gate_accepts_delayed_full_slot_but_expires_after_six_hours():
    claims = {}
    slot = datetime(2026, 8, 12, 0, 7, tzinfo=UTC)
    assert latest_slot("full", datetime(2026, 8, 12, 0, 19, tzinfo=UTC)) == slot
    assert due_slot("full", datetime(2026, 8, 12, 0, 19, tzinfo=UTC), claims) == slot
    assert due_slot("full", datetime(2026, 8, 12, 6, 8, tzinfo=UTC), claims) is None


def test_utc_gate_maps_both_heartbeat_slots_and_previous_day():
    assert latest_slot("heartbeat", datetime(2026, 8, 12, 8, 9, tzinfo=UTC)) == datetime(
        2026, 8, 12, 8, 7, tzinfo=UTC
    )
    assert latest_slot("heartbeat", datetime(2026, 8, 12, 16, 9, tzinfo=UTC)) == datetime(
        2026, 8, 12, 16, 7, tzinfo=UTC
    )
    assert latest_slot("heartbeat", datetime(2026, 8, 12, 7, 0, tzinfo=UTC)) == datetime(
        2026, 8, 11, 16, 7, tzinfo=UTC
    )


def test_claim_is_atomic_attempt_receipt_and_prevents_duplicate(tmp_path):
    path = tmp_path / "claims.json"
    now = datetime(2026, 8, 12, 0, 19, tzinfo=UTC)
    expected = datetime(2026, 8, 12, 0, 7, tzinfo=UTC)
    assert claim_due_slot("full", now, path) == expected
    assert claim_due_slot("full", now, path) is None
    assert read_claims(path)["full"]["slot"] == expected.isoformat()
    assert path.stat().st_mode & 0o777 == 0o600


def test_heartbeat_claim_can_be_released_for_safe_same_slot_retry(tmp_path):
    path = tmp_path / "claims.json"
    now = datetime(2026, 8, 12, 8, 19, tzinfo=UTC)
    slot = datetime(2026, 8, 12, 8, 7, tzinfo=UTC)
    assert claim_due_slot("heartbeat", now, path) == slot
    assert release_claim("heartbeat", now, path) == slot
    assert "heartbeat" not in read_claims(path)
    assert claim_due_slot("heartbeat", now, path) == slot


def test_claim_release_refuses_an_expired_or_different_slot(tmp_path):
    path = tmp_path / "claims.json"
    claimed = datetime(2026, 8, 12, 8, 19, tzinfo=UTC)
    assert claim_due_slot("heartbeat", claimed, path) is not None
    assert release_claim("heartbeat", datetime(2026, 8, 12, 16, 19, tzinfo=UTC), path) is None
    assert "heartbeat" in read_claims(path)


def test_new_install_seeds_current_slots_without_overwriting_claims(tmp_path):
    path = tmp_path / "claims.json"
    installed = datetime(2026, 8, 11, 13, 55, tzinfo=UTC)
    assert seed_current_slots(installed, path)
    claims = read_claims(path)
    assert claims["full"]["slot"] == "2026-08-11T00:07:00+00:00"
    assert claims["heartbeat"]["slot"] == "2026-08-11T08:07:00+00:00"
    assert not seed_current_slots(datetime(2026, 8, 12, 1, 0, tzinfo=UTC), path)
    assert read_claims(path) == claims
