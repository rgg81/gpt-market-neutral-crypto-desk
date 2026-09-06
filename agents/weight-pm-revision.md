# Weight PM — Sole Revision

The Weight Adversary rejected the original allocation. Read `weight_packet.json`,
`pm_weights.json`, `allocation_adversary.json`, and `revision_digest.json`. Make exactly one
revised weight vector that obeys every structured constraint. Symbols and sides remain immutable;
all 20 stay held; each side sums exactly to 1.0; packet min/max weights still apply.

Write only `pm_weights_revision.json` with role `pm_revision`. Copy the exact
`revision_digest.source_proposal_sha256` mapping into `source_proposal_sha256`. Explain the
constraint response concisely in `rationale` and `disagreements_resolved`. No second adversarial
pass occurs and no prose may appear outside the file.
