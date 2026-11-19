# Data availability: what is known at forecast time

Measured by polling Elexon every 20–30 s (2026-11-18 19:50 – 2026-11-19 17:23 UTC) with `spf.watch_publication`. Resolution is about ±30 s. Items already present when the watcher (re)started are excluded.

| measurement | n | min | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| System price: first seen − period end (min) | 42 | 19.2 | 19.8 | 20.4 | 24.8 |
| System price: first seen − createdDateTime (min) | 42 | 0.9 | 1.3 | 1.6 | 1.8 |
| MID (APX): first seen − period start (min) | 43 | 12.1 | 12.3 | 12.4 | 12.5 |
| BOALF: first seen − acceptance time (min) | 9416 | 0.2 | 1.1 | 1.7 | 11.0 |
| IMBALNGC: first seen − publishTime (min) | 40 | 3.3 | 4.7 | 7.1 | 7.8 |
| MELNGC: first seen − publishTime (min) | 39 | 1.8 | 2.7 | 3.9 | 12.8 |
| NDF: first seen − publishTime (min) | 40 | 0.2 | 0.7 | 1.4 | 1.6 |
| TSDF: first seen − publishTime (min) | 40 | 0.2 | 0.8 | 1.5 | 1.6 |
| INDO: first seen − publishTime (min) | 40 | 0.4 | 0.7 | 0.9 | 1.0 |
| LOLPDRM: first seen − publishTime (min) | 40 | 0.1 | 0.4 | 0.6 | 0.6 |
| WINDFOR: first seen − publishTime (min) | 7 | 0.4 | 0.8 | 1.0 | 1.0 |
| FUELINST: first seen − publishTime (min) | 252 | 0.3 | 0.6 | 0.8 | 0.9 |

MID values changed after first publication in 0 of 43 watched periods.

## Rules used by the backtest

Forecasts are issued at SP0 start + 25 min.

- System price / NIV: a period is known once its end + 21 min has passed (SP−1 at issue time).
- MID: known once its period start + 20 min has passed (SP0 at issue time).
- BOALF acceptances: used if issued at least 3 min before issue time.
- Datasets with a publishTime (IMBALNGC, MELNGC, NDF, TSDF, WINDFOR, LOLPDRM, INDO, FUELINST): latest version with publishTime ≤ issue time − 8 min.
- PN and BOD: final at gate closure (period start − 1 h), so used for SP0..SP+2 only; nothing about SP+3's PN or BOD is used.
- Every NESO forecast is the version published at the time, never a later revision (all versions are stored).

## Late items against the rules

| item | n | rule (min) | later than rule | worst (min) |
|---|---:|---:|---:|---:|
| System price after period end | 42 | 21 | 2 (4.8%) | 24.8 |
| BOALF after acceptance time | 9416 | 3 | 12 (0.1%) | 11.0 |
| IMBALNGC after publishTime | 40 | 8 | 0 (0.0%) | 7.8 |
| MELNGC after publishTime | 39 | 8 | 1 (2.6%) | 12.8 |
| NDF after publishTime | 40 | 8 | 0 (0.0%) | 1.6 |
| TSDF after publishTime | 40 | 8 | 0 (0.0%) | 1.6 |
| INDO after publishTime | 40 | 8 | 0 (0.0%) | 1.0 |
| LOLPDRM after publishTime | 40 | 8 | 0 (0.0%) | 0.6 |
| WINDFOR after publishTime | 7 | 8 | 0 (0.0%) | 1.0 |

A system price later than 21 min is still known at issue time if it arrives within 25 min of its period end: 0 of 42 watched prices did not. Late acceptances arrived in batches (Elexon-side delays). The backtest applies the rules above throughout, so it does not model these rare late arrivals; live, `spf.live predict` warns when SP−1's price is not yet published.
