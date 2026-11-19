"""Ingest the extra Elexon datasets this project needs, keeping every published version.

Two kinds:

- Forecast / real-time datasets queried by *publish time* (IMBALNGC, MELNGC, NDF, TSDF, WINDFOR,
  LOLPDRM, INDO, FUELINST). One file per publish day in data/elexon/<DS>/<YYYY-MM-DD>.parquet, every
  version kept with its `publishTime`, so features can be built "as of" any moment.
- Per-unit BM data for a settlement day (PN, BOD), filtered to the units with an acceptance that day
  (bmdash `acceptances`), in data/elexon/PN|BOD/<settlement date>.parquet. Both are final at gate
  closure (one hour before the period), which is what makes them usable for SP0..SP+2 at forecast time.

    uv run python -m spf.ingest forecasts --from 2024-01-01
    uv run python -m spf.ingest bm --from 2024-01-01
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("BMDASH_DATA", str(Path("data/bmdash").resolve()))

import pandas as pd  # noqa: E402
from bmdash.elexon import Elexon  # noqa: E402

ROOT = Path("data/elexon")
LONDON = ZoneInfo("Europe/London")

# dataset -> extra query params. National boundary only where the dataset is split by boundary.
FORECASTS = {
    "IMBALNGC": {"boundary": "N"},  # NESO indicated imbalance (indicated generation - indicated demand)
    "MELNGC": {"boundary": "N"},    # NESO indicated margin
    "TSDF": {"boundary": "N"},      # transmission system demand forecast
    "NDF": {},                      # national demand forecast
    "WINDFOR": {},                  # wind generation forecast
    "LOLPDRM": {},                  # loss-of-load probability and de-rated margin
    "INDO": {},                     # initial national demand outturn (per period, published just after)
    "FUELINST": {},                 # generation by fuel, 5-minute
    "DGWS": {},                     # wind and solar forecasts (day-ahead and intraday), B1440
    "AGWS": {},                     # wind and solar outturn, B1630 (published ~2 h after)
    "FREQ": {},                     # system frequency, 15-second (queried by measurement time, ~1 min delay)
    "REMIT": {},                    # outage messages, every revision with its publishTime
    "SYSWARN": {},                  # NESO system warnings (margin notices, SO-SO trade prices, ...)
}


NO_STREAM = {"INDO"}  # no /stream endpoint (a 404 reads as "no data")


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, compression="zstd", index=False)
    tmp.replace(path)


def _days(a: date, b: date) -> list[date]:
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _stale(path: Path, d: date) -> bool:
    """Fetch if missing, or if the day was still in progress (or very recent) when last fetched."""
    if not path.exists():
        return True
    fetched = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return fetched < datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=2)


async def forecast_day(api: Elexon, ds: str, d: date) -> int:
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    params = {**FORECASTS[ds], "publishDateTimeFrom": _iso(start), "publishDateTimeTo": _iso(start + timedelta(days=1))}
    if ds == "FREQ":
        rows = await api.get("/datasets/FREQ/stream", {"measurementDateTimeFrom": _iso(start),
                                                       "measurementDateTimeTo": _iso(start + timedelta(days=1))})
        df = pd.DataFrame(rows)
        if len(df):
            t = pd.to_datetime(df.measurementTime, utc=True)
            df = df[(t >= start) & (t < start + timedelta(days=1))]
        df["fetched_at"] = pd.Timestamp.now(tz="UTC")
        _write(df, ROOT / ds / f"{d.isoformat()}.parquet")
        return len(df)
    if ds in NO_STREAM:
        rows = await api.get(f"/datasets/{ds}", {**params, "format": "json"})
    else:
        rows = await api.get(f"/datasets/{ds}/stream", params)
    df = pd.DataFrame(rows)
    if len(df):
        pub = pd.to_datetime(df.publishTime, utc=True)
        df = df[(pub >= start) & (pub < start + timedelta(days=1))]  # the range is inclusive at both ends
        for c in ("outageProfile",):  # nested lists (REMIT): keep as JSON text
            if c in df.columns:
                df[c] = df[c].map(lambda x: json.dumps(x) if isinstance(x, list) else None)
    df["fetched_at"] = pd.Timestamp.now(tz="UTC")
    _write(df, ROOT / ds / f"{d.isoformat()}.parquet")
    return len(df)


def _bounds(d: date) -> tuple[datetime, datetime]:
    s = datetime(d.year, d.month, d.day, tzinfo=LONDON).astimezone(timezone.utc)
    n = d + timedelta(days=1)
    return s, datetime(n.year, n.month, n.day, tzinfo=LONDON).astimezone(timezone.utc)


_UNITS_CACHE: dict[date, set[str]] = {}


def _accepted_units(d: date) -> set[str]:
    p = Path(os.environ["BMDASH_DATA"]) / "acceptances" / f"{d.isoformat()}.parquet"
    if not p.exists():
        return set()
    a = pd.read_parquet(p, columns=["ngc_bm_unit"])
    return set(a.ngc_bm_unit.dropna())


async def bm_day(api: Elexon, ds: str, d: date) -> int:
    units = _accepted_units(d)
    s, e = _bounds(d)
    chunks = []
    t = s
    while t < e:  # 6-hour requests keep each response around 10 MB
        u = min(t + timedelta(hours=6), e)
        chunks.append(api.get(f"/datasets/{ds}/stream", {"from": _iso(t), "to": _iso(u)}))
        t = u
    parts = await asyncio.gather(*chunks)
    df = pd.DataFrame([r for p in parts for r in p])
    if len(df):
        df = df[(df.settlementDate == d.isoformat()) & df.nationalGridBmUnit.isin(units)]
        df = df.drop(columns=[c for c in ("dataset",) if c in df.columns]).drop_duplicates()
    _write(df, ROOT / ds / f"{d.isoformat()}.parquet")
    return len(df)


async def run(kind: str, a: date, b: date, datasets: list[str], concurrency: int) -> None:
    jobs = []
    for ds in datasets:
        for d in _days(a, b):
            path = ROOT / ds / f"{d.isoformat()}.parquet"
            if kind == "bm" and not _accepted_units(d):
                continue  # bmdash acceptances for this day not held yet
            if _stale(path, d):
                jobs.append((ds, d))
    print(f"{kind}: {len(jobs)} dataset-days to fetch", flush=True)
    t0, done, failed = time.time(), 0, []
    async with Elexon(concurrency=concurrency) as api:
        sem = asyncio.Semaphore(concurrency)

        async def one(ds: str, d: date) -> None:
            nonlocal done
            async with sem:
                try:
                    n = await (forecast_day(api, ds, d) if kind == "forecasts" else bm_day(api, ds, d))
                except Exception as e:
                    failed.append(f"{ds} {d}: {e}")
                    return
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(jobs)} ({time.time() - t0:.0f}s) last {ds} {d} {n} rows", flush=True)

        await asyncio.gather(*(one(ds, d) for ds, d in jobs))
    print(f"done {done}/{len(jobs)} in {time.time() - t0:.0f}s; {len(failed)} failed", flush=True)
    for f in failed[:20]:
        print("  FAILED", f)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("kind", choices=["forecasts", "bm"])
    p.add_argument("--from", dest="start", default="2024-01-01")
    p.add_argument("--to", dest="end")
    p.add_argument("--datasets", nargs="*")
    p.add_argument("--concurrency", type=int, default=12)
    a = p.parse_args()
    end = date.fromisoformat(a.end) if a.end else datetime.now(timezone.utc).date()
    ds = a.datasets or (list(FORECASTS) if a.kind == "forecasts" else ["PN", "BOD"])
    asyncio.run(run(a.kind, date.fromisoformat(a.start), end, ds, a.concurrency))
