"""ENTSO-E Transparency Platform: neighbours' day-ahead load and wind/solar forecasts, and Irish SEM prices.

Usage:
    uv run python -m spf.entsoe --from 2023-01-01

The token is read from $ENTSOE_API_TOKEN, else from data/secrets/entsoe_token (gitignored). It is
never printed or stored with the data.

Datasets (one request per zone per quarter):

| kind | ENTSO-E document | zones |
|---|---|---|
| load | A65 total load, day-ahead forecast (processType A01) | FR NL BE DE-LU NO2 DK1 IE (Republic of Ireland) |
| ws | A69 wind and solar forecast, day-ahead (A01): B16 solar, B18 offshore, B19 onshore wind | same |
| price | A44 day-ahead prices | IE (SEM), which Energy-Charts does not offer |

Known-at rules. The API serves only the latest revision of each document and no publication time, so
each fetch records `fetched_at` and fixed rules decide what was known at tau:
- load and SEM price for CET day D: from D-1 14:00 CET. Regulation 543/2013 has the day-ahead load
  forecast out 2 h before day-ahead gate closure (10:00 CET); SEM's day-ahead auction results appear
  around 12:00 CET on D-1.
- wind/solar for CET day D: from D-1 18:00 CET, the regulation's deadline.
Targets are at most two hours ahead, so these rules only bind around midnight CET.

Interconnector flows follow the GB-neighbour price spread and the neighbours' residual load, and flows
change at hour boundaries.

Output: data/entsoe/{load,ws,price}.parquet (start_time UTC, zone, series, value, fetched_at)
"""
from __future__ import annotations

import argparse
import os
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

URL = "https://web-api.tp.entsoe.eu/api"
OUT = Path("data/entsoe")
TOKEN_FILE = Path("data/secrets/entsoe_token")
ZONES = {
    "fr": "10YFR-RTE------C",
    "nl": "10YNL----------L",
    "be": "10YBE----------2",
    "delu": "10Y1001A1001A82H",
    "no2": "10YNO-2--------T",
    "dk1": "10YDK-1--------W",
    "ie": "10YIE-1001A00010",  # Republic of Ireland: load and wind (the SEM zone has no load forecast)
}
SEM = "10Y1001A1001A59C"      # Single Electricity Market (Ireland and Northern Ireland): prices
CONT = ("fr", "nl", "be", "delu")           # GB's continental neighbours by interconnector capacity
LOAD_KNOWN = pd.Timedelta(hours=-10)        # CET day D known from D 00:00 CET - 10 h = D-1 14:00 CET
WS_KNOWN = pd.Timedelta(hours=-6)           # D-1 18:00 CET
EUR_PER_GBP = 1.16                          # as spf.eu_dayahead
HH = pd.Timedelta(minutes=30)
RES = {"PT15M": 15, "PT30M": 30, "PT60M": 60}


def _token() -> str:
    t = os.environ.get("ENTSOE_API_TOKEN") or (TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
    if not t:
        raise RuntimeError(f"no ENTSO-E token: set ENTSOE_API_TOKEN or write it to {TOKEN_FILE}")
    return t


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse(xml: bytes) -> pd.DataFrame:
    """(start_time, series, value) rows from any ENTSO-E time-series document. Curve type A03 omits
    positions whose value repeats the previous one: they are filled forward to the period end."""
    root = ET.fromstring(xml)
    if _local(root.tag).startswith("Acknowledgement"):
        return pd.DataFrame(columns=["start_time", "series", "value"])
    rows = []
    for ts in root.iter():
        if _local(ts.tag) != "TimeSeries":
            continue
        series = "value"
        for el in ts.iter():
            if _local(el.tag) == "psrType":
                series = el.text
        for per in ts:
            if _local(per.tag) != "Period":
                continue
            start = end = None
            step = 60
            pts: dict[int, float] = {}
            for el in per.iter():
                t = _local(el.tag)
                if t == "start":
                    start = pd.Timestamp(el.text)
                elif t == "end":
                    end = pd.Timestamp(el.text)
                elif t == "resolution":
                    step = RES.get(el.text, 60)
                elif t == "Point":
                    kv = {_local(c.tag): c.text for c in el}
                    v = kv.get("quantity", kv.get("price.amount"))
                    pts[int(kv["position"])] = float(v)
            if start is None or not pts:
                continue
            n = int((end - start) / pd.Timedelta(minutes=step)) if end is not None else max(pts)
            last = None
            for pos in range(1, n + 1):
                last = pts.get(pos, last)
                if last is not None:
                    rows.append((start + pd.Timedelta(minutes=step * (pos - 1)), series, last))
    df = pd.DataFrame(rows, columns=["start_time", "series", "value"])
    return df.drop_duplicates(["start_time", "series"], keep="last")


def _params(kind: str, eic: str) -> dict:
    if kind == "load":
        return {"documentType": "A65", "processType": "A01", "outBiddingZone_Domain": eic}
    if kind == "ws":
        return {"documentType": "A69", "processType": "A01", "in_Domain": eic}
    return {"documentType": "A44", "in_Domain": eic, "out_Domain": eic}


def _get(c: httpx.Client, params: dict) -> bytes:
    for wait in (0, 10, 30, 60, 120):
        time.sleep(wait or 0.3)
        try:
            r = c.get(URL, params=params)
        except httpx.HTTPError:
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            continue
        if r.status_code == 400 and b"No matching data" in r.content:
            return b"<Acknowledgement_MarketDocument/>"
        r.raise_for_status()
        return r.content
    raise RuntimeError(f"ENTSO-E request kept failing: {params.get('documentType')}")


def fetch(start: date, end: date, kinds=("load", "ws", "price")) -> None:
    """Fetch [start, end) by quarter for every zone and merge into the stored files."""
    tok = _token()
    with httpx.Client(timeout=180) as c:
        for kind in kinds:
            zones = {"ie": SEM} if kind == "price" else ZONES
            parts = []
            for z, eic in zones.items():
                a = start
                while a < end:
                    b = min(a + timedelta(days=92), end)
                    p = _params(kind, eic) | {"securityToken": tok,
                                              "periodStart": a.strftime("%Y%m%d0000"),
                                              "periodEnd": b.strftime("%Y%m%d0000")}
                    try:
                        df = parse(_get(c, p))
                    except Exception as ex:  # noqa: BLE001
                        print(f"  {kind} {z} {a}: FAILED {type(ex).__name__}", flush=True)
                        df = pd.DataFrame()
                    if len(df):
                        parts.append(df.assign(zone=z))
                    a = b
                print(f"  {kind} {z}: {sum(len(x) for x in parts if x.zone.iat[0] == z)} rows", flush=True)
            if not parts:
                continue
            new = pd.concat(parts, ignore_index=True)
            new["fetched_at"] = pd.Timestamp.now(tz="UTC")
            path = OUT / f"{kind}.parquet"
            if path.exists():
                old = pd.read_parquet(path)
                new = pd.concat([old, new]).drop_duplicates(["start_time", "zone", "series"], keep="last")
            OUT.mkdir(parents=True, exist_ok=True)
            new.sort_values(["zone", "series", "start_time"]).to_parquet(path, index=False)


def _half_hourly(kind: str) -> pd.DataFrame:
    """Wide frame (half-hour start index) with one column per zone and series, e.g. fr_value, fr_B16."""
    path = OUT / f"{kind}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df["start_time"] = pd.to_datetime(df.start_time, utc=True).dt.as_unit("ns")
    df["col"] = df.zone + "_" + df.series
    wide = df.pivot_table(index="start_time", columns="col", values="value")
    fine = pd.date_range(wide.index.min().floor("h"), wide.index.max().ceil("h"), freq="15min")
    q = wide.reindex(wide.index.union(fine)).ffill(limit=3)  # hourly and 30-min data onto 15 min
    return q.resample("30min").mean()


def features(sp0: pd.DatetimeIndex, tau: pd.Series, gb_da: pd.DataFrame | None = None) -> pd.DataFrame:
    """ent_ features for SP0..SP+3 as known at tau: residual load (load - wind - solar) by zone, its step
    into the next period, continental totals, and the SEM price and its spread to the GB day-ahead price."""
    load, ws, price = _half_hourly("load"), _half_hourly("ws"), _half_hourly("price")
    out = pd.DataFrame(index=sp0)
    if load.empty or ws.empty:
        print("no ENTSO-E load/wind data: skipping ent_ features")
        return out
    tau_np = tau.to_numpy()
    res, wind = {}, {}
    for h in (0, 1, 2, 3):
        t = sp0 + h * HH
        day_cet = t.tz_convert("Europe/Brussels").normalize().tz_convert("UTC")
        k_load = (day_cet + LOAD_KNOWN).to_numpy() <= tau_np
        k_ws = (day_cet + WS_KNOWN).to_numpy() <= tau_np
        L, W = load.reindex(t), ws.reindex(t)
        res[h], wind[h] = {}, {}
        for z in ZONES:
            lz = L.get(f"{z}_value")
            if lz is None:
                continue
            w = sum(W[c].fillna(0) for c in (f"{z}_B18", f"{z}_B19") if c in W)
            s = W[f"{z}_B16"].fillna(0) if f"{z}_B16" in W else 0
            r = (lz - w - s).where(k_load & k_ws).to_numpy()
            res[h][z] = r
            wind[h][z] = (w if isinstance(w, pd.Series) else pd.Series(0.0, index=t)).where(k_ws).to_numpy()
        if "ie_value" in price:
            res[h]["ie_price"] = price["ie_value"].reindex(t).where(k_load).to_numpy()
    for z in [z for z in ZONES if z in res[1]]:
        out[f"ent_res_{z}_h1"] = res[1][z]
        out[f"ent_res_{z}_d1"] = res[1][z] - res[0][z]
    cont = [z for z in CONT if z in res[1]]
    if cont:
        tot = {h: np.sum([res[h][z] for z in cont], axis=0) for h in res}
        out["ent_res_cont_h1"] = tot[1]
        out["ent_res_cont_h3"] = tot[3]
        out["ent_res_cont_d1"] = tot[1] - tot[0]
        out["ent_res_cont_d3"] = tot[3] - tot[2]
        out["ent_wind_cont_h1"] = np.sum([wind[1][z] for z in cont], axis=0)
        out["ent_wind_cont_d1"] = out.ent_wind_cont_h1 - np.sum([wind[0][z] for z in cont], axis=0)
    if "ie_price" in res[1]:
        for h in (1, 3):
            out[f"ent_ie_price_h{h}"] = res[h]["ie_price"]
        out["ent_ie_price_d1"] = res[1]["ie_price"] - res[0]["ie_price"]
        if gb_da is not None and "da_h1" in gb_da:
            out["ent_ie_spread_h1"] = gb_da.da_h1.to_numpy() * EUR_PER_GBP - res[1]["ie_price"]
    return out.astype(float).replace([np.inf, -np.inf], np.nan)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2023-01-01")
    p.add_argument("--to", dest="end")
    p.add_argument("--kinds", nargs="*", default=["load", "ws", "price"])
    a = p.parse_args()
    end = date.fromisoformat(a.end) if a.end else datetime.now(timezone.utc).date() + timedelta(days=2)
    fetch(date.fromisoformat(a.start), end, tuple(a.kinds))
    for k in a.kinds:
        f = OUT / f"{k}.parquet"
        if f.exists():
            d = pd.read_parquet(f)
            print(k, len(d))
            print(d.groupby(["zone", "series"]).start_time.agg(["min", "max", "count"]).to_string())
