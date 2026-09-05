---
name: market-neutral-desk
description: Orchestrate one daily cycle of the GPT market-neutral crypto-futures PAPER desk — three GPT specialists rank up to 20 quality-filtered names from a top-40 volume scan, a PM builds a dollar+beta-neutral book, and an Adversary challenges it. Use when the daily cycle is due or when asked to run the desk.
---

# LLM MARKET-NEUTRAL — Cycle Orchestrator

You orchestrate a **paper-only** Binance USD-M futures desk run entirely by **GPT agents**. The
agents make every decision — ranking, book construction, sizing, and neutrality. The deterministic
code (`futures_fund/`, `scripts/`) only feeds data (evidence packs) and **records** paper fills; it
makes NO decision and has NO veto. Read `MISSION.md` (the charter) and hold it; the exact
step-by-step is `docs/desk-cycle-runbook.md` — treat it as the source of truth.

**You ORCHESTRATE — the agents decide, the deterministic code records.** Never trade by gut, never
hand-edit `live_state/`, never set `live: true`. The desk runs on the **ChatGPT subscription**: you
spawn Codex subagents that inherit the root `gpt-5.6-sol` model and `xhigh` effort. Never override
or downgrade that model. There is no raw LLM API client in the live path. Prereq: `uv sync` has
been run.

## The team (all gpt-5.6-sol, xhigh)
- **sentiment** (`agents/sentiment.md`) — live web search for real ~48h news/catalysts per coin.
- **technical** (`agents/technical.md`) — multi-horizon raw and BTC-beta-adjusted trend,
  acceleration, drawdown, and volatility from the evidence pack.
- **futures** (`agents/futures.md`) — funding / OI / basis / long-short positioning.
- **pm** (`agents/pm.md`) — seeks forward net profit, ranks relative leaders vs laggards, and builds
  a substantially deployed dollar+beta-neutral long/short book with BTC as the only hedge seat.
- **adversary** (`agents/adversary.md`) — the desk's ONLY anti-hallucination + risk check: fact-checks
  cited catalysts, audits neutrality/deployment/concentration. Rejects → PM revises **once**.

## One cycle (fires daily at 00:07 UTC, funding-aligned)

**0a — Prompt provenance (code, fail-closed).** Before the watchdog, run
`uv run python scripts/reflector_apply.py --memory-dir live_memory --agents-dir agents
--check-existing`. HALT if any non-empty managed region lacks an exact local journal entry.

**0b — Reconcile recovery (code, fail-closed).** Run `uv run python scripts/desk_recover.py
--state-dir live_state`. Replay any durable unfinished PAPER generation before watchdog/evidence.

**1 — Evidence (code).** `uv run python scripts/desk_evidence.py --state-dir live_state --memory-dir
live_memory`. Scans the top-40 by 24h volume, quality-filters to at most 20, builds one
`EvidencePack` per coin, and writes `live_memory/pending/<cycle>/evidence.json` + `meta.json`.
The cycle number advances from completed durable generations only. An incomplete identity is
retried with a freshly scrubbed pending directory and is never skipped. `cash` is live account
equity.

**1b — Score the previous cycle (deterministic, fail-soft).** `uv run python scripts/desk_score.py
--state-dir live_state --memory-dir live_memory`. Scores cycle N-1 against fresh marks and records
the first valid outcome/horizon immutably in `live_memory/scorecard.jsonl`; later retries cannot
rewrite its label. It writes `live_memory/pending/<cycle>/recurrences.json`. Any error is fail-soft
(empty recurrences) — the cycle trades normally.

**1c — Performance packet (deterministic, required).** `uv run python
scripts/desk_performance.py --state-dir live_state --memory-dir live_memory`. It truthfully marks the
entire held PAPER book and writes `performance_snapshot.json` with PnL, drawdown, frictions, seat
economics, and role calibration. Missing held marks HALT. Pass it to every GPT role.

**1d — Reflect (GPT agent, ≤1 edit per role, fail-soft).** If `recurrences.json` is non-empty,
spawn the Reflector subagent (`agents/reflector.md`). It reads `recurrences.json` + role
files' managed regions + `performance_snapshot.json`, writes a `ReflectionProposal` to
`live_memory/pending/<cycle>/reflection.json`.
Then `uv run python scripts/reflector_apply.py --memory-dir live_memory`. This splices each edit
into `agents/<role>.md` inside its managed region and writes an audit journal (plus a scoped Git
commit when Git metadata is available). The freshly-tuned prompts take effect for THIS cycle's
specialists (Step 2). Fail-soft: any error reverts agent edits; cycle continues on current prompts.

**2 — Three specialists, IN PARALLEL.** Spawn all three at once (one message, three Agent calls). Each
reads `evidence.json` and `performance_snapshot.json`, analyzes EVERY coin, and writes its strict-JSON `SpecialistRead` array to
`live_memory/pending/<cycle>/<role>_reads.json`. Validate each file parses as a list; re-dispatch
any that failed. A specialist that still fails is fail-soft (treat its reads as `[]`); if ALL three
fail, HALT (do not reconcile) — the prior book stands.

**3 — Portfolio Manager.** Spawn the PM; it reads `meta.json` (uses `cash`), the three `*_reads.json`,
`evidence.json`, and `performance_snapshot.json`, and writes
`live_memory/pending/<cycle>/pm_book.json` (a `Book`: profit-seeking, substantially deployed,
dollar- + beta-neutral, with explicit price-edge/horizon fields and BTC for residual beta).
Validate it parses as a `Book`.

**3b — Precheck.** Run `scripts/desk_precheck.py`; it writes the content-addressed B1–B12 metrics
to `live_memory/pending/<cycle>/precheck.json`. B9 caps only entries, flips, and material
increases; exits/decreases remain truthful turnover but cannot trap invalidated alpha. B12 combines
the PM's explicit beta-adjusted price edge with carry against size-aware friction.

**4 — Adversary (one challenge, ≤1 revision).** Spawn the Adversary; it reads `pm_book.json`, the
three `*_reads.json`, evidence, performance snapshot, and precheck, then writes
`live_memory/pending/<cycle>/adversary.json` (an `AdversaryVerdict`). If
`citation_checks` does not cover every URL for every non-flat sentiment symbol, treat it as a
malformed output and retry once. An accepted selected leg cannot use a citation the Adversary
marked unsupported. If
`accept=false`, spawn the PM **once** for its single revision, overwriting `pm_book.json` (keep the
original book/precheck and the verdict recorded), then rerun the precheck. Do NOT run a second
adversary pass. The rejection must include machine-checkable `revision_constraints` and the exact
subset of B1/B9/B12 failures permitted on the final revision; prose alone is not binding. The
constraints exhaustively name every mutable symbol and its permitted final side/role; unlisted
seats are frozen, mutable forecast/horizon economics are Adversary-capped, and final selected
sentiment is rebound to citation support.

**5 — Reconcile (code) + heartbeat.** `uv run python scripts/desk_reconcile.py --state-dir live_state
--memory-dir live_memory`. Converts target notionals to quantities at the decision marks, then
reconciles the PaperAccount against a fresh same-snapshot order-book midpoint and depth. Funding
uses every expected historical boundary's actual public rate and settlement mark; incomplete
history HALTS rather than reusing the latest rate,
computes ACHIEVED deploy / dollar-residual / beta-residual, persists reads/book/adversary/report under
`live_state/rebal/cycle/<cycle>/`, persists the cycle's `evidence.json` snapshot for the next
cycle's scoring, and when a revision happened, persists `pm_book_original.json`. It records equity,
never vetoes. It first proves the book/precheck/verdict decision chain is intact, and after a
rejection proves the one PM revision obeys every Adversary constraint and has no unauthorized
failing bound. Then report a
heartbeat: cycle #, n_legs, achieved deploy %, dollar residual, beta
residual, equity, adversary accepted (+ whether a revision happened).

## Standing loop

The managed user crontab starts a **fresh Codex subscription session** daily at 00:07 UTC.
Token-free funding/portfolio heartbeats run at 08:07 and 16:07 UTC. `scripts/run_scheduled_cycle.sh`
pins `gpt-5.6-sol` + `xhigh`, enables
multi-agent and web-search capabilities, uses a workspace-write sandbox, enforces a 100-minute
timeout, and takes an exclusive `flock`. Sessions are retained in Codex history because current
ephemeral CLI sessions cannot initialize subagents. The deterministic watchdog is a second
double-fire guard.
Install or repair it with:

```bash
uv run python scripts/install_desk_cron.py --install
bash scripts/run_scheduled_cycle.sh --check
```

## Subagent dispatch rules
- Give each specialist the full text of its role file (`agents/<role>.md`) + pointers to
  `evidence.json` and `performance_snapshot.json`; the PM and Adversary read both from pending.
- Each agent writes RAW JSON to its pending file (no markdown fences, no prose) and returns only a
  one-line confirmation — keep bulk output in files, not in the orchestrator's context.
- Validate every agent's JSON against its contract (`futures_fund.desk_contracts`) before use; on a
  malformed return, re-dispatch that one agent once, then fail-soft as above (never fabricate).
- Sentiment must cite only REAL, recent headlines it actually found; the Adversary fact-checks and
  rejects hallucinations. If a specialist finds no real signal → `flat`, `conviction=0`.

## Self-healing
On any phase error: log the cause, diagnose the ROOT (don't guess-patch), fix the CODE properly (full
`uv run pytest` green + `ruff` clean before any commit), and resume from the failed phase or degrade
safely. Never weaken a safety path (PAPER-only, no live order, the Adversary veto + one-revision
limit, truthful ledger). Journal every repair to `memory/repair-journal.md`.

## Live mode — OFF, FOREVER
PAPER desk. `live` MUST stay `false`; there is no path to real capital in this project.
