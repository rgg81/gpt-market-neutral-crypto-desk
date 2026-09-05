"""Emit deduplicated local desk-health alerts and optionally call an operator hook."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from futures_fund.desk_health import build_health_report
from futures_fund.durable_io import (
    canonical_json_sha256,
    durable_write_bytes,
    durable_write_json,
    durable_write_text,
)


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _alert_state(path: Path) -> tuple[dict, datetime | None, str | None]:
    """Load the deduplication cursor without letting its corruption silence alerts."""
    try:
        raw = _read_json(path, {})
        if not isinstance(raw, dict):
            raise ValueError("alert state is not a JSON object")
        last_emitted = raw.get("last_emitted_at")
        parsed: datetime | None = None
        if last_emitted is not None:
            parsed = datetime.fromisoformat(str(last_emitted).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("last_emitted_at must be timezone-aware")
            parsed = parsed.astimezone(UTC)
        return raw, parsed, None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return {}, None, f"{type(exc).__name__}: {exc}"


def _with_alert_storage_failure(
    report: dict,
    detail: str,
    *,
    code: str = "HEALTH_ALERT_STATE_INVALID",
) -> dict:
    """Promote broken alert storage to an observable, locally persisted CRITICAL."""
    amended = dict(report)
    issues = [dict(row) for row in report.get("issues", [])]
    issues.append(
        {
            "severity": "CRITICAL",
            "code": code,
            "detail": detail,
        }
    )
    amended["issues"] = issues
    amended["status"] = "CRITICAL"
    return amended


def _preserve_invalid_state(path: Path) -> Path | None:
    """Keep the untrusted cursor bytes available after the live cursor is repaired."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    digest = sha256(raw).hexdigest()
    preserved = path.with_name(f"{path.stem}.invalid-{digest[:16]}{path.suffix}")
    if not preserved.exists():
        durable_write_bytes(preserved, raw)
    return preserved


def _append_local_alert(path: Path, event: dict) -> None:
    rows: list[dict] = []
    if path.exists():
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"malformed local alert ledger {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"non-object local alert row {path}:{line_number}")
            rows.append(row)
    rows.append(event)
    durable_write_text(path, "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))


def _alert_fingerprint(report: dict) -> str:
    """Fingerprint condition identity, not continuously changing diagnostic measurements.

    Age, lag, and PnL details naturally change on every ten-minute health poll. Including those
    values would turn one stale condition into an alert storm and defeat the reminder window.
    The full details remain in every emitted event; severity/code transitions emit immediately.
    """
    identities = sorted(
        {
            (str(row.get("severity", "")), str(row.get("code", "")))
            for row in report.get("issues", [])
            if isinstance(row, dict)
        }
    )
    material = {
        "status": report["status"],
        "issues": [
            {"severity": severity, "code": code}
            for severity, code in identities
        ],
    }
    return canonical_json_sha256(material)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default="live_state")
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--remind-hours", type=float, default=24.0)
    parser.add_argument("--hook-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--alert-command",
        help=(
            "optional shell-like command; invoked without a shell and receives report JSON on stdin"
        ),
    )
    parser.add_argument("--now", help="ISO UTC test/forensic timestamp")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    if args.now:
        now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    logs = Path(args.log_dir)
    logs.mkdir(parents=True, exist_ok=True)
    state_path = logs / "desk-health-alert-state.json"
    ledger_path = logs / "desk-health-alerts.jsonl"
    try:
        report = build_health_report(args.state_dir, args.log_dir, now=now)
    except Exception as exc:  # noqa: BLE001 - the alert path must survive malformed desk state
        report = {
            "schema_version": 1,
            "paper_only": True,
            "generated_at": now.isoformat(),
            "status": "CRITICAL",
            "issues": [
                {
                    "severity": "CRITICAL",
                    "code": "HEALTH_REPORT_FAILED",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
    fingerprint = _alert_fingerprint(report)
    prior, last_emitted_at, state_error = _alert_state(state_path)
    if state_error is not None:
        preserved = _preserve_invalid_state(state_path)
        detail = state_error
        if preserved is not None:
            detail = f"{detail}; preserved={preserved}"
        report = _with_alert_storage_failure(report, detail)
        fingerprint = _alert_fingerprint(report)
    prior_status = prior.get("status")
    reminder_due = bool(
        last_emitted_at
        and now - last_emitted_at >= timedelta(hours=max(0.0, args.remind_hours))
    )
    recovered = report["status"] == "HEALTHY" and prior_status in {"CAUTION", "CRITICAL"}
    actionable = report["status"] != "HEALTHY" or recovered
    new_condition = fingerprint != prior.get("fingerprint")
    emit = actionable and (new_condition or reminder_due)
    hook_pending = bool(prior.get("hook_pending") and fingerprint == prior.get("fingerprint"))

    if not emit and not hook_pending:
        print(json.dumps({"status": report["status"], "alert": "suppressed_duplicate"}))
        return 0

    event = {
        "ts": now.isoformat(),
        "kind": "desk_health_recovered" if recovered else "desk_health_alert",
        "status": report["status"],
        "fingerprint": fingerprint,
        "issues": report.get("issues", []),
    }
    if emit:
        try:
            _append_local_alert(ledger_path, event)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            preserved = _preserve_invalid_state(ledger_path)
            detail = f"{type(exc).__name__}: {exc}"
            if preserved is not None:
                detail = f"{detail}; preserved={preserved}"
            report = _with_alert_storage_failure(
                report,
                detail,
                code="HEALTH_ALERT_LEDGER_INVALID",
            )
            fingerprint = _alert_fingerprint(report)
            event.update(
                {
                    "status": report["status"],
                    "fingerprint": fingerprint,
                    "issues": report.get("issues", []),
                }
            )
            durable_write_text(ledger_path, "")
            _append_local_alert(ledger_path, event)
    next_state = {
        "version": 1,
        "status": report["status"],
        "fingerprint": fingerprint,
        "last_emitted_at": now.isoformat() if emit else prior.get("last_emitted_at"),
        "hook_pending": bool(args.alert_command),
    }
    durable_write_json(state_path, next_state)

    if args.alert_command:
        command = shlex.split(args.alert_command)
        if not command:
            raise ValueError("--alert-command is empty")
        try:
            completed = subprocess.run(  # noqa: S603 - explicit operator hook, no shell
                command,
                input=json.dumps(report),
                text=True,
                check=False,
                timeout=max(0.1, args.hook_timeout_seconds),
            )
        except subprocess.TimeoutExpired:
            print(json.dumps({"status": report["status"], "alert": "hook_timed_out"}))
            return 2
        if completed.returncode != 0:
            print(json.dumps({"status": report["status"], "alert": "hook_failed"}))
            return 2
        next_state["hook_pending"] = False
        next_state["last_hook_at"] = now.isoformat()
        durable_write_json(state_path, next_state)
    print(json.dumps({"status": report["status"], "alert": "emitted", "event": event}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
