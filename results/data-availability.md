# Data availability: what is known at forecast time

Measured by polling Elexon every 20–30 s (2026-11-18 19:50 – 2026-11-18 21:22 UTC) with `spf.watch_publication`. Resolution is about ±30 s. Items already present when the watcher (re)started are excluded.

| measurement | n | min | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| System price: first seen − period end (min) | 3 | 19.8 | 19.8 | 20.3 | 20.4 |
| System price: first seen − createdDateTime (min) | 3 | 1.2 | 1.4 | 1.7 | 1.7 |
| MID (APX): first seen − period start (min) | 3 | 12.3 | 12.4 | 12.4 | 12.4 |
| BOALF: first seen − acceptance time (min) | 362 | 0.3 | 0.9 | 1.5 | 1.7 |
| IMBALNGC: first seen − publishTime (min) | 2 | 5.1 | 5.3 | 5.5 | 5.5 |
| FUELINST: first seen − publishTime (min) | 12 | 0.3 | 0.5 | 0.8 | 0.8 |

MID values changed after first publication in 0 of 3 watched periods.

## Rules used by the backtest

Forecasts are issued at SP0 start + 25 min.

- System price / NIV: a period is known once its end + 21 min has passed (SP−1 at issue time).
- MID: known once its period start + 20 min has passed (SP0 at issue time).
- BOALF acceptances: used if issued at least 5 min before issue time.
- Datasets with a publishTime (IMBALNGC, MELNGC, NDF, TSDF, WINDFOR, LOLPDRM, INDO, FUELINST): latest version with publishTime ≤ issue time − 3 min.
- PN and BOD: final at gate closure (period start − 1 h), so used for SP0..SP+2 only; nothing about SP+3's PN or BOD is used.
- Every NESO forecast is the version published at the time, never a later revision (all versions are stored).
