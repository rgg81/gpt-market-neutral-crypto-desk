"""Cycle step 1c (deterministic, FAIL-SOFT): apply the Reflector's proposal to agent prompts.

    uv run python scripts/reflector_apply.py --memory-dir live_memory

Reads `<memory>/pending/reflection.json` (written by the Reflector subagent). Each edit is spliced
into `agents/<role>.md` inside its managed region; the guard reverts anything that would touch
protected text. Applied edits are journaled and, when a clean Git worktree is available, committed.
An exact byte snapshot provides rollback even in copied workspaces without Git metadata. Any error
is logged and skipped — learning never blocks a cycle. PAPER ONLY."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import traceback
from pathlib import Path

from futures_fund.reflection import (
    apply_reflection,
    audit_managed_region_provenance,
    scored_cycles,
)

JOURNAL_NAME = "reflector-journal.md"


def _pending_dir(memory_dir: str) -> Path:
    """CURRENT per-cycle pending dir via the pointer; flat root as legacy fallback."""
    from futures_fund.pending_io import resolve_pending
    try:
        pending, _meta = resolve_pending(memory_dir)
        return pending
    except (FileNotFoundError, ValueError, KeyError):
        return Path(memory_dir) / "pending"


def _git_root(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def _git_paths_clean(root: Path, path: Path) -> bool:
    relative = path.resolve().relative_to(root.resolve())
    unstaged = subprocess.run(
        ["git", "-C", str(root), "diff", "--quiet", "--", str(relative)],
        check=False,
    )
    staged = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet", "--", str(relative)],
        check=False,
    )
    return unstaged.returncode == 0 and staged.returncode == 0


def _snapshot_files(agents_dir: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in agents_dir.glob("*.md") if path.is_file()}


def _restore_files(snapshot: dict[Path, bytes]) -> None:
    for path, content in snapshot.items():
        path.write_bytes(content)


def _snapshot_optional(path: Path) -> tuple[bool, bytes]:
    return (path.exists(), path.read_bytes() if path.exists() else b"")


def _restore_optional(path: Path, snapshot: tuple[bool, bytes]) -> None:
    existed, content = snapshot
    if existed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    else:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply a reflection proposal to the agent prompts.")
    ap.add_argument("--memory-dir", default="live_memory")
    ap.add_argument("--agents-dir", default="agents")
    ap.add_argument(
        "--check-existing",
        action="store_true",
        help="verify active managed regions against this desk's local reflector journal",
    )
    args = ap.parse_args(argv)
    journal_path = Path(args.memory_dir) / JOURNAL_NAME
    if args.check_existing:
        issues = audit_managed_region_provenance(args.agents_dir, journal_path)
        if issues:
            print(json.dumps({"managed_region_provenance": "FAILED", "issues": issues}, indent=2))
            return 1
        print("OK: every active reflector region is backed by the local journal")
        return 0

    pending = _pending_dir(args.memory_dir)
    proposal_path = pending / "reflection.json"
    agents_dir = Path(args.agents_dir)
    if not proposal_path.exists():
        print("no reflection.json; nothing to apply")
        return 0
    prompt_snapshot: dict[Path, bytes] = {}
    journal_snapshot = _snapshot_optional(journal_path)
    try:
        proposal = json.loads(proposal_path.read_text())
        rec_path = pending / "recurrences.json"
        recs = json.loads(rec_path.read_text()) if rec_path.exists() else []
        allowed = {r.get("role") for r in recs}
        meta_path = pending / "meta.json"
        current_cycle = int(json.loads(meta_path.read_text())["cycle"]) \
            if meta_path.exists() else None
        # Evidence-integrity guard: an edit citing a PAST cycle with no ScoreRecord is refused
        # (2026-07 review: fabricated per-cycle scores were live in decision prompts). The note's
        # own [cN] date tag and a future retire_if target are legitimately unscored.
        git_root = _git_root(agents_dir)
        git_clean = bool(git_root and _git_paths_clean(git_root, agents_dir))
        prompt_snapshot = _snapshot_files(agents_dir)
        res = apply_reflection(
            proposal,
            agents_dir,
            journal_path,
            allowed_roles=allowed,
            known_cycles=scored_cycles(args.memory_dir),
            current_cycle=current_cycle,
        )
        if res["applied"]:
            roles = ", ".join(res["applied"])
            if git_root and git_clean:
                relative = agents_dir.resolve().relative_to(git_root.resolve())
                msg = (
                    f"chore(reflector): auto-tune prompts [{roles}]\n\n"
                    "Auto-generated by the GPT self-learning loop; managed-region-only, "
                    "guard-verified."
                )
                # --only commits the listed working-tree paths without sweeping unrelated staged
                # changes. A dirty agents/ tree is journal-only instead of being mixed in.
                subprocess.run(
                    [
                        "git", "-C", str(git_root), "commit", "-q", "--only",
                        "-m", msg, "--", str(relative),
                    ],
                    check=True,
                )
                res["audit"] = "journal+git"
            elif git_root:
                res["audit"] = "journal-only (agents tree was dirty before reflection)"
            else:
                res["audit"] = "journal-only (no Git worktree)"
        print(json.dumps(res, indent=2))
    except Exception:  # noqa: BLE001 — fail-soft; do not block the cycle
        _restore_files(prompt_snapshot)
        _restore_optional(journal_path, journal_snapshot)
        print("reflector_apply failed (fail-soft); reverted agent edits", file=sys.stderr)
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
