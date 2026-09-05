from __future__ import annotations

import json

from scripts import desk_health_alert


def _report(status="CRITICAL"):
    return {
        "status": status,
        "issues": [{"severity": status, "code": "TEST", "detail": "same"}]
        if status != "HEALTHY"
        else [],
    }


def test_local_alerts_are_deduplicated_without_external_credentials(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(desk_health_alert, "build_health_report", lambda *_a, **_k: _report())
    args = [
        "--state-dir",
        str(tmp_path / "state"),
        "--log-dir",
        str(logs),
        "--now",
        "2026-09-05T00:00:00+00:00",
    ]
    assert desk_health_alert.main(args) == 0
    assert desk_health_alert.main(args) == 0
    assert len((logs / "desk-health-alerts.jsonl").read_text().splitlines()) == 1
    state = json.loads((logs / "desk-health-alert-state.json").read_text())
    assert state["status"] == "CRITICAL"
    assert state["hook_pending"] is False


def test_dynamic_age_detail_does_not_defeat_alert_deduplication(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    detail = {"minutes": 70}

    def report(*_args, **_kwargs):
        return {
            "status": "CAUTION",
            "issues": [
                {
                    "severity": "CAUTION",
                    "code": "HEARTBEAT_STALE",
                    "detail": f"heartbeat age is {detail['minutes']} minutes",
                }
            ],
        }

    monkeypatch.setattr(desk_health_alert, "build_health_report", report)
    base = ["--state-dir", str(tmp_path / "state"), "--log-dir", str(logs)]
    assert desk_health_alert.main([*base, "--now", "2026-09-05T00:00:00+00:00"]) == 0
    detail["minutes"] = 80
    assert desk_health_alert.main([*base, "--now", "2026-09-05T00:10:00+00:00"]) == 0

    assert len((logs / "desk-health-alerts.jsonl").read_text().splitlines()) == 1


def test_operator_hook_gets_report_on_stdin_and_retries_failure(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(desk_health_alert, "build_health_report", lambda *_a, **_k: _report())
    calls = []

    class Result:
        returncode = 1

    def run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return Result()

    monkeypatch.setattr(desk_health_alert.subprocess, "run", run)
    args = [
        "--state-dir",
        str(tmp_path / "state"),
        "--log-dir",
        str(logs),
        "--now",
        "2026-09-05T00:00:00+00:00",
        "--alert-command",
        "/usr/bin/logger desk",
    ]
    assert desk_health_alert.main(args) == 2
    assert desk_health_alert.main(args) == 2
    assert len((logs / "desk-health-alerts.jsonl").read_text().splitlines()) == 1
    assert calls[0][0] == ["/usr/bin/logger", "desk"]
    assert json.loads(calls[0][1])["status"] == "CRITICAL"
    assert json.loads((logs / "desk-health-alert-state.json").read_text())["hook_pending"] is True


def test_operator_hook_timeout_keeps_delivery_pending(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    monkeypatch.setattr(desk_health_alert, "build_health_report", lambda *_a, **_k: _report())

    def timeout(*_args, **_kwargs):
        raise desk_health_alert.subprocess.TimeoutExpired("hook", 0.1)

    monkeypatch.setattr(desk_health_alert.subprocess, "run", timeout)
    result = desk_health_alert.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(logs),
            "--alert-command",
            "/usr/bin/logger desk",
            "--hook-timeout-seconds",
            "0.1",
        ]
    )

    assert result == 2
    state = json.loads((logs / "desk-health-alert-state.json").read_text())
    assert state["hook_pending"] is True


def test_recovery_transition_emits_once(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    status = {"value": "CRITICAL"}
    monkeypatch.setattr(
        desk_health_alert,
        "build_health_report",
        lambda *_a, **_k: _report(status["value"]),
    )
    base = ["--state-dir", str(tmp_path / "state"), "--log-dir", str(logs)]
    assert desk_health_alert.main([*base, "--now", "2026-09-05T00:00:00+00:00"]) == 0
    status["value"] = "HEALTHY"
    assert desk_health_alert.main([*base, "--now", "2026-09-05T01:00:00+00:00"]) == 0
    assert desk_health_alert.main([*base, "--now", "2026-09-05T01:00:00+00:00"]) == 0
    rows = [
        json.loads(line) for line in (logs / "desk-health-alerts.jsonl").read_text().splitlines()
    ]
    assert [row["kind"] for row in rows] == ["desk_health_alert", "desk_health_recovered"]


def test_health_builder_failure_becomes_a_persisted_critical_alert(tmp_path, monkeypatch):
    logs = tmp_path / "logs"

    def fail(*_args, **_kwargs):
        raise ValueError("corrupt completion marker")

    monkeypatch.setattr(desk_health_alert, "build_health_report", fail)
    result = desk_health_alert.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(logs),
            "--now",
            "2026-09-05T00:00:00+00:00",
        ]
    )
    assert result == 0
    row = json.loads((logs / "desk-health-alerts.jsonl").read_text())
    assert row["status"] == "CRITICAL"
    assert row["issues"][0]["code"] == "HEALTH_REPORT_FAILED"


def test_corrupt_alert_cursor_becomes_a_persisted_critical_alert(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "desk-health-alert-state.json").write_text("{broken")
    monkeypatch.setattr(
        desk_health_alert,
        "build_health_report",
        lambda *_a, **_k: _report("HEALTHY"),
    )

    assert desk_health_alert.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(logs),
            "--now",
            "2026-09-05T00:00:00+00:00",
        ]
    ) == 0

    row = json.loads((logs / "desk-health-alerts.jsonl").read_text())
    assert row["status"] == "CRITICAL"
    assert row["issues"][0]["code"] == "HEALTH_ALERT_STATE_INVALID"
    repaired = json.loads((logs / "desk-health-alert-state.json").read_text())
    assert repaired["status"] == "CRITICAL"
    preserved = list(logs.glob("desk-health-alert-state.invalid-*.json"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "{broken"


def test_naive_alert_cursor_timestamp_cannot_crash_or_suppress_alert(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "desk-health-alert-state.json").write_text(
        json.dumps(
            {
                "status": "CRITICAL",
                "fingerprint": "old",
                "last_emitted_at": "2026-09-04T00:00:00",
                "hook_pending": False,
            }
        )
    )
    monkeypatch.setattr(desk_health_alert, "build_health_report", lambda *_a, **_k: _report())

    assert desk_health_alert.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(logs),
            "--now",
            "2026-09-05T00:00:00+00:00",
        ]
    ) == 0

    row = json.loads((logs / "desk-health-alerts.jsonl").read_text())
    assert any(issue["code"] == "HEALTH_ALERT_STATE_INVALID" for issue in row["issues"])


def test_corrupt_alert_ledger_is_preserved_and_replaced_with_critical_event(
    tmp_path, monkeypatch
):
    logs = tmp_path / "logs"
    logs.mkdir()
    ledger = logs / "desk-health-alerts.jsonl"
    ledger.write_text("{broken\n")
    monkeypatch.setattr(desk_health_alert, "build_health_report", lambda *_a, **_k: _report())

    assert desk_health_alert.main(
        [
            "--state-dir",
            str(tmp_path / "state"),
            "--log-dir",
            str(logs),
            "--now",
            "2026-09-05T00:00:00+00:00",
        ]
    ) == 0

    row = json.loads(ledger.read_text())
    assert row["status"] == "CRITICAL"
    assert any(issue["code"] == "HEALTH_ALERT_LEDGER_INVALID" for issue in row["issues"])
    preserved = list(logs.glob("desk-health-alerts.invalid-*.jsonl"))
    assert len(preserved) == 1
    assert preserved[0].read_text() == "{broken\n"
