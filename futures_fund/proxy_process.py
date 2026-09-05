"""Narrow process manager for the user's local Binance candle proxy.

Only the exact uvicorn application rooted at the configured ``~/binance-proxy`` project may be
signalled.  This module is invoked by the host-side scheduled launcher, before the cycle is claimed;
it is intentionally not a general port/process killer.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from futures_fund.durable_io import durable_write_json


class ProxyProcessError(RuntimeError):
    """The configured proxy could not be proven healthy or safely started."""


PROXY_MONITOR_FILE = "binance-proxy-monitor.json"


@dataclass(frozen=True)
class _ProxyIdentity:
    """Immutable process identity; Linux PIDs alone can be reused after exit."""

    pid: int
    start_time_ticks: int
    cmdline: tuple[str, ...]


def _flag_value(args: tuple[str, ...], name: str) -> str | None:
    values: list[str] = []
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            values.append(args[index + 1])
        elif arg.startswith(f"{name}="):
            values.append(arg.split("=", 1)[1])
    return values[0] if len(values) == 1 else None


def _process_start_time(stat_text: str) -> int:
    """Read Linux /proc/PID/stat field 22 without being confused by spaces in comm."""
    try:
        tail = stat_text.rsplit(")", 1)[1].split()
        return int(tail[19])
    except (IndexError, TypeError, ValueError) as exc:
        raise ProxyProcessError("malformed process stat identity") from exc


def _resolved_process_arg(arg: str, cwd: Path) -> Path | None:
    if "/" not in arg:
        return None
    path = Path(arg)
    return (path if path.is_absolute() else cwd / path).resolve()


def _read_proxy_identity(
    project_dir: Path,
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    expected_port: int | None = None,
) -> _ProxyIdentity | None:
    """Return an identity only for this project's exact loopback Uvicorn application."""
    process_dir = proc_root / str(pid)
    try:
        cwd = (process_dir / "cwd").resolve(strict=True)
        args = tuple(
            raw.decode(errors="replace")
            for raw in (process_dir / "cmdline").read_bytes().split(b"\0")
            if raw
        )
        start_time = _process_start_time((process_dir / "stat").read_text())
    except (FileNotFoundError, OSError, PermissionError, ProcessLookupError, ProxyProcessError):
        return None

    project = project_dir.resolve()
    if cwd != project or "binance_proxy.app:app" not in args:
        return None
    expected_uvicorn = (project / ".venv" / "bin" / "uvicorn").resolve()
    if expected_uvicorn not in {
        resolved
        for arg in args
        if (resolved := _resolved_process_arg(arg, cwd)) is not None
    }:
        return None
    app_dir_raw = _flag_value(args, "--app-dir")
    if app_dir_raw is None:
        return None
    app_dir_path = Path(app_dir_raw)
    app_dir = (
        app_dir_path if app_dir_path.is_absolute() else cwd / app_dir_path
    ).resolve()
    if app_dir != (project / "src").resolve():
        return None
    if _flag_value(args, "--host") != "127.0.0.1":
        return None
    port_raw = _flag_value(args, "--port")
    try:
        port = int(port_raw) if port_raw is not None else None
    except ValueError:
        return None
    if port is None or not 1 <= port <= 65535:
        return None
    if expected_port is not None and port != expected_port:
        return None
    return _ProxyIdentity(pid=pid, start_time_ticks=start_time, cmdline=args)


def proxy_start_command(project_dir: Path, base_url: str) -> list[str]:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ProxyProcessError("managed Binance proxy must use a local HTTP loopback URL")
    port = parsed.port or 80
    uvicorn = project_dir / ".venv" / "bin" / "uvicorn"
    app_dir = project_dir / "src"
    if not uvicorn.is_file() or not os.access(uvicorn, os.X_OK):
        raise ProxyProcessError(f"proxy uvicorn is missing or not executable: {uvicorn}")
    if not app_dir.is_dir():
        raise ProxyProcessError(f"proxy app directory is missing: {app_dir}")
    return [
        str(uvicorn),
        "binance_proxy.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--app-dir",
        str(app_dir),
    ]


def _managed_proxy_identities(
    project_dir: Path,
    *,
    proc_root: Path = Path("/proc"),
    expected_port: int | None = None,
) -> list[_ProxyIdentity]:
    identities: list[_ProxyIdentity] = []
    for path in proc_root.iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        identity = _read_proxy_identity(
            project_dir,
            int(path.name),
            proc_root=proc_root,
            expected_port=expected_port,
        )
        if identity is not None:
            identities.append(identity)
    return sorted(identities, key=lambda identity: identity.pid)


def managed_proxy_pids(
    project_dir: Path,
    *,
    proc_root: Path = Path("/proc"),
    expected_port: int | None = None,
) -> list[int]:
    """Return PIDs whose cwd, exact Uvicorn script/app/flags, and start identity are proven."""
    return [
        identity.pid
        for identity in _managed_proxy_identities(
            project_dir,
            proc_root=proc_root,
            expected_port=expected_port,
        )
    ]


def _listener_socket_inodes(proc_root: Path, port: int) -> set[str]:
    """Return socket inodes listening on exactly IPv4 127.0.0.1:port."""
    inodes: set[str] = set()
    tcp = proc_root / "net" / "tcp"
    try:
        lines = tcp.read_text().splitlines()[1:]
    except (FileNotFoundError, OSError, PermissionError):
        return inodes
    expected_local = f"0100007F:{port:04X}"
    for line in lines:
        fields = line.split()
        if len(fields) > 9 and fields[1].upper() == expected_local and fields[3] == "0A":
            inodes.add(fields[9])
    return inodes


def _pid_socket_inodes(proc_root: Path, pid: int) -> set[str]:
    sockets: set[str] = set()
    try:
        descriptors = list((proc_root / str(pid) / "fd").iterdir())
    except (FileNotFoundError, OSError, PermissionError, ProcessLookupError):
        return sockets
    for descriptor in descriptors:
        try:
            target = os.readlink(descriptor)
        except (FileNotFoundError, OSError, PermissionError, ProcessLookupError):
            continue
        if target.startswith("socket:[") and target.endswith("]"):
            sockets.add(target[8:-1])
    return sockets


def _proxy_health_snapshot(
    project_dir: Path,
    base_url: str,
    *,
    proc_root: Path = Path("/proc"),
) -> dict:
    parsed = urlparse(base_url)
    port = parsed.port or 80
    http_healthy = _healthy(base_url)
    identities = _managed_proxy_identities(
        project_dir, proc_root=proc_root, expected_port=port
    )
    managed = [identity.pid for identity in identities]
    listeners = _listener_socket_inodes(proc_root, port)
    owners = [
        identity.pid
        for identity in identities
        if _pid_socket_inodes(proc_root, identity.pid) & listeners
        and _read_proxy_identity(
            project_dir,
            identity.pid,
            proc_root=proc_root,
            expected_port=port,
        )
        == identity
    ]
    identity_bound = http_healthy and bool(owners)
    return {
        "http_healthy": http_healthy,
        "managed_pids": managed,
        "listener_owner_pids": owners,
        "identity_bound": identity_bound,
    }


def _healthy(base_url: str, timeout_seconds: float = 2.0) -> bool:
    try:
        with httpx.Client(base_url=base_url, timeout=timeout_seconds, trust_env=False) as client:
            response = client.get("/healthz")
            return response.status_code == 200 and response.json() == {"status": "ok"}
    except Exception:  # noqa: BLE001 - health probe is deliberately boolean
        return False


def probe_binance_proxy(settings) -> dict:
    """Read-only health/process probe: never start, stop, signal, or write a file."""
    project_dir = Path(settings.data.binance_proxy_project_dir).expanduser().resolve()
    base_url = settings.data.binance_klines_proxy_url.rstrip("/")
    # Validate that the configured process target is narrow even though this path never starts it.
    proxy_start_command(project_dir, base_url)
    health = _proxy_health_snapshot(project_dir, base_url)
    return {
        "status": "HEALTHY" if health["identity_bound"] else "UNHEALTHY",
        "action": "probe_only",
        "checked_at": time.time(),
        "base_url": base_url,
        "project_dir": str(project_dir),
        "pids": health["listener_owner_pids"],
        **health,
    }


def _record_monitor(logs: Path, result: dict) -> None:
    durable_write_json(logs / PROXY_MONITOR_FILE, result)


def _stop_exact_proxy(
    project_dir: Path,
    *,
    grace_seconds: float = 3.0,
    proc_root: Path = Path("/proc"),
    expected_port: int | None = None,
) -> list[int]:
    identities = _managed_proxy_identities(
        project_dir, proc_root=proc_root, expected_port=expected_port
    )
    for identity in identities:
        current = _read_proxy_identity(project_dir, identity.pid, proc_root=proc_root)
        if current != identity:
            continue
        try:
            os.kill(identity.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        remaining = [
            identity
            for identity in identities
            if _read_proxy_identity(project_dir, identity.pid, proc_root=proc_root) == identity
        ]
        if not remaining:
            return [identity.pid for identity in identities]
        time.sleep(0.1)
    for identity in identities:
        # Re-read both start time and exact cmdline immediately before SIGKILL. If a PID was
        # recycled during the grace window, the replacement process is never signalled.
        if _read_proxy_identity(project_dir, identity.pid, proc_root=proc_root) != identity:
            continue
        try:
            os.kill(identity.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return [identity.pid for identity in identities]


def ensure_binance_proxy(settings, *, log_dir: str | Path = "logs") -> dict:
    """Return healthy status, starting/restarting only the exact configured proxy when necessary."""
    project_dir = Path(settings.data.binance_proxy_project_dir).expanduser().resolve()
    base_url = settings.data.binance_klines_proxy_url.rstrip("/")
    configured_port = urlparse(base_url).port or 80
    logs = Path(log_dir)
    logs.mkdir(parents=True, exist_ok=True)
    lock_path = logs / "binance-proxy-start.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        # Avoid restarting on a single transient overloaded probe.
        for _ in range(3):
            health = _proxy_health_snapshot(project_dir, base_url)
            if health["identity_bound"]:
                result = {
                    "status": "HEALTHY",
                    "action": "none",
                    "checked_at": time.time(),
                    "base_url": base_url,
                    "project_dir": str(project_dir),
                    "pids": health["listener_owner_pids"],
                    **health,
                }
                _record_monitor(logs, result)
                return result
            time.sleep(0.25)

        stopped = _stop_exact_proxy(project_dir, expected_port=configured_port)
        command = proxy_start_command(project_dir, base_url)
        log_path = logs / "binance-proxy-managed.log"
        with log_path.open("ab", buffering=0) as output:
            process = subprocess.Popen(  # noqa: S603 - fixed executable/args, no shell
                command,
                cwd=project_dir,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )

        deadline = time.monotonic() + settings.data.binance_proxy_start_timeout_seconds
        while time.monotonic() < deadline:
            health = _proxy_health_snapshot(project_dir, base_url)
            if health["identity_bound"] and process.pid in health["listener_owner_pids"]:
                pid_path = logs / "binance-proxy.pid"
                durable_write_json(
                    pid_path,
                    {
                        "pid": process.pid,
                        "base_url": base_url,
                        "project_dir": str(project_dir),
                        "started_at": time.time(),
                    },
                )
                result = {
                    "status": "HEALTHY",
                    "action": "restarted" if stopped else "started",
                    "checked_at": time.time(),
                    "base_url": base_url,
                    "project_dir": str(project_dir),
                    "pid": process.pid,
                    "stopped_pids": stopped,
                    **health,
                }
                _record_monitor(logs, result)
                return result
            if process.poll() is not None:
                message = (
                    f"Binance proxy exited during startup with code {process.returncode}; "
                    f"inspect {log_path}"
                )
                _record_monitor(
                    logs,
                    {
                        "status": "UNHEALTHY",
                        "action": "start_failed",
                        "checked_at": time.time(),
                        "base_url": base_url,
                        "project_dir": str(project_dir),
                        "error": message,
                    },
                )
                raise ProxyProcessError(message)
            time.sleep(0.25)

        try:
            process.terminate()
        except ProcessLookupError:
            pass
        message = (
            "Binance proxy did not become healthy within "
            f"{settings.data.binance_proxy_start_timeout_seconds}s; inspect {log_path}"
        )
        _record_monitor(
            logs,
            {
                "status": "UNHEALTHY",
                "action": "start_timeout",
                "checked_at": time.time(),
                "base_url": base_url,
                "project_dir": str(project_dir),
                "error": message,
            },
        )
        raise ProxyProcessError(message)
