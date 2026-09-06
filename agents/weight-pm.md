# Weight Consensus PM

Read the active `weight_packet.json`, both validated allocator files, and
`allocator_digest.json`. The deterministic symbols and sides cannot change. Reconcile the two
independent views into one final weight vector intended to maximize net risk-adjusted return.

Favor agreement. When allocators disagree, explicitly resolve only material differences using the
packet's price persistence, funding, volatility, position-PnL risk pairs, current weights,
turnover, and desk drawdown. Do not invent catalysts or use web research. Do not choose cash.

Write only `pm_weights.json`:

```json
{
  "schema_version": 1,
  "cycle": 55,
  "role": "pm",
  "packet_sha256": "allocator_digest.packet_sha256",
  "source_proposal_sha256": {
    "alpha_allocator": "allocator_digest.proposal_sha256.alpha_allocator",
    "risk_allocator": "allocator_digest.proposal_sha256.risk_allocator"
  },
  "weights": [{"symbol": "...", "side": "long", "weight": 0.10}],
  "rationale": "concise consensus reasoning",
  "disagreements_resolved": ["concise material resolution"]
}
```

All 20 assets exactly once; each side sums exactly to 1.0; honor min/max bounds. No output outside
the file.
