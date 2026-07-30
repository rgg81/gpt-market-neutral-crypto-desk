[GPT DESK 8h CYCLE — autonomous scheduled firing]

Run exactly ONE full paper-desk cycle in this repository. Do not ask questions. Read and obey
`AGENTS.md`, `MISSION.md`, and `docs/desk-cycle-runbook.md`; the runbook is the source of truth.

Before the watchdog, check whether `ops/next-cycle-directive.md` exists. If it does, read it in
full and treat it as a binding one-shot user directive. Pass its full text verbatim to the PM and
Adversary dispatches as `binding_user_directive`; do not merely summarize it.

This firing explicitly requires the Codex multi-agent workflow:

- The root orchestrator is `gpt-5.6-sol` at `xhigh` reasoning.
- Spawn the Reflector (only when due), all three specialists, the PM, and the Adversary as GPT
  subagents. Every subagent must inherit the root `gpt-5.6-sol` model and `xhigh` effort. Never
  select, mention, or fall back to Claude/Opus or a cheaper/faster GPT model.
- Spawn sentiment, technical, and futures concurrently, then wait for all three before the PM.
- The agents make decisions. Deterministic scripts only collect evidence, compute the documented
  precheck, validate provenance, and record paper fills.

Execute the runbook sequence exactly:

0a. Before the watchdog, run the managed-region provenance check from the runbook. Any failure
    HALTS before evidence; do not let copied or unjournaled calibration text reach an agent.
0. Watchdog. If it reports EARLY, stand down immediately without opening a cycle.
1. Evidence.
1b. Score the prior cycle, fail-soft.
1c. Reflect only when recurrences are non-empty, fail-soft.
2. Three GPT specialists in parallel, with the documented validation/retry rules.
3. GPT portfolio manager.
3b. Deterministic precheck.
4. GPT adversary; at most one PM revision, followed by a fresh precheck.
   The verdict's citation_checks must cover every non-flat sentiment symbol and every URL exactly;
   an accepted selected leg cannot rely on a claim the Adversary marked unsupported.
4a. When a binding directive exists, validate the FINAL precheck against it after any revision.
    An Adversary `accept=true` on a plainly noncompliant original is a failed output and gets the
    one documented output retry. A noncompliant final revision HALTs before reconcile.
5. Reconcile only a directive-compliant final book, then print the complete heartbeat.
5a. If a pending one-shot directive exists, archive it only after a successful reconcile that
    satisfies its stated completion condition. On stand-down, HALT, or noncompliance, leave it
    pending.

Hard boundaries:

- PAPER ONLY. `live` remains exactly `false`; never add or call an order-placement path.
- Work only inside this v2 repository and its `live_state/` and `live_memory/`. Do not inspect,
  migrate, or mutate the original sibling desk.
- Never fabricate an agent output, evidence item, source, decision, report, or successful cycle.
- Directive compliance is part of decision-chain validation, not an optional preference. Never
  reconcile or archive a book that misses a pending directive's numeric completion condition.
- On a safety/provenance failure, HALT and leave the prior paper book standing.
- Do not edit the scheduler or user crontab during a cycle.

Finish with a concise heartbeat containing the cycle number, schedule status, legs, deployment,
dollar and beta residuals, equity, turnover, fees, slippage, funding, decision-to-execution age,
adversary result, and whether a revision occurred. If the cycle stood down or halted, say so and
give the exact reason.
