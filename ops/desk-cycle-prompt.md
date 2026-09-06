[GPT WEEKLY CROSS-SECTION DESK — one autonomous PAPER cycle]

Read `AGENTS.md`, `MISSION.md`, and all of `docs/desk-cycle-runbook.md`, then execute exactly one
cycle without asking questions. PAPER only. You are the root orchestrator; never impersonate a
weight agent.

Keep output and token use compact. Do not browse the web. Do not run the legacy sentiment,
technical, futures, PM, Adversary, scoring, performance-packet, or Reflector path.

Run the runbook preflight and watchdog in order. Stand down immediately on `EARLY`. Otherwise run
`desk_cross_section_prepare.py`. Spawn Alpha Allocator and Risk Allocator concurrently with their
full prompt files and the active pending path. Wait for both, validate each, allow only the one
documented malformed-output retry, and build `allocator_digest.json`.

Then spawn the Weight PM, validate it, and build `allocation_precheck.json`. Spawn the Weight
Adversary and validate it. If rejected, dispatch exactly one Weight PM revision bound to
`revision_digest.json`; never run a second Adversary. Finalize the allocation and reconcile through
`desk_cross_section_reconcile.py`. A failure at any stage is a HALT: report the exact cause and do
not retry the cycle or fabricate an output.

End with a concise result containing cycle/week, the fixed ten longs and ten shorts, achieved
positions/deployment/dollar residual, equity, turnover, fees, slippage, funding, Adversary result,
and whether the weekly universe refreshed.
