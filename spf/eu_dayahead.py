"""Continental day-ahead prices (SDAC auction) for GB's interconnected neighbours, from Energy-Charts.

Energy-Charts (Fraunhofer ISE, api.energy-charts.info) republishes the day-ahead auction prices for each
bidding zone: hourly until 30 Sep 2025, 15-minute from 1 Oct 2025. Zones: FR, NL, BE, DE-LU, NO2, DK1,
the far ends of IFA/IFA2/ElecLink, BritNed, Nemo, NeuConnect, North Sea Link and Viking. Ireland (SEM) is
not offered.

Interconnector flows follow the spread between GB and continental day-ahead prices, and flows change
at hour (and now quarter-hour) boundaries, a known source of imbalance.

Known-at rule: the SDAC auction for delivery day D (CET) publishes results around 12:45-13:00 CET on
D-1. Every price of day D is treated as known from D-1 14:00 CET (EU_KNOWN). Energy-Charts gives no
publication time: each fetch records `fetched_at`, and the rule above is used instead.

Prices are in EUR/MWh. The spread to GB uses a fixed EUR_PER_GBP: the rate moved between about 1.13 and
1.21 over 2023-2026, so the spread carries a few percent of level error. That is small against the
spreads of tens of EUR that move flows.

Output: data/eu/dayahead.parquet (start_time UTC, zone, price_eur, fetched_at)

    uv run python -m spf.eu_dayahead --from 2023-01-01
"""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

URL = "https://api.energy-charts.info/price"
OUT = Path("data/eu/dayahead.parquet")
ZONES = ("FR", "NL", "BE", "DE-LU", "NO2", "DK1")
EU_KNOWN = pd.Timedelta(hours=-10)  # day D (CET) known from D 00:00 CET - 10 h = D-1 14:00 CET
EUR_PER_GBP = 1.16
HH = pd.Timedelta(minutes=30)
PAUSE = 5  # seconds between API calls


def _col(z: str) -> str:
    return z.lower().replace("-", "")


def _get(c: httpx.Client, params: dict) -> httpx.Response:
    """GET with a pause between calls and backoff on 429: the API rate-limits bursts."""
    for wait in (0, 30, 60, 120, 240, 480):
        time.sleep(wait or PAUSE)
        r = c.get(URL, params=params)
        if r.status_code != 429:
            r.raise_for_status()
            return r
    r.raise_for_status()
    return r


def fetch(start: date, end: date) -> pd.DataFrame:
    """Fetch [start, end] for every zone a year at a time and merge into the stored file."""
    parts = []
    with httpx.Client(timeout=120) as c:
        for z in ZONES:
            y0 = start
            while y0 <= end:
                y1 = min(date(y0.year, 12, 31), end)
                r = _get(c, {"bzn": z, "start": y0.isoformat(), "end": y1.isoformat()})
                d = r.json()
                if d.get("unix_seconds"):
                    parts.append(pd.DataFrame({
                        "start_time": pd.to_datetime(d["unix_seconds"], unit="s", utc=True),
                        "zone": z, "price_eur": pd.to_numeric(pd.Series(d["price"]), errors="coerce")}))
                y0 = date(y1.year + 1, 1, 1)
    new = pd.concat(parts, ignore_index=True)
    new["fetched_at"] = pd.Timestamp.now(tz="UTC")
    if OUT.exists():  # keep earlier fetches' rows for times this fetch did not cover
        old = pd.read_parquet(OUT)
        new = pd.concat([old, new]).drop_duplicates(["start_time", "zone"], keep="last")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    new = new.dropna(subset=["price_eur"]).sort_values(["zone", "start_time"])
    new.to_parquet(OUT, index=False)
    return new


def half_hourly() -> pd.DataFrame:
    """Price per half hour (start time index) and zone column: hourly prices repeated, 15-min ones averaged."""
    df = pd.read_parquet(OUT)
    df["start_time"] = pd.to_datetime(df.start_time, utc=True).dt.as_unit("ns")
    wide = df.pivot_table(index="start_time", columns="zone", values="price_eur")
    idx = pd.date_range(wide.index.min().floor("h"), wide.index.max().ceil("h"), freq="30min")
    # resolution changes over time: forward-fill within the hour for hourly data, then average to 30 min
    q = wide.reindex(wide.index.union(pd.date_range(idx[0], idx[-1], freq="15min"))).ffill(limit=3)
    out = q.resample("30min").mean().reindex(idx)
    out.columns = [_col(c) for c in out.columns]
    return out


def features(sp0: pd.DatetimeIndex, tau: pd.Series, gb_da: pd.DataFrame | None = None) -> pd.DataFrame:
    """Neighbour prices for SP+1..SP+3 as known at tau, and their spread to the GB day-ahead price.

    gb_da: the da_h0..da_h3 columns from spf.dayahead (GBP/MWh), if available.
    """
    hh = half_hourly()
    zs = list(hh.columns)
    out = pd.DataFrame(index=sp0)
    tau_np = tau.to_numpy()
    means = {}
    for h in (0, 1, 2, 3):
        t = sp0 + h * HH
        day_cet = t.tz_convert("Europe/Brussels").normalize().tz_convert("UTC")
        known = (day_cet + EU_KNOWN).to_numpy() <= tau_np
        v = hh.reindex(t)
        v = v.where(np.repeat(known[:, None], v.shape[1], axis=1))
        v.index = sp0
        means[h] = v.mean(axis=1)
        if h in (1, 3):
            for z in zs:
                out[f"eu_{z}_h{h}"] = v[z]
            out[f"eu_mean_h{h}"] = means[h]
            out[f"eu_range_h{h}"] = v.max(axis=1) - v.min(axis=1)
    out["eu_mean_d1"] = means[1] - means[0]   # continental price step into SP+1: flow ramps
    out["eu_mean_d3"] = means[3] - means[2]
    if gb_da is not None:
        for h in (1, 2, 3):
            if f"da_h{h}" in gb_da:
                gb_eur = gb_da[f"da_h{h}"] * EUR_PER_GBP
                out[f"eu_spread_h{h}"] = gb_eur - means[h]          # > 0: GB dearer, imports expected
        if "da_h0" in gb_da and "eu_spread_h1" in out:
            out["eu_spread_d1"] = out.eu_spread_h1 - (gb_da.da_h0 * EUR_PER_GBP - means[0])
        for z in ("fr", "nl", "no2"):
            if f"eu_{z}_h1" in out and "da_h1" in gb_da:
                out[f"eu_spread_{z}_h1"] = gb_da.da_h1 * EUR_PER_GBP - out[f"eu_{z}_h1"]
    return out.astype(float).replace([np.inf, -np.inf], np.nan)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2023-01-01")
    p.add_argument("--to", dest="end")
    a = p.parse_args()
    end = date.fromisoformat(a.end) if a.end else datetime.now(timezone.utc).date() + timedelta(days=1)
    d = fetch(date.fromisoformat(a.start), end)
    print(OUT, len(d), d.start_time.min(), d.start_time.max())
    print(d.groupby("zone").start_time.agg(["min", "max", "count"]))
