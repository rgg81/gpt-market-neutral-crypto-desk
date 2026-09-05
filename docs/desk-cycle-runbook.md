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

`ops/next-cycle-directive.md` is the sole canonical one-shot user-instruction inbox. Do not read,
copy, overwrite, or delete that mutable pathname by hand. After the watchdog admits a cycle and
before market-data network work, evidence atomically moves that concrete regular-file instance to
state-owned `live_state/directive-claims-v1/` and reads the claim. It passes the full claimed text to
the PM and Adversary as `binding_user_directive`. EARLY stands down before claiming. HALT or any
later pre-commit failure leaves the same private claim pending for the same cycle retry.
Evidence copies the exact UTF-8 bytes to `pending/<cycle>/binding_user_directive.md`. `meta.json`
binds the claim UUID, durable claim-intent hash, raw-payload and canonical-text hashes, fixed relative
source identity, and canonical capability list/hash. Precheck and reconcile reject missing,
partial, changed, invented, or wrong-cycle claim provenance. A new file written to the canonical
inbox while a claim is active is a distinct queued instruction—even when byte-identical—and is
never read or removed by the active cycle.
Only a successful durable reconcile archives the exact claim as manifest-bound schema-v2
`binding_user_directive.json`. After `complete.json` publishes, finalization renames and removes
only that UUID-derived state-owned payload and writes its consumption receipt; it never unlinks the
canonical inbox. Reconcile recovery, outcome attestation, and `desk_recover.py` replay this exact
claim finalization idempotently. Thus a committed claim cannot silently bind again, while an
unsuccessful cycle cannot consume it.
Every new reconcile WAL also binds either explicit claim absence or the exact seven-field claim
identity and matching schema-v2 receipt. Staging and recovery revalidate that relationship under
the exclusive state transaction lock. A new claim is refused while the WAL exists and for an
already completed cycle, closing the verify-to-commit window even for standalone CLIs.
This crash protocol requires Linux `renameat2(RENAME_NOREPLACE)`. Run the repository and state as
one trusted local desk account: pre-existing symlinks and cooperating writer races fail closed, but
a hostile process with concurrent write/rename access to repository or state ancestors is outside
the security boundary because it can already rewrite code, prompts, and paper-account artifacts.
Directive presence or prose never creates machine-readable restart-graduation authority. That
single capability requires this exact first line:

```text
<!-- desk-directive-capabilities: ["controlled_restart_graduation"] -->
```

The JSON list must be unique, sorted, and contain only known capabilities. Evidence parses it
before network work and hash-binds the canonical list separately from the full directive; a
malformed/unknown reserved header HALTs before agents. No header means an empty capability list:
the directive remains a binding user instruction, but cannot supersede restart qualification.

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
PAPER reconcile intent idempotently and publishes that cycle's `complete.json` last, then recovers
the directive lifecycle: a prepared same-cycle claim is materialized/reused, while a completed
manifest-bound claim is finalized by UUID. It validates historical schema-v2 receipts against their
state-owned consumption acknowledgements, but never derives cleanup authority from legacy receipt
paths and never removes the canonical inbox. A recovery failure HALTS. Heartbeats run reconcile
recovery before touching the account.
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
The full launcher owns `logs/desk-cycle.lock` before this command, so `--check-existing` first
recovers any durable Reflector apply transaction and consumed-but-unhandled cooldown receipt, then
performs the audit. The unlocked host health probe instead uses the strictly read-only mode:

```bash
uv run python scripts/reflector_apply.py --memory-dir live_memory \
  --agents-dir agents --probe-existing
```

That mode never recovers or writes. It fails and reports any pending apply transaction or
consumed-but-unhandled receipt for the next lock-owning full preflight; it cannot race an active
cycle by rolling prompt state backward or forward.
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
After reproducing the non-EARLY watchdog status and before its first market request, evidence claims
or reuses the exact state-owned directive instance described above. Agents read only the resulting
pending artifact, never the mutable canonical inbox.

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
the scheduled declared horizon (`24`, `72`, or `168` hours only) with the same tolerance while
retaining actual elapsed time and mark provenance. Schema v5 also binds the origin precheck/risk
model and prices a standardized full-target round trip on both directional sides of the origin L2:
taker fees, displayed-depth haircut, adverse selection, and a fixed book-breadth legging reserve
are included; funding is excluded. Missing policy, either side, full visible fill, or finite curve
cost makes the cost-net label unavailable rather than zero. Immutable v4 rows remain valid gross
forecast calibration but can never satisfy a cost-net expansion gate. Legacy/off-horizon rows
remain auditable but cannot train the desk. Unchanged
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
non-overlapping sample counts, off-horizon exclusions, and exact-horizon complete-cohort cost-net
coverage. A cost-net cohort is usable only when every selected leg has a schema-v5 full-round-trip
label; one unpriced leg excludes the whole cohort, and 24h/72h/168h observations never pool for
calibration. The canonical starter-risk expansion evidence is
`cost_net_independent_time_cohort_n >= 12`, `cost_net_calibration_status="usable"`,
`cost_net_residual_risk_weighted_status="usable"`, and positive
`residual_risk_weighted_realized_round_trip_cost_net_price_edge_frac` in the BookLeg's exact
horizon bucket. Primary cost-net values use only the latest 12 consecutive complete cohorts (or
the shorter trailing streak); total-complete, trailing-complete, and primary-window counts are
reported separately. Partial, unpriced, or off-schedule matching-horizon cohorts and forecasts
unmarked beyond the five-minute scheduled-mark tolerance reset the consecutive streak and block
while newer than the newest complete cohort, so a later result cannot reconnect to old wins across
a gap. An exact, on-schedule, fully priced cohort excluded only for temporal overlap remains
audit-only and neither enters effective n nor blocks recency. At snapshot time, the newest complete
cohort must be no older than max(72 hours, twice its horizon). These are desk-process calibration
facts by horizon, not per-symbol performance histories.
It also reports alpha gross separately from hedge gross,
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
Outcome-based calibration uses only mature immutable score rows, but the state-only
`pm_gate_inactive` liveness window ends at the newest completed prior cycle even when its outcome
is not mature. Its cooldown uses the pending decision/source cycle, never the older score-origin
identifier. The prose adapter for Books predating structured `candidate_reviews` is code-limited
to immutable cycles 51–53; every later Book needs structured gate-causal rows. If multiple
recurrences implicate one role, the Reflector must issue one consolidated full-region edit;
duplicate role edits invalidate the whole proposal before any prompt, journal, head, or anchor
mutation. The sole unscored-cycle citation exception is an `edits[].evidence` string copied exactly
from a same-role, sealed `pm_gate_inactive` recurrence row. It never applies to `region_text`,
`reason`, or `retire_if`, to a paraphrase, to another role/kind, or to any claimed performance
outcome. A malformed proposed edit leaves the authority unconsumed and the recurrence retryable;
only an authenticated explicit no-edit decision or a fully applied proposal starts cooldown.
If a proposal does not edit every surfaced role, its bound `no_action_reason` must explain the
omission; otherwise the whole proposal is rejected before mutation and no recurrence is consumed.
On an incomplete-cycle retry, a durable head event or consumption receipt reconstructs the handled
marker from the authority's retained canonical packet before publishing an empty stand-down. This
makes cooldown crash-consistent and never moves a newer handled-cycle clock backwards.

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
`risk_model.json`, `performance_snapshot.json`, `specialist_reads.sha256` and its exact value), the
current held book and the newest prior completed manifest-bound `book.json` (when one exists),
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
stay fully costed but cannot trap invalidated risk. Its ordinary limit is two. From an exactly
empty trusted `current_book`, the base limit becomes four only when every proposed seat is a new
non-BTC alpha; a BTC/hedge seat, dust/incumbent, flip, increase, or role change leaves the limit at
two. `cold_start_reentry_eligible` and `b9_aggressive_change_limit` expose this state-derived fact
in the hashed precheck and the Adversary's exact echo. Eligibility additionally requires a complete
proposed-symbol residual-risk model; `risk_model_available` is explicitly echoed, and unavailable
covariance is never represented as zero restart risk. B2/B4 still make a valid non-empty restart a
four-seat, at-least-two-per-side construction, and B3 must be met without a fifth hedge. This is
capacity for GPT judgment, never deterministic selection or forced deployment. On the base-rule
path when no binding cold-start directive is present, the Book explicitly records
`controlled_restart_phase` and `controlled_restart_origin_cycle`. Initial eligibility requires
phase true, origin equal to the current cycle, and the cold-start facts above. A nonempty-inventory
continuation requires the exact active origin from the newest prior manifest-bound Book. The
precheck loads that newest Book without searching backward on a corrupt generation and publishes
the prior cycle/hash/origin/phase plus proposed origin/phase and explicit initial, continuation,
and lineage-validity facts. An active prior Book is accepted as lineage input only when its
manifest also binds a schema-v7+ precheck whose canonical artifact hash, internal hash, cycle,
phase/origin, valid-lineage flag, and initial-or-continuation assertion all authenticate. Inactive
legacy Books remain readable. False/null explicitly ends a phase only with a fully flat proposed
Book; a nonempty Book preserves the active origin even after judged expansion. An ended origin
cannot reactivate. An empty account with an active prior may only submit that fully-flat explicit
end, not replace the origin. A genuinely flat account may start a new origin only with no active
prior and no binding directive. `binding_user_directive_present` and
`binding_user_directive_controlled_restart_graduation` are independently derived from cycle meta
and hash-echoed: an empty-account directive must use false/null lineage and its distinct 98–102%
path. These hashed facts inform the sole Adversary and add no deterministic trading veto.

For an eligible initial and every pre-qualification valid continuation, PM and Adversary cap the
Book at the lesser of 20% cash gross and the gross producing 8% annualized residual volatility,
target absolute beta residual at most 2% cash, and explicitly justify the resulting B1
under-deployment. Risk does not rise until every selected seat has at least 12 independent
matching-horizon cost-net time cohorts,
proven by its schema-v5 bucket fields
`cost_net_independent_time_cohort_n >= 12`, `cost_net_calibration_status="usable"`,
`cost_net_residual_risk_weighted_status="usable"`, and
`residual_risk_weighted_realized_round_trip_cost_net_price_edge_frac > 0`; aggregate and
cross-horizon rows cannot qualify. Partial/unpriced/off-schedule gaps and forecasts unmarked more
than five minutes past maturity reset the streak and block while newest; exact fully priced
temporal overlaps are audit-only. The newest complete cohort must satisfy the max(72 hours, twice
its horizon) age cap. This schema-v5 round-trip price edge excludes funding.
Passing permits judged expansion under ordinary portfolio
bounds, but phase remains true and its exact origin remains fixed until the Book is fully flat.
For every active Book the Adversary returns `controlled_restart_risk_audit`, echoing the exact
gross/20%-cash, residual-volatility/8%, beta-residual/2%, and expansion facts plus one exact-horizon
performance row for every selected alpha. By desk policy the Adversary rejects an initial
expansion. A continuation beyond any cap requires all selected rows to derive qualified and
explicit Adversary approval/note unless the exact typed
`controlled_restart_graduation` capability is present and the Adversary explicitly records
`directive_graduation_capability_used=true`. A generic directive hash, unrelated prose, or false
usage never bypasses qualification. The capability applies only during an already-authenticated
continuation for that cycle; structured audit/approval and exact origin remain required, and the
base gate resumes without it. Deterministic binding authenticates facts, scope, and the
Adversary's explicit choice; it does not derive the trading verdict. Inactive Books must not carry
this audit.
B10 keeps every priced selected seat and
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
The current-schema `metrics_echo` must also explicitly copy
`cold_start_reentry_eligible`, `b9_aggressive_change_limit`, `risk_model_available`, and every
controlled-restart proposed/prior identity plus initial/continuation/validity flag; omission or
disagreement is invalid. It separately copies `binding_user_directive_present` and
`binding_user_directive_controlled_restart_graduation`; neither can be inferred by the agent.
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
  unreviewed revision. Controlled-restart origin/phase is frozen across the one unreviewed
  revision except that a constraint-authorized fully flat final Book may explicitly end it; both
  prechecks must carry the identical newest manifest-bound prior lineage; and
  an active revised Book cannot add or change a selected alpha symbol/horizon outside the original
  `controlled_restart_risk_audit`. Any final expansion must remain within its explicitly approved
  coverage; qualification may be superseded only when that original audit truthfully set
  `directive_graduation_capability_used=true` against the exact-cycle typed capability; and
  at least one constraint must correct the
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
and structured revision compliance. It independently reloads the same newest prior
manifest-bound Book lineage used by precheck; an invalid newest completion never falls back to an
older origin.
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
ledger are durable; an interrupted commit is recovered before any later task. When the cycle carries
a directive, its schema-v2 archive binds the exact claim UUID and intent/payload provenance. Only
after publication does reconcile finalize that private claim and record consumption. A failed
finalization reports recovery required while leaving both the completed generation and any newer
canonical inbox file intact. This validates workflow provenance; it records and never makes a
trading decision.

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
