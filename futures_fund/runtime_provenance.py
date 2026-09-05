"""Reproducible fingerprints for the exact desk and proxy build used by a state event."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from futures_fund.durable_io import canonical_json_sha256, file_sha256

PROVENANCE_SCHEMA_VERSION = 2
SUPPORTED_PROVENANCE_SCHEMA_VERSIONS = {1, PROVENANCE_SCHEMA_VERSION}
DECISION_START_PROVENANCE_ARTIFACT = "runtime_provenance.json"
PRE_REFLECTION_PERFORMANCE_ARTIFACT = "performance_snapshot_pre_reflection.json"
PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT = (
    "performance_snapshot_pre_reflection.sha256"
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed git executable and caller-owned arguments
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
    )


def _fallback_files(root: Path) -> list[Path]:
    names = {"pyproject.toml", "uv.lock", "config.yaml"}
    # ``src`` is load-bearing for the separately fingerprinted proxy when it is deployed from a
    # source tree without Git metadata. Omitting it would make proxy-code mutations invisible.
    roots = ("futures_fund", "scripts", "agents", "ops", "src")
    files = [root / name for name in names if (root / name).is_file()]
    for name in roots:
        base = root / name
        if base.is_dir():
            files.extend(
                path
                for path in base.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
    return sorted(set(files))


def _source_inventory(root: Path) -> tuple[list[dict], dict]:
    listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    git_available = listed.returncode == 0
    if git_available:
        relative_names = [
            raw.decode("utf-8", errors="surrogateescape")
            for raw in listed.stdout.split(b"\0")
            if raw
        ]
        files = [root / name for name in relative_names]
    else:
        files = _fallback_files(root)
    inventory: list[dict] = []
    for path in sorted(files):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        inventory.append(
            {
                "path": relative,
                "sha256": file_sha256(path),
                "executable": bool(path.stat().st_mode & 0o111),
            }
        )
    head = _git(root, "rev-parse", "HEAD") if git_available else None
    branch = _git(root, "branch", "--show-current") if git_available else None
    status = _git(root, "status", "--porcelain=v1", "-z") if git_available else None
    status_bytes = status.stdout if status is not None and status.returncode == 0 else b""
    git_state = {
        "available": git_available,
        "head": (
            head.stdout.decode().strip() if head is not None and head.returncode == 0 else None
        ),
        "branch": (
            branch.stdout.decode().strip()
            if branch is not None and branch.returncode == 0
            else None
        ),
        "dirty": bool(status_bytes),
        "status_sha256": sha256(status_bytes).hexdigest(),
    }
    return inventory, git_state


def _named_file_hashes(root: Path, relative_paths: list[str]) -> dict[str, str | None]:
    return {
        name: file_sha256(root / name) if (root / name).is_file() else None
        for name in relative_paths
    }


def _proxy_fingerprint(project_dir: Path, base_url: str) -> dict:
    project_dir = project_dir.expanduser().resolve()
    inventory, git_state = _source_inventory(project_dir)
    selected = [
        row
        for row in inventory
        if row["path"].startswith("src/")
        or row["path"] in {"pyproject.toml", "uv.lock", "requirements.txt"}
    ]
    uvicorn = project_dir / ".venv" / "bin" / "uvicorn"
    payload = {
        "project_dir": str(project_dir),
        "base_url": base_url.rstrip("/"),
        "git": git_state,
        "files": selected,
        "uvicorn_sha256": file_sha256(uvicorn) if uvicorn.is_file() else None,
    }
    return {**payload, "fingerprint_sha256": canonical_json_sha256(payload)}


def capture_runtime_provenance(
    repo_root: str | Path,
    *,
    proxy_project_dir: str | Path,
    proxy_base_url: str,
    captured_at: datetime | None = None,
) -> dict:
    """Capture content, not cleanliness: dirty and untracked source is fingerprinted exactly."""
    root = Path(repo_root).resolve()
    inventory, git_state = _source_inventory(root)
    prompt_paths = ["ops/desk-cycle-prompt.md"] + [
        path.relative_to(root).as_posix() for path in sorted((root / "agents").glob("*.md"))
    ]
    source_payload = {"files": inventory}
    prompts = _named_file_hashes(root, prompt_paths)
    static = _named_file_hashes(root, ["config.yaml", "uv.lock", "pyproject.toml"])
    proxy = _proxy_fingerprint(Path(proxy_project_dir), proxy_base_url)
    payload = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "captured_at": (captured_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "repo_root": str(root),
        "git": git_state,
        "source_tree_sha256": canonical_json_sha256(source_payload),
        "source_file_count": len(inventory),
        # Keep the inventory in the sealed artifact so the tree hash is independently
        # reconstructible. Only the artifact path/hash enters cycle meta and agent-facing
        # packets; this potentially large list is never injected into a prompt.
        "source_inventory": inventory,
        "files": static,
        "prompt_files": prompts,
        "prompt_bundle_sha256": canonical_json_sha256(prompts),
        "proxy": proxy,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": os.path.realpath(sys.executable),
        },
    }
    payload["provenance_sha256"] = canonical_json_sha256(payload)
    return payload


def default_runtime_provenance(*, captured_at: datetime | None = None) -> dict:
    """Capture this checkout and the configured local proxy without mutating either."""
    from futures_fund.config import load_settings

    root = Path(__file__).resolve().parents[1]
    settings = load_settings(root / "config.yaml")
    return capture_runtime_provenance(
        root,
        proxy_project_dir=settings.data.binance_proxy_project_dir,
        proxy_base_url=settings.data.binance_klines_proxy_url,
        captured_at=captured_at,
    )


def verify_runtime_provenance(value: object) -> bool:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") not in SUPPORTED_PROVENANCE_SCHEMA_VERSIONS
    ):
        return False
    expected = value.get("provenance_sha256")
    body = dict(value)
    body.pop("provenance_sha256", None)
    if not isinstance(expected, str) or canonical_json_sha256(body) != expected:
        return False
    # Schema 1 remains readable for historical manifests. New captures prove that the reported
    # source-tree digest/count are actually derivable from a path-level inventory.
    if value.get("schema_version") == 1:
        return True
    inventory = value.get("source_inventory")
    if not isinstance(inventory, list) or len(inventory) != value.get("source_file_count"):
        return False
    paths: list[str] = []
    for row in inventory:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "executable"}:
            return False
        path = row.get("path")
        digest = row.get("sha256")
        if (
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or not isinstance(digest, str)
            or len(digest) != 64
            or not isinstance(row.get("executable"), bool)
        ):
            return False
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        return False
    if canonical_json_sha256({"files": inventory}) != value.get("source_tree_sha256"):
        return False
    prompts = value.get("prompt_files")
    if not isinstance(prompts, dict) or canonical_json_sha256(prompts) != value.get(
        "prompt_bundle_sha256"
    ):
        return False
    proxy = value.get("proxy")
    if not isinstance(proxy, dict):
        return False
    proxy_body = dict(proxy)
    proxy_expected = proxy_body.pop("fingerprint_sha256", None)
    return isinstance(proxy_expected, str) and canonical_json_sha256(proxy_body) == proxy_expected


def load_bound_decision_start_provenance(
    pending_dir: str | Path,
    meta: dict,
    *,
    require_current_match: bool = False,
) -> dict:
    """Load the path-fixed provenance artifact bound by cycle meta.

    When ``require_current_match`` is true (the reconcile path), recapture the current build using
    the original timestamp and require byte-equivalent canonical content. This catches any desk,
    prompt, config, environment, or proxy source change between evidence and PAPER execution.
    """
    artifact = meta.get("decision_start_runtime_provenance_artifact")
    expected_sha256 = meta.get("decision_start_runtime_provenance_sha256")
    if artifact != DECISION_START_PROVENANCE_ARTIFACT or not isinstance(expected_sha256, str):
        raise ValueError("cycle meta lacks fixed decision-start runtime provenance binding")
    path = Path(pending_dir) / DECISION_START_PROVENANCE_ARTIFACT
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            "decision-start runtime provenance artifact is missing or malformed"
        ) from exc
    if not verify_runtime_provenance(value):
        raise ValueError("decision-start runtime provenance artifact failed self-verification")
    if value.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        raise ValueError("decision-start runtime provenance requires the reconstructible schema")
    if canonical_json_sha256(value) != expected_sha256:
        raise ValueError("decision-start runtime provenance artifact/meta hash mismatch")
    bound_captured_at = meta.get("decision_start_runtime_provenance_captured_at")
    try:
        captured_at = datetime.fromisoformat(str(value["captured_at"]))
        bound_at = datetime.fromisoformat(str(bound_captured_at))
        evidence_at = datetime.fromisoformat(str(meta["now"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("decision-start runtime provenance has an invalid timestamp") from exc
    if captured_at.tzinfo is None or bound_at.tzinfo is None or evidence_at.tzinfo is None:
        raise ValueError("decision-start runtime provenance timestamps must be timezone-aware")
    if captured_at.astimezone(UTC) != bound_at.astimezone(UTC):
        raise ValueError("decision-start runtime provenance timestamp does not match its meta bind")
    if captured_at.astimezone(UTC) < evidence_at.astimezone(UTC):
        raise ValueError("decision-start runtime provenance predates the cycle evidence")
    if require_current_match:
        current = default_runtime_provenance(captured_at=captured_at)
        if canonical_json_sha256(current) != expected_sha256:
            raise ValueError("runtime build changed after decision-start provenance was sealed")
    return value


def load_bound_pre_reflection_performance(pending_dir: str | Path, meta: dict) -> dict:
    """Load the immutable performance packet the Reflector actually consumed."""
    artifact = meta.get("pre_reflection_performance_snapshot_artifact")
    expected_sha256 = meta.get("pre_reflection_performance_snapshot_sha256")
    if artifact != PRE_REFLECTION_PERFORMANCE_ARTIFACT or not isinstance(
        expected_sha256, str
    ):
        raise ValueError("cycle meta lacks fixed pre-reflection performance binding")
    pending = Path(pending_dir)
    try:
        value = json.loads((pending / PRE_REFLECTION_PERFORMANCE_ARTIFACT).read_text())
        sidecar = (pending / PRE_REFLECTION_PERFORMANCE_SHA256_ARTIFACT).read_text().strip()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("pre-reflection performance archive is missing or malformed") from exc
    actual = canonical_json_sha256(value)
    if actual != expected_sha256 or sidecar != expected_sha256:
        raise ValueError("pre-reflection performance archive hash mismatch")
    return value
