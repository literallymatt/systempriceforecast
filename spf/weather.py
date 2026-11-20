"""Weather forecasts as they were issued: NOAA GFS 0.5 degree runs from the public AWS archive.

Why GFS: every run is kept with its exact init time, and S3 keeps each file's Last-Modified time, so
the backtest can use only the forecasts that had been published at the forecast time. (Open-Meteo's
historical APIs stitch runs together without saying when each value became available, and its free
quota is per IP. NESO's own wind/solar/demand forecasts are already features; this is an independent
weather view on top of them.)

Runs at 00/06/12/18 UTC appear on S3 about 3 h 35 min after init (measured Nov 2026, f003-f015 within
a few minutes of each other, and the same in 2023-24 samples). A run counts as available at
max(init + RUN_LAG, Last-Modified of its last file + 5 min).

For each run, leads 3..18 h (3-hourly) of five fields are fetched by byte range using the .idx files,
and sampled at the nearest grid point to a fixed set of GB locations:

- t2m: 2 m temperature (K), at population centres;
- ws100: 100 m wind speed (m/s), at offshore and onshore wind clusters;
- tcc: total cloud cover (%), and dswrf: downward short-wave radiation (W/m2, mean over the 3 h
  before valid time, de-accumulated from GFS's 6-hourly averaging windows), at solar clusters.

Output: data/weather/gfs/<init date>.parquet, long format
(init, lead, valid_time, avail_time, point, var, value).

    uv run python -m spf.weather --from 2023-01-01
"""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import eccodes
import httpx
import numpy as np
import pandas as pd

BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
OUT = Path("data/weather/gfs")
RUNS = (0, 6, 12, 18)
LEADS = (3, 6, 9, 12, 15, 18)
RUN_LAG = timedelta(hours=4, minutes=30)

# idx "variable:level" -> short name
FIELDS = {
    "TMP:2 m above ground": "t2m",
    "UGRD:100 m above ground": "u100",
    "VGRD:100 m above ground": "v100",
    "TCDC:entire atmosphere": "tcc",
    "DSWRF:surface": "dswrf",
}

# name -> (lat, lon, group, weight). Weights: rough population (temp), installed GW (wind, solar).
POINTS = {
    # population centres
    "london": (51.5, -0.1, "pop", 9.0), "birmingham": (52.5, -1.9, "pop", 3.0),
    "manchester": (53.5, -2.25, "pop", 3.0), "leeds": (53.8, -1.55, "pop", 2.0),
    "glasgow": (55.86, -4.25, "pop", 2.0), "newcastle": (55.0, -1.6, "pop", 1.5),
    "bristol": (51.45, -2.6, "pop", 1.5), "nottingham": (52.95, -1.15, "pop", 1.5),
    "southampton": (50.9, -1.4, "pop", 1.5), "cardiff": (51.5, -3.2, "pop", 1.0),
    "edinburgh": (55.95, -3.2, "pop", 1.0), "norwich": (52.6, 1.3, "pop", 0.5),
    # offshore wind clusters
    "hornsea": (53.9, 1.9, "off", 2.6), "dogger": (54.75, 2.0, "off", 2.4),
    "east_anglia": (52.0, 2.0, "off", 2.5), "thames": (51.6, 1.4, "off", 1.5),
    "lincs": (53.4, 0.8, "off", 2.5), "moray": (58.2, -2.8, "off", 1.5),
    "forth": (56.4, -2.0, "off", 1.6), "irish_sea": (54.0, -3.6, "off", 2.5),
    "rampion": (50.7, -0.3, "off", 0.4),
    # onshore wind
    "s_scotland": (55.5, -3.8, "on", 3.0), "highlands": (57.5, -4.5, "on", 3.0),
    "wales": (52.3, -3.7, "on", 0.8), "n_england": (54.5, -2.5, "on", 0.8),
    # solar
    "cornwall": (50.4, -4.8, "sol", 1.0), "devon_som": (51.0, -3.2, "sol", 1.5),
    "wiltshire": (51.3, -1.9, "sol", 1.5), "kent": (51.2, 0.8, "sol", 1.0),
    "e_anglia_pv": (52.3, 1.0, "sol", 1.0), "oxon": (51.7, -1.3, "sol", 1.0),
    "lincs_pv": (53.0, -0.4, "sol", 1.0), "hants": (51.0, -1.3, "sol", 1.0),
}
VARS_FOR = {"pop": ("t2m", "tcc"), "off": ("ws100",), "on": ("ws100",), "sol": ("tcc", "dswrf", "t2m")}


def _grid_index(lat: float, lon: float) -> int:
    """Index into a 0.5 degree global grid, scanned north to south, lon 0..359.5."""
    i = int(round((90 - lat) / 0.5))
    j = int(round((lon % 360) / 0.5)) % 720
    return i * 720 + j


IDX = {p: _grid_index(la, lo) for p, (la, lo, _, _) in POINTS.items()}


def _url(init: datetime, lead: int, idx: bool = False) -> str:
    d, r = init.strftime("%Y%m%d"), f"{init.hour:02d}"
    return f"{BASE}/gfs.{d}/{r}/atmos/gfs.t{r}z.pgrb2.0p50.f{lead:03d}" + (".idx" if idx else "")


def _ranges(idx_text: str) -> list[tuple[str, str, int, int | None]]:
    """(short name, step text, start byte, end byte) for each wanted field."""
    lines = [ln.split(":") for ln in idx_text.strip().splitlines()]
    out = []
    for k, ln in enumerate(lines):
        key = f"{ln[3]}:{ln[4]}"
        if key not in FIELDS:
            continue
        step = ln[5]
        if FIELDS[key] == "tcc" and "ave" in step:
            continue  # keep the instantaneous cloud cover
        end = int(lines[k + 1][1]) - 1 if k + 1 < len(lines) else None
        out.append((FIELDS[key], step, int(ln[1]), end))
    return out


def _decode(buf: bytes) -> np.ndarray:
    h = eccodes.codes_new_from_message(buf)
    try:
        return eccodes.codes_get_values(h)
    finally:
        eccodes.codes_release(h)


async def _get(client: httpx.AsyncClient, url: str, headers: dict | None = None) -> httpx.Response:
    for attempt in range(6):
        try:
            r = await client.get(url, headers=headers)
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError("retry", request=r.request, response=r)
            return r
        except (httpx.HTTPError, asyncio.TimeoutError):
            if attempt == 5:
                raise
            await asyncio.sleep(1.5 * 2**attempt)
    raise RuntimeError("unreachable")


async def fetch_lead(client: httpx.AsyncClient, init: datetime, lead: int) -> tuple[dict, datetime] | None:
    r = await _get(client, _url(init, lead, idx=True))
    if r.status_code == 404:
        return None
    r.raise_for_status()
    modified = parsedate_to_datetime(r.headers["last-modified"])
    vals: dict[str, tuple[str, np.ndarray]] = {}
    for name, step, a, b in _ranges(r.text):
        rng = f"bytes={a}-{'' if b is None else b}"
        g = await _get(client, _url(init, lead), {"Range": rng})
        g.raise_for_status()
        v = _decode(g.content)
        vals[name] = (step, np.array([v[i] for i in IDX.values()]))
    return vals, modified


async def fetch_run(client: httpx.AsyncClient, init: datetime) -> pd.DataFrame:
    res = await asyncio.gather(*(fetch_lead(client, init, L) for L in LEADS))
    got = {L: x for L, x in zip(LEADS, res) if x is not None}
    if not got:
        return pd.DataFrame()
    avail = max(init + RUN_LAG, max(m for _, m in got.values()) + timedelta(minutes=5))
    names = list(IDX)
    rows = []
    for L, (vals, _) in got.items():
        per: dict[str, np.ndarray] = {}
        if "u100" in vals and "v100" in vals:
            per["ws100"] = np.hypot(vals["u100"][1], vals["v100"][1])
        for k in ("t2m", "tcc"):
            if k in vals:
                per[k] = vals[k][1]
        if "dswrf" in vals:
            step, v = vals["dswrf"]
            # GFS averages from the start of each 6 h window: f003 is 0-3, f006 is 0-6, f009 is 6-9, ...
            if L % 6 == 0 and (L - 3) in got and "dswrf" in got[L - 3][0]:
                per["dswrf"] = 2 * v - got[L - 3][0]["dswrf"][1]
            elif L % 6 == 3:
                per["dswrf"] = v
        for var, arr in per.items():
            for p, x in zip(names, arr):
                if var in VARS_FOR[POINTS[p][2]]:
                    rows.append((init, L, init + timedelta(hours=L), avail, p, var, float(x)))
    return pd.DataFrame(rows, columns=["init", "lead", "valid_time", "avail_time", "point", "var", "value"])


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, compression="zstd", index=False)
    tmp.replace(path)


async def fetch_day(client: httpx.AsyncClient, d: date) -> int:
    inits = [datetime(d.year, d.month, d.day, r, tzinfo=timezone.utc) for r in RUNS]
    now = datetime.now(timezone.utc)
    inits = [i for i in inits if i + RUN_LAG <= now]
    parts = await asyncio.gather(*(fetch_run(client, i) for i in inits))
    df = pd.concat([p for p in parts if len(p)] or [pd.DataFrame()], ignore_index=True)
    if len(df):
        _write(df, OUT / f"{d.isoformat()}.parquet")
    return len(df)


def _complete(path: Path) -> bool:
    if not path.exists():
        return False
    return pd.read_parquet(path, columns=["init"]).init.nunique() == len(RUNS)


async def run(a: date, b: date, concurrency: int = 6, force: bool = False) -> None:
    days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
    days = [d for d in days if force or not _complete(OUT / f"{d.isoformat()}.parquet")]
    print(f"weather: {len(days)} days to fetch", flush=True)
    t0, done, failed = time.time(), 0, []
    limits = httpx.Limits(max_connections=concurrency * 24, max_keepalive_connections=concurrency * 24)
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15), limits=limits) as client:
        sem = asyncio.Semaphore(concurrency)

        async def one(d: date) -> None:
            nonlocal done
            async with sem:
                try:
                    n = await fetch_day(client, d)
                except Exception as e:  # noqa: BLE001
                    failed.append(f"{d}: {e!r}")
                    return
                done += 1
                if done % 20 == 0:
                    print(f"  {done}/{len(days)} ({time.time() - t0:.0f}s) last {d} {n} rows", flush=True)

        await asyncio.gather(*(one(d) for d in days))
    print(f"done {done}/{len(days)} in {time.time() - t0:.0f}s; {len(failed)} failed", flush=True)
    for f in failed[:20]:
        print("  FAILED", f)


# ---- features ---------------------------------------------------------------------------------------

def _power_curve(ws: pd.Series) -> pd.Series:
    """Crude normalised turbine output: cut-in 3 m/s, rated 12 m/s, cut-out 25 m/s."""
    p = ((ws**3 - 27) / (12**3 - 27)).clip(0, 1)
    return p.where(ws < 25, 0.0)


def load() -> pd.DataFrame:
    """Regional aggregates per (init, valid_time): pop-weighted temperature, wind speed and power proxy per
    cluster group, solar radiation and cloud over the solar clusters."""
    files = sorted(OUT.glob("*.parquet"))
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    for c in ("init", "valid_time", "avail_time"):
        df[c] = pd.to_datetime(df[c], utc=True).dt.as_unit("ns")
    # dswrf is the mean over the 3 h before valid_time: place it at the middle of that window
    sw = df["var"] == "dswrf"
    df.loc[sw, "valid_time"] = df.loc[sw, "valid_time"] - pd.Timedelta(minutes=90)
    df["group"] = df.point.map(lambda p: POINTS[p][2])
    df["w"] = df.point.map(lambda p: POINTS[p][3])
    ws = df[df["var"] == "ws100"].copy()
    ws_pw = ws.assign(var="wpow", value=_power_curve(ws.value))
    df = pd.concat([df, ws_pw], ignore_index=True)
    df["wv"] = df.value * df.w
    g = df.groupby(["init", "valid_time", "avail_time", "group", "var"])[["wv", "w"]].sum()
    agg = (g.wv / g.w).rename("value").reset_index()
    agg["name"] = "wx_" + agg.group + "_" + agg["var"]
    wide = agg.pivot_table(index=["init", "valid_time", "avail_time"], columns="name", values="value").reset_index()
    wide.columns.name = None
    if "wx_pop_t2m" in wide:
        wide["wx_pop_t2m"] -= 273.15
    if "wx_sol_t2m" in wide:
        wide["wx_sol_t2m"] -= 273.15
    # all wind as one capacity-weighted proxy
    if {"wx_off_wpow", "wx_on_wpow"} <= set(wide.columns):
        w_off = sum(v[3] for v in POINTS.values() if v[2] == "off")
        w_on = sum(v[3] for v in POINTS.values() if v[2] == "on")
        wide["wx_wind_cf"] = (wide.wx_off_wpow * w_off + wide.wx_on_wpow * w_on) / (w_off + w_on)
    return wide.sort_values(["init", "valid_time"])


def _interp_run(run: pd.DataFrame, times: pd.DatetimeIndex, cols: list[str]) -> pd.DataFrame:
    r = run.set_index("valid_time")[cols]
    r = r[~r.index.duplicated()]
    idx = r.index.union(times)
    return r.reindex(idx).interpolate(method="time", limit_area="inside").reindex(times)


def features(sp0: pd.DatetimeIndex, tau: pd.Series, horizons=(1, 2, 3)) -> pd.DataFrame:
    """For each forecast (sp0, tau): the latest GFS run with avail_time <= tau, interpolated to the middle of
    SP+h, plus the change from the previous run (a sign of forecast uncertainty) and the run's age."""
    wide = load()
    cols = [c for c in wide.columns if c.startswith("wx_")]
    runs = wide.groupby("init").avail_time.first().sort_values()
    out = pd.DataFrame(index=sp0)
    # which run is current at each tau, and the one before it
    pos = np.searchsorted(runs.to_numpy(), tau.to_numpy(), side="right") - 1
    run_list = runs.index.to_list()
    cur = pd.Series([run_list[p] if p >= 0 else pd.NaT for p in pos], index=sp0)
    prev = pd.Series([run_list[p - 1] if p >= 1 else pd.NaT for p in pos], index=sp0)
    out["wx_age_h"] = (tau - cur).dt.total_seconds() / 3600
    by_run = {k: v for k, v in wide.groupby("init")}
    hh = pd.Timedelta(minutes=30)
    res = {f"{c}_h{h}": np.full(len(sp0), np.nan) for c in cols for h in horizons}
    res |= {f"{c}_rev_h{h}": np.full(len(sp0), np.nan) for c in cols for h in (1,)}
    for which, ser in (("cur", cur), ("prev", prev)):
        for init, rows in pd.Series(np.arange(len(sp0)), index=ser.to_numpy()).groupby(level=0):
            if pd.isna(init) or init not in by_run:
                continue
            ii = rows.to_numpy()
            for h in horizons if which == "cur" else (1,):
                times = pd.DatetimeIndex(sp0[ii] + h * hh + hh / 2)
                v = _interp_run(by_run[init], times, cols)
                for c in cols:
                    if which == "cur":
                        res[f"{c}_h{h}"][ii] = v[c].to_numpy()
                    else:
                        res[f"{c}_rev_h{h}"][ii] = v[c].to_numpy()
    for c in cols:
        res[f"{c}_rev_h1"] = res[f"{c}_h1"] - res[f"{c}_rev_h1"]
    for k, v in res.items():
        out[k] = v
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2023-01-01")
    p.add_argument("--to", dest="end")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    end = date.fromisoformat(a.end) if a.end else datetime.now(timezone.utc).date()
    asyncio.run(run(date.fromisoformat(a.start), end, a.concurrency, a.force))
