[GPT DESK 24h CYCLE — autonomous scheduled firing]

Run exactly one PAPER desk cycle in this repository without asking questions. Read `AGENTS.md`,
`MISSION.md`, and the **complete** `docs/desk-cycle-runbook.md`; execute that runbook exactly. It
is the source of truth for command order, artifacts, validation, retry limits, decision authority,
and failure handling. Read any `ops/next-cycle-directive.md` in full and preserve its one-shot
binding/archive semantics.

Runtime identity is fixed: root and every real Reflector, specialist, PM, and Adversary subagent
are `gpt-5.6-sol` at `xhigh`, authenticated by Codex login. Do not set a subagent model override,
use a raw API key, impersonate a role, or substitute another model. Spawn sentiment, technical,
and futures concurrently; wait for all three final notifications before dispatching the PM.

The mandatory sequence is:

```text
proxy ensure/freshness → managed-prompt provenance → durable recovery → watchdog
→ evidence → scheduled scoring → performance → optional real Reflector
→ repeat managed-prompt provenance check
→ seal post-reflection decision-start runtime provenance
→ three real specialists in parallel → immutable read digest
→ real PM → deterministic precheck → real Adversary
→ at most one receipt-bound PM revision and fresh precheck
→ decision-chain validation → PAPER reconcile → heartbeat
```

Enforce these release-critical checks from the runbook:

- An EARLY watchdog stands down. A proxy, candle-freshness, recovery, provenance, all-specialist,
  malformed-after-allowed-retry, or decision-chain failure HALTS with the prior book untouched.
- Current-forming proxy candles prove freshness but never enter completed-candle statistics.
  Direct-Binance OHLCV, stale/empty fallback, fabricated evidence, and invented outputs are
  forbidden.
- After reflection and its repeated provenance check, run `scripts/desk_decision_start.py` before
  spawning specialists. Never create or mutate a specialist/decision artifact before that seal,
  and never run reflection after it.
- Deterministic code supplies and binds evidence, performance, risk, precheck, execution and
  accounting facts. GPT agents alone rank, construct, size, accept, reject, and revise trades.
- All roles read the exact current `performance_snapshot.json`. The PM and Adversary use the
  complete immutable specialist digest, current descriptive risk packet, current managed entry
  policy, exact directive when present, and all incumbent-thesis provenance required by the
  runbook.
- Price-relative alpha is the anchor. Carry may lead only in verified chop. Every incumbent is
  compared with cash and the best replacement; a broken non-hedge price thesis with no positive
  forward edge exits. Loss-control drops/decreases never consume the aggressive-action cap.
- PM forecasts use only 24/72/168h, carry calibration/invalidation evidence, and are one-time
  horizon outcomes. Candidate reviews cover every current non-flat technical candidate and every
  selected alpha leg so rejected opportunity cost can be scored without code creating a trade.
- The Adversary opens every cited non-flat sentiment URL and is the sole veto. It may reject once;
  the single PM revision stays within immutable receipts and typed constraints. Never run a second
  adversarial pass or silently repair an agent decision in code.
- Reconcile uses decision-mark quantities, exchange-valid PAPER quantities, fresh two-sided books,
  raw published settlement funding, truthful fees/slippage, and the durable manifest contract.
  It never calls an order-placement API.
- `live` remains exactly `false`. Work only in this v2 state/memory. Do not edit scheduler or
  crontab inside the cycle.

Finish with the runbook heartbeat fields. On stand-down or HALT, report the exact reason and never
claim success.
