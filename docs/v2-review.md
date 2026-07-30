# V2 review and upgrade record

Date: 2026-07-28
Reference desk: `crypto-trade-claude-code-market-neutral` at `88d667f`

## Baseline

The reference desk was healthy on its existing contract: 317 tests passed and Ruff was clean.
V2 started as an empty scaffold. Its baseline was copied from the reference desk's tracked source
only; live paper-account state, memory, archives, caches, secrets, and virtual environments were
deliberately not migrated.

## Findings addressed

1. The Adversary payload was schema-checked but not bound to the actual precheck. A stale hash,
   incorrect metric echo, wrong bound set, or mismatched precheck/book could reach reconcile.
2. The documented one-revision flow was not proven: a rejected verdict did not require the
   original book and original precheck artifacts.
3. `live: true` could validate and construct an authenticated Binance client despite the
   paper-only charter.
4. An unused direct Anthropic API runner contradicted the subscription-only operating model.
5. `pending/current.json` could point outside its exact per-cycle directory.
6. Specialist arrays were validated item-by-item but not for complete, duplicate-free universe
   coverage.
7. Evidence collection created two Binance clients and loaded markets twice per cycle.
8. Active prompts and documentation still described top-12/B1-B11 behavior after the
   implementation had moved to a top-40 scan, a 20-name post-gate cap, and B1-B12.

## V2 changes

- Reconcile recomputes final and original prechecks, verifies their canonical SHA-256 values, and
  binds the Adversary's cycle, hash, metric echo, and exact B1-B12 rulings before any paper fill.
- Rejected decisions require the documented original/revision trail. The PM still receives one
  revision and the deterministic code still makes no trading decision.
- Agent contracts reject NaN/infinity, malformed hashes, duplicate/missing bounds, unexplained
  accepted failures, and content-free rejections.
- `Settings.live` is `Literal[False]`; Binance construction is always public/keyless; the direct
  raw-LLM API runner and dependency were removed.
- Pending pointers are confined to `<memory>/pending/<cycle>`.
- Every successful specialist output must cover every evidence symbol exactly once.
- Universe scan and evidence assembly share one Binance client.
- The lockfile is tracked and refreshed for v0.2.0.
- Sentiment now requires dated, opened, URL-backed sources; technical reads are explicitly
  cross-sectional and limited to fields actually present; futures reads use explicit carry signs;
  the PM follows a minimal-change carry construction sequence; the Adversary produces exact,
  actionable one-pass revision demands; the Reflector resolves the current cycle and avoids
  small-sample strategy drift.

## GPT scheduler upgrade

- The active reasoning team is pinned to `gpt-5.6-sol` at `xhigh` effort on the root and every
  inherited subagent, using ChatGPT subscription authentication rather than a raw API key.
- A persistent managed cron launches a fresh Codex session at 00:07 / 08:07 / 16:07 UTC. The
  launcher combines a UTC-slot guard, exclusive `flock`, 100-minute timeout, workspace-write
  sandbox, native web search, and the deterministic desk watchdog.
- The crontab installer is idempotent and preserves unrelated jobs. It accounts for this Debian
  cron daemon's lack of per-user timezone support and refuses unstable timezone mappings.
- A live subscription smoke test proved the root model/effort, network-enabled workspace sandbox,
  and inherited GPT subagent handshake. Sessions remain in Codex history because Codex CLI
  ephemeral sessions currently cannot initialize collaboration threads.
- Reflector changes now remain audited and reversible even in this copied workspace, whose `.git`
  metadata is unavailable: successful edits use an append-only journal and exact-byte rollback;
  a clean Git worktree additionally receives a scoped commit.

## Verification

```text
uv lock --check        passed
uv run pytest          344 passed
uv run ruff check .    passed
compileall             passed
desk_evidence --help   passed
desk_reconcile --help  passed
Codex GPT handshake    ROOT_READY + AGENT_READY
managed cron check     installed and current
```

All tests are offline/deterministic. No live exchange order path was added or exercised.
