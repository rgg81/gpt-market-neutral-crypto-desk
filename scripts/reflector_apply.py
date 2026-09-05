"""Cycle step 1d (deterministic, FAIL-SOFT): apply the Reflector's proposal to agent prompts.

    uv run python scripts/reflector_apply.py --memory-dir live_memory

Reads the current `<memory>/pending/<cycle>/reflection.json` (written by the Reflector subagent).
Each edit is spliced
into `agents/<role>.md` inside its managed region; the guard reverts anything that would touch
protected text. Applied edits are journaled and, when a clean Git worktree is available, committed.
The versioned latest-head artifact binds each edit to its source cycle, canonical proposal, complete
surfaced-recurrence packet, and exact journal append; an independent state anchor detects rollback
of the prompt/live-memory set. Exact byte snapshots cover all four files even without Git metadata.
Any error is logged and skipped — learning never blocks a cycle. PAPER ONLY."""
from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
import traceback
from pathlib import Path

from futures_fund.reflection import (
    apply_reflection,
    audit_managed_region_provenance,
    bootstrap_reflector_heads,
    mark_recurrences_handled,
    reflection_authority_consumption_path,
    reflector_head_anchor_path,
    reflector_heads_path,
    scored_cycles,
    write_reflection_authority_consumption,
)

JOURNAL_NAME = "reflector-journal.md"


def _pending_dir(memory_dir: str) -> tuple[Path, dict]:
    """Resolve only the fresh per-cycle pointer; legacy flat pending is never executable."""
    from futures_fund.pending_io import resolve_pending
    return resolve_pending(memory_dir)


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


def _snapshot_optional(path: Path) -> tuple[bool, bytes, int | None]:
    if not path.exists():
        return False, b"", None
    return True, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore_optional(path: Path, snapshot: tuple[bool, bytes, int | None]) -> None:
    existed, content, mode = snapshot
    if existed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mode is not None:
            path.chmod(mode)
    else:
        path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply a reflection proposal to the agent prompts.")
    ap.add_argument("--memory-dir", default="live_memory")
    ap.add_argument("--agents-dir", default="agents")
    ap.add_argument("--state-dir", default="live_state")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-existing",
        action="store_true",
        help="verify active managed regions against the exact v1 latest-head artifact",
    )
    mode.add_argument(
        "--bootstrap-heads",
        action="store_true",
        help=(
            "one-time audited migration: bind reviewed active regions to reflector-heads-v1.json"
        ),
    )
    args = ap.parse_args(argv)
    journal_path = Path(args.memory_dir) / JOURNAL_NAME
    heads_path = reflector_heads_path(journal_path)
    anchor_path = reflector_head_anchor_path(args.state_dir)
    if args.bootstrap_heads:
        try:
            result = bootstrap_reflector_heads(
                args.agents_dir, journal_path, heads_path, anchor_path
            )
        except (OSError, ValueError) as exc:
            print(json.dumps({"reflector_heads_bootstrap": "FAILED", "error": str(exc)}, indent=2))
            return 1
        print(json.dumps({
            "reflector_heads_bootstrap": "CREATED" if result["created"] else "ALREADY_CURRENT",
            "path": str(heads_path),
            "anchor_path": str(anchor_path),
            "schema_version": result["artifact"]["schema_version"],
            "generation": result["artifact"]["generation"],
        }, indent=2))
        return 0
    if args.check_existing:
        issues = audit_managed_region_provenance(
            args.agents_dir, journal_path, heads_path, anchor_path
        )
        if issues:
            print(json.dumps({"managed_region_provenance": "FAILED", "issues": issues}, indent=2))
            return 1
        print("OK: every active reflector region matches its exact v1 latest head")
        return 0

    pending, resolved_meta = _pending_dir(args.memory_dir)
    proposal_path = pending / "reflection.json"
    agents_dir = Path(args.agents_dir)
    if not proposal_path.exists():
        print("no reflection.json; nothing to apply")
        return 0
    current_cycle = int(resolved_meta["cycle"])
    consumption_path = reflection_authority_consumption_path(args.state_dir, current_cycle)
    handled_path = Path(args.memory_dir) / "recurrence-handled.json"
    prompt_snapshot: dict[Path, bytes] = {}
    journal_snapshot = _snapshot_optional(journal_path)
    heads_snapshot = _snapshot_optional(heads_path)
    anchor_snapshot = _snapshot_optional(anchor_path)
    consumption_snapshot = _snapshot_optional(consumption_path)
    handled_snapshot = _snapshot_optional(handled_path)
    try:
        proposal = json.loads(proposal_path.read_text())
        rec_path = pending / "recurrences.json"
        recs = json.loads(rec_path.read_text()) if rec_path.exists() else []
        recurrences_seal = (pending / "recurrences.sha256").read_text().strip()
        allowed = {r.get("role") for r in recs}
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
            known_cycles=scored_cycles(args.memory_dir, state_dir=args.state_dir),
            current_cycle=current_cycle,
            surfaced_recurrences=recs,
            sealed_recurrences_sha256=recurrences_seal,
            anchor_path=anchor_path,
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
        write_reflection_authority_consumption(
            args.state_dir,
            args.memory_dir,
            source_cycle=current_cycle,
            recurrences_sha256=recurrences_seal,
            outcome="head_applied" if res["applied"] else "no_head_change",
        )
        # Mark handled only after every requested audit action succeeds. If a Git/audit failure
        # triggers rollback, the event remains retryable rather than being silently cooled down.
        mark_recurrences_handled(args.memory_dir, recs, cycle=current_cycle)
        print(json.dumps(res, indent=2))
    except Exception:  # noqa: BLE001 — fail-soft; do not block the cycle
        _restore_files(prompt_snapshot)
        _restore_optional(journal_path, journal_snapshot)
        _restore_optional(heads_path, heads_snapshot)
        _restore_optional(anchor_path, anchor_snapshot)
        _restore_optional(consumption_path, consumption_snapshot)
        _restore_optional(handled_path, handled_snapshot)
        print("reflector_apply failed (fail-soft); reverted agent edits", file=sys.stderr)
        traceback.print_exc()
    return 0


if __name__ == "__main__":
    sys.exit(main())
