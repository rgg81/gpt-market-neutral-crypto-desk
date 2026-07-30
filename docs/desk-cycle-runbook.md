# Desk Cycle Runbook — 8h LLM Market-Neutral Desk (PAPER)

This is the exact orchestration for **one** 8h cycle. It is executed by a **Codex root agent** on
the ChatGPT subscription, with every decision subagent inheriting `gpt-5.6-sol` and `xhigh`
reasoning. There is **no raw API-key runner** in the live path. **PAPER ONLY** (`live: false`
forever): deterministic code feeds data (evidence, precheck metrics, watchdog status), validates
provenance, and records paper fills; it never makes or vetoes a decision. The GPT Adversary agent
is the desk's only anti-hallucination / risk check.

Cadence: fires at **00:07 / 08:07 / 16:07 UTC** (7 min past the funding boundary, so the fresh 8h
candle has opened and funding has settled). Each firing produces a **fresh cycle** — `desk_evidence`
picks `cycle = max(existing) + 1`.

Paths: state `live_state/`, memory `live_memory/`, agent role prompts `agents/*.md`. All pending
artifacts live in the PER-CYCLE dir `live_memory/pending/<cycle>/` (pointer:
`live_memory/pending/current.json`) — never write agent outputs anywhere else.

Before Step 0, read `ops/next-cycle-directive.md` when it exists. It is a binding one-shot user
instruction: pass its full text to the PM and Adversary as `binding_user_directive`. Archive it to
`live_memory/directives/applied/cycle-<N>.md` only after a successful reconcile satisfying its
completion condition. EARLY, HALT, or noncompliance leaves it pending.

---

## Step 0a — Managed-region provenance (deterministic, fail-closed)

Before the watchdog or evidence, verify that every non-empty auto-managed prompt region was
actually written by this desk's local reflector journal:

```bash
uv run python scripts/reflector_apply.py --memory-dir live_memory \
  --agents-dir agents --check-existing
```

Any failure HALTS before opening a cycle. This prevents calibration notes copied from a predecessor
desk—with unrelated cycle numbers or scores—from silently steering the live agents.

## Step 0 — Watchdog (deterministic)

```bash
uv run python scripts/desk_watchdog.py --state-dir live_state
```

Note the `schedule_status`. **EARLY (< 6h since the last completed cycle) → STAND DOWN**: do not
open a new cycle; report the stand-down and stop (turnover costs real money; cycles 3-5 once ran
within 2.7h and paid ~$210K of churn). LATE/MISSED_N → proceed with ONE catch-up cycle (never
backfill missed ones) and inject the watchdog JSON into the PM/Adversary dispatch prompts as
`schedule_status`.

## Step 1 — Evidence (deterministic)

```bash
uv run python scripts/desk_evidence.py --state-dir live_state --memory-dir live_memory
```

Writes `live_memory/pending/<cycle>/evidence.json` + `meta.json` and updates
`pending/current.json`. The universe is quality-gated (age ≥ 30d, |24h chg| ≤ 25%, depth ≥ $250K,
ADV floor) with currently-held symbols always unioned in; `universe_drops` in the output shows
what each gate removed. `cash` is the **live account equity**. Note the printed `cycle`, `cash`,
and `pending_dir` — every subsequent step reads/writes THAT directory.

Liquidity evidence is internally timestamp-consistent: `liquidity_mid`, bid/ask depth, spread, and
`slippage_curve_bps` all come from one two-sided L2 snapshot. The curve uses the worse crossing
side at each clip size. It never compares that book with an earlier funding mark and never
multiplies reasoning-delay price movement into slippage.

## Step 1b — Score the previous cycle (deterministic, fail-soft)

```bash
uv run python scripts/desk_score.py --state-dir live_state --memory-dir live_memory
```

Scores cycle N-1 against the fresh marks, appends `live_memory/scorecard.jsonl`, and writes
`pending/<cycle>/recurrences.json`. Any error is fail-soft (empty recurrences).

## Step 1c — Reflect (GPT agent, ≤1 edit per role, fail-soft)

If `recurrences.json` is non-empty, spawn the Reflector subagent (`agents/reflector.md`), inheriting
the root GPT model and effort. It writes a `ReflectionProposal` to
`pending/<cycle>/reflection.json`. Then apply it:

```bash
uv run python scripts/reflector_apply.py --memory-dir live_memory
```

The apply path enforces the managed-region guard AND the evidence-integrity guard: an edit citing
a cycle with no ScoreRecord in `scorecard.jsonl` is refused (the Reflector once fabricated
per-cycle scores into live prompts). Applied changes are journaled and, when Git metadata is
available, committed. Fail-soft: any error restores the exact pre-reflection prompt files.

## Step 2 — Three specialists, IN PARALLEL

Spawn **all three at once** with three parallel `spawn_agent` calls. Do not set a model override:
each agent must inherit the root `gpt-5.6-sol` model and `xhigh` effort. Each reads
`pending/<cycle>/evidence.json` and **writes its own output file into the SAME per-cycle dir**,
returning only a one-line confirmation.

| role | prompt file | writes | web search? |
|------|-------------|--------|-------------|
| sentiment | `agents/sentiment.md` | `pending/<cycle>/sentiment_reads.json` | **yes** — cite real headlines |
| technical | `agents/technical.md` | `pending/<cycle>/technical_reads.json` | no |
| futures   | `agents/futures.md`   | `pending/<cycle>/futures_reads.json`   | no |

Each dispatch prompt = the role file's full text + the explicit per-cycle paths. **WAIT for all
three completion notifications before dispatching the PM** — do not poll files as a readiness
signal (a slow specialist plus a leftover file caused the cycle-5 stale-read race; per-cycle dirs
make stale reads structurally impossible, but the PM must still see all three FRESH files).

After all three return, validate each file parses as a non-empty JSON list whose symbols match
this cycle's universe. Missing/malformed → re-dispatch that one specialist once; still failing →
fail-soft (`[]`, the PM proceeds on the others). ALL THREE failed → HALT (prior book stands).

## Step 3 — Portfolio manager

Spawn one GPT PM subagent (`agents/pm.md`), inheriting the root model and effort. Prompt = role text
+ the per-cycle paths
(`meta.json` for `cash`, the three `*_reads.json`, `evidence.json`), the current held book
(symbol/side/notional at fresh marks), `schedule_status` from Step 0, and the exact pending
`binding_user_directive` when present. It writes the
strict-JSON `Book` to `pending/<cycle>/pm_book.json`. Validate the file parses as a `Book`.

## Step 3b — Precheck (deterministic data feed)

```bash
uv run python scripts/desk_precheck.py --state-dir live_state --memory-dir live_memory
```

Computes `PrecheckMetrics` on the PROPOSED book (gross/deploy/residuals/concentration/hedge/
per-leg-beta-$/turnover + bounds B1-B12) into `pending/<cycle>/precheck.json`. Note the
`bounds_failing` list and the `sha256`. This is DATA for the Adversary — the code does not veto.

## Step 4 — Adversary (one challenge, ≤1 revision)

Spawn one GPT Adversary subagent (`agents/adversary.md`), inheriting the root model and effort.
Prompt = role text + the per-cycle paths for `pm_book.json`, the three `*_reads.json`,
`evidence.json`, **and `precheck.json`** (quote the sha256), plus the exact pending
`binding_user_directive` when present. It writes the strict-JSON
`AdversaryVerdict` to `pending/<cycle>/adversary.json`.

Validate the verdict: `AdversaryVerdict.model_validate` must pass AND `cycle` must equal this
cycle AND `precheck_sha256` must match. `citation_checks` must cover every non-flat sentiment
symbol exactly once, repeat every cited URL exactly, and truthfully mark whether the symbol appears
in the reviewed book. An accepted book may not use a selected sentiment claim the Adversary marked
unsupported. A malformed/mismatched verdict is a FAILED OUTPUT, not a decision → re-dispatch the
Adversary ONCE (this is validation-of-form, not a second adversarial pass). Still invalid → HALT;
the prior book stands.

When a binding one-shot directive exists, its numeric requirements are also output-validation
criteria. An `accept=true` verdict on a plainly noncompliant original proposal is a failed output
and gets the same single re-dispatch. After any PM revision and fresh precheck, validate the FINAL
metrics against the directive before Step 5. A noncompliant final revision HALTs without
reconcile; the prior completed book stands and the directive remains pending.

- `accept=true` → keep `pm_book.json` as final.
- `accept=false` → copy `pm_book.json` → `pending/<cycle>/pm_book_original.json` and
  `precheck.json` → `precheck_original.json`. Spawn the PM **once** for its single revision
  (`agents/pm.md`, "When revising"), giving it `{original, objections, demanded_changes, cash,
  current_book, precheck}`. It overwrites `pm_book.json`. Re-run Step 3b so the final book's
  `precheck.json` is fresh. Keep the adversary verdict as recorded. Do not run a second adversary
  pass; enforce any binding directive against this final deterministic precheck before reconcile.

## Step 5 — Reconcile (deterministic) + heartbeat

```bash
uv run python scripts/desk_reconcile.py --state-dir live_state --memory-dir live_memory
```

Before any fill, reconcile recomputes the final precheck against the ORIGINAL evidence marks and
binds the exact cycle, book, SHA-256, metrics echo, B1–B12 rulings, and (after rejection)
original/revision trail. It then captures a FRESH execution snapshot for every touched symbol: a
complete two-sided L2 book supplies both the fill reference (top-of-book midpoint) and the depth
walked for slippage. This separation is load-bearing: market drift while GPT agents reason is not
slippage. If two-sided depth is unavailable, use a fresh mark with the ADV/half-spread fallback;
never combine one side of a fresh book with an old evidence mark.

The PM's `target_notional` is anchored to its evidence mark before execution:
`target_qty = target_notional / decision_mark`. Reconcile fills that fixed quantity against the
fresh execution book. An exactly preserved `current_book` leg is therefore a true no-op even when
the price moved during reasoning; the movement changes achieved exposure, not quantity behind the
PM's back.

Settles FUNDING on the held book through the execution timestamp, reconciles the PaperAccount,
computes the ACHIEVED metrics, and persists everything
(reads/book/adversary/report/execution/precheck[+originals]) under
`live_state/rebal/cycle/<cycle>/`, plus `live_state/ledger.jsonl` (per-cycle PnL attribution) and
the equity point (REAL wall-clock ts, monotonicity-guarded). `execution.json` records decision mark,
execution mark, decision-to-execution move, best bid/ask, spread/depth, timestamp, price source,
decision-anchored target quantity, current/delta quantity, and planned turnover.
It HALTs (prior book stands) on: all-specialists-failed, an invalid decision chain, or a held
position with no mark. This validates workflow provenance; it records and never makes a trading
decision.

Then report a heartbeat: cycle #, schedule_status, n_legs, achieved deploy %, dollar residual,
beta residual, equity, **turnover_usd / fees_paid_cycle / slippage_paid_cycle /
funding_settled_cycle / decision_age_seconds** (frictions and execution staleness are never
invisible again), adversary accepted (+ revision?).

## Failure handling

Any step error → log the cause, fix the ROOT (never fabricate a report). Evidence fetch fail →
retry once, else HALT (prior book stands). Specialist fail → fail-soft as above. A HALT leaves
the ledger byte-identical — verify before re-running. Never set `live: true`. Never hand-edit
`live_state/`. After a session death, follow `docs/desk-restart-runbook.md`.
