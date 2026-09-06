"""Install or inspect the daily weight-review plus token-free-heartbeat crontab block.

Debian cron uses the daemon's timezone and does not support per-user ``CRON_TZ``. The launchers are
therefore polled in every local hour and independently claim a bounded UTC slot. No host-timezone
mapping is needed, and clock corrections cannot silently skip an exact-minute job or create
duplicate desk attempts.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

BEGIN = "# BEGIN market-neutral-v2 GPT desk (managed)"
END = "# END market-neutral-v2 GPT desk (managed)"
TASK_POLL_MINUTES = "7-57/10"
HEALTH_POLL_MINUTES = "2-52/10"
ROOT = Path(__file__).resolve().parents[1]


def managed_block(root: Path) -> str:
    """Build the exact managed crontab block."""
    full_launcher = shlex.quote(str(root / "scripts" / "run_scheduled_cycle.sh"))
    heartbeat_launcher = shlex.quote(str(root / "scripts" / "run_desk_heartbeat.sh"))
    health_python = shlex.quote(str(root / ".venv" / "bin" / "python"))
    health_script = shlex.quote(str(root / "scripts" / "desk_health_alert.py"))
    state_dir = shlex.quote(str(root / "live_state"))
    log_dir = shlex.quote(str(root / "logs"))
    full_log = shlex.quote(str(root / "logs" / "desk-cycle.log"))
    heartbeat_log = shlex.quote(str(root / "logs" / "desk-heartbeat.log"))
    health_log = shlex.quote(str(root / "logs" / "desk-health.log"))
    return "\n".join(
        [
            BEGIN,
            "# Daily GPT weight review at 00:07 UTC; weekly selection refreshes automatically.",
            "# Ten-minute polling; full slots claim once, failed heartbeats retry "
            "within six hours.",
            f"{TASK_POLL_MINUTES} * * * * /usr/bin/bash {full_launcher} --scheduled "
            f">> {full_log} 2>&1",
            "# Token-free PAPER funding/portfolio heartbeats at 08:07 and 16:07 UTC.",
            f"{TASK_POLL_MINUTES} * * * * /usr/bin/bash {heartbeat_launcher} --scheduled "
            f">> {heartbeat_log} 2>&1",
            "# Token-free read-only SLO check with deduplicated local alerts.",
            "# Offset from :07 task polling so normal durable publication is never alerted.",
            f"{HEALTH_POLL_MINUTES} * * * * {health_python} {health_script} "
            f"--state-dir {state_dir} --log-dir {log_dir} "
            f">> {health_log} 2>&1",
            END,
        ]
    )


def without_managed_block(existing: str) -> str:
    """Remove all prior managed blocks while preserving every unrelated crontab line."""
    output: list[str] = []
    inside = False
    for line in existing.splitlines():
        if line == BEGIN:
            if inside:
                raise ValueError("nested managed crontab block")
            inside = True
            continue
        if line == END:
            if not inside:
                raise ValueError("managed crontab end marker without begin marker")
            inside = False
            continue
        if not inside:
            output.append(line)
    if inside:
        raise ValueError("unterminated managed crontab block")
    return "\n".join(output).rstrip()


def render_crontab(existing: str, block: str) -> str:
    """Idempotently append one managed block to the current user crontab."""
    base = without_managed_block(existing)
    return f"{base}\n\n{block}\n" if base else f"{block}\n"


def read_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], text=True, capture_output=True, check=False)
    if result.returncode == 0:
        return result.stdout
    if "no crontab for" in result.stderr.lower():
        return ""
    raise RuntimeError(result.stderr.strip() or f"crontab -l failed with {result.returncode}")


def install_crontab(content: str) -> None:
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def expected_crontab() -> tuple[str, str]:
    return read_crontab(), managed_block(ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true", help="install/update the managed block")
    action.add_argument("--check", action="store_true", help="verify the managed block is current")
    action.add_argument("--print", dest="print_only", action="store_true",
                        help="print the crontab that --install would write")
    args = parser.parse_args(argv)

    existing, block = expected_crontab()
    expected = render_crontab(existing, block)
    if args.print_only:
        print(expected, end="")
        return 0
    if args.check:
        current_block_present = block in existing
        exactly_one = existing.count(BEGIN) == 1 and existing.count(END) == 1
        if current_block_present and exactly_one:
            print("OK: managed GPT desk schedule is installed and current")
            return 0
        print("STALE_OR_MISSING: run with --install", file=sys.stderr)
        return 1

    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for name in ("desk-cycle.log", "desk-heartbeat.log", "desk-health.log"):
        log_path = log_dir / name
        log_path.touch(exist_ok=True)
        log_path.chmod(0o600)
    claims_path = log_dir / "desk-schedule-claims.json"
    if not claims_path.exists():
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "desk_schedule_gate.py"),
                "--claims",
                str(claims_path),
                "--seed-all",
            ],
            text=True,
            check=True,
        )
    install_crontab(expected)
    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
