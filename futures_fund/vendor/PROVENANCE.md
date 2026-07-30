# Vendored analytical scripts

Copied **verbatim** from the predecessor desk's `futures_fund/vendor/`
(itself lifted from the user's personal Claude Code skills) so the market-neutral desk is
self-contained and reproducible (spec §11 — project-only, all committed).

| File | Upstream source |
|---|---|
| `overfit_detector.py` | `~/.claude/skills/walk-forward-validation/scripts/overfit_detector.py` |

**Do not hand-edit** beyond import hygiene. To update, re-copy from upstream and re-run the smoke
tests. We use only the pure compute functions (DSR / PBO); the data-fetch helpers are unused here.
