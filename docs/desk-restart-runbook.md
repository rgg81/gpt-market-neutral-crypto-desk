# Desk Scheduler and Recovery Runbook

The standing loop is a **persistent user-crontab launcher**. At each funding-aligned boundary it
opens one fresh Codex session authenticated by the ChatGPT subscription. The launcher
pins the root to `gpt-5.6-sol` at `xhigh`, enables GPT subagents and web search, confines writes to
this v2 workspace, and never uses a raw API key.

Cadence: **00:07 / 08:07 / 16:07 UTC**. On this host (`America/Sao_Paulo`, UTC-03:00), cron fires at
05:07 / 13:07 / 21:07 local time. The installer refuses DST-changing or fractional-offset
timezones rather than silently drifting. The launcher independently checks the UTC slot, so a
stale host-timezone mapping fails closed.

## 1. Verify the launcher and schedule

```bash
bash scripts/run_scheduled_cycle.sh --check
uv run python scripts/install_desk_cron.py --check
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

## 2. Diagnose a gap

```bash
uv run python scripts/desk_watchdog.py --state-dir live_state
tail -n 200 logs/desk-cycle.log
```

- `ON_TIME` / `EARLY`: nothing was missed. EARLY means do not run a cycle now.
- `LATE` / `MISSED_N`: run one catch-up cycle, never one cycle per missed boundary.
- A launcher timeout or failure leaves the prior completed paper book standing. Diagnose the root
  from the log before retrying; never fabricate or hand-edit completion artifacts.

## 3. Run one manual catch-up

The manual form bypasses only the launcher's UTC-slot check. The desk watchdog remains mandatory
and will stand down if the last completed cycle is too recent:

```bash
bash scripts/run_scheduled_cycle.sh
```

The `flock` at `logs/desk-cycle.lock` prevents overlap between manual and scheduled runs. The hard
runtime limit is 100 minutes.

## 4. What the launcher guarantees

- Root and inherited subagents: **`gpt-5.6-sol`, `xhigh`**, no model downgrade.
- GPT Reflector, three parallel GPT specialists, GPT PM, and GPT Adversary.
- ChatGPT subscription login; `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`, and `CODEX_API_KEY` are
  removed from the launched environment.
- Codex `workspace-write` sandbox scoped to this repository, with network enabled for the public
  Binance evidence feed and native web search enabled for sentiment.
- Fresh Codex session per firing, retained in history for multi-agent support and audit; no
  session-expiry dependency.
- Separate v2 `live_state/` and `live_memory/`; the original sibling desk is out of scope.

## 5. Never do these

- Never replace the Codex launcher with a bare deterministic trading script. The agents decide.
- Never use Claude/Opus or a raw OpenAI API runner for a desk cycle.
- Never set `live=true`, add order placement, share state with the original desk, or backfill
  multiple cycles.
- Never remove the watchdog, UTC-slot check, `flock`, timeout, Adversary, or one-revision limit.
