# GB system price forecast: SP+1, SP+2, SP+3

The final model is `lgbm_2023_stack_bag3`. It is three LightGBM models, one per horizon, each averaged over 3
seeds. Each predicts the price minus the latest known MID, and is fed NIV forecasts from a first-stage model.
All inputs are free public data from Elexon Insights (BMRS), which includes NESO's published forecasts.

## Accuracy (MAE, £/MWh)

| window | forecast | SP+1 | SP+2 | SP+3 |
|---|---|---:|---:|---:|
| DEV (2025-04 – 2026-04, walk-forward) | **model** | **13.85** | **17.67** | **20.12** |
| | latest MID | 20.50 | 22.11 | 23.72 |
| | last known price | 21.17 | 25.08 | 28.02 |
| TEST (2026-05 – 2026-11-18, held out, scored once) | **model** | **19.76** | **24.94** | **28.51** |
| | latest MID | 27.61 | 29.81 | 32.20 |
| | last known price | 29.28 | 34.33 | 38.47 |

On TEST the model's error is 28% / 16% / 11% below the latest-MID baseline at SP+1 / SP+2 / SP+3, and 33% /
27% / 26% below the last known price.

The TEST months are harder for every method. Prices are higher and more volatile into autumn (October MAE at
SP+1 is 26.6, against 12.7 in May), so the baselines are worse too. TEST was not used for any modelling
choice. Full breakdowns are in [analysis-lgbm_2023_stack_bag3_test.md](analysis-lgbm_2023_stack_bag3_test.md)
and [analysis-lgbm_2023_stack_dev.md](analysis-lgbm_2023_stack_dev.md).

## Forecast set-up and what is known when

The forecast is issued 25 minutes into the current period (SP0). Forecasts cover the next three periods: from
SP 24 at hh:25, it forecasts SP 25, 26 and 27. Every input is restricted to what had been published by then.
The publication delays were measured by watching Elexon live; see [data-availability.md](data-availability.md).

| input | measured delay | rule used |
|---|---|---|
| System price / NIV | median 19.8 min after the period ends (p95 20.4, worst 24.8) | SP−1 is the latest (known at end + 21 min) |
| MID (APX) | 12.1–12.5 min after the period starts; never revised (43 periods) | SP0's MID is used (known at start + 20 min) |
| BOALF acceptances | median 1.1 min (p95 1.7; 0.1% later than 3, worst 11) | acceptances issued ≥ 3 min before issue |
| NESO forecasts (IMBALNGC, MELNGC, TSDF, NDF, WINDFOR, LOLPDRM, INDO) | IMBALNGC median 4.7 min (worst 7.8); MELNGC median 2.7 (one at 12.8); others < 2 | latest version with publishTime ≤ issue − 8 min |
| FUELINST, wind/solar outturn | < 1 min | publishTime ≤ issue − 2 min |
| System frequency | about 40 s | ≤ issue − 2 min |
| PN, BOD | final at gate closure (start − 1 h) | SP0..SP+2 only; nothing about SP+3 |

DISBSAD (about 2.5 h late) is not used. Every version of each NESO forecast is stored, and features use an as-of
join, so the model only ever sees the forecast that was live at the time. A check that rebuilds features with
the live code for 61 recent periods matched the training frame in all 240 features.

## Features (240, plus 6 stacked)

- **Price history:** the last price, NIV and volumes from SP−1 back. Rolling means and standard deviations
  over 3 h, 1 day and 1 week. Share of periods short in the last day. The same period yesterday and last week.
- **MID:** SP0..SP−5 prices and volumes, the daily mean, and the last price minus MID.
- **NESO forecasts for SP0..SP+3:** imbalance (IMBALNGC), margin, TSDF, NDF, wind (WINDFOR), and LoLP/de-rated
  margin. Also the forecast's age and its revision over the last hour.
- **Outturns:**
  - generation by fuel (FUELINST) and its 30-min change;
  - wind and solar forecast error so far;
  - demand outturn (INDO) and its error against the forecast;
  - system frequency deviation over the last 10/30/60 min.
- **Scheduled position:** PN of every BM unit, aggregated by technology (gas, wind, interconnectors, pumped
  storage, batteries, ...) for SP0..SP+2. Includes the change from the previous period, and scheduled
  generation against the demand forecast. This gave the largest single gain late on (about −0.6 at SP+1).
- **Live balancing actions** for SP0..SP+2 from BOALF acceptances so far:
  - accepted offer and bid MWh, split into system and energy actions;
  - unit counts;
  - volume-weighted, max and min accepted prices (priced from BOD);
  - a crude price estimate from the marginal accepted action.
- **Calendar:** hour, weekday, month, bank holidays.
- **Stacked:** out-of-fold NIV forecasts and P(short) for each horizon, from first-stage LightGBM models.

## Model

- **Target:** price minus the latest MID (SP0's), with the last known price as the fallback.
- **Model:** LightGBM, L1 objective, 1500 rounds, learning rate 0.03, 63 leaves, averaged over 3 seeds.
- **First stage:** NIV regression and P(short) per horizon, fitted out-of-fold within the training set.
- **Backtest:** monthly walk-forward on an expanding window from 2023-01-15. There is a 3 h gap before each test
  month.

## What helped (DEV MAE at SP+1 / SP+2 / SP+3)

| step | SP+1 | SP+2 | SP+3 |
|---|---:|---:|---:|
| latest MID baseline | 20.50 | 22.11 | 23.72 |
| LightGBM on price, MID, NESO forecasts, outturns | 17.25 | 19.79 | 21.61 |
| + live BOALF actions | 15.84 | 18.98 | 21.06 |
| + solar, cleaned frequency | 15.79 | 18.91 | 20.89 |
| + stacked NIV and P(short) | 14.96 | 18.52 | 20.77 |
| + training from 2023 | 14.53 | 18.31 | 20.55 |
| + PN aggregated by technology | 13.89 | 17.73 | 20.18 |
| + 3-seed average (final) | 13.85 | 17.67 | 20.12 |

These were tried and did not help:

- anchoring on the raw price or the last price instead of MID;
- L2 and Huber objectives (much worse: 19.0 and 18.0 at SP+1);
- recency weighting;
- different leaf counts, learning rates and feature fractions;
- anchoring on the BOALF price estimate;
- stacking the side as well as NIV;
- blending with other variants.

## Limitations

- **Spikes.** Prices ≥ £250 are badly under-forecast: TEST SP+1 MAE is 54 there, with a bias of −50. The
  worst 5% of forecasts carry 21% of the total error.
- **Changes of side.** Error is much higher when the system flips: TEST SP+1 is 26.6 when it flips and 16.3
  when it doesn't. Reading a forecast above MID as a call that the system will be
  short (and below as long), the model gets the side right 75% / 66% / 58% of the time.
- **SP+3** gains least: by then its PN, BOD and acceptances are not yet known.
- **Delays** were measured over 22 hours (18–19 Nov 2026). The rules held for nearly everything, with rare
  Elexon-side delays: 2 of 42 system prices came after 21 min (worst 24.8, still before the 25-minute issue
  time), 12 of 9,416 acceptances after 3 min, and 1 of 39 MELNGC forecasts after 8 min. The backtest does not
  model these. Live, `predict` warns if SP−1's price is not yet out. See
  [data-availability.md](data-availability.md).
- **Revisions.** System prices were taken as finally published. In samples they never changed after first
  publication.

## Running it

```bash
uv run python -m spf.live train --exp lgbm_2023_stack_bag3   # about 4 min, fits on all history
uv run python -m spf.live predict                            # refreshes the last 2 days, then forecasts
```

Run `predict` at least 25 minutes into a period. Before that, it forecasts from the previous period and says
so. Forecasts are logged to `data/live_forecasts.csv`. To rebuild everything from scratch, see the module
docstrings: `spf.ingest`, `spf.mid`, `spf.pn_agg`, `spf.bm_live`, `spf.features`, then `spf.model --exp ...`.
