# Vendored analytical scripts

Forked from the predecessor desk's `futures_fund/vendor/` so the market-neutral desk is
self-contained and reproducible. Local changes are explicit below and covered by repository tests.

| File | Upstream source |
|---|---|
| `overfit_detector.py` | `~/.claude/skills/walk-forward-validation/scripts/overfit_detector.py` |

`overfit_detector.py` carries a local correctness hardening patch: PBO validates finite 2-D input
and supports observation-level purge and embargo around test blocks. Any upstream refresh must
preserve or deliberately supersede those changes and pass `tests/test_research_validation.py`.
The desk uses only its pure DSR/PBO/minimum-track-record computations; demo helpers are not part of
production orchestration.
