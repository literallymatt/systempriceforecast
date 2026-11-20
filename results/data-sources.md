# Open data for the system price forecast: what we use, what we added, what else exists

Written 2026-11-19. The question was: are we pulling in enough data, can we add weather, and what other
open sources could help? The rule for every source is the same as the rest of the project: a feature is only
usable if we can show **when** it was published, so the backtest sees what was known at the forecast time
(SP0 start + 25 min), not a later revision.

## Already used before this change

Elexon Insights (BMRS), with every published version kept: system prices and NIV, MID, BOALF acceptances,
PN and BOD (final at gate closure), IMBALNGC, MELNGC, TSDF, NDF, WINDFOR, LOLPDRM, INDO, FUELINST, DGWS/AGWS
(wind and solar forecast and outturn), FREQ. See [forecast.md](forecast.md).

## Added in this change

| source | what | how "known at tau" is enforced | module |
|---|---|---|---|
| **NOAA GFS 0.5°** (AWS open data, `noaa-gfs-bdp-pds`) | Weather forecasts: 2 m temperature at population centres, 100 m wind speed (and a power-curve proxy) at offshore and onshore wind clusters, cloud and solar radiation at solar clusters | Each run's S3 Last-Modified time is stored; a run is used from max(init + 4 h 30, Last-Modified + 5 min). Runs land ~3 h 35 after init | `spf.weather` |
| **N2EX day-ahead hourly price** (Nord Pool, via the NESO data portal) | The day-ahead auction price for SP0..SP+3's hours, the daily mean, DA − MID | Cleared the day before delivery; targets are under 2 h ahead | `spf.dayahead` |
| **Elexon MELS** | Maximum export limits in force now, by technology, and their change over the last hour; headroom = MEL − PN | MEL in force at tau − 5 min (the stream keeps only the segments that took effect, so the level in force now is the honest quantity) | `spf.mel_agg` |
| **Elexon REMIT** (every revision) | Unplanned and planned unavailable MW active at SP+1 / SP+3, gas outages, transmission outages, MW newly announced in the last 1 h / 3 h | Latest revision with publishTime ≤ tau − 8 min | `spf.ingest` (REMIT), `spf.extra_features` |
| **Continental day-ahead prices** (Energy-Charts, `api.energy-charts.info`, CC BY 4.0) | SDAC auction prices for FR, NL, BE, DE-LU, NO2, DK1 at SP+1 / SP+3, their mean, range, step into the next period, and spread to the GB day-ahead price | Auction day D (CET) known from D-1 14:00 CET. No publication time is given; each fetch records `fetched_at` | `spf.eu_dayahead` |
| **Elexon SYSWARN** | NESO system warnings: margin notices, demand-reduction risk, NRAPM, SO-SO trade prices: count in the last 12 h, hours since the last one | publishTime ≤ tau − 8 min | `spf.ingest` (SYSWARN), `spf.extra_features` |
| **ENTSO-E Transparency Platform** (added 20 Nov) | Day-ahead load (A65) and wind/solar (A69) forecasts for FR, NL, BE, DE-LU, NO2, DK1 and the Republic of Ireland, as residual load (load − wind − solar) at SP+1 and its step from SP0, continental totals at SP+1/SP+3, continental wind; Irish SEM day-ahead price (A44) at SP+1/SP+3 and its spread to the GB day-ahead price | The API serves only the latest revision, with no publication time: each fetch records `fetched_at`, and fixed rules apply: load and SEM price for CET day D known from D-1 14:00 CET, wind/solar from D-1 18:00 CET (the regulation's deadline). Token from `ENTSOE_API_TOKEN` or the gitignored `data/secrets/entsoe_token` | `spf.entsoe` |

Why GFS rather than Open-Meteo: Open-Meteo's historical-forecast API stitches the latest runs together and
does not say when each value became available, so a backtest on it would leak the future at a 1–2 h horizon.
Its "previous runs" API is honest only at day-ahead lead times. (Its free quota is also per IP and was
exhausted from this cloud container.) The GFS archive keeps every run with its publication time, and the
same files serve live use.

## Results (DEV walk-forward, Apr 2025 to Apr 2026, MAE in GBP/MWh, single seed)

| inputs | SP+1 | SP+2 | SP+3 |
|---|---|---|---|
| baseline (`base_stack`, the previous model's inputs) | 13.88 | 17.71 | 20.20 |
| baseline + all five sources above (`open_all`) | 13.98 | 17.78 | 20.13 |
| baseline + GFS weather | 13.96 | 17.72 | 20.19 |
| baseline + MEL / headroom | 13.98 | 17.76 | 20.23 |
| baseline + N2EX day-ahead | 13.87 | 17.71 | 20.08 |
| baseline + REMIT | 13.93 | 17.76 | 20.27 |
| baseline + SYSWARN | 13.91 | 17.73 | 20.21 |
| baseline + N2EX + continental day-ahead (`base_plus_da_eu`) | 13.90 | **17.64** | **20.03** |
| same, 3 seeds (`base_plus_da_eu_bag3`) vs previous model 3 seeds (13.85 / 17.67 / 20.12) | 13.85 | **17.56** | **19.97** |

Seed-to-seed noise is about 0.05. None of the five sources clearly helps: weather, MEL, REMIT and system
warnings are within noise or slightly worse, and the GB day-ahead price is level at SP+1/SP+2 and 0.12
better at SP+3, which needs a multi-seed check (`base_plus_dayahead_bag3`) before it is relied on.

The reading: at a 1-2 hour horizon the existing Elexon inputs (NESO's own wind and demand forecasts,
recent prices, NIV, PN and BOD) already carry what weather, outages and declared capacity would add. More
data helps only if it is genuinely new information; continental prices (interconnector flows) are the best
remaining candidate, and the only addition that helps: GB plus continental day-ahead prices are level at
SP+1 and about 0.1-0.15 better at SP+2 / SP+3 on DEV with three seeds, and about 0.2 better on the held-out
TEST months (19.74 / 24.77 / 28.34 against 19.76 / 24.94 / 28.51). `base_plus_da_eu_bag3` is now the live
default; it excludes the weather, MEL, REMIT and SYSWARN features. Full scores are in `experiments.jsonl`.

## Rerun on 20 Nov 2026, with ENTSO-E

Every dataset was refreshed to 20 Nov and the GFS history fetched in full (1,420 days, none failed), then the
frame was rebuilt and the experiments rerun on a Windows machine. Same DEV window, MAE in GBP/MWh:

| inputs | seeds | SP+1 | SP+2 | SP+3 |
|---|---:|---:|---:|---:|
| baseline (`base_stack`) | 1 | 13.90 | 17.75 | 20.19 |
| + N2EX day-ahead (`base_plus_dayahead`) | 1 | 13.89 | 17.72 | 20.05 |
| + N2EX + continental day-ahead (`base_plus_da_eu`) | 1 | 13.87 | 17.63 | 20.06 |
| + ENTSO-E (`base_plus_entsoe`) | 1 | 13.90 | 17.68 | 20.03 |
| + N2EX + continental + ENTSO-E (`base_plus_da_eu_ent`) | 1 | 13.95 | 17.66 | 20.00 |
| + GFS weather, full history (`base_plus_weather`) | 1 | 13.92 | 17.72 | 20.21 |
| everything (`open_all`) | 1 | 13.99 | 17.71 | 20.03 |
| previous model (`lgbm_2023_stack_bag3`, baseline inputs) | 3 | 13.85 | 17.67 | 20.12 |
| live model (`base_plus_da_eu_bag3`, + N2EX + continental day-ahead), rescored on this data | 3 | **13.82** | **17.56** | 19.97 |
| live model + ENTSO-E (`base_plus_da_eu_ent_bag3`) | 3 | 13.91 | 17.59 | **19.92** |

- The baseline reproduces the earlier run within seed noise (13.88 / 17.71 / 20.20 before).
- Weather still adds nothing, now with the full GFS history behind it.
- ENTSO-E on its own helps the baseline at SP+2 / SP+3 (17.68 / 20.03 against 17.75 / 20.19), but it
  carries much the same information as the day-ahead prices: on top of the live model's inputs it is
  0.06 worse at SP+1, 0.03 worse at SP+2 and 0.05 better at SP+3, all within seed noise. It is not added
  to the live model. The ingest stays in place (`spf.entsoe`, fetched live), so the `ent_` features can be
  revisited, for example in an SP+3-only input set.
- The previous model's TEST score with two more days of data: 19.80 / 24.95 / 28.47 (19.76 / 24.94 /
  28.51 to 18 Nov).

## Other open sources considered, not added (yet)

Ranked by how much I expect them to help, given what the model already has.

| source | what it would add | access | why not now |
|---|---|---|---|
| **ENTSO-E scheduled cross-border flows and intraday forecasts** | Day-ahead load, wind/solar and SEM prices are now in (`spf.entsoe`). Still left: scheduled commercial exchanges between neighbours, and intraday wind/solar updates | Same token | GB borders left ENTSO-E after 2021; intraday forecasts are revised without a published history of versions |
| **SEMO / SEMOpx (Ireland)** | Irish balancing prices (the SEM day-ahead price now comes from ENTSO-E) | Public reports (reports.sem-o.com) | Small interconnectors; balancing prices are published after the fact |
| **NESO "upcoming trades"** | NESO's own forward trades (interconnector and other energy trades) before delivery: a direct signal of the position NESO expects | NESO data portal, refreshed every 10 min | Current snapshot only, no history of versions. Worth collecting live from now on |
| **Sheffield Solar PV_Live** | Near-real-time national and regional solar outturn estimates. Embedded solar error is a large part of the demand error | Free API (api.pvlive.uk), reachable | Only the latest revision is kept (`updated_gmt` is the final revision's time), so the first estimates are not available historically. Elexon AGWS solar is already used. Could be logged live |
| **NESO embedded wind and solar forecasts** | NESO's own embedded forecast, used in its demand forecast | NESO data portal | Only the latest forecast is published; history of versions is not kept. DGWS covers similar ground |
| **NESO carbon intensity forecast** | Generation mix forecast | api.carbonintensity.org.uk | Derived from the same NESO forecasts already used |
| **Elexon NONBM, NETBSAD, DISBSAD** | Non-BM STOR and balancing-services adjustments that feed the price | Elexon | DISBSAD is ~2.5 h late. NONBM is small |
| **Elexon SEL/SIL, MZT, NDZ (dynamic data)** | Unit flexibility: stable limits and minimum zero times shape what NESO can actually accept | Elexon | Large per-unit data. MEL headroom captures the first-order effect |
| **National Gas data portal** | System average gas price (SAP), linepack, gas demand. Sets the CCGT offer price level | data.nationalgas.com | Daily and slow-moving; MID and DA already reflect the energy price level. The API returned nothing to a simple query and needs more work |
| **Carbon price (UK ETS)** | CCGT marginal cost | No good free daily feed | Slow-moving |
| **ECMWF open data (AWS)** | A second, often better, weather model than GFS | AWS open data, free | GFS first; ECMWF 0.25° only goes back to Feb 2024 (0.4° before that), so the training history mixes resolutions |
| **Met Office DataHub / METAR observations** | Real-time weather observations: temperature and wind as they happen | DataHub needs a key; METAR archives (e.g. Iowa Environmental Mesonet) are free | FUELINST wind outturn and frequency already give a real-time view of the system |
| **Calendar extras** | School holidays (NESO "school-holiday-percentages"), clock-change days, big TV events | NESO portal | Small; bank holidays are already in |
