from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from futures_fund.durable_io import canonical_json_sha256
from futures_fund.runtime_provenance import (
    DECISION_START_PROVENANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_ARTIFACT,
    PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT,
    capture_runtime_provenance,
    load_bound_decision_start_provenance,
    verify_runtime_provenance,
)


def test_runtime_fingerprint_binds_dirty_source_config_lock_prompts_and_proxy(tmp_path):
    repo = tmp_path / "repo"
    proxy = tmp_path / "proxy"
    for path, text in (
        (repo / "config.yaml", "live: false\n"),
        (repo / "uv.lock", "lock-v1\n"),
        (repo / "pyproject.toml", "[project]\n"),
        (repo / "ops" / "desk-cycle-prompt.md", "run safely\n"),
        (repo / "agents" / "pm.md", "paper only\n"),
        (repo / "futures_fund" / "desk.py", "VALUE = 1\n"),
        (proxy / "pyproject.toml", "[project]\n"),
        (proxy / "uv.lock", "proxy-lock\n"),
        (proxy / "src" / "binance_proxy" / "app.py", "app = object()\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    when = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)
    first = capture_runtime_provenance(
        repo, proxy_project_dir=proxy, proxy_base_url="http://127.0.0.1:8000", captured_at=when
    )
    assert verify_runtime_provenance(first)
    assert first["files"]["config.yaml"]
    assert first["files"]["uv.lock"]
    assert first["prompt_bundle_sha256"]
    assert first["proxy"]["fingerprint_sha256"]
    assert first["source_file_count"] == len(first["source_inventory"])
    assert first["source_tree_sha256"] == canonical_json_sha256(
        {"files": first["source_inventory"]}
    )
    assert any(row["path"] == "futures_fund/desk.py" for row in first["source_inventory"])

    (repo / "futures_fund" / "desk.py").write_text("VALUE = 2\n")
    second = capture_runtime_provenance(
        repo, proxy_project_dir=proxy, proxy_base_url="http://127.0.0.1:8000", captured_at=when
    )
    assert second["source_tree_sha256"] != first["source_tree_sha256"]
    assert second["files"] == first["files"]


def test_decision_start_provenance_is_path_fixed_meta_bound_and_rechecked(tmp_path, monkeypatch):
    import futures_fund.runtime_provenance as runtime_provenance

    repo = tmp_path / "repo"
    proxy = tmp_path / "proxy"
    (repo / "futures_fund").mkdir(parents=True)
    (repo / "futures_fund" / "desk.py").write_text("VALUE = 1\n")
    (repo / "config.yaml").write_text("live: false\n")
    (proxy / "src").mkdir(parents=True)
    (proxy / "src" / "app.py").write_text("app = object()\n")
    when = datetime(2026, 9, 5, 0, 7, tzinfo=UTC)
    sealed = capture_runtime_provenance(
        repo,
        proxy_project_dir=proxy,
        proxy_base_url="http://127.0.0.1:8000",
        captured_at=when,
    )
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / DECISION_START_PROVENANCE_ARTIFACT).write_text(json.dumps(sealed))
    meta = {
        "now": when.isoformat(),
        "decision_start_runtime_provenance_artifact": DECISION_START_PROVENANCE_ARTIFACT,
        "decision_start_runtime_provenance_sha256": canonical_json_sha256(sealed),
        "decision_start_runtime_provenance_captured_at": when.isoformat(),
    }
    monkeypatch.setattr(
        runtime_provenance,
        "default_runtime_provenance",
        lambda *, captured_at: sealed,
    )

    assert load_bound_decision_start_provenance(pending, meta, require_current_match=True) == sealed

    wrong_current = {**sealed, "source_tree_sha256": "f" * 64}
    monkeypatch.setattr(
        runtime_provenance,
        "default_runtime_provenance",
        lambda *, captured_at: wrong_current,
    )
    with pytest.raises(ValueError, match="runtime build changed"):
        load_bound_decision_start_provenance(pending, meta, require_current_match=True)

    (pending / DECISION_START_PROVENANCE_ARTIFACT).write_text(
        json.dumps({**sealed, "source_file_count": sealed["source_file_count"] + 1})
    )
    with pytest.raises(ValueError, match="self-verification"):
        load_bound_decision_start_provenance(pending, meta)


def test_schema_one_runtime_provenance_remains_readable_for_historical_manifests():
    body = {"schema_version": 1, "captured_at": "2026-01-01T00:00:00+00:00", "legacy": True}
    assert verify_runtime_provenance({**body, "provenance_sha256": canonical_json_sha256(body)})


def test_evidence_nonempty_reflection_then_seal_matches_reconcile_build(tmp_path, monkeypatch):
    """The authoritative build is post-reflection, not the earlier evidence-time tree."""
    import futures_fund.runtime_provenance as runtime_provenance
    from scripts import desk_decision_start

    repo = tmp_path / "repo"
    proxy = tmp_path / "proxy"
    for path, text in (
        (repo / "config.yaml", "live: false\n"),
        (repo / "uv.lock", "lock\n"),
        (repo / "pyproject.toml", "[project]\n"),
        (repo / "ops" / "desk-cycle-prompt.md", "run\n"),
        (repo / "agents" / "pm.md", "<!-- AUTO-REFLECT START -->old<!-- AUTO-REFLECT END -->\n"),
        (repo / "futures_fund" / "desk.py", "VALUE = 1\n"),
        (proxy / "src" / "app.py", "app = object()\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    seal_at = datetime.now(UTC) - timedelta(minutes=1)
    evidence_at = seal_at - timedelta(minutes=5)
    before_reflection = capture_runtime_provenance(
        repo,
        proxy_project_dir=proxy,
        proxy_base_url="http://127.0.0.1:8000",
        captured_at=evidence_at,
    )

    state = tmp_path / "state"
    memory = tmp_path / "memory"
    pending = memory / "pending" / "1"
    pending.mkdir(parents=True)
    (memory / "pending" / "current.json").write_text(
        json.dumps(
            {
                "cycle": 1,
                "dir": str(pending),
                "created": evidence_at.isoformat(),
            }
        )
    )
    meta = {"cycle": 1, "now": evidence_at.isoformat(), "cash": 20_000.0}
    (pending / "meta.json").write_text(json.dumps(meta))
    (pending / "evidence.json").write_text("[]")
    (pending / "risk_model.json").write_text("{}")
    initial_performance = {
        "cycle": 1,
        "as_of_ts": evidence_at.isoformat(),
        "bindings": {"meta_sha256": canonical_json_sha256(meta)},
    }
    (pending / "performance_snapshot.json").write_text(json.dumps(initial_performance))
    (pending / "performance_snapshot.sha256").write_text(
        canonical_json_sha256(initial_performance) + "\n"
    )

    # Simulate a non-empty, successfully applied and checked Reflector proposal.
    (pending / "reflection.json").write_text(
        json.dumps({"edits": [{"role": "pm", "region": "new governed calibration"}]})
    )
    (repo / "agents" / "pm.md").write_text(
        "<!-- AUTO-REFLECT START -->new governed calibration<!-- AUTO-REFLECT END -->\n"
    )

    def capture_post_reflection(*, captured_at):
        return capture_runtime_provenance(
            repo,
            proxy_project_dir=proxy,
            proxy_base_url="http://127.0.0.1:8000",
            captured_at=captured_at,
        )

    def rebuild(*args, **kwargs):
        rebuilt_meta = json.loads((args[2] / "meta.json").read_text())
        return {
            "cycle": 1,
            "as_of_ts": evidence_at.isoformat(),
            "bindings": {"meta_sha256": canonical_json_sha256(rebuilt_meta)},
            "stage": "post_reflection_decision_start",
        }

    monkeypatch.setattr(desk_decision_start, "_validate_reflector_heads", lambda **kwargs: None)
    monkeypatch.setattr(desk_decision_start, "default_runtime_provenance", capture_post_reflection)
    monkeypatch.setattr(runtime_provenance, "default_runtime_provenance", capture_post_reflection)
    monkeypatch.setattr(desk_decision_start, "build_performance_snapshot", rebuild)
    monkeypatch.setattr(
        desk_decision_start,
        "load_settings",
        lambda: SimpleNamespace(account_size_usdt=20_000.0),
    )

    result = desk_decision_start.seal_decision_start(
        state_dir=str(state),
        memory_dir=str(memory),
        agents_dir=str(repo / "agents"),
        captured_at=seal_at,
    )
    sealed_meta = json.loads((pending / "meta.json").read_text())
    sealed = load_bound_decision_start_provenance(pending, sealed_meta, require_current_match=True)
    final_performance = json.loads((pending / "performance_snapshot.json").read_text())
    assert sealed["prompt_bundle_sha256"] != before_reflection["prompt_bundle_sha256"]
    assert sealed["captured_at"] == seal_at.isoformat()
    assert result["runtime_provenance_sha256"] == canonical_json_sha256(sealed)
    assert sealed_meta["pre_reflection_performance_snapshot_sha256"] == (
        canonical_json_sha256(initial_performance)
    )
    assert sealed_meta["pre_reflection_performance_snapshot_artifact"] == (
        PRE_REFLECTION_PERFORMANCE_ARTIFACT
    )
    assert json.loads((pending / PRE_REFLECTION_PERFORMANCE_ARTIFACT).read_text()) == (
        initial_performance
    )
    assert (pending / PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT).read_text().strip() == (
        canonical_json_sha256(initial_performance)
    )
    assert final_performance["bindings"]["meta_sha256"] == canonical_json_sha256(sealed_meta)
    assert (pending / "performance_snapshot.sha256").read_text().strip() == (
        canonical_json_sha256(final_performance)
    )

    # An orchestration retry before specialists is idempotent and preserves the original seal.
    repeated = desk_decision_start.seal_decision_start(
        state_dir=str(state),
        memory_dir=str(memory),
        agents_dir=str(repo / "agents"),
        captured_at=datetime.now(UTC),
    )
    assert repeated == result
    assert json.loads((pending / DECISION_START_PROVENANCE_ARTIFACT).read_text()) == sealed

    pre_reflection_path = pending / PRE_REFLECTION_PERFORMANCE_ARTIFACT
    archived = pre_reflection_path.read_text()
    pre_reflection_path.write_text('{"tampered": true}')
    with pytest.raises(ValueError, match="pre-reflection performance archive hash mismatch"):
        desk_decision_start.seal_decision_start(
            state_dir=str(state),
            memory_dir=str(memory),
            agents_dir=str(repo / "agents"),
        )
    pre_reflection_path.write_text(archived)

    (repo / "agents" / "pm.md").write_text("post-seal unauthorized mutation\n")
    with pytest.raises(ValueError, match="runtime build changed"):
        load_bound_decision_start_provenance(pending, sealed_meta, require_current_match=True)


def test_decision_start_seal_refuses_existing_specialist_output(tmp_path, monkeypatch):
    from scripts import desk_decision_start

    memory = tmp_path / "memory"
    pending = memory / "pending" / "1"
    pending.mkdir(parents=True)
    now = datetime.now(UTC) - timedelta(minutes=1)
    (memory / "pending" / "current.json").write_text(
        json.dumps({"cycle": 1, "dir": str(pending), "created": now.isoformat()})
    )
    (pending / "meta.json").write_text(
        json.dumps({"cycle": 1, "now": now.isoformat(), "cash": 20_000.0})
    )
    (pending / "technical_reads.json").write_text("[]")
    monkeypatch.setattr(desk_decision_start, "_validate_reflector_heads", lambda **kwargs: None)

    with pytest.raises(ValueError, match="must precede every specialist"):
        desk_decision_start.seal_decision_start(
            state_dir=str(tmp_path / "state"),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
            captured_at=now,
        )


def test_decision_start_seal_refuses_preexisting_unbound_artifact(tmp_path, monkeypatch):
    from scripts import desk_decision_start

    memory = tmp_path / "memory"
    pending = memory / "pending" / "1"
    pending.mkdir(parents=True)
    now = datetime.now(UTC) - timedelta(minutes=1)
    (memory / "pending" / "current.json").write_text(
        json.dumps({"cycle": 1, "dir": str(pending), "created": now.isoformat()})
    )
    meta = {"cycle": 1, "now": now.isoformat(), "cash": 20_000.0}
    (pending / "meta.json").write_text(json.dumps(meta))
    (pending / DECISION_START_PROVENANCE_ARTIFACT).write_text("{}")
    monkeypatch.setattr(desk_decision_start, "_validate_reflector_heads", lambda **kwargs: None)

    with pytest.raises(ValueError, match="unbound decision-start provenance"):
        desk_decision_start.seal_decision_start(
            state_dir=str(tmp_path / "state"),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
            captured_at=now,
        )


def _decision_start_crash_fixture(tmp_path, monkeypatch):
    import futures_fund.runtime_provenance as runtime_provenance
    from scripts import desk_decision_start

    repo = tmp_path / "repo"
    proxy = tmp_path / "proxy"
    for path, text in (
        (repo / "config.yaml", "live: false\n"),
        (repo / "uv.lock", "lock\n"),
        (repo / "pyproject.toml", "[project]\n"),
        (repo / "agents" / "pm.md", "paper only\n"),
        (repo / "futures_fund" / "desk.py", "VALUE = 1\n"),
        (proxy / "src" / "app.py", "app = object()\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    evidence_at = datetime.now(UTC) - timedelta(minutes=10)
    seal_at = evidence_at + timedelta(minutes=1)
    state = tmp_path / "state"
    memory = tmp_path / "memory"
    pending = memory / "pending" / "7"
    pending.mkdir(parents=True)
    (memory / "pending" / "current.json").write_text(
        json.dumps({"cycle": 7, "dir": str(pending), "created": evidence_at.isoformat()})
    )
    evidence = []
    risk_model = {}
    (pending / "evidence.json").write_text(json.dumps(evidence))
    (pending / "risk_model.json").write_text(json.dumps(risk_model))
    meta = {
        "cycle": 7,
        "now": evidence_at.isoformat(),
        "cash": 20_000.0,
        "evidence_sha256": canonical_json_sha256(evidence),
        "risk_model_sha256": canonical_json_sha256(risk_model),
    }
    (pending / "meta.json").write_text(json.dumps(meta))
    initial = {
        "cycle": 7,
        "as_of_ts": evidence_at.isoformat(),
        "bindings": {"meta_sha256": canonical_json_sha256(meta)},
        "stage": "pre_reflection",
    }
    (pending / "performance_snapshot.json").write_text(json.dumps(initial))
    (pending / "performance_snapshot.sha256").write_text(canonical_json_sha256(initial) + "\n")
    provenance = capture_runtime_provenance(
        repo,
        proxy_project_dir=proxy,
        proxy_base_url="http://127.0.0.1:8000",
        captured_at=seal_at,
    )

    def capture(*, captured_at):
        assert captured_at == seal_at
        return provenance

    def rebuild(*args, **kwargs):
        rebuilt_meta = json.loads((args[2] / "meta.json").read_text())
        return {
            "cycle": 7,
            "as_of_ts": evidence_at.isoformat(),
            "bindings": {"meta_sha256": canonical_json_sha256(rebuilt_meta)},
            "stage": "post_reflection_decision_start",
        }

    monkeypatch.setattr(desk_decision_start, "_validate_reflector_heads", lambda **kwargs: None)
    monkeypatch.setattr(desk_decision_start, "default_runtime_provenance", capture)
    monkeypatch.setattr(runtime_provenance, "default_runtime_provenance", capture)
    monkeypatch.setattr(desk_decision_start, "build_performance_snapshot", rebuild)
    monkeypatch.setattr(
        desk_decision_start,
        "load_settings",
        lambda: SimpleNamespace(account_size_usdt=20_000.0),
    )
    return desk_decision_start, state, memory, pending, seal_at


@pytest.mark.parametrize("failure_boundary", range(1, 9))
def test_decision_start_transaction_replays_every_durable_boundary(
    tmp_path, monkeypatch, failure_boundary
):
    desk_decision_start, state, memory, pending, seal_at = _decision_start_crash_fixture(
        tmp_path, monkeypatch
    )
    original_json = desk_decision_start.durable_write_json
    original_text = desk_decision_start.durable_write_text
    original_unlink = desk_decision_start.durable_unlink
    writes = 0

    def after_boundary(call, *args, **kwargs):
        nonlocal writes
        result = call(*args, **kwargs)
        writes += 1
        if writes == failure_boundary:
            raise OSError(f"injected crash after durable boundary {failure_boundary}")
        return result

    monkeypatch.setattr(
        desk_decision_start,
        "durable_write_json",
        lambda *args, **kwargs: after_boundary(original_json, *args, **kwargs),
    )
    monkeypatch.setattr(
        desk_decision_start,
        "durable_write_text",
        lambda *args, **kwargs: after_boundary(original_text, *args, **kwargs),
    )
    monkeypatch.setattr(
        desk_decision_start,
        "durable_unlink",
        lambda *args, **kwargs: after_boundary(original_unlink, *args, **kwargs),
    )
    with pytest.raises(OSError, match=f"injected crash after durable boundary {failure_boundary}"):
        desk_decision_start.seal_decision_start(
            state_dir=str(state),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
            captured_at=seal_at,
        )

    monkeypatch.setattr(desk_decision_start, "durable_write_json", original_json)
    monkeypatch.setattr(desk_decision_start, "durable_write_text", original_text)
    monkeypatch.setattr(desk_decision_start, "durable_unlink", original_unlink)
    result = desk_decision_start.seal_decision_start(
        state_dir=str(state),
        memory_dir=str(memory),
        agents_dir=str(tmp_path / "agents"),
        captured_at=seal_at + timedelta(minutes=1),
    )
    assert result["cycle"] == 7
    assert not (pending / desk_decision_start.DECISION_START_TRANSACTION_ARTIFACT).exists()
    sealed_meta = json.loads((pending / "meta.json").read_text())
    assert sealed_meta["decision_start_runtime_provenance_captured_at"] == seal_at.isoformat()
    final = json.loads((pending / "performance_snapshot.json").read_text())
    assert (pending / "performance_snapshot.sha256").read_text().strip() == (
        canonical_json_sha256(final)
    )


def test_decision_start_transaction_retains_intent_until_fresh_rebuild_verifies(
    tmp_path, monkeypatch
):
    desk_decision_start, state, memory, pending, seal_at = _decision_start_crash_fixture(
        tmp_path, monkeypatch
    )
    stable_builder = desk_decision_start.build_performance_snapshot
    calls = 0

    def changes_during_publication(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = stable_builder(*args, **kwargs)
        if calls == 2:
            return {**result, "changed_after_intent": True}
        return result

    monkeypatch.setattr(
        desk_decision_start, "build_performance_snapshot", changes_during_publication
    )
    with pytest.raises(ValueError, match="changed during decision-start transaction"):
        desk_decision_start.seal_decision_start(
            state_dir=str(state),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
            captured_at=seal_at,
        )
    intent = pending / desk_decision_start.DECISION_START_TRANSACTION_ARTIFACT
    assert intent.exists()

    result = desk_decision_start.seal_decision_start(
        state_dir=str(state),
        memory_dir=str(memory),
        agents_dir=str(tmp_path / "agents"),
    )
    assert result["cycle"] == 7
    assert not intent.exists()


def test_decision_start_transaction_rejects_conflicting_partial_artifact(tmp_path, monkeypatch):
    desk_decision_start, state, memory, pending, seal_at = _decision_start_crash_fixture(
        tmp_path, monkeypatch
    )
    original_json = desk_decision_start.durable_write_json

    def crash_after_intent(path, value, **kwargs):
        result = original_json(path, value, **kwargs)
        if path.name == desk_decision_start.DECISION_START_TRANSACTION_ARTIFACT:
            raise OSError("injected crash after intent")
        return result

    monkeypatch.setattr(desk_decision_start, "durable_write_json", crash_after_intent)
    with pytest.raises(OSError, match="injected crash after intent"):
        desk_decision_start.seal_decision_start(
            state_dir=str(state),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
            captured_at=seal_at,
        )
    monkeypatch.setattr(desk_decision_start, "durable_write_json", original_json)
    (pending / PRE_REFLECTION_PERFORMANCE_ARTIFACT).write_text('{"conflict": true}')
    with pytest.raises(
        ValueError, match=f"conflicting {PRE_REFLECTION_PERFORMANCE_ARTIFACT} artifact"
    ):
        desk_decision_start.seal_decision_start(
            state_dir=str(state),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
        )


def test_decision_start_transaction_rejects_hash_tampering(tmp_path, monkeypatch):
    desk_decision_start, state, memory, pending, seal_at = _decision_start_crash_fixture(
        tmp_path, monkeypatch
    )
    original_json = desk_decision_start.durable_write_json

    def crash_after_intent(path, value, **kwargs):
        result = original_json(path, value, **kwargs)
        if path.name == desk_decision_start.DECISION_START_TRANSACTION_ARTIFACT:
            raise OSError("injected crash after intent")
        return result

    monkeypatch.setattr(desk_decision_start, "durable_write_json", crash_after_intent)
    with pytest.raises(OSError, match="injected crash after intent"):
        desk_decision_start.seal_decision_start(
            state_dir=str(state),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
            captured_at=seal_at,
        )
    monkeypatch.setattr(desk_decision_start, "durable_write_json", original_json)
    intent_path = pending / desk_decision_start.DECISION_START_TRANSACTION_ARTIFACT
    intent = json.loads(intent_path.read_text())
    intent["performance_snapshot"]["tampered"] = True
    intent_path.write_text(json.dumps(intent))

    with pytest.raises(ValueError, match="decision-start transaction hash mismatch"):
        desk_decision_start.seal_decision_start(
            state_dir=str(state),
            memory_dir=str(memory),
            agents_dir=str(tmp_path / "agents"),
        )


@pytest.mark.parametrize("mutation", ["tracked_source", "config", "prompt", "proxy_source"])
def test_reconcile_loader_rejects_real_postseal_source_mutations(tmp_path, monkeypatch, mutation):
    import futures_fund.runtime_provenance as runtime_provenance

    repo = tmp_path / "repo"
    proxy = tmp_path / "proxy"
    targets = {
        "tracked_source": repo / "futures_fund" / "desk.py",
        "config": repo / "config.yaml",
        "prompt": repo / "agents" / "pm.md",
        "proxy_source": proxy / "src" / "app.py",
    }
    for path in targets.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {path.name}\n")
    (repo / "uv.lock").write_text("lock\n")
    (repo / "pyproject.toml").write_text("[project]\n")
    (repo / "ops").mkdir()
    (repo / "ops" / "desk-cycle-prompt.md").write_text("run\n")
    sealed_at = datetime.now(UTC) - timedelta(minutes=1)

    def capture_current(*, captured_at):
        return capture_runtime_provenance(
            repo,
            proxy_project_dir=proxy,
            proxy_base_url="http://127.0.0.1:8000",
            captured_at=captured_at,
        )

    sealed = capture_current(captured_at=sealed_at)
    pending = tmp_path / "pending"
    pending.mkdir()
    (pending / DECISION_START_PROVENANCE_ARTIFACT).write_text(json.dumps(sealed))
    meta = {
        "now": (sealed_at - timedelta(minutes=5)).isoformat(),
        "decision_start_runtime_provenance_artifact": DECISION_START_PROVENANCE_ARTIFACT,
        "decision_start_runtime_provenance_sha256": canonical_json_sha256(sealed),
        "decision_start_runtime_provenance_captured_at": sealed_at.isoformat(),
    }
    monkeypatch.setattr(runtime_provenance, "default_runtime_provenance", capture_current)
    assert load_bound_decision_start_provenance(pending, meta, require_current_match=True) == sealed

    targets[mutation].write_text(f"mutated {mutation}\n")
    with pytest.raises(ValueError, match="runtime build changed"):
        load_bound_decision_start_provenance(pending, meta, require_current_match=True)
