from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.install_desk_cron import (
    BEGIN,
    END,
    local_hours,
    managed_block,
    render_crontab,
    stable_utc_offset_hours,
    without_managed_block,
)


def test_local_hours_maps_sao_paulo_to_funding_boundaries():
    assert local_hours(-3) == (5, 13, 21)


def test_managed_block_is_gpt_sol_xhigh_launcher():
    block = managed_block(Path("/srv/desk"), "America/Sao_Paulo", -3)
    assert "7 5,13,21 * * *" in block
    assert "/usr/bin/bash /srv/desk/scripts/run_scheduled_cycle.sh --scheduled" in block
    assert "00:07/08:07/16:07 UTC" in block
    assert "GPT desk" in block


def test_launcher_pins_sol_xhigh_and_supports_real_subagents():
    launcher = Path("scripts/run_scheduled_cycle.sh").read_text()
    assert 'readonly MODEL="gpt-5.6-sol"' in launcher
    assert 'readonly EFFORT="xhigh"' in launcher
    assert 'BASH_SOURCE[0]' in launcher
    assert "CODEX_BIN_OVERRIDE" in launcher and "UV_BIN_OVERRIDE" in launcher
    assert "/home/" not in launcher
    assert "--enable multi_agent" in launcher
    assert "sandbox_workspace_write.network_access=true" in launcher
    assert "--ephemeral" not in launcher  # Codex CLI ephemeral threads cannot spawn subagents
    assert "unset OPENAI_API_KEY AZURE_OPENAI_API_KEY CODEX_API_KEY" in launcher


def test_render_preserves_unrelated_jobs_and_is_idempotent():
    old = "MAILTO=x@example.com\n0 * * * * /bin/existing\n"
    block = managed_block(Path("/srv/desk"), "America/Sao_Paulo", -3)
    first = render_crontab(old, block)
    second = render_crontab(first, block)
    assert second == first
    assert "0 * * * * /bin/existing" in second
    assert second.count(BEGIN) == 1
    assert second.count(END) == 1


def test_unterminated_managed_block_is_refused():
    with pytest.raises(ValueError, match="unterminated"):
        without_managed_block(f"keep\n{BEGIN}\nstale")


def test_stable_offset_rejects_dst_zone():
    with pytest.raises(RuntimeError, match="changes UTC offset"):
        stable_utc_offset_hours(
            "Europe/Zurich",
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_sao_paulo_offset_is_stable():
    assert stable_utc_offset_hours(
        "America/Sao_Paulo",
        now=datetime(2026, 1, 1, tzinfo=UTC),
    ) == -3
