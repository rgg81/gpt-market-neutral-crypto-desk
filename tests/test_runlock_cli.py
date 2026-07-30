# tests/test_runlock_cli.py
from __future__ import annotations

from scripts.runlock_cli import main


def test_acquire_then_release_round_trip(tmp_path, capsys):
    state = str(tmp_path / "state")
    assert main(["acquire", "--owner", "weekly", "--state-dir", state]) == 0
    assert "ACQUIRED" in capsys.readouterr().out

    # a second acquire while held prints LOCKED (still exit 0 — caller stands down, not an error)
    assert main(["acquire", "--owner", "daily", "--state-dir", state]) == 0
    assert "LOCKED:" in capsys.readouterr().out

    assert main(["release", "--owner", "weekly", "--state-dir", state]) == 0
    assert "RELEASED" in capsys.readouterr().out

    # after release, a fresh acquire succeeds again
    assert main(["acquire", "--owner", "weekly", "--state-dir", state]) == 0
    assert "ACQUIRED" in capsys.readouterr().out


def test_status_reports_free_and_held(tmp_path, capsys):
    state = str(tmp_path / "state")
    assert main(["status", "--state-dir", state]) == 0
    assert "FREE" in capsys.readouterr().out
    main(["acquire", "--owner", "weekly", "--state-dir", state])
    capsys.readouterr()
    assert main(["status", "--state-dir", state]) == 0
    assert "HELD:" in capsys.readouterr().out
