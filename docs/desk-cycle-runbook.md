# Desk Cycle Runbook — Daily LLM Market-Neutral Desk (PAPER)

This is the exact orchestration for **one** 24h decision cycle. It is executed by a **Codex root agent** on
the ChatGPT subscription, with every decision subagent inheriting `gpt-5.6-sol` and `xhigh`
reasoning. There is **no raw API-key runner** in the live path. **PAPER ONLY** (`live: false`
forever): deterministic code feeds data (evidence, precheck metrics, watchdog status), validates
provenance, and records paper fills; it never makes or vetoes a decision. The GPT Adversary agent
is the desk's only anti-hallucination / risk check.

Cadence: the full GPT ensemble fires **once daily at 00:07 UTC**, seven minutes past a funding
boundary. Token-free deterministic heartbeats settle and mark the unchanged PAPER book at
**08:07 and 16:07 UTC**; those heartbeats are not decision cycles and cannot create fills. Each
full firing advances only after the prior durable generation is complete. An interrupted identity
is retried after its pending directory is scrubbed; the allocator never skips an incomplete state
generation.

Paths: state `live_state/`, memory `live_memory/`, agent role prompts `agents/*.md`. All pending
artifacts live in the PER-CYCLE dir `live_memory/pending/<cycle>/` (pointer:
`live_memory/pending/current.json`) — never write agent outputs anywhere else.

Before Step 0, read `ops/next-cycle-directive.md` when it exists. It is a binding one-shot user
instruction: pass its full text to the PM and Adversary as `binding_user_directive`. Archive it to
`live_memory/directives/applied/cycle-<N>.md` only after a successful reconcile satisfying its
completion condition. EARLY, HALT, or noncompliance leaves it pending.
Evidence copies the exact text to `pending/<cycle>/binding_user_directive.md` and binds its
canonical SHA-256 in `meta.json`; reconcile rejects a missing, changed, or invented directive.

## Step 0c — Mandatory candle-proxy freshness (deterministic, fail-closed)

```bash
uv run python scripts/ensure_binance_proxy.py
uv run python scripts/desk_data_preflight.py
```

Run this before the watchdog. Production klines come exclusively from the local caching proxy at
`http://127.0.0.1:8000` (`~/binance-proxy`); there is no direct-Binance candle fallback. The
preflight requires a healthy proxy and verifies that both the BTC 1h and 1d responses contain the
currently-forming UTC candle. Any failure HALTS before a cycle is claimed, allowing the scheduled
poller to retry later without consuming the slot.

The ensure command is the authorized self-healing boundary. Under a host-side lock it retries the
health probe, then may signal only a process whose cmdline matches both
`binance_proxy.app:app` and the exact configured `~/binance-proxy/src` app directory. It starts the
project's own `.venv/bin/uvicorn`, waits for `/healthz`, and records its managed log/PID under this
desk's `logs/`. It never kills an unrelated listener on port 8000. Failure to establish health
HALTS before the schedule claim.

Step 1 repeats the freshness proof for **every** selected or held symbol. `meta.json.candle_data`
records the exact proxy source, check times, latest candle boundaries, and complete request set.
Missing/stale 1h momentum or 1d beta data is fatal; never convert it to zero momentum, zero
volatility, or fallback beta.

It also fetches token-free scoring-only marks for every symbol dispatched in every still-unscored
completed cycle and every still-pending explicit PM forecast. Those names are stored in
`scoring_marks.json`, not
the agent evidence packet, so universe attrition cannot create survivorship-biased learning and
does not expand specialist prompts. `meta.json` hashes evidence, risk, scoring marks, and the exact
expected proxy request set; performance, precheck, and reconcile bind that same meta packet.

Evidence derives raw and BTC-beta-adjusted 6h/24h/72h/168h momentum, 24h acceleration, and 72h
drawdown from completed proxy candles. The mandatory current-forming tail is retained as a
separate timestamped live mark/intrabar observation and freshness proof; it never enters beta,
covariance, volatility, or declared-horizon momentum. The legacy approximately-199h endpoint remains for continuity
but cannot be the agents' entire price thesis. These are measurements, not a deterministic rank or
trade decision.

Funding evidence labels Binance's latest settled funding as backward-looking and derives
`conservative_funding_8h_bps` only from sufficiently complete, recent 24h/72h/168h history with
dispersion and sign-persistence diagnostics. Contract OI is separate from USD-value OI and carries
timestamped 24h/72h/168h changes; the long/short ratio carries own-history changes, z-score, and
percentile. Stale/incomplete ancillary history is neutral evidence, never a fabricated zero-edge
confirmation.

---

## Step 0b — Recover a durable reconcile generation (deterministic, fail-closed)

```bash
uv run python scripts/desk_recover.py --state-dir live_state
```

Run this after managed-region provenance and before the watchdog. It replays any complete durable
PAPER reconcile intent idempotently and publishes that cycle's `complete.json` last. A recovery
failure HALTS. Heartbeats run the same recovery before touching the account.
The completion marker contains hashes for every reconcile artifact plus the exact ledger and
equity rows, the committed account snapshot, and runtime provenance. New generations additionally
commit that exact artifact membership and those hashes into a recomputable generation root carried
by the append-only account-event hash chain; changing an artifact and merely rewriting its
manifest, deleting a member, or swapping a member across cycles therefore invalidates the chain.
Runtime provenance binds the
Git commit/dirty digest, tracked and untracked source tree, config, dependency/build inputs, cycle
and role prompts, Python runtime, and proxy source/config without requiring a clean tree. A content
mismatch makes the generation incomplete for provenance-sensitive reads. Publication uses a
same-directory temporary file, file `fsync`, atomic replace, then parent-directory `fsync`; the
completion marker is always last. Recovery verifies the durable intent and either completes that
exact generation idempotently or fails closed.
`scripts/desk_backfill_manifests.py` is the audited, idempotent one-time migration for protocol
cycles created before manifests existed.

Historical manifest-bound daily score rows created before score schema v2 require one audited
operator migration before this code can run a cycle:

```bash
uv run python scripts/desk_scorecard_migrate.py \
  --state-dir live_state --memory-dir live_memory
```

Run it outside a desk cycle. It acquires the same `logs/desk-cycle.lock` used by cycles and
heartbeats, archives the exact source scorecard and per-cycle attributions in a content-addressed
generation, independently rebuilds every manifest-bound row from the canonical earliest complete
observation, publishes attributions before the aggregate scorecard, and closes with a durable
protocol receipt. It is idempotent and recovers its own WAL after interruption. While that WAL is
present, production score and performance readers fail closed. It never edits account, ledger,
equity, completion manifests, books, or fills. Do not hand-edit or delete its WAL/archive/protocol;
rerun the same command after diagnosing an interruption.

---

## Step 0a — Managed-region provenance (deterministic, fail-closed)

Before the watchdog or evidence, verify that every auto-managed prompt region (including a blank
retirement) exactly matches its role's current `reflector-heads-v1.json` head. The head artifact
binds the complete local journal; each new head also binds its source cycle, canonical Reflector
proposal, and complete surfaced-recurrence packet. A monotonic
`live_state/reflector-head-anchor-v1.json` independently binds the latest generation so restoring
only prompts plus live-memory files to an older self-consistent snapshot still fails:

```bash
uv run python scripts/reflector_apply.py --memory-dir live_memory \
  --agents-dir agents --check-existing
```

Any failure HALTS before opening a cycle. This prevents calibration notes copied from a predecessor
desk—or a stale historically journaled prompt rollback—from silently steering the live agents.
For a pre-v1 desk only, an operator may establish the trust anchor once, outside a cycle and only
after reviewing the current prompts and journal:

```bash
uv run python scripts/reflector_apply.py --memory-dir live_memory \
  --agents-dir agents --bootstrap-heads
```

This migration creates missing generation-zero head/anchor files idempotently but never overwrites
a mismatched or tampered existing head, and refuses to rebootstrap a deleted head while its state
anchor remains.
Non-empty regions must already be journal-backed; blank heads are recorded explicitly and are not
inferred from ambiguous legacy correction rows with an empty `region:` field.

## Step 0 — Watchdog (deterministic)

```bash
uv run python scripts/desk_watchdog.py --state-dir live_state
```

Note the `schedule_status`. **EARLY (< 18h since the last completed cycle) → STAND DOWN**: do not
open a new cycle; report the stand-down and stop (turnover costs real money; cycles 3-5 once ran
within 2.7h and paid ~$210K of churn). LATE/MISSED_N → proceed with ONE catch-up cycle (never
backfill missed ones) and inject the watchdog JSON into the PM/Adversary dispatch prompts as
`schedule_status`. Evidence independently reproduces this classification at its exact `meta.now`,
requires the allocator to equal `last_completed_cycle + 1`, and binds the receipt/hash into
`meta.json`; EARLY, a future clock, or an unprovable prior timestamp therefore cannot be bypassed
by prompt behavior.

## Step 1 — Evidence (deterministic)

```bash
uv run python scripts/desk_evidence.py --state-dir live_state --memory-dir live_memory
```

Writes `live_memory/pending/<cycle>/evidence.json`, `risk_model.json`, and `meta.json` and updates
`pending/current.json`. The universe is quality-gated (age ≥ 60d, |24h chg| ≤ 25%, depth ≥ $250K,
ADV floor) with currently-held symbols always unioned in; `universe_drops` in the output shows
what each gate removed. `cash` is the **live account equity**. Note the printed `cycle`, `cash`,
and `pending_dir` — every subsequent step reads/writes THAT directory.
`meta.json` includes the deterministic `watchdog_receipt` and its SHA-256; reconcile reproduces it
from the completed state history and refuses a changed receipt, non-next cycle, or forbidden
cadence status before any PAPER mutation.

Every 1h/1d OHLCV range is requested from `~/binance-proxy` with explicit `startTime`: immutable
closed candles use its disk cache and only the currently-forming tail is refreshed. Evidence HALTS
unless every required `(symbol, timeframe)` appears in the proxy audit with the current UTC candle.
The candle fields are required; unlike ancillary funding/OI reads, they are never fail-soft.

`risk_model.json` uses completed mandatory hourly candles to publish beta-residual covariance,
correlation, pairwise sample counts, and annualized residual volatility. It is descriptive data
for PM/Adversary judgment, never deterministic sizing or veto authority. Its content hash is bound
through `meta.json`, the performance packet, precheck, reconcile, and durable cycle artifacts.

Liquidity evidence is internally timestamp-consistent: `liquidity_mid`, bid/ask depth, spread,
`slippage_curve_buy_bps`, `slippage_curve_sell_bps`, and legacy/display `slippage_curve_bps` all
come from one two-sided L2 snapshot. BUY uses asks and SELL uses bids; each directional point exists
only when that crossing side visibly covers the full clip. The aggregate curve remains the worse
side on their intersection. Precheck prices each immediate and future execution direction
separately, so a one-way reduction/drop is not blocked by irrelevant opposite-side depth, while a
round trip still fails closed if either required side is missing. Liquidity never compares that
book with an earlier funding mark or multiplies reasoning-delay price movement into slippage.

## Step 1b — Score eligible prior cycles (deterministic, fail-soft)

```bash
uv run python scripts/desk_score.py --state-dir live_state --memory-dir live_memory
```

Scans every unscored completed origin and targets the scheduled 24h outcome using a five-minute
slot tolerance against hash-bound, completed `scoring_marks.json` packets containing its full
universe. Pending cycle-N marks are
never labels: an aborted attempt therefore cannot train the desk, and a fail-soft miss is caught
up rather than lost to survivorship bias. It immutably records the first valid outcome/horizon in
`live_memory/scorecard.jsonl`; a retry cannot relabel the decision at a later horizon. A late
outage/catch-up outcome remains immutable audit evidence but is excluded from role/PM calibration.
It writes immutable per-leg alpha forecast outcomes to `live_memory/forecast-scorecard.jsonl` at
the scheduled declared horizon (`24`, `72`, or `168` hours only) with the same tolerance while retaining actual elapsed time and
mark provenance. Legacy/off-horizon rows remain auditable but cannot train the desk. Unchanged
overlapping seat renewals do not inflate the effective sample; explicit material thesis changes
are identified separately, but every overlapping outcome remains audit-only and cannot enter the
headline calibration/risk-capacity sample. It
writes `pending/<cycle>/recurrences.json`; handled recurrence types have a three-scored-cycle
cooldown, and inactivity/recovery events are emitted only for roles with an active managed note.
Any error is fail-soft (empty recurrences).

## Step 1c — Performance packet (deterministic, required)

```bash
uv run python scripts/desk_performance.py --state-dir live_state --memory-dir live_memory
```

Writes `pending/<cycle>/performance_snapshot.json` from the current evidence marks, PAPER account,
deduplicated ledger, and deduplicated scorecard. The packet exposes net PnL, drawdown, frictions,
cycle windows plus fixed-UTC 7d/28d/calendar-week returns and daily Sharpe/Sortino with observation
warnings, current seat PnL/carry, raw and persistent beta-adjusted trend status, loss fraction,
maximum-horizon carry, carry-recovery intervals, and each role's hit/edge/abstention history. Missing marks
for any held position HALT: an incomplete packet could hide a loss. Pass the exact packet path to
the Reflector, all three specialists, PM, and Adversary. Every agent must read it before acting.
Every held alpha position includes `committed_thesis`: the exact edge, horizon, calibration basis,
and invalidation from the newest prior completed manifest-bound BookLeg, plus its committed cycle
and manifest Book SHA. Resolution requires an exact account cycle/hash and symbol/side/role match.
A damaged, unbound, stale, or unmatched newest generation is explicitly unavailable; the resolver
never falls back to an older thesis. The agents must then use fresh-entry-quality current evidence
rather than silently reconstructing continuation provenance.
It reports selected-side profitability separately from directional forecast accuracy, effective
non-overlapping sample counts, off-horizon exclusions, alpha gross separately from hedge gross,
alpha beta before the hedge, hedge efficiency, same-side positive-correlation clusters,
signed-position co-risk clusters, and descriptive
drawdown/rolling-edge risk-capacity context. These are measurements for GPT judgment, not code
permission or a deterministic trade throttle.

## Step 1d — Reflect (GPT agent, ≤1 edit per role, fail-soft)

If `recurrences.json` is non-empty, spawn the Reflector subagent (`agents/reflector.md`), inheriting
the root GPT model and effort. It writes a `ReflectionProposal` to
`pending/<cycle>/reflection.json`. Then apply it:

```bash
uv run python scripts/reflector_apply.py --memory-dir live_memory
```

The apply path enforces the managed-region guard AND the evidence-integrity guard: an edit citing
a cycle with no ScoreRecord in `scorecard.jsonl` is refused (the Reflector once fabricated
per-cycle scores into live prompts). Applied changes are journaled and, when Git metadata is
available, committed. The prompt, journal, v1 head artifact, and state anchor update as one rollback
unit; fail-soft errors restore their exact pre-reflection bytes. A source cycle is positive,
monotonic, and single-use. The journal authorization retains the schema-validated canonical
proposal and complete `desk_score`-sealed recurrence payload after pending files are pruned.
`desk_score` also exclusive-creates a mode-`0400`
`live_state/reflector-authority-v1/cycle-<N>.json` receipt against the pre-reflection head and
journal; replacing both pending recurrence files cannot manufacture authorization. The receipt
retains the canonical recurrence packet: an incomplete-cycle retry restores it when unconsumed,
or publishes empty recurrences when a head event/consumption receipt proves cycle N already used
that authority. Never delete an authority receipt to unbrick a retry.

Immediately after this fail-soft step, rerun the fail-closed Step 0a `--check-existing` command.
HALT before specialists if it fails. This closes the mutation window between the initial preflight
and agent dispatch; a rejected/malformed proposal that rolled back cleanly will still pass.

## Step 1e — Seal the post-reflection decision build (deterministic, fail-closed)

Immediately after the repeated managed-region check, and before spawning any specialist, run:

```bash
uv run python scripts/desk_decision_start.py --state-dir live_state \
  --memory-dir live_memory --agents-dir agents
```

This is the authoritative decision-start provenance boundary. It captures the complete source,
prompt, configuration, dependency, runtime, and local proxy inventory only after any legitimate
Reflector edit has been applied and verified. It binds the fixed artifact path, hash, and actual
seal timestamp into `meta.json`, preserves the exact packet the Reflector read as the fixed,
hash-bound `performance_snapshot_pre_reflection.json` audit artifact, and deterministically
rebuilds `performance_snapshot.json` against the sealed meta packet for every downstream role.
Both packets are retained in the completed cycle manifest. The potentially large inventory remains in
`runtime_provenance.json`; prompts receive only its path/hash through meta.

Before publishing any seal artifact, the command durably writes a hash-bound
`decision-start-transaction.json` containing the exact base/sealed meta, pre-reflection packet,
post-reflection provenance, and rebuilt performance packet. An interrupted invocation replays only
missing or canonically identical outputs from that intent; any conflicting artifact HALTS. The
intent is removed only after both performance packets, both sidecars, meta, provenance, the current source
build, and a fresh deterministic performance rebuild all verify. Do not delete or edit an
unfinished intent; rerun this same command while still before specialist dispatch.

The command refuses to run if any specialist read, read digest, PM Book, precheck, Adversary
verdict, or revision receipt already exists. Reconcile re-captures the current build at the sealed
timestamp and requires exact canonical equality, so any post-seal code, prompt, config, dependency,
environment, or proxy-source mutation HALTS with the prior book standing. Do not run the Reflector
again after this boundary.

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

Each dispatch prompt = the role file's full text + the explicit per-cycle evidence and
`performance_snapshot.json` paths. **WAIT for all three completion notifications before
dispatching the PM** — do not poll files as a readiness
signal (a slow specialist plus a leftover file caused the cycle-5 stale-read race; per-cycle dirs
make stale reads structurally impossible, but the PM must still see all three FRESH files).

After all three return, validate each file parses as a non-empty JSON list whose symbols match
this cycle's universe. Missing/malformed → re-dispatch that one specialist once; still failing →
fail-soft (`[]`, the PM proceeds on the others). ALL THREE failed → HALT (prior book stands).

Only after all three final outputs have settled—including any one retry and documented fail-soft
replacement—bind the complete normalized specialist packet:

```bash
uv run python scripts/desk_reads_digest.py --memory-dir live_memory
```

This validates and canonicalizes all three role files (a failed role becomes literal `[]`), then
exclusive-creates the current cycle's `specialist_reads.sha256` with read-only mode `0400`. An
existing sidecar, any attempted second digest, or any other failure HALTS before the PM. The digest covers role
identity and order plus every symbol and field, including all rationales, evidence, flat reads, and
unselected alternatives—not only lean/conviction or selected seats. Pass the
sidecar path and its exact 64-hex `specialist_reads_sha256` value to the PM, the Adversary, and the
sole PM revision if one is required. Both the original and revised Book must echo that exact value;
the Adversary verdict echoes the same value. Never regenerate or mutate specialist output after
this point.

## Step 3 — Portfolio manager

Spawn one GPT PM subagent (`agents/pm.md`), inheriting the root model and effort. Prompt = role text
+ the per-cycle paths
(`meta.json` for `cash`, the three `*_reads.json`, `evidence.json`,
`risk_model.json`, `performance_snapshot.json`, `specialist_reads.sha256` and its exact value), the current held book
(symbol/side/seat_role/notional at fresh marks), `schedule_status` from Step 0, and the exact pending
`binding_user_directive` when present. It writes the
strict-JSON `Book` to `pending/<cycle>/pm_book.json`. Validate the file parses as a `Book`.
The Book's `specialist_reads_sha256` must exactly echo the immutable sidecar; missing/mismatched
binding is invalid output.
Its `candidate_reviews` must cover every non-flat technical symbol/side and every selected alpha
leg, exactly echo all same-side non-flat specialist role/lean/conviction values, and bind each
selected row's side/notional/edge/horizon to its BookLeg. `exclusion_reason="entry_gate"` is the
PM's causal claim for governed shadow learning; deterministic validation never creates a trade.
For the new production book, every horizon must be exactly `24`, `72`, or `168`, and every alpha
leg must have non-empty calibration-basis and invalidation fields. Historical models remain
parseable; this is new-output validation and a malformed PM output gets only the documented retry.
Preserve every persisted `seat_role` in `current_book` by default; only an untyped legacy position
defaults to alpha. A deliberate same-side relabel starts a new semantic lifecycle at one mark with
no fee/slippage for the retained-quantity transfer. If it also reduces size, charge and attribute
the reduced slice to the old role first, then rebase only the survivor into the new role. A
same-side `hedge→alpha` relabel is a fresh semantic alpha entry for B9 and the action gate even when
executable turnover is zero and `is_new=false`; the old hedge lifecycle cannot qualify it. An
`alpha→hedge` relabel needs the typed hedge/counterfactual audit and cannot inherit alpha thesis
economics. A role-changing flip remains aggressive, and a role-changing increase remains
aggressive. Every drop, flip, or role change ends the incumbent's old lifecycle and requires its
own prior-inventory ExitAudit; a hold or role-preserving reduction continues via SeatAudit. Audit the newly opened
side/role separately through the applicable Seat/Action/Hedge audit.

## Step 3b — Precheck (deterministic data feed)

```bash
uv run python scripts/desk_precheck.py --state-dir live_state --memory-dir live_memory
```

Computes `PrecheckMetrics` on the PROPOSED book (gross/deploy/residuals/concentration/hedge/
per-leg-beta-$/executable turnover, alpha-versus-hedge gross and beta, full-book forecast/carry
economics, beta-residual portfolio volatility/concentration/same-side clusters,
signed-position co-risk clusters (including opposite-side/negative-correlation amplification),
and bounds B1-B12) into `pending/<cycle>/precheck.json`. Risk fields are data only and do not add a
deterministic B13. B8 records
the count, `is_new`, and hold-breaking claims truthfully; every resize above one cent counts. B9
caps only aggressive changes (new entries, flips, and same-side increases); drops and decreases
stay fully costed but cannot trap invalidated risk. B10 keeps every priced selected seat and
loss-control exit at or below 75bp and applies the tighter complete 50bp
`est_slippage_bps_2k` screen to every aggressive alpha new/flip/increase, including a hedge→alpha
semantic entry. `hard_ban_violations` also records the objective post-crash/fade new-short and
low-displayed-depth oversize facts from the immutable evidence packet. B12 uses the BUY/ask or SELL/bid curve for each
actual execution direction. Because PM/current-book notionals are denominated at the evidence
`mark` while the depth curves are USD tiers at `liquidity_mid`, B12 first freezes the quantity and
values every immediate and future-exit clip as `decision_notional / mark * liquidity_mid`. Both
curve selection and fee/slippage dollars use that converted clip. Current directional evidence
without a positive same-snapshot midpoint fails closed; only immutable aggregate-only legacy
evidence retains its original dollar interpretation. A flip prices its immediate close+entry as
one combined convex signed-delta clip, then adds the opposite-side eventual new-side exit. B12 accrues the PM's
explicit selected-side price edge only through its stated horizon, caps that one-time contribution
after maturity, and lets only conservative carry continue. Complete per-change rows persist
friction and economics for entries, flips, resizes, drops, and insurance-exempt hedge changes.
It also publishes carry-only payback and the forecast edge required to meet B12; those expose
forecast gameability and are never forecasts themselves. The Adversary audits the PM's
horizon-matched calibration basis against the price and performance evidence. A BTC hedge has zero
price alpha and is payback-exempt only when it reduces absolute beta-$ versus both the non-BTC book
and carrying the held BTC exposure into that proposed alpha book; its friction is still audited.
Note the `bounds_failing` list and the `sha256`. This is DATA for the Adversary — the code does not
veto.
It also snapshots the exact current PM auto-managed region into `entry_gate_policy.json` after any
Reflector edit. The hash-bound text is a semantic policy handoff, not a deterministic trade rule.

## Step 4 — Adversary (one challenge, ≤1 revision)

Spawn one GPT Adversary subagent (`agents/adversary.md`), inheriting the root model and effort.
Prompt = role text + the per-cycle paths for `pm_book.json`, the three `*_reads.json`,
`evidence.json`, `risk_model.json`, `performance_snapshot.json`, `specialist_reads.sha256`,
`entry_gate_policy.json`, **and
`precheck.json`** (quote both hashes), plus the exact pending
`binding_user_directive` when present. It writes the strict-JSON
`AdversaryVerdict` to `pending/<cycle>/adversary.json`.
The verdict must echo `entry_gate_policy_sha256` and read/copy the exact 64-hex value from the
deterministic `performance_snapshot.sha256` sidecar into `performance_snapshot_sha256`; it must not
recompute a hash from JSON formatting. It must likewise copy the exact 64-hex value from
`specialist_reads.sha256` into `specialist_reads_sha256`. Reconcile independently re-hashes the
active PM managed region and canonical-rebuilds and re-hashes the entire performance packet and
complete normalized three-role read bundle. It HALTs if any input changed or was derived
incorrectly; the read digest binds every rationale/evidence item and unselected alternative, not
only lean/conviction or selected seats. Both the PM Book and Adversary verdict must echo the same
exact immutable digest. It also echoes `binding_user_directive_sha256`
when a pending directive exists (null otherwise).

Validate the verdict: `AdversaryVerdict.model_validate` must pass AND `cycle` must equal this
cycle AND `precheck_sha256` must match. For current schema, `hard_ban_violations_confirmed` must
exactly echo the precheck list; any row makes acceptance invalid and the final revision must clear
all rows. These facts cannot be overridden by prose, a directive, or allowed failing bounds.
`citation_checks` must cover every non-flat sentiment
symbol exactly once, repeat every cited URL exactly, and truthfully mark whether the symbol appears
in the reviewed book. An accepted book may not use a selected sentiment claim the Adversary marked
unsupported. A malformed/mismatched verdict is a FAILED OUTPUT, not a decision → re-dispatch the
Adversary ONCE (this is validation-of-form, not a second adversarial pass). Still invalid → HALT;
the prior book stands.

B7, B8, B10, and B11 are never overridable on either the reviewed original or final revision—not by
prose, `override_rationale`, a user directive, or `revision_allowed_failing_bounds`. A B9 exception
requires an exact directive hash and one `directive_exception_audits` row naming every and only
final aggressive symbol. An aggressive B12 exception likewise names every and only aggressive
offender; without directive authority, B12 exceptions are limited to priced loss-control
reductions/drops. No form of authority may override an unpriced B12 change. Stale, broader, unused,
or symbol-mismatched directive authority is invalid output.

`seat_audits` exactly cover selected alpha seats. Every accepted incumbent must affirm current
continuation, cash/best-replacement comparison, horizon-matched forecast calibration, objective
invalidation, and residual-risk review, and exactly echo every current chosen-side/opposing
specialist read. The PM and Adversary first compare current evidence with the immutable
`committed_thesis.invalidation_condition`; a rewrite cannot reset a triggered condition, and an
unavailable record requires fresh-entry-quality requalification or removal. Seat audits also echo
the performance packet's position age and expiry flag. Every incumbent `SeatAudit` exactly echoes
`prior_thesis_available`, `prior_thesis_cycle`, and `prior_thesis_book_sha256`, and records
`prior_thesis_provenance_reviewed`, `prior_invalidation_reviewed`, and
`prior_invalidation_triggered`. An available thesis requires both reviews; a triggered condition
requires fresh-entry requalification to retain. During cold migration an unavailable thesis uses
false/null/null, provenance-reviewed true, both invalidation flags false, and likewise requires
fresh-entry-quality requalification to retain—never an older fallback. New/flipped seats use
false/null/null and false review flags. Any accepted
expired retained seat (hold/increase/reduction) must additionally be freshly
requalified and must exactly echo every chosen-side and opposing non-flat specialist read in its
requalification support/opposition lists. `action_audits` exactly cover new/flipped/increased
alpha slices and likewise echo every chosen-side and opposing non-flat role, lean, and conviction
from the persisted reads, and affirm forecast-calibration and incremental-risk review. Empty echo
lists are truthful when all persisted reads are flat; the current hash-bound managed policy and
the GPT Adversary—not a permanent code vote count—decide whether direct-price/specialist evidence
passes. The binder proves those echoes
are complete and truthful; the Adversary applies the PM's currently active managed entry gate, so
deterministic code does not originate a trade or preserve a calibration rule after it retires.
On a rejection, a claimed expired-seat requalification remains subject to the exact-echo rule;
otherwise it must be false with empty echoes and the residual must be removed. The sole revision
cannot introduce any new/flip/increase alpha action that was absent from the reviewed original.
It also cannot enlarge a retained aggressive slice beyond the original reviewed incremental
notional; revision constraints do not substitute for a second opportunity/friction audit.
On a rejection, the sole revision cannot retain a seat whose seat audit failed or preserve an
aggressive slice whose action gate failed while merely fixing another constraint.

`exit_audits` cover the exact union of every incumbent whose original
`change_costs.action` is `"drop"`, `"flip"`, or `"role_change"` and every held incumbent whose
revision constraint prospectively ends its lifecycle. A held `drop_symbol` uses `action="drop"`.
A typed `permit_symbol_mutation`, `max_symbol_notional`, or `min_symbol_notional` constraint enters
the union when its `final_seat_role` or `final_side` differs from held inventory. Use the held prior
side/role; classify `action="role_change"` when the final role differs (role change takes precedence
even if side also differs), otherwise classify `action="flip"` when only the final side differs.
A typed constraint preserving both role and side is not an exit. Include typed hedges; exclude
new-from-flat entries. ExitAudit identity is the exact (`symbol`, `action`) pair. Preserve every
original precheck lifecycle-ending action and independently add each different prospectively
mandated action. If an original proposal flips or role-changes a symbol and the rejection mandates
`drop_symbol`, require two rows for that same symbol: the original `"flip"`/`"role_change"` row and
the prospective `"drop"` row. This enables a rejected replacement lifecycle to receive a reviewed
loss-control drop without losing the audit of the original disposition. Duplicate (`symbol`,
`action`) pairs are forbidden; distinct required actions for one symbol are mandatory. A hold
or role-preserving reduction does not end the lifecycle and continues via `SeatAudit`. Every authorized disposition
requires `friction_reviewed`, `current_evidence_reviewed`,
`loss_control_or_opportunity_reviewed`, and `beta_dollar_impact_reviewed` all true. Ended alpha
lifecycles carry the same exact prior-thesis identity/review fields; ended hedges set all
alpha-thesis claims false/null. Every ExitAudit exactly echoes the current bound prior-side
`supporting_specialists` and `opposing_specialists` arrays and reviews truthful friction—including
zero transfer friction for a pure role change—current evidence, opportunity/loss control, and
dollar/beta impact. The separate Seat/Action/Hedge audit covers the new lifecycle. A direct flip,
zero-turnover relabel, risk reduction, or prospective one-revision exit never permits silently
omitting an old-lifecycle audit.

When a binding one-shot directive exists, its numeric requirements are also output-validation
criteria. An `accept=true` verdict on a plainly noncompliant original proposal is a failed output
and gets the same single re-dispatch. After any PM revision and fresh precheck, validate the FINAL
metrics against the directive before Step 5. A noncompliant final revision HALTs without
reconcile; the prior completed book stands and the directive remains pending.

- `accept=true` → keep `pm_book.json` as final.
- `accept=false` → copy `pm_book.json` → `pending/<cycle>/pm_book_original.json` and
  `precheck.json` → `precheck_original.json`. Before dispatching the sole PM revision, create its
  immutable input receipt exactly once:

  ```bash
  uv run python scripts/desk_revision_receipt.py prepare --memory-dir live_memory
  ```

  This exclusively creates `revision_dispatch_receipt.json`, binding cycle, original Book,
  original precheck, recorded Adversary verdict, constraints, and allowed final failures. If the
  command fails or a receipt already exists, HALT; never edit/delete it or dispatch another PM.
  Spawn the PM **once** for its single revision. Its prompt is the complete current
  `agents/pm.md` followed by the revision-only `agents/pm-revision.md`, giving it
  `{original, objections, demanded_changes,
  revision_constraints, revision_allowed_failing_bounds, cash, current_book, precheck,
  performance_snapshot, specialist_reads_sha256, schedule_status, binding_user_directive}` plus
  the exact current paths for `specialist_reads.sha256`,
  `sentiment_reads.json`, `technical_reads.json`, `futures_reads.json`, `evidence.json`, and
  `risk_model.json`. Explicitly require the fresh PM subagent to read all five files before output
  so it can recalculate beta neutrality, regime evidence, opportunity cost, and size-aware
  friction inside the Adversary's mutation envelope. Every rejection must contain at least one structured constraint;
  the revised Book must preserve the exact original `specialist_reads_sha256` echo.
  prose-only demands are invalid. It overwrites `pm_book.json`. Immediately after that one output,
  seal the immutable output receipt before running another precheck:

  ```bash
  uv run python scripts/desk_revision_receipt.py seal --memory-dir live_memory
  ```

  This validates strict Book JSON and exclusively creates `revision_output_receipt.json` bound to
  the prepared dispatch and exact final Book hash. Missing/changed/already-sealed receipts,
  malformed revision output, or any attempt to prepare/seal again HALTS. There is no second PM
  revision and no malformed-output retry after `prepare`. Re-run Step 3b so the final book's
  `precheck.json` is fresh. Keep the adversary verdict as recorded. Do not run a second adversary
  pass; enforce any binding directive against this final deterministic precheck before reconcile.
  Reconcile proves the revision obeys every Adversary constraint and that every final failing bound
  was explicitly allowed by the Adversary. Symbol constraints are an exhaustive typed mutation
  envelope: unlisted seats are frozen; every permitted final seat is side/role-bound to an exact
  Adversary-authored price forecast plus an exact `{24,72,168}` horizon. Every v2 typed alpha
  constraint also binds the exact calibration-basis and invalidation strings that may survive the
  unreviewed revision; and at least one constraint must correct the
  rejected original. Restoring a held side the original PM omitted or flipped away from requires a
  complete `revision_fallback_seat_audits` incumbent review; changing a hedge or its surrounding
  alpha-beta context requires an exact `revision_hedge_audit`. Reconcile also rebinds final selected symbols
  to the recorded citation support judgments. This enforces the sole veto; code chooses no trade.

## Step 5 — Reconcile (deterministic) + heartbeat

```bash
uv run python scripts/desk_reconcile.py --state-dir live_state --memory-dir live_memory
```

This is the only production decision-to-fill entry point. Never invoke the legacy
`futures_fund.desk_cycle.run_cycle` or `scripts/run_desk_cli.py` during a desk cycle: both are
offline integration-test seams that fail before I/O without an explicit capability bound to the
exact canned `StubAgentRunner`; the checked-in CLI cannot construct any runner.

Before any fill, reconcile recomputes the final precheck against the ORIGINAL evidence marks and
binds the exact cycle, book, SHA-256, metrics echo, B1–B12 rulings, and (after rejection) both
immutable one-attempt revision receipts, the original/revision trail, objective hard-ban facts,
and structured revision compliance.
An accepted original carrying either receipt also HALTs. It then captures a FRESH execution snapshot for every touched symbol: a
complete two-sided L2 book supplies both the fill reference (top-of-book midpoint) and the depth
walked for slippage. This separation is load-bearing: market drift while GPT agents reason is not
slippage. A touched symbol without a complete two-sided execution book HALTS before a paper fill;
never synthesize missing depth or combine one side of a fresh book with an old evidence mark.

The PM's `target_notional` is anchored to its evidence mark before execution:
`target_qty = target_notional / decision_mark`. Reconcile fills that fixed quantity against the
fresh execution book. An exactly preserved `current_book` leg is therefore a true no-op even when
the price moved during reasoning; the movement changes achieved exposure, not quantity behind the
PM's back.

Fetches paginated public historical funding rates **and settlement marks** and retains every
returned event in the account-clock window. The account persists each symbol's last observed
interval. Because Binance does not expose interval-effective history, the collector accepts only
an exact stable-schedule nominal-boundary set or a set exactly explained by at most one old-to-new
interval switch. It persists the stable/transition proof (including every possible first-new
boundary when the exact switch instant is observationally ambiguous) in the decision-cycle
`funding_interval_proofs` artifact and token-free heartbeat record. A raw settlement timestamp up
to one second after its nominal boundary proves that boundary while remaining unchanged in the
accounting audit; duplicate timestamps, two events mapping to one boundary, a second transition,
or any unexplained missing/extra boundary HALT rather than applying the latest rate to missed
events. It then settles FUNDING on the held book through the execution timestamp, reconciles the PaperAccount,
computes the ACHIEVED metrics, and persists everything through one durable, replayable generation
(reads/book/adversary/report/execution/precheck[+originals]) under
`live_state/rebal/cycle/<cycle>/`, plus `live_state/ledger.jsonl` (idempotent per-cycle PnL attribution) and
the equity point (REAL wall-clock ts, monotonicity-guarded). `execution.json` records decision mark,
execution mark, decision-to-execution move, best bid/ask, spread/depth, timestamp, price source,
decision-anchored target quantity, current/delta quantity, and planned turnover. It also retains
the exact raw bid/ask ladders and effective displayed-depth-haircut ladders used by the fill model.
Prices and quantities must be positive and finite, bids strictly descending, asks strictly
ascending, and the top of book uncrossed; malformed, duplicate, unordered, locked, or crossed
snapshots HALT without being sorted or repaired. These rows make recorded VWAP and slippage
independently replayable.
It HALTs (prior book stands) on: all-specialists-failed, an invalid decision chain, or a held
position with no mark. `complete.json` is published only after the account, artifacts, equity, and
ledger are durable; an interrupted commit is recovered before any later task. This validates
workflow provenance; it records and never makes a trading
decision.

Then report a decision-cycle heartbeat: cycle #, schedule_status, n_legs, achieved deploy %,
dollar residual, beta residual, equity, **turnover_usd / fees_paid_cycle / slippage_paid_cycle /
funding_settled_cycle / decision_age_seconds** (frictions and execution staleness are never
invisible again), adversary accepted (+ revision?).

## Intervening token-free heartbeats

For the 08:07 and 16:07 UTC slots, cron polls `scripts/run_desk_heartbeat.sh --scheduled` every ten
minutes. A UTC gate accepts a delayed slot for at most six hours and atomically claims it only
after network/runtime preflight succeeds. It shares the full cycle's `flock`, fetches a current
public funding/mark snapshot plus exact historical funding events only for held symbols, requires a
fresh mark and complete boundary history for every position, settles the paper funding
clock through that boundary, and commits account plus deployment, dollar, beta, equity, and
position statistics through one durable, idempotent heartbeat transaction to
`live_state/portfolio-heartbeats.jsonl`. It never starts Codex, invokes an agent, scans for a
replacement, resizes a hedge, or records a fill. Failure leaves all position quantities unchanged
and is reported in `logs/desk-heartbeat.log`. A failed scheduled heartbeat releases only its exact
claim so cron can retry it safely within the grace window; full GPT claims remain attempt receipts.
Every held-symbol mark must be finite and positive; funding rate and beta finite; and interval an
exact integer in `{1,2,4,8}`. Settlement is staged on a copy and rejects non-positive/non-finite
equity before advancing the official account clock. Durable JSON never admits NaN or infinity.

## Failure handling

Any step error → log the cause, fix the ROOT (never fabricate a report). Evidence fetch fail →
retry once, else HALT (prior book stands). Specialist fail → fail-soft as above. A HALT leaves
the ledger byte-identical — verify before re-running. Never set `live: true`. Never hand-edit
`live_state/`. After a session death, follow `docs/desk-restart-runbook.md`.
