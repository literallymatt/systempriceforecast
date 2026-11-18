# Step 1: publication lag and baselines

_Generated 2026-11-18 20:09 UTC by `spf/baselines.py`._

## Summary

- System prices are first published **18–19 minutes after a period ends** (median 18.6). A forecast issued at the start of SP0 knows SP-2 at the latest; one issued 25 minutes in knows SP-1.
- The bar to beat, as MAE in £/MWh, with the baseline that sets it:

| issue time | SP+1 | SP+2 | SP+3 | MID of target (reference) |
|---|---|---|---|---|
| SP0 + 0 min (knows SP-2) | 28.2 (baseline 1) | 31.5 (baseline 1) | 33.9 (baseline 1) | 20.3 |
| SP0 + 25 min (knows SP-1) | 23.9 (baseline 1) | 28.2 (baseline 1) | 31.5 (baseline 1) | 20.3 |

- MID of the target period is the most accurate of the three, but it is published during the target period itself, so it cannot be used for SP+1..SP+3. Treat it as a reference for how much the short-term traded price already explains, and as a feature once its timing is respected.
- Same period yesterday is the weakest baseline at every horizon and in every quarter.
- Errors grow with the price level: 2026 prices are higher and more volatile, and every baseline's MAE is far higher in 2026 Q4 than in 2025 Q4 (see the quarterly table).

## Data

- System prices (bmdash `system_prices`): 28,648 periods from 2025-03-31 23:00 to 2026-11-18 18:30 UTC; 0 periods missing from the grid.
- SSP ≠ SBP in 0 periods, so SSP is the target throughout.
- MID: APX (`APXMIDP`) from `data/mid` (this repo). N2EX has traded volume in only 79 of 28,460 periods, so it is not used. APX periods with zero volume (23) are treated as missing.
- Every baseline is scored on the same rows: periods where the actual and all baseline inputs exist.

## Publication lag

Elexon publishes a period's system price shortly after it ends, then republishes it about 24 h 44 min after the period starts. The API only serves the latest version and bmdash keeps no publication time, so the lag can't be read from history: it was measured live.

Minutes from the **end** of a period to its first publication:

| source | count | min | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| createdDateTime | 44 | 18.3 | 18.6 | 19.0 | 19.5 |

Periods observed: 2026-11-17 21:30 – 2026-11-18 19:00 UTC. Median 18.6 min, max 19.5 min. The scoring uses the max (19.5 min) to decide what was known.

MID timing (spot check, 18 Nov 2026 19:50 UTC): MID was out for the period that started at 19:30 but not for 20:00. So MID appears during or just before its own period, and MID for SP+1..SP+3 is not published at forecast time: baseline 3 as specified uses information from the future. The exact first-appearance minute (needed to score 3b, the latest MID known at issue time) needs `spf.watch_publication` to have run; rerun this script after it has.

So with a forecast issued inside SP0:

- at **SP0 + 0 min**, the latest price known is **SP-2**, so SP+1..SP+3 are 3–5 periods past the last known price.
- at **SP0 + 25 min**, the latest price known is **SP-1**, so SP+1..SP+3 are 2–4 periods past the last known price.

Revisions: comparing 47 periods (14–15 Nov 2026) captured on their initial version against the republished version, neither price nor NIV changed in any. The stored history is therefore a fair stand-in for the initial price, though the sample is small; `spf.price_snapshot` keeps snapshots so this can be rechecked.

## Results

Error = forecast − actual, £/MWh. Lower is better.

### Forecast issued at SP0 + 0 min (latest known price SP-2)

| horizon | baseline | n | MAE | RMSE | bias |
|---|---|---:|---:|---:|---:|
| SP+1 | 1. Last known price | 28577 | 28.2 | 42.6 | -0.0 |
| SP+1 | 2. Same period yesterday | 28577 | 41.2 | 58.5 | -0.3 |
| SP+1 | 3. MID of target period *(not known in time)* | 28577 | 20.3 | 28.2 | -0.6 |
| SP+2 | 1. Last known price | 28577 | 31.5 | 46.6 | -0.0 |
| SP+2 | 2. Same period yesterday | 28577 | 41.2 | 58.5 | -0.3 |
| SP+2 | 3. MID of target period *(not known in time)* | 28577 | 20.3 | 28.2 | -0.6 |
| SP+3 | 1. Last known price | 28577 | 33.9 | 49.5 | -0.1 |
| SP+3 | 2. Same period yesterday | 28577 | 41.2 | 58.5 | -0.3 |
| SP+3 | 3. MID of target period *(not known in time)* | 28577 | 20.3 | 28.2 | -0.6 |

MAE by band of the actual price:

| horizon | price band (£/MWh) | n | 1. Last known price | 2. Same period yesterday | 3. MID of target period *(not known in time)* |
|---|---|---:|---:|---:|---:|
| SP+1 | < 0 | 1629 | 28.7 | 73.7 | 24.8 |
| SP+1 | 0–50 | 2626 | 35.5 | 56.5 | 23.7 |
| SP+1 | 50–100 | 11915 | 22.9 | 30.4 | 15.7 |
| SP+1 | 100–150 | 8814 | 26.9 | 38.3 | 19.6 |
| SP+1 | 150–250 | 3215 | 39.2 | 52.8 | 29.0 |
| SP+1 | ≥ 250 | 378 | 79.2 | 106.2 | 62.9 |
| SP+2 | < 0 | 1629 | 34.5 | 73.7 | 24.8 |
| SP+2 | 0–50 | 2626 | 39.9 | 56.5 | 23.7 |
| SP+2 | 50–100 | 11915 | 24.8 | 30.4 | 15.7 |
| SP+2 | 100–150 | 8814 | 30.4 | 38.3 | 19.6 |
| SP+2 | 150–250 | 3215 | 44.6 | 52.8 | 29.0 |
| SP+2 | ≥ 250 | 378 | 88.1 | 106.2 | 62.9 |
| SP+3 | < 0 | 1629 | 40.2 | 73.7 | 24.8 |
| SP+3 | 0–50 | 2626 | 42.4 | 56.5 | 23.7 |
| SP+3 | 50–100 | 11915 | 26.0 | 30.4 | 15.7 |
| SP+3 | 100–150 | 8814 | 33.3 | 38.3 | 19.6 |
| SP+3 | 150–250 | 3215 | 47.8 | 52.8 | 29.0 |
| SP+3 | ≥ 250 | 378 | 93.7 | 106.2 | 62.9 |

Direction hit rate (long vs short, from the sign of NIV):

| horizon | n | 1. Last known (persist NIV sign) | 2. Same period yesterday (NIV sign) | Always long |
|---|---:|---:|---:|---:|
| SP+1 | 28577 | 0.617 | 0.529 | 0.527 |
| SP+2 | 28577 | 0.584 | 0.529 | 0.527 |
| SP+3 | 28577 | 0.568 | 0.529 | 0.527 |

### Forecast issued at SP0 + 25 min (latest known price SP-1)

| horizon | baseline | n | MAE | RMSE | bias |
|---|---|---:|---:|---:|---:|
| SP+1 | 1. Last known price | 28577 | 23.9 | 37.8 | -0.0 |
| SP+1 | 2. Same period yesterday | 28577 | 41.2 | 58.5 | -0.3 |
| SP+1 | 3. MID of target period *(not known in time)* | 28577 | 20.3 | 28.2 | -0.6 |
| SP+2 | 1. Last known price | 28577 | 28.2 | 42.6 | -0.0 |
| SP+2 | 2. Same period yesterday | 28577 | 41.2 | 58.5 | -0.3 |
| SP+2 | 3. MID of target period *(not known in time)* | 28577 | 20.3 | 28.2 | -0.6 |
| SP+3 | 1. Last known price | 28577 | 31.5 | 46.6 | -0.0 |
| SP+3 | 2. Same period yesterday | 28577 | 41.2 | 58.5 | -0.3 |
| SP+3 | 3. MID of target period *(not known in time)* | 28577 | 20.3 | 28.2 | -0.6 |

MAE by band of the actual price:

| horizon | price band (£/MWh) | n | 1. Last known price | 2. Same period yesterday | 3. MID of target period *(not known in time)* |
|---|---|---:|---:|---:|---:|
| SP+1 | < 0 | 1629 | 22.3 | 73.7 | 24.8 |
| SP+1 | 0–50 | 2626 | 30.7 | 56.5 | 23.7 |
| SP+1 | 50–100 | 11915 | 19.8 | 30.4 | 15.7 |
| SP+1 | 100–150 | 8814 | 22.3 | 38.3 | 19.6 |
| SP+1 | 150–250 | 3215 | 33.6 | 52.8 | 29.0 |
| SP+1 | ≥ 250 | 378 | 65.5 | 106.2 | 62.9 |
| SP+2 | < 0 | 1629 | 28.7 | 73.7 | 24.8 |
| SP+2 | 0–50 | 2626 | 35.5 | 56.5 | 23.7 |
| SP+2 | 50–100 | 11915 | 22.9 | 30.4 | 15.7 |
| SP+2 | 100–150 | 8814 | 26.9 | 38.3 | 19.6 |
| SP+2 | 150–250 | 3215 | 39.2 | 52.8 | 29.0 |
| SP+2 | ≥ 250 | 378 | 79.2 | 106.2 | 62.9 |
| SP+3 | < 0 | 1629 | 34.5 | 73.7 | 24.8 |
| SP+3 | 0–50 | 2626 | 39.9 | 56.5 | 23.7 |
| SP+3 | 50–100 | 11915 | 24.8 | 30.4 | 15.7 |
| SP+3 | 100–150 | 8814 | 30.4 | 38.3 | 19.6 |
| SP+3 | 150–250 | 3215 | 44.6 | 52.8 | 29.0 |
| SP+3 | ≥ 250 | 378 | 88.1 | 106.2 | 62.9 |

Direction hit rate (long vs short, from the sign of NIV):

| horizon | n | 1. Last known (persist NIV sign) | 2. Same period yesterday (NIV sign) | Always long |
|---|---:|---:|---:|---:|
| SP+1 | 28577 | 0.674 | 0.529 | 0.527 |
| SP+2 | 28577 | 0.617 | 0.529 | 0.527 |
| SP+3 | 28577 | 0.584 | 0.529 | 0.527 |

### MAE by quarter (issue at SP0 + 0 min)

Baselines need no fitting, so this is the walk-forward view: does the ranking hold over time?

| quarter | horizon | n | mean price | price sd | 1. Last known price | 2. Same period yesterday | 3. MID of target period *(not known in time)* |
|---|---|---:|---:|---:|---:|---:|---:|
| 2025Q2 | SP+1 | 4310 | 71.5 | 46.0 | 27.7 | 41.6 | 20.8 |
| 2025Q2 | SP+2 | 4310 | 71.5 | 46.0 | 31.5 | 41.6 | 20.8 |
| 2025Q2 | SP+3 | 4310 | 71.5 | 46.0 | 34.0 | 41.6 | 20.8 |
| 2025Q3 | SP+1 | 4415 | 71.6 | 40.0 | 25.6 | 35.8 | 19.0 |
| 2025Q3 | SP+2 | 4415 | 71.6 | 40.0 | 28.4 | 35.8 | 19.0 |
| 2025Q3 | SP+3 | 4415 | 71.6 | 40.0 | 30.5 | 35.8 | 19.0 |
| 2025Q4 | SP+1 | 4418 | 75.7 | 35.9 | 23.8 | 29.7 | 16.8 |
| 2025Q4 | SP+2 | 4418 | 75.7 | 35.9 | 26.0 | 29.7 | 16.8 |
| 2025Q4 | SP+3 | 4418 | 75.7 | 35.9 | 27.4 | 29.7 | 16.8 |
| 2026Q1 | SP+1 | 4318 | 91.7 | 41.5 | 22.0 | 35.5 | 15.6 |
| 2026Q1 | SP+2 | 4318 | 91.7 | 41.5 | 24.8 | 35.5 | 15.6 |
| 2026Q1 | SP+3 | 4318 | 91.7 | 41.5 | 26.5 | 35.5 | 15.6 |
| 2026Q2 | SP+1 | 4366 | 94.6 | 52.0 | 27.8 | 42.6 | 19.8 |
| 2026Q2 | SP+2 | 4366 | 94.6 | 52.0 | 31.3 | 42.6 | 19.8 |
| 2026Q2 | SP+3 | 4366 | 94.6 | 52.0 | 34.2 | 42.6 | 19.8 |
| 2026Q3 | SP+1 | 4406 | 121.8 | 61.6 | 34.3 | 52.0 | 23.9 |
| 2026Q3 | SP+2 | 4406 | 121.8 | 61.6 | 38.7 | 52.0 | 23.9 |
| 2026Q3 | SP+3 | 4406 | 121.8 | 61.6 | 41.9 | 52.0 | 23.9 |
| 2026Q4 | SP+1 | 2344 | 175.5 | 73.4 | 42.9 | 60.2 | 30.8 |
| 2026Q4 | SP+2 | 2344 | 175.5 | 73.4 | 47.4 | 60.2 | 30.8 |
| 2026Q4 | SP+3 | 2344 | 175.5 | 73.4 | 50.2 | 60.2 | 30.8 |

## Notes

- "Same period yesterday" and "MID of target" don't depend on the horizon, so they score the same at SP+1, SP+2 and SP+3 (each horizon is scored on the same set of target periods).
- Same period yesterday is 48 periods earlier in UTC, so it is off by an hour on the two clock-change days a year.
- Direction: Elexon NIV > 0 means the system is short. The price baselines don't forecast a direction directly, so their direction is the NIV sign of the period they copy.
- Reproduce: with `BMDASH_DATA=data/bmdash`, `uv run bmdash ingest --from 2025-04-01 --no-costs`, then `uv run python -m spf.mid`; `spf.price_snapshot` and `spf.watch_publication` collect the live lag measurements; then `uv run python -m spf.baselines`.
