"""N2EX GB day-ahead hourly auction prices (Nord Pool), from the NESO data portal.

The auction for day D clears late morning on D-1 (UK time), so every hourly price of D is known from
D-1 12:00 UTC onwards (DA_KNOWN). Delivery periods are labelled in GMT, and every day has 24 rows (clock
change days included), so they are UTC hours. NESO gives no publication time: each fetch records
`fetched_at`, and the known-by rule above is used instead.

Output: data/neso/n2ex_da.parquet (start_time UTC hour, price GBP/MWh, fetched_at)

    uv run python -m spf.dayahead
"""
from __future__ import annotations

import io
from pathlib import Path

import httpx
import pandas as pd

URL = "https://api.neso.energy/datastore/dump/4f27eea5-7038-4f73-9740-e3e4ad47c26a"
OUT = Path("data/neso/n2ex_da.parquet")
DA_KNOWN = pd.Timedelta(hours=-12)  # day D is known from D 00:00 UTC + DA_KNOWN, i.e. D-1 12:00 UTC


def fetch() -> pd.DataFrame:
    r = httpx.get(URL, timeout=120, follow_redirects=True)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    hour = df["Delivery Period"].str.strip().str[:5]
    df["start_time"] = pd.to_datetime(df.Date.str.strip() + " " + hour, utc=True)
    out = df[["start_time", "Price"]].rename(columns={"Price": "price"}).drop_duplicates("start_time", keep="last")
    out["fetched_at"] = pd.Timestamp.now(tz="UTC")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.sort_values("start_time").to_parquet(OUT, index=False)
    return out


def load() -> pd.DataFrame:
    df = pd.read_parquet(OUT)
    df["start_time"] = pd.to_datetime(df.start_time, utc=True).dt.as_unit("ns")
    return df.set_index("start_time").sort_index()


def features(sp0: pd.DatetimeIndex, tau: pd.Series, horizons=(0, 1, 2, 3)) -> pd.DataFrame:
    """Day-ahead price of the hour containing SP+h, the daily mean and the price change into SP+1's hour."""
    da = load().price
    out = pd.DataFrame(index=sp0)
    hh = pd.Timedelta(minutes=30)
    for h in horizons:
        t = (sp0 + h * hh).floor("h")
        v = da.reindex(t).to_numpy()
        known = (t.floor("D") + DA_KNOWN) <= tau.to_numpy()
        out[f"da_h{h}"] = pd.Series(v, index=sp0).where(known)
    day = da.groupby(da.index.floor("D")).mean()
    out["da_daymean"] = day.reindex((sp0 + hh).floor("D")).to_numpy()
    out["da_d1"] = out.da_h1 - out.da_h0
    return out


if __name__ == "__main__":
    d = fetch()
    print(OUT, len(d), d.start_time.min(), d.start_time.max())
