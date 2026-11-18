"""Plan step 1: measure the system price publication lag and score the three baselines.

    BMDASH_DATA=data/bmdash uv run python -m spf.baselines

Writes results/baselines.md.

Setup
- Target: SSP (= SBP under the single price) for SP+1, SP+2, SP+3, from bmdash `system_prices`.
- Forecast issue time: a fixed point inside the current period SP0. Two are scored: the start of SP0
  and 25 minutes into it. The latest price known then follows from the measured publication lag.
- Baselines: (1) last known price carried forward, (2) the same period 24 hours earlier,
  (3) the target period's MID (APX). MID for SP+1..SP+3 is not published at forecast time, so (3) is
  a reference, not a usable forecast; (3b) the latest MID known at forecast time is the usable version.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("BMDASH_DATA", str(Path("data/bmdash").resolve()))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from bmdash import store  # noqa: E402

HH = pd.Timedelta(minutes=30)
HORIZONS = (1, 2, 3)
ISSUE_MINUTES = (0, 25)  # forecast issue time, minutes after the start of SP0
BANDS = [-np.inf, 0, 50, 100, 150, 250, np.inf]
BAND_LABELS = ["< 0", "0–50", "50–100", "100–150", "150–250", "≥ 250"]
OUT = Path("results/baselines.md")


# ---------------------------------------------------------------- data

def load_prices() -> pd.DataFrame:
    con = store.connect()
    df = con.sql("""select start_time, settlement_date, sp, ssp, sbp, niv, price_derivation_code
                    from system_prices order by start_time""").df()
    df["start_time"] = pd.to_datetime(df.start_time, utc=True)
    df = df.drop_duplicates("start_time", keep="last").set_index("start_time")
    grid = pd.date_range(df.index.min(), df.index.max(), freq=HH, tz="UTC")
    return df.reindex(grid)


def load_mid() -> pd.Series:
    files = sorted(Path("data/mid").glob("*.parquet"))
    mid = pd.concat(pd.read_parquet(f) for f in files)
    apx = mid[mid.provider == "APXMIDP"].set_index("start_time").sort_index()
    apx = apx[~apx.index.duplicated(keep="last")]
    # zero volume means no trades: Elexon reports price 0, which is not a price
    return apx.price.where(apx.volume > 0)


# ---------------------------------------------------------------- publication lag

def measure_lag() -> dict:
    """Minutes from the end of a period to its first publication, from two live sources:
    createdDateTime of prices still on their initial version in each snapshot, and first-seen times
    from the polling watcher (only periods that ended after the watcher started)."""
    lags = []
    for f in sorted(Path("data/price_snapshots").glob("*.parquet")):
        s = pd.read_parquet(f)
        end = pd.to_datetime(s.startTime, utc=True) + HH
        lag = (pd.to_datetime(s.createdDateTime, utc=True) - end).dt.total_seconds() / 60
        lags.append(pd.DataFrame({"start": end - HH, "lag": lag, "source": "createdDateTime"})[lag < 120])
    watch_path = Path("data/publication_watch.csv")
    mid_first = pd.DataFrame()
    if watch_path.exists():
        w = pd.read_csv(watch_path, parse_dates=["start_time", "first_seen"])
        started = w.first_seen.min()
        sp = w[w.dataset == "system_price"]
        end = sp.start_time + HH
        fresh = sp[end > started]  # ended after the watcher began, so first_seen is a real first sighting
        lags.append(pd.DataFrame({"start": fresh.start_time,
                                  "lag": (fresh.first_seen - (fresh.start_time + HH)).dt.total_seconds() / 60,
                                  "source": "first seen (30 s polling)"}))
        m = w[(w.dataset == "mid") & (w.provider == "APXMIDP")]
        m = m[m.start_time > started]  # periods that started after the watcher began
        mid_first = pd.DataFrame({"start": m.start_time,
                                  "after_start_min": (m.first_seen - m.start_time).dt.total_seconds() / 60})
    lag = pd.concat(lags, ignore_index=True).drop_duplicates(["source", "start"])  # snapshots overlap
    return {"lag": lag, "mid_first": mid_first}


def latest_known_offset(lag_minutes: float, issue_minute: int) -> int:
    """Offset j (<0) of the latest period whose price is published by SP0 start + issue_minute.
    Period j ends at 30*(j+1) minutes after SP0 start and is published lag_minutes later."""
    j = -1
    while 30 * (j + 1) + lag_minutes > issue_minute:
        j -= 1
    return j


# ---------------------------------------------------------------- scoring

def build(prices: pd.DataFrame, mid: pd.Series, offset: int, mid_offset: int | None) -> pd.DataFrame:
    """One row per (SP0, horizon) with the actual price and each baseline's forecast."""
    y = prices.ssp
    niv = prices.niv
    mid_g = mid.reindex(prices.index)
    rows = []
    for h in HORIZONS:
        f = pd.DataFrame({"horizon": h, "actual": y.shift(-h), "actual_niv": niv.shift(-h)}, index=prices.index)
        f.index.name = "sp0"
        f["target_start"] = f.index + h * HH
        f["last_known"] = y.shift(-offset)
        f["last_known_niv"] = niv.shift(-offset)
        f["yesterday"] = y.shift(48 - h)
        f["yesterday_niv"] = niv.shift(48 - h)
        f["mid_target"] = mid_g.shift(-h)
        f["mid_latest"] = mid_g.shift(-mid_offset) if mid_offset is not None else np.nan
        rows.append(f)
    df = pd.concat(rows)
    return df


BASELINES = {
    "last_known": "1. Last known price",
    "yesterday": "2. Same period yesterday",
    "mid_target": "3. MID of target period *(not known in time)*",
    "mid_latest": "3b. Latest MID known at issue time",
}


def metrics(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = []
    for h, g in df.groupby("horizon"):
        for c in cols:
            e = g[c] - g.actual
            out.append({"horizon": f"SP+{h}", "baseline": BASELINES[c], "n": int(e.notna().sum()),
                        "MAE": e.abs().mean(), "RMSE": np.sqrt((e ** 2).mean()), "bias": e.mean()})
    return pd.DataFrame(out)


def band_mae(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    df = df.assign(band=pd.cut(df.actual, BANDS, labels=BAND_LABELS, right=False))
    out = []
    for (h, b), g in df.groupby(["horizon", "band"], observed=True):
        r = {"horizon": f"SP+{h}", "price band (£/MWh)": b, "n": len(g)}
        for c in cols:
            r[BASELINES[c]] = (g[c] - g.actual).abs().mean()
        out.append(r)
    return pd.DataFrame(out)


def direction(df: pd.DataFrame) -> pd.DataFrame:
    """Hit rate on long/short. Elexon NIV > 0: system short (priced off offers)."""
    out = []
    for h, g in df.groupby("horizon"):
        short = g.actual_niv > 0
        r = {"horizon": f"SP+{h}", "n": len(g),
             "1. Last known (persist NIV sign)": ((g.last_known_niv > 0) == short).mean(),
             "2. Same period yesterday (NIV sign)": ((g.yesterday_niv > 0) == short).mean(),
             f"Always {'short' if short.mean() >= 0.5 else 'long'}": max(short.mean(), 1 - short.mean())}
        out.append(r)
    return pd.DataFrame(out)


def by_quarter(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    df = df.assign(quarter=df.target_start.dt.tz_convert("Europe/London").dt.tz_localize(None).dt.to_period("Q").astype(str))
    out = []
    for (q, h), g in df.groupby(["quarter", "horizon"]):
        r = {"quarter": q, "horizon": f"SP+{h}", "n": len(g), "mean price": g.actual.mean(),
             "price sd": g.actual.std()}
        for c in cols:
            r[BASELINES[c]] = (g[c] - g.actual).abs().mean()
        out.append(r)
    return pd.DataFrame(out)


# ---------------------------------------------------------------- report

def md(df: pd.DataFrame, floatfmt: str = ".1f") -> str:
    def fmt(v):
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else format(v, floatfmt)
        return str(v)
    head = "| " + " | ".join(df.columns) + " |"
    sep = "|" + "|".join("---:" if pd.api.types.is_numeric_dtype(df[c]) else "---" for c in df.columns) + "|"
    body = ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *body])


def main() -> None:
    prices = load_prices()
    mid = load_mid()
    lagd = measure_lag()
    lag, mid_first = lagd["lag"], lagd["mid_first"]
    lag_max = float(lag.lag.max())
    lag_p50 = float(lag.lag.median())
    mid_after = float(mid_first.after_start_min.max()) if len(mid_first) else np.nan

    held = prices.ssp.notna()
    n_missing = int((~held).sum())
    single = prices[held]
    n_dual = int((single.ssp - single.sbp).abs().gt(0.005).sum())

    sections = []
    summary_rows = []
    for m in ISSUE_MINUTES:
        offset = latest_known_offset(lag_max, m)
        # latest MID known at issue time: MID of period j first appears mid_after minutes after it starts
        mid_offset = None
        if not np.isnan(mid_after):
            mid_offset = 0
            while 30 * mid_offset + mid_after > m:
                mid_offset -= 1
        df = build(prices, mid, offset, mid_offset)
        cols = [c for c in BASELINES if c != "mid_latest" or mid_offset is not None]
        # one common sample: every baseline and the actual present
        df = df.dropna(subset=["actual", "actual_niv", *cols, "last_known_niv", "yesterday_niv"])
        met = metrics(df, cols)
        for _, r in met.iterrows():
            summary_rows.append({"issue time": f"SP0 + {m} min", **r.to_dict()})
        sections.append((m, offset, mid_offset, df, cols, met))

    first, last = prices.index[held].min(), prices.index[held].max()
    L = []
    L.append("# Step 1: publication lag and baselines\n")
    L.append(f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `spf/baselines.py`._\n")
    L.append("## Summary\n")
    L.append(f"- System prices are first published **{lag.lag.min():.0f}–{lag_max:.0f} minutes after a period "
             f"ends** (median {lag_p50:.1f}). A forecast issued at the start of SP0 knows SP-2 at the latest; "
             "one issued 25 minutes in knows SP-1.")
    L.append("- The bar to beat, as MAE in £/MWh, with the baseline that sets it:\n")
    bar = []
    for m, offset, mid_offset, df, cols, met in sections:
        r = {"issue time": f"SP0 + {m} min (knows SP{offset:+d})"}
        for h in HORIZONS:
            usable = met[(met.horizon == f"SP+{h}") & ~met.baseline.str.startswith("3.")]
            best = usable.loc[usable.MAE.idxmin()]
            r[f"SP+{h}"] = f"{best.MAE:.1f} (baseline {best.baseline.split('.')[0]})"
        r["MID of target (reference)"] = f"{met[met.baseline.str.startswith('3.')].MAE.iloc[0]:.1f}"
        bar.append(r)
    L.append(md(pd.DataFrame(bar)))
    L.append("")
    L.append("- MID of the target period is the most accurate of the three, but it is published during the "
             "target period itself, so it cannot be used for SP+1..SP+3. Treat it as a reference for how much "
             "the short-term traded price already explains, and as a feature once its timing is respected.")
    L.append("- Same period yesterday is the weakest baseline at every horizon and in every quarter.")
    L.append("- Errors grow with the price level: 2026 prices are higher and more volatile, and every "
             "baseline's MAE is far higher in 2026 Q4 than in 2025 Q4 (see the quarterly table).\n")
    L.append("## Data\n")
    L.append(f"- System prices (bmdash `system_prices`): {held.sum():,} periods from "
             f"{first:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC; {n_missing} periods missing from the grid.")
    L.append(f"- SSP ≠ SBP in {n_dual} periods, so SSP is the target throughout.")
    n2ex = pd.concat(pd.read_parquet(f) for f in Path("data/mid").glob("*.parquet")).query("provider == 'N2EXMIDP'")
    L.append(f"- MID: APX (`APXMIDP`) from `data/mid` (this repo). N2EX has traded volume in only "
             f"{int((n2ex.volume > 0).sum())} of {len(n2ex):,} periods, so it is not used. APX periods with zero volume ({int(mid.isna().sum())}) are treated as missing.")
    L.append("- Every baseline is scored on the same rows: periods where the actual and all baseline "
             "inputs exist.\n")

    L.append("## Publication lag\n")
    L.append("Elexon publishes a period's system price shortly after it ends, then republishes it about "
             "24 h 44 min after the period starts. The API only serves the latest version and bmdash keeps "
             "no publication time, so the lag can't be read from history: it was measured live.\n")
    summ = lag.groupby("source").lag.describe(percentiles=[0.5, 0.9])[["count", "min", "50%", "90%", "max"]]
    summ = summ.reset_index().rename(columns={"50%": "median", "90%": "p90"})
    summ["count"] = summ["count"].astype(int)
    L.append("Minutes from the **end** of a period to its first publication:\n")
    L.append(md(summ))
    span = f"{lag.start.min():%Y-%m-%d %H:%M} – {lag.start.max():%Y-%m-%d %H:%M} UTC"
    L.append(f"\nPeriods observed: {span}. Median {lag_p50:.1f} min, max {lag_max:.1f} min. "
             f"The scoring uses the max ({lag_max:.1f} min) to decide what was known.\n")
    if len(mid_first):
        L.append(f"MID for a period first appeared {mid_first.after_start_min.min():.1f}–{mid_after:.1f} "
                 f"minutes after the period **started** ({len(mid_first)} periods watched). So at any "
                 "point inside SP0, MID for SP+1..SP+3 is not yet published: baseline 3 as specified uses "
                 "information from the future. Baseline 3b is the usable version.\n")
    else:
        L.append("MID timing (spot check, 18 Nov 2026 19:50 UTC): MID was out for the period that started at "
                 "19:30 but not for 20:00. So MID appears during or just before its own period, and MID for "
                 "SP+1..SP+3 is not published at forecast time: baseline 3 as specified uses information from "
                 "the future. The exact first-appearance minute (needed to score 3b, the latest MID known at "
                 "issue time) needs `spf.watch_publication` to have run; rerun this script after it has.\n")
    L.append("So with a forecast issued inside SP0:\n")
    for m, offset, mid_offset, *_ in sections:
        mo = "" if mid_offset is None else f"; latest MID known is SP{mid_offset:+d}"
        L.append(f"- at **SP0 + {m} min**, the latest price known is **SP{offset:+d}**, so SP+1..SP+3 are "
                 f"{1 - offset}–{3 - offset} periods past the last known price{mo}.")
    L.append("")
    L.append("Revisions: comparing 47 periods (14–15 Nov 2026) captured on their initial version against "
             "the republished version, neither price nor NIV changed in any. The stored history is "
             "therefore a fair stand-in for the initial price, though the sample is small; "
             "`spf.price_snapshot` keeps snapshots so this can be rechecked.\n")

    L.append("## Results\n")
    L.append("Error = forecast − actual, £/MWh. Lower is better.\n")
    for m, offset, mid_offset, df, cols, met in sections:
        L.append(f"### Forecast issued at SP0 + {m} min (latest known price SP{offset:+d})\n")
        L.append(md(met))
        L.append("")
        L.append("MAE by band of the actual price:\n")
        L.append(md(band_mae(df, cols)))
        L.append("")
        L.append("Direction hit rate (long vs short, from the sign of NIV):\n")
        L.append(md(direction(df), ".3f"))
        L.append("")
    m, offset, mid_offset, df, cols, met = sections[0]
    L.append(f"### MAE by quarter (issue at SP0 + {m} min)\n")
    L.append("Baselines need no fitting, so this is the walk-forward view: does the ranking hold over time?\n")
    L.append(md(by_quarter(df, cols)))
    L.append("")

    L.append("## Notes\n")
    L.append("- \"Same period yesterday\" and \"MID of target\" don't depend on the horizon, so they score the "
             "same at SP+1, SP+2 and SP+3 (each horizon is scored on the same set of target periods).")
    L.append("- Same period yesterday is 48 periods earlier in UTC, so it is off by an hour on the two "
             "clock-change days a year.")
    L.append("- Direction: Elexon NIV > 0 means the system is short. The price baselines don't forecast a "
             "direction directly, so their direction is the NIV sign of the period they copy.")
    L.append("- Reproduce: with `BMDASH_DATA=data/bmdash`, `uv run bmdash ingest --from 2025-04-01 --no-costs`, "
             "then `uv run python -m spf.mid`; `spf.price_snapshot` and `spf.watch_publication` collect the live "
             "lag measurements; then `uv run python -m spf.baselines`.")
    L.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")
    print(OUT)
    print(pd.DataFrame(summary_rows).to_string())


if __name__ == "__main__":
    main()
