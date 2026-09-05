from __future__ import annotations

import os
import signal
from pathlib import Path
from types import SimpleNamespace

from futures_fund import proxy_process
from futures_fund.proxy_process import (
    _proxy_health_snapshot,
    _stop_exact_proxy,
    managed_proxy_pids,
    probe_binance_proxy,
    proxy_start_command,
)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "binance-proxy"
    uvicorn = project / ".venv" / "bin" / "uvicorn"
    uvicorn.parent.mkdir(parents=True)
    uvicorn.write_text("#!/bin/sh\n")
    uvicorn.chmod(0o755)
    (project / "src").mkdir()
    return project


def _cmdline(
    proc_root: Path,
    pid: int,
    args: list[str],
    *,
    cwd: Path,
    start_time: int = 100,
) -> None:
    path = proc_root / str(pid)
    path.mkdir(parents=True)
    (path / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
    (path / "cwd").symlink_to(cwd, target_is_directory=True)
    (path / "stat").write_text(
        f"{pid} (uvicorn) S " + " ".join(["0"] * 18 + [str(start_time)])
    )


def _listener(proc_root: Path, pid: int, *, port: int, inode: int) -> None:
    net = proc_root / "net"
    net.mkdir(exist_ok=True)
    (net / "tcp").write_text(
        "  sl  local_address rem_address st tx_queue tr retrnsmt uid timeout inode\n"
        f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 "
        f"00:00000000 00000000 1000 0 {inode}\n"
    )
    fd = proc_root / str(pid) / "fd"
    fd.mkdir()
    (fd / "7").symlink_to(f"socket:[{inode}]")


def test_start_command_is_exact_local_project_uvicorn(tmp_path):
    project = _project(tmp_path)
    command = proxy_start_command(project, "http://127.0.0.1:8123")
    assert command == [
        str(project / ".venv" / "bin" / "uvicorn"),
        "binance_proxy.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8123",
        "--app-dir",
        str(project / "src"),
    ]


def test_pid_matcher_never_selects_unrelated_uvicorn(tmp_path):
    project = _project(tmp_path)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    exact = [
        str(project / ".venv" / "bin" / "uvicorn"),
        "binance_proxy.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--app-dir",
        str(project / "src"),
    ]
    _cmdline(proc_root, 111, exact, cwd=project)
    _cmdline(
        proc_root,
        222,
        ["/somewhere/uvicorn", "other.app:app", "--app-dir", str(project / "src")],
        cwd=project,
    )
    _cmdline(
        proc_root,
        333,
        ["/somewhere/uvicorn", "binance_proxy.app:app", "--app-dir", "/other/project/src"],
        cwd=project,
    )
    assert managed_proxy_pids(project, proc_root=proc_root) == [111]


def test_relative_project_uvicorn_command_is_identity_bound(tmp_path):
    project = _project(tmp_path)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _cmdline(
        proc_root,
        111,
        [
            str(project / ".venv" / "bin" / "python3"),
            ".venv/bin/uvicorn",
            "binance_proxy.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8123",
            "--app-dir",
            "src",
        ],
        cwd=project,
    )
    assert managed_proxy_pids(project, proc_root=proc_root, expected_port=8123) == [111]


def test_http_health_without_exact_listener_owner_is_unhealthy(tmp_path, monkeypatch):
    project = _project(tmp_path)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(proxy_process, "_healthy", lambda *_args: True)

    snapshot = _proxy_health_snapshot(
        project, "http://127.0.0.1:8123", proc_root=proc_root
    )

    assert snapshot["http_healthy"] is True
    assert snapshot["managed_pids"] == []
    assert snapshot["listener_owner_pids"] == []
    assert snapshot["identity_bound"] is False


def test_health_requires_exact_managed_process_to_own_listener(tmp_path, monkeypatch):
    project = _project(tmp_path)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _cmdline(
        proc_root,
        111,
        proxy_start_command(project, "http://127.0.0.1:8123"),
        cwd=project,
    )
    _listener(proc_root, 111, port=8123, inode=777)
    monkeypatch.setattr(proxy_process, "_healthy", lambda *_args: True)

    snapshot = _proxy_health_snapshot(
        project, "http://127.0.0.1:8123", proc_root=proc_root
    )

    assert snapshot["managed_pids"] == [111]
    assert snapshot["listener_owner_pids"] == [111]
    assert snapshot["identity_bound"] is True


def test_health_revalidates_process_identity_after_listener_lookup(tmp_path, monkeypatch):
    project = _project(tmp_path)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _cmdline(
        proc_root,
        111,
        proxy_start_command(project, "http://127.0.0.1:8123"),
        cwd=project,
    )
    _listener(proc_root, 111, port=8123, inode=777)
    monkeypatch.setattr(proxy_process, "_healthy", lambda *_args: True)
    original = proxy_process._read_proxy_identity
    calls = 0

    def recycled_after_scan(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs) if calls == 1 else None

    monkeypatch.setattr(proxy_process, "_read_proxy_identity", recycled_after_scan)

    snapshot = _proxy_health_snapshot(
        project, "http://127.0.0.1:8123", proc_root=proc_root
    )

    assert snapshot["managed_pids"] == [111]
    assert snapshot["listener_owner_pids"] == []
    assert snapshot["identity_bound"] is False


def test_sigkill_fallback_does_not_signal_reused_pid(tmp_path, monkeypatch):
    project = _project(tmp_path)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    command = proxy_start_command(project, "http://127.0.0.1:8123")
    _cmdline(proc_root, 111, command, cwd=project, start_time=100)
    signals: list[tuple[int, signal.Signals]] = []

    def fake_kill(pid: int, sig: signal.Signals) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGTERM:
            # Same numeric PID, but a different /proc start time means the old process exited and
            # its PID was recycled during the grace window.
            (proc_root / str(pid) / "stat").write_text(
                f"{pid} (unrelated) S " + " ".join(["0"] * 18 + ["999"])
            )

    monkeypatch.setattr(os, "kill", fake_kill)

    assert _stop_exact_proxy(project, grace_seconds=0.0, proc_root=proc_root) == [111]
    assert signals == [(111, signal.SIGTERM)]


def test_stop_signals_only_the_configured_port_for_same_project(tmp_path, monkeypatch):
    project = _project(tmp_path)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    _cmdline(
        proc_root,
        111,
        proxy_start_command(project, "http://127.0.0.1:8123"),
        cwd=project,
    )
    _cmdline(
        proc_root,
        222,
        proxy_start_command(project, "http://127.0.0.1:9000"),
        cwd=project,
    )
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    stopped = _stop_exact_proxy(
        project,
        grace_seconds=0.0,
        proc_root=proc_root,
        expected_port=8123,
    )

    assert stopped == [111]
    assert signals == [(111, signal.SIGTERM), (111, signal.SIGKILL)]


def test_probe_only_never_invokes_start_stop_or_monitor_write(tmp_path, monkeypatch):
    project = _project(tmp_path)
    settings = SimpleNamespace(
        data=SimpleNamespace(
            binance_proxy_project_dir=str(project),
            binance_klines_proxy_url="http://127.0.0.1:8123",
        )
    )
    monkeypatch.setattr(
        "futures_fund.proxy_process._proxy_health_snapshot",
        lambda *_args: {
            "http_healthy": True,
            "managed_pids": [42],
            "listener_owner_pids": [42],
            "identity_bound": True,
        },
    )
    result = probe_binance_proxy(settings)
    assert result["status"] == "HEALTHY"
    assert result["action"] == "probe_only"
    assert result["pids"] == [42]
    assert not (tmp_path / "logs").exists()
