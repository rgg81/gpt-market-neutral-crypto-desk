from __future__ import annotations

import pytest

from futures_fund import durable_io


def test_durable_write_fsyncs_file_before_replace_and_directory_after(tmp_path, monkeypatch):
    events: list[str] = []
    real_replace = durable_io.os.replace

    def fsync(_descriptor):
        events.append("fsync")

    def replace(source, destination):
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(durable_io.os, "fsync", fsync)
    monkeypatch.setattr(durable_io.os, "replace", replace)
    path = durable_io.durable_write_text(tmp_path / "state.json", "truth\n")

    assert path.read_text() == "truth\n"
    replace_index = events.index("replace")
    assert "fsync" in events[:replace_index]
    assert "fsync" in events[replace_index + 1 :]
    assert not list(tmp_path.glob(".*.tmp"))
    assert path.stat().st_mode & 0o777 == 0o600


def test_durable_unlink_fsyncs_parent(tmp_path, monkeypatch):
    path = tmp_path / "intent.json"
    path.write_text("{}")
    calls = 0

    def fsync(_descriptor):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(durable_io.os, "fsync", fsync)
    assert durable_io.durable_unlink(path)
    assert not path.exists()
    assert calls == 1


def test_durable_json_and_canonical_hash_reject_nonstandard_nan(tmp_path):
    with pytest.raises(ValueError, match="Out of range float values"):
        durable_io.canonical_json_sha256({"equity": float("nan")})
    destination = tmp_path / "state.json"
    with pytest.raises(ValueError, match="Out of range float values"):
        durable_io.durable_write_json(destination, {"equity": float("nan")})
    assert not destination.exists()
