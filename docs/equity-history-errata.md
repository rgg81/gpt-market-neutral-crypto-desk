# Equity-history errata — cycles 1-5 (pre-reset ledger)

The 2026-07-10 forensic review found the recorded timestamps for cycles 1-3 were **manufactured**
(taken from a hand-set `meta.now`, not the wall clock) and the series is **time-disordered**
(cycle 3's stamp is LATER than cycle 4's). The equity VALUES are correct; only the timestamps lie.
Per the truthful-ledger rule the recorded file is left untouched; this errata documents the truth.
Any time-scaled statistic (Sharpe x 365, funding-clock math) over cycles 1-5 must use the artifact
mtimes below, not the recorded `ts`.

| cycle | recorded ts (WRONG for 1-3)        | true artifact write time (UTC) |
|-------|------------------------------------|--------------------------------|
| 1     | 2026-07-09T12:19:00+00:00          | ~2026-07-09 (manufactured; see cycle-1 artifact mtimes) |
| 2     | 2026-07-09T16:26:00+00:00          | ~2026-07-09 (manufactured) |
| 3     | 2026-07-10T00:00:00+00:00          | 2026-07-09 22:13:38 (report.json mtime) — NOTE: later than c4's stamp |
| 4     | 2026-07-09T22:37:10.766125+00:00   | 2026-07-09 22:44:19 |
| 5     | 2026-07-10T00:37:10.490972+00:00   | 2026-07-10 00:57:13 |
| 6     | 2026-07-10T08:37:30.826120+00:00   | ~2026-07-10 08:5x (accurate to minutes) |

Consequence: cycles 3-5 actually ran within ~2.7 real hours (not 3 x 8h) — the churn/cadence
stats for that window are correspondingly compressed.

Fixed going forward (commit alongside this file): `record_equity` now stamps the REAL wall clock
at reconcile time and enforces monotonicity (a non-monotonic append raises instead of recording).
