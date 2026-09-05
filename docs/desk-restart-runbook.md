# Desk Scheduler and Recovery Runbook

The standing loop is a **persistent user-crontab launcher**. At 00:07 UTC daily it opens one fresh
Codex session authenticated by the ChatGPT subscription. The launcher
pins the root to `gpt-5.6-sol` at `xhigh`, enables GPT subagents and web search, confines writes to
this v2 workspace, and never uses a raw API key.

Full-GPT cadence: **00:07 UTC daily**. Token-free PAPER funding/portfolio heartbeats run at
**08:07 / 16:07 UTC**. UTC is authoritative; do not encode a fixed local offset. On a
`Europe/Zurich` host those times are 01:07 / 09:07 / 17:07 during CET and 02:07 / 10:07 / 18:07
during CEST. Cron polls every ten minutes; each launcher uses a UTC-aware gate with a six-hour
grace window. Delayed dispatch, daylight-saving transition, clock correction, or temporary
preflight outage is retried without launching the same task twice.

## 1. Verify the launcher and schedule

```bash
bash scripts/run_scheduled_cycle.sh --check
bash scripts/run_scheduled_cycle.sh --probe-only
bash scripts/run_desk_heartbeat.sh --check
uv run python scripts/install_desk_cron.py --check
uv run python scripts/desk_health.py
systemctl is-active cron
```

Expected: ChatGPT login, a valid lockfile, `READY model=gpt-5.6-sol effort=xhigh`, a current managed
cron block, and an active cron daemon. The installer preserves every unrelated crontab line and
owns only the block between:

```text
# BEGIN market-neutral-v2 GPT desk (managed)
# END market-neutral-v2 GPT desk (managed)
```

To install or repair the managed block:

```bash
uv run python scripts/install_desk_cron.py --install
```

`--probe-only` is genuinely read-only: it neither claims a slot nor starts the proxy, refreshes
candles, creates directories, or mutates desk state. `desk_health.py` is also token-free and
read-only; it reports cycle, heartbeat, funding-clock and proxy-monitor age, flat-book duration,
completion/account consistency, hash-chain validity, and duplicate/conflicting audit rows.

## 2. Diagnose a gap

```bash
uv run python scripts/desk_watchdog.py --state-dir live_state
tail -n 200 logs/desk-cycle.log
tail -n 200 logs/desk-heartbeat.log
```

- `ON_TIME` / `EARLY`: nothing was missed. EARLY means do not run a cycle now.
- `LATE` / `MISSED_N`: run one catch-up cycle, never one cycle per missed boundary.
- A launcher timeout or failure leaves the prior completed paper book standing. Diagnose the root
  from the log before retrying; never fabricate or hand-edit completion artifacts.
- Network/runtime preflight failures occur before a slot is claimed and are retried automatically.
  Full-GPT post-claim failures keep their attempt receipt and use the manual recovery path after
  diagnosis. A failed token-free heartbeat uses its durable account+audit transaction, releases
  only that exact slot claim, and is retried automatically within the grace window.

After power loss, run recovery and then health:

```bash
uv run python scripts/desk_recover.py --state-dir live_state
uv run python scripts/desk_health.py --state-dir live_state --log-dir logs --strict
```

Cycle and heartbeat intents use same-directory temporary files, file `fsync`, atomic replace, and
parent-directory `fsync`. Completion binds the account snapshot and runtime provenance. Recovery
finishes that exact durable intent or fails closed; never edit a pending transaction by hand.

## 3. Run one manual catch-up

The manual form bypasses only the launcher's UTC-slot check. The desk watchdog remains mandatory
and will stand down if the last completed cycle is too recent:

```bash
bash scripts/run_scheduled_cycle.sh
```

The shared `flock` at `logs/desk-cycle.lock` prevents overlap between full cycles and token-free
heartbeats. The full-cycle hard runtime limit is 100 minutes.

## 4. What the launcher guarantees

- Root and inherited subagents: **`gpt-5.6-sol`, `xhigh`**, no model downgrade.
- GPT Reflector, three parallel GPT specialists, GPT PM, and GPT Adversary.
- ChatGPT subscription login; `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`, and `CODEX_API_KEY` are
  removed from the launched environment.
- Codex `workspace-write` sandbox scoped to this repository, with network enabled for the public
  Binance evidence feed and native web search enabled for sentiment.
- Fresh Codex session per firing, retained in history for multi-agent support and audit; no
  session-expiry dependency.
- Ten-minute cron polling with a six-hour UTC recovery window. Full cycles retain an atomic
  one-attempt receipt; idempotent token-free heartbeats release a failed claim for bounded retry.
- Separate v2 `live_state/` and `live_memory/`; the original sibling desk is out of scope.
- Immediately after any governed reflection, new generations seal the Git commit and dirty-state
  digest, reconstructible tracked/untracked source inventory, configuration, lock/build inputs,
  prompts, Python runtime, and proxy source/config. Reconcile rejects any later build change. A
  dirty tree remains runnable but is reproducibly identified.

## 5. Local alerts and optional user-systemd deployment

The alert wrapper writes a deduplicated local event ledger and needs no credentials:

```bash
uv run python scripts/desk_health_alert.py --state-dir live_state --log-dir logs
```

It emits once per changed condition, repeats after its reminder interval, and emits on recovery.
An operator may explicitly add `--alert-command '/path/to/program args'`; the program runs without
a shell and receives report JSON on stdin. Nothing external is configured or sent by default.

`ops/systemd-user/` contains optional non-privileged templates. Substitute both `@DESK_ROOT@` and
`@BINANCE_PROXY_ROOT@`, review the units, and install the rendered units under
`~/.config/systemd/user/`. Use them **instead of** the managed cron block. The proxy has its own
service; the cycle launcher switches to an exact read-only readiness probe so it cannot replace a
systemd-owned process. Task timers poll every ten minutes while the UTC gate still admits only
00:07 and 08:07/16:07; health polling is offset by five minutes. For redundant hosts, allow exactly
one writer using external fencing, replicate a crash-consistent snapshot, and require recovery plus
a healthy report before promotion. Never concurrently write `live_state/`.

## 6. Never do these

- Never replace the Codex launcher with a bare deterministic trading script. The agents decide.
- Never use Claude/Opus or a raw OpenAI API runner for a desk cycle.
- Never set `live=true`, add order placement, share state with the original desk, or backfill
  multiple cycles.
- Never remove the watchdog, UTC-slot check, `flock`, timeout, Adversary, or one-revision limit.
