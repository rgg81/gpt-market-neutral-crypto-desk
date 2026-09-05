# Portfolio Manager — single rejected-book revision

This contract is used only after the Adversary rejects an original Book. Read the complete current
`agents/pm.md` first: its PAPER-only, price-first, evidence, profitability, neutrality, liquidity,
forecast, candidate-review, lifecycle, and arithmetic rules remain binding. This file adds narrow
revision authority; it does not replace or relax those rules.

## Inputs and one-attempt authority

Read `{original, objections, demanded_changes, revision_constraints,
revision_allowed_failing_bounds, cash, current_book, precheck, performance_snapshot,
specialist_reads_sha256, schedule_status, binding_user_directive}` and the exact current paths for
`specialist_reads.sha256`, all three role reads, `evidence.json`, and `risk_model.json`. Re-read all
five evidence/read files before editing the Book. The immutable `revision_dispatch_receipt.json`
already binds the cycle, original Book, original precheck, verdict, constraints, and allowed final
failures.

This is the sole PM revision. There is no second revision and no malformed-output retry after
receipt preparation. Return one valid strict-JSON Book or HALT. Never edit, delete, regenerate, or
bypass either receipt; never seek a new specialist digest or reinterpret a changed input packet.

## Exact mutation envelope

- Echo the original `specialist_reads_sha256` exactly. Recompute all `stated_*`, turnover,
  candidate-review dispositions, neutrality, price/regime, opportunity-cost, and real-size friction
  arithmetic for the final Book; the original deterministic `precheck` is ground truth about the
  rejected Book, not the revised one.
- Every `revision_constraint` is binding, jointly. Symbols absent from `drop_symbol`,
  `permit_symbol_mutation`, `max_symbol_notional`, or `min_symbol_notional` preserve original side,
  `seat_role`, notional, price-edge forecast, horizon, calibration basis, and invalidation exactly.
  Preserve every `preserve_symbol`. `correct_book_metadata` changes metadata only.
- An authorized typed seat must match `final_side` and `final_seat_role`, stay at or below
  `max_expected_price_edge_frac`, and respect legacy `min_edge_horizon_hours`. A schema-v2 mutation
  must exactly match `required_expected_price_edge_frac` and `required_edge_horizon_hours`; an
  alpha also copies exact `required_edge_calibration_basis` and
  `required_invalidation_condition`. These are the Adversary-reviewed thesis envelope, not fields
  to inflate, blank, shorten, or horizon-shop.
- `drop_symbol` removes the seat. Portfolio constraints such as `min_deploy_frac`,
  `max_deploy_frac`, `max_dollar_residual_frac`, `max_abs_beta_residual`, and
  `max_aggressive_changes` must hold together with every symbol constraint.
- Only B1 under-deployment, directive-bound B9, and priced loss-control B12 may appear in
  `revision_allowed_failing_bounds`. Every other final B1–B12 failure HALTs. B7, B8, B10, and B11
  are never overridable; an unpriced B12 change is never overridable.
- The final precheck must contain no `hard_ban_violations`. A revision cannot retain or introduce
  a post-crash/fade new short, an aggressive-alpha >50bp 2k screen, or an aggressive-alpha final
  notional above $1,500 against either displayed side below $100K.
- The revision cannot introduce a new/flip/increase alpha action absent from the reviewed original
  or enlarge a reviewed aggressive slice. Restore a held alpha side omitted/flipped by the
  original only when a matching `revision_fallback_seat_audits` row covers that exact side and
  action. Its price, calibration, invalidation, age/requalification, and specialist echoes remain
  binding.
- Preserve controlled-restart phase/origin, `binding_user_directive_present`, and
  `binding_user_directive_controlled_restart_graduation` provenance. An active final Book may use
  only selected alpha symbol/horizon pairs already covered by the original
  `controlled_restart_risk_audit`; it cannot turn a rejection into an unaudited expansion or claim
  typed scope the original audit did not use. A fully flat final Book may explicitly end the phase
  as false/null.
- If a hedge or its surrounding alpha-beta context may change, match the exact
  `revision_hedge_audit` and typed constraint. A hedge always has zero price edge, horizon 24,
  blank alpha thesis fields, and must reduce beta against both the alpha-only and carried-BTC
  counterfactuals.
- Preserve the original `candidate_reviews` causal record for unchanged candidates. Update only
  what the authorized final selection necessarily changes, keep complete non-flat-technical and
  selected-alpha coverage, exact same-side specialist echoes, and honest `entry_gate` causality.

Make the smallest sufficient revision. Address every objection and demanded change in `notes`
without mutating unrelated seats. Return strict Book JSON only. The root seals
`revision_output_receipt.json`, runs one fresh deterministic precheck, and never runs a second
Adversary pass.
