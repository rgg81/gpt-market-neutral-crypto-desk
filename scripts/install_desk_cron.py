"""Install or inspect the managed 8-hour GPT desk crontab block.

Debian cron uses the daemon's timezone and does not support per-user ``CRON_TZ``. The installer
therefore maps the funding-boundary UTC hours to the host timezone and records that mapping in the
managed block. The launcher independently checks the UTC slot before starting Codex, so a stale
timezone mapping fails closed instead of trading at the wrong time.
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BEGIN = "# BEGIN market-neutral-v2 GPT desk (managed)"
END = "# END market-neutral-v2 GPT desk (managed)"
UTC_HOURS = (0, 8, 16)
MINUTE = 7
ROOT = Path(__file__).resolve().parents[1]


def system_timezone(path: Path = Path("/etc/timezone")) -> str:
    """Return the configured IANA timezone name."""
    if not path.exists():
        raise RuntimeError("/etc/timezone is unavailable; refusing to guess cron hours")
    name = path.read_text().strip()
    if not name:
        raise RuntimeError("/etc/timezone is empty; refusing to guess cron hours")
    return name


def stable_utc_offset_hours(zone_name: str, now: datetime | None = None) -> int:
    """Return a whole-hour offset, refusing zones that change offset in the next 370 days."""
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"unknown system timezone: {zone_name}") from exc
    start = now or datetime.now(UTC)
    offsets: set[timedelta | None] = set()
    for days in range(0, 371, 14):
        offsets.add((start + timedelta(days=days)).astimezone(zone).utcoffset())
    if len(offsets) != 1:
        raise RuntimeError(
            f"{zone_name} changes UTC offset within 370 days; use a UTC-aware systemd timer"
        )
    offset = offsets.pop()
    if offset is None or offset.total_seconds() % 3600:
        raise RuntimeError(f"{zone_name} does not have a stable whole-hour UTC offset")
    return int(offset.total_seconds() // 3600)


def local_hours(offset_hours: int) -> tuple[int, ...]:
    """Map 00/08/16 UTC into sorted local cron hours."""
    return tuple(sorted((hour + offset_hours) % 24 for hour in UTC_HOURS))


def managed_block(root: Path, zone_name: str, offset_hours: int) -> str:
    """Build the exact managed crontab block."""
    hours = ",".join(str(hour) for hour in local_hours(offset_hours))
    launcher = shlex.quote(str(root / "scripts" / "run_scheduled_cycle.sh"))
    log = shlex.quote(str(root / "logs" / "desk-cycle.log"))
    sign = "+" if offset_hours >= 0 else "-"
    offset = f"UTC{sign}{abs(offset_hours):02d}:00"
    return "\n".join(
        [
            BEGIN,
            f"# Funding-aligned 00:07/08:07/16:07 UTC; host {zone_name} ({offset}).",
            f"{MINUTE} {hours} * * * /usr/bin/bash {launcher} --scheduled >> {log} 2>&1",
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
    zone_name = system_timezone()
    offset_hours = stable_utc_offset_hours(zone_name)
    return read_crontab(), managed_block(ROOT, zone_name, offset_hours)


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
    log_path = log_dir / "desk-cycle.log"
    log_path.touch(exist_ok=True)
    log_path.chmod(0o600)
    install_crontab(expected)
    print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
