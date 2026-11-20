# Step 1: publication lag and baselines

_Generated 2026-11-20 09:16 UTC by `spf/baselines.py`._

## Summary

- System prices are first published **18–25 minutes after a period ends** (median 19.2). A forecast issued at the start of SP0 knows SP-2 at the latest; one issued 25 minutes in knows SP-1.
- The bar to beat, as MAE in £/MWh, with the baseline that sets it:

| issue time | SP+1 | SP+2 | SP+3 | MID of target (reference) |
|---|---|---|---|---|
| SP0 + 0 min (knows SP-2) | 27.9 (baseline 3b) | 29.6 (baseline 3b) | 31.2 (baseline 3b) | 23.3 |
| SP0 + 25 min (knows SP-1) | 26.0 (baseline 1) | 27.9 (baseline 3b) | 29.6 (baseline 3b) | 23.3 |

- MID of the target period is the most accurate of the three, but it is published during the target period itself, so it cannot be used for SP+1..SP+3. Treat it as a reference for how much the short-term traded price already explains, and as a feature once its timing is respected.
- Same period yesterday is the weakest baseline at every horizon and in every quarter.
- Errors grow with the price level: 2026 prices are higher and more volatile, and every baseline's MAE is far higher in 2026 Q4 than in 2025 Q4 (see the quarterly table).

## Data

- System prices (bmdash `system_prices`): 68,122 periods from 2023-01-01 00:00 to 2026-11-20 07:30 UTC; 6 periods missing from the grid.
- SSP ≠ SBP in 0 periods, so SSP is the target throughout.
- MID: APX (`APXMIDP`) from `data/mid` (this repo). N2EX has traded volume in only 156 of 67,840 periods, so it is not used. APX periods with zero volume (38) are treated as missing.
- Every baseline is scored on the same rows: periods where the actual and all baseline inputs exist.

## Publication lag

Elexon publishes a period's system price shortly after it ends, then republishes it about 24 h 44 min after the period starts. The API only serves the latest version and bmdash keeps no publication time, so the lag can't be read from history: it was measured live.

Minutes from the **end** of a period to its first publication:

| source | count | min | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| createdDateTime | 44 | 18.3 | 18.6 | 19.0 | 19.5 |
| first seen (30 s polling) | 42 | 19.2 | 19.8 | 20.3 | 24.8 |

Periods observed: 2026-11-17 21:30 – 2026-11-19 16:30 UTC. Median 19.2 min, max 24.8 min. The scoring uses the max (24.8 min) to decide what was known.

MID for a period first appeared 12.1–12.5 minutes after the period **started** (43 periods watched). So at any point inside SP0, MID for SP+1..SP+3 is not yet published: baseline 3 as specified uses information from the future. Baseline 3b is the usable version.

So with a forecast issued inside SP0:

- at **SP0 + 0 min**, the latest price known is **SP-2**, so SP+1..SP+3 are 3–5 periods past the last known price; latest MID known is SP-1.
- at **SP0 + 25 min**, the latest price known is **SP-1**, so SP+1..SP+3 are 2–4 periods past the last known price; latest MID known is SP+0.

Revisions: comparing 47 periods (14–15 Nov 2026) captured on their initial version against the republished version, neither price nor NIV changed in any. The stored history is therefore a fair stand-in for the initial price, though the sample is small; `spf.price_snapshot` keeps snapshots so this can be rechecked.

## Results

Error = forecast − actual, £/MWh. Lower is better.

### Forecast issued at SP0 + 0 min (latest known price SP-2)

| horizon | baseline | n | MAE | RMSE | bias |
|---|---|---:|---:|---:|---:|
| SP+1 | 1. Last known price | 67992 | 30.8 | 55.6 | -0.0 |
| SP+1 | 2. Same period yesterday | 67992 | 42.6 | 75.7 | -0.0 |
| SP+1 | 3. MID of target period *(not known in time)* | 67992 | 23.3 | 38.8 | -0.6 |
| SP+1 | 3b. Latest MID known at issue time | 67992 | 27.9 | 44.6 | -0.6 |
| SP+2 | 1. Last known price | 67989 | 34.1 | 59.4 | -0.0 |
| SP+2 | 2. Same period yesterday | 67989 | 42.6 | 75.7 | -0.0 |
| SP+2 | 3. MID of target period *(not known in time)* | 67989 | 23.3 | 38.8 | -0.6 |
| SP+2 | 3b. Latest MID known at issue time | 67989 | 29.6 | 47.2 | -0.6 |
| SP+3 | 1. Last known price | 67985 | 36.4 | 62.4 | -0.0 |
| SP+3 | 2. Same period yesterday | 67985 | 42.6 | 75.7 | -0.0 |
| SP+3 | 3. MID of target period *(not known in time)* | 67985 | 23.3 | 38.8 | -0.6 |
| SP+3 | 3b. Latest MID known at issue time | 67985 | 31.2 | 49.6 | -0.6 |

MAE by band of the actual price:

| horizon | price band (£/MWh) | n | 1. Last known price | 2. Same period yesterday | 3. MID of target period *(not known in time)* | 3b. Latest MID known at issue time |
|---|---|---:|---:|---:|---:|---:|
| SP+1 | < 0 | 3683 | 35.7 | 73.8 | 30.3 | 36.9 |
| SP+1 | 0–50 | 8625 | 36.4 | 51.4 | 26.9 | 32.6 |
| SP+1 | 50–100 | 28706 | 24.8 | 32.4 | 17.6 | 21.0 |
| SP+1 | 100–150 | 20027 | 29.3 | 39.5 | 22.5 | 26.8 |
| SP+1 | 150–250 | 6408 | 43.6 | 57.3 | 35.7 | 42.0 |
| SP+1 | ≥ 250 | 543 | 128.6 | 173.6 | 109.8 | 127.1 |
| SP+2 | < 0 | 3684 | 41.2 | 73.8 | 30.3 | 40.4 |
| SP+2 | 0–50 | 8623 | 40.0 | 51.4 | 26.9 | 34.7 |
| SP+2 | 50–100 | 28702 | 26.9 | 32.4 | 17.6 | 22.0 |
| SP+2 | 100–150 | 20027 | 32.8 | 39.4 | 22.5 | 28.5 |
| SP+2 | 150–250 | 6410 | 48.9 | 57.3 | 35.7 | 44.5 |
| SP+2 | ≥ 250 | 543 | 141.1 | 173.6 | 109.8 | 136.2 |
| SP+3 | < 0 | 3683 | 46.6 | 73.8 | 30.3 | 44.0 |
| SP+3 | 0–50 | 8623 | 42.4 | 51.4 | 26.9 | 36.6 |
| SP+3 | 50–100 | 28699 | 28.3 | 32.4 | 17.6 | 22.9 |
| SP+3 | 100–150 | 20026 | 35.3 | 39.4 | 22.5 | 30.3 |
| SP+3 | 150–250 | 6411 | 52.4 | 57.3 | 35.7 | 47.1 |
| SP+3 | ≥ 250 | 543 | 149.1 | 173.6 | 109.8 | 145.8 |

Direction hit rate (long vs short, from the sign of NIV):

| horizon | n | 1. Last known (persist NIV sign) | 2. Same period yesterday (NIV sign) | Always long |
|---|---:|---:|---:|---:|
| SP+1 | 67992 | 0.627 | 0.541 | 0.526 |
| SP+2 | 67989 | 0.593 | 0.541 | 0.526 |
| SP+3 | 67985 | 0.573 | 0.542 | 0.526 |

### Forecast issued at SP0 + 25 min (latest known price SP-1)

| horizon | baseline | n | MAE | RMSE | bias |
|---|---|---:|---:|---:|---:|
| SP+1 | 1. Last known price | 68001 | 26.0 | 49.3 | -0.0 |
| SP+1 | 2. Same period yesterday | 68001 | 42.6 | 75.7 | -0.0 |
| SP+1 | 3. MID of target period *(not known in time)* | 68001 | 23.3 | 38.8 | -0.6 |
| SP+1 | 3b. Latest MID known at issue time | 68001 | 26.2 | 42.3 | -0.6 |
| SP+2 | 1. Last known price | 67992 | 30.8 | 55.6 | -0.0 |
| SP+2 | 2. Same period yesterday | 67992 | 42.6 | 75.7 | -0.0 |
| SP+2 | 3. MID of target period *(not known in time)* | 67992 | 23.3 | 38.8 | -0.6 |
| SP+2 | 3b. Latest MID known at issue time | 67992 | 27.9 | 44.6 | -0.6 |
| SP+3 | 1. Last known price | 67989 | 34.1 | 59.4 | -0.0 |
| SP+3 | 2. Same period yesterday | 67989 | 42.6 | 75.7 | -0.0 |
| SP+3 | 3. MID of target period *(not known in time)* | 67989 | 23.3 | 38.8 | -0.6 |
| SP+3 | 3b. Latest MID known at issue time | 67989 | 29.6 | 47.2 | -0.6 |

MAE by band of the actual price:

| horizon | price band (£/MWh) | n | 1. Last known price | 2. Same period yesterday | 3. MID of target period *(not known in time)* | 3b. Latest MID known at issue time |
|---|---|---:|---:|---:|---:|---:|
| SP+1 | < 0 | 3685 | 28.0 | 73.7 | 30.3 | 33.7 |
| SP+1 | 0–50 | 8624 | 31.2 | 51.4 | 26.9 | 30.5 |
| SP+1 | 50–100 | 28708 | 21.4 | 32.4 | 17.6 | 19.9 |
| SP+1 | 100–150 | 20031 | 24.3 | 39.5 | 22.5 | 25.2 |
| SP+1 | 150–250 | 6410 | 37.2 | 57.3 | 35.7 | 39.5 |
| SP+1 | ≥ 250 | 543 | 101.4 | 173.6 | 109.8 | 119.4 |
| SP+2 | < 0 | 3683 | 35.7 | 73.8 | 30.3 | 36.9 |
| SP+2 | 0–50 | 8625 | 36.4 | 51.4 | 26.9 | 32.6 |
| SP+2 | 50–100 | 28706 | 24.8 | 32.4 | 17.6 | 21.0 |
| SP+2 | 100–150 | 20027 | 29.3 | 39.5 | 22.5 | 26.8 |
| SP+2 | 150–250 | 6408 | 43.6 | 57.3 | 35.7 | 42.0 |
| SP+2 | ≥ 250 | 543 | 128.6 | 173.6 | 109.8 | 127.1 |
| SP+3 | < 0 | 3684 | 41.2 | 73.8 | 30.3 | 40.4 |
| SP+3 | 0–50 | 8623 | 40.0 | 51.4 | 26.9 | 34.7 |
| SP+3 | 50–100 | 28702 | 26.9 | 32.4 | 17.6 | 22.0 |
| SP+3 | 100–150 | 20027 | 32.8 | 39.4 | 22.5 | 28.5 |
| SP+3 | 150–250 | 6410 | 48.9 | 57.3 | 35.7 | 44.5 |
| SP+3 | ≥ 250 | 543 | 141.1 | 173.6 | 109.8 | 136.2 |

Direction hit rate (long vs short, from the sign of NIV):

| horizon | n | 1. Last known (persist NIV sign) | 2. Same period yesterday (NIV sign) | Always long |
|---|---:|---:|---:|---:|
| SP+1 | 68001 | 0.684 | 0.541 | 0.526 |
| SP+2 | 67992 | 0.627 | 0.541 | 0.526 |
| SP+3 | 67989 | 0.593 | 0.541 | 0.526 |

### MAE by quarter (issue at SP0 + 0 min)

Baselines need no fitting, so this is the walk-forward view: does the ranking hold over time?

| quarter | horizon | n | mean price | price sd | 1. Last known price | 2. Same period yesterday | 3. MID of target period *(not known in time)* | 3b. Latest MID known at issue time |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 2023Q1 | SP+1 | 4239 | 132.6 | 80.5 | 53.0 | 68.7 | 43.5 | 50.8 |
| 2023Q1 | SP+2 | 4239 | 132.7 | 80.5 | 58.7 | 68.6 | 43.5 | 53.0 |
| 2023Q1 | SP+3 | 4239 | 132.7 | 80.5 | 61.5 | 68.6 | 43.5 | 54.8 |
| 2023Q2 | SP+1 | 4364 | 87.6 | 46.2 | 31.8 | 41.6 | 27.5 | 31.6 |
| 2023Q2 | SP+2 | 4364 | 87.6 | 46.2 | 34.7 | 41.6 | 27.5 | 33.0 |
| 2023Q2 | SP+3 | 4364 | 87.6 | 46.2 | 37.6 | 41.6 | 27.5 | 34.2 |
| 2023Q3 | SP+1 | 4404 | 75.5 | 47.4 | 34.0 | 43.5 | 25.7 | 30.6 |
| 2023Q3 | SP+2 | 4403 | 75.5 | 47.4 | 37.6 | 43.5 | 25.7 | 32.2 |
| 2023Q3 | SP+3 | 4402 | 75.6 | 47.4 | 39.8 | 43.5 | 25.7 | 33.4 |
| 2023Q4 | SP+1 | 4414 | 83.3 | 55.7 | 38.0 | 50.0 | 30.7 | 35.5 |
| 2023Q4 | SP+2 | 4414 | 83.3 | 55.7 | 41.5 | 50.0 | 30.7 | 37.1 |
| 2023Q4 | SP+3 | 4414 | 83.3 | 55.7 | 43.8 | 50.0 | 30.7 | 38.3 |
| 2024Q1 | SP+1 | 4366 | 65.3 | 31.8 | 22.1 | 28.6 | 17.4 | 20.6 |
| 2024Q1 | SP+2 | 4366 | 65.3 | 31.8 | 24.5 | 28.6 | 17.4 | 21.6 |
| 2024Q1 | SP+3 | 4366 | 65.3 | 31.8 | 26.0 | 28.6 | 17.4 | 22.5 |
| 2024Q2 | SP+1 | 4366 | 63.6 | 40.8 | 27.0 | 37.3 | 20.0 | 24.1 |
| 2024Q2 | SP+2 | 4366 | 63.6 | 40.8 | 30.0 | 37.4 | 20.0 | 25.5 |
| 2024Q2 | SP+3 | 4366 | 63.6 | 40.8 | 31.7 | 37.3 | 20.0 | 26.9 |
| 2024Q3 | SP+1 | 4416 | 68.4 | 40.1 | 26.3 | 36.8 | 20.1 | 23.9 |
| 2024Q3 | SP+2 | 4416 | 68.4 | 40.1 | 29.0 | 36.8 | 20.1 | 25.6 |
| 2024Q3 | SP+3 | 4416 | 68.4 | 40.1 | 31.5 | 36.8 | 20.1 | 27.2 |
| 2024Q4 | SP+1 | 4418 | 87.1 | 48.0 | 29.1 | 38.5 | 20.9 | 25.6 |
| 2024Q4 | SP+2 | 4418 | 87.1 | 48.0 | 31.8 | 38.5 | 20.9 | 27.2 |
| 2024Q4 | SP+3 | 4418 | 87.1 | 48.0 | 33.8 | 38.5 | 20.9 | 28.7 |
| 2025Q1 | SP+1 | 4318 | 104.2 | 135.1 | 32.8 | 47.9 | 24.8 | 29.4 |
| 2025Q1 | SP+2 | 4318 | 104.2 | 135.1 | 36.4 | 47.9 | 24.8 | 31.2 |
| 2025Q1 | SP+3 | 4318 | 104.2 | 135.1 | 38.9 | 47.9 | 24.8 | 33.5 |
| 2025Q2 | SP+1 | 4353 | 71.3 | 46.1 | 27.7 | 41.7 | 20.8 | 25.2 |
| 2025Q2 | SP+2 | 4353 | 71.3 | 46.1 | 31.5 | 41.6 | 20.8 | 27.0 |
| 2025Q2 | SP+3 | 4352 | 71.3 | 46.1 | 34.1 | 41.7 | 20.8 | 29.0 |
| 2025Q3 | SP+1 | 4414 | 71.7 | 40.0 | 25.6 | 35.8 | 19.0 | 22.9 |
| 2025Q3 | SP+2 | 4414 | 71.7 | 40.0 | 28.4 | 35.8 | 19.0 | 24.5 |
| 2025Q3 | SP+3 | 4414 | 71.7 | 40.0 | 30.5 | 35.8 | 19.0 | 26.0 |
| 2025Q4 | SP+1 | 4418 | 75.7 | 35.9 | 23.8 | 29.7 | 16.8 | 20.0 |
| 2025Q4 | SP+2 | 4418 | 75.7 | 35.9 | 26.0 | 29.7 | 16.8 | 21.3 |
| 2025Q4 | SP+3 | 4418 | 75.7 | 35.9 | 27.4 | 29.7 | 16.8 | 22.6 |
| 2026Q1 | SP+1 | 4318 | 91.7 | 41.5 | 22.0 | 35.5 | 15.6 | 19.2 |
| 2026Q1 | SP+2 | 4318 | 91.7 | 41.5 | 24.8 | 35.5 | 15.6 | 20.7 |
| 2026Q1 | SP+3 | 4318 | 91.7 | 41.5 | 26.5 | 35.5 | 15.6 | 22.2 |
| 2026Q2 | SP+1 | 4364 | 94.6 | 52.0 | 27.8 | 42.6 | 19.8 | 24.3 |
| 2026Q2 | SP+2 | 4364 | 94.6 | 52.0 | 31.3 | 42.6 | 19.8 | 26.4 |
| 2026Q2 | SP+3 | 4364 | 94.6 | 52.0 | 34.2 | 42.6 | 19.8 | 28.6 |
| 2026Q3 | SP+1 | 4402 | 121.7 | 61.6 | 34.3 | 52.0 | 23.9 | 29.5 |
| 2026Q3 | SP+2 | 4400 | 121.7 | 61.6 | 38.7 | 51.9 | 23.9 | 32.3 |
| 2026Q3 | SP+3 | 4398 | 121.7 | 61.6 | 41.9 | 51.9 | 23.9 | 35.1 |
| 2026Q4 | SP+1 | 2418 | 175.3 | 72.9 | 42.7 | 60.3 | 30.7 | 37.3 |
| 2026Q4 | SP+2 | 2418 | 175.3 | 72.9 | 47.2 | 60.3 | 30.7 | 39.6 |
| 2026Q4 | SP+3 | 2418 | 175.3 | 72.9 | 49.9 | 60.3 | 30.7 | 42.2 |

## Notes

- "Same period yesterday" and "MID of target" don't depend on the horizon, so they score the same at SP+1, SP+2 and SP+3 (each horizon is scored on the same set of target periods).
- Same period yesterday is 48 periods earlier in UTC, so it is off by an hour on the two clock-change days a year.
- Direction: Elexon NIV > 0 means the system is short. The price baselines don't forecast a direction directly, so their direction is the NIV sign of the period they copy.
- Reproduce: with `BMDASH_DATA=data/bmdash`, `uv run bmdash ingest --from 2025-04-01 --no-costs`, then `uv run python -m spf.mid`; `spf.price_snapshot` and `spf.watch_publication` collect the live lag measurements; then `uv run python -m spf.baselines`.
