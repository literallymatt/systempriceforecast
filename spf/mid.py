"""Market Index Data (MID) ingest: one Parquet file per month in data/mid/.

Elexon's MID rows carry no publication time, so each row records `fetched_at` instead. Watching the
live feed (spf.watch_publication) shows a period's MID first appears while that period is under way,
so MID for SP+1..SP+3 is NOT known at forecast time.

    uv run python -m spf.mid --from 2025-04-01
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from bmdash.elexon import Elexon

DATA = Path("data/mid")
SCHEMA = pa.schema([
    ("start_time", pa.timestamp("us", tz="UTC")), ("settlement_date", pa.date32()), ("sp", pa.int16()),
    ("provider", pa.string()), ("price", pa.float64()), ("volume", pa.float64()),
    ("fetched_at", pa.timestamp("us", tz="UTC")),
])


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


async def fetch_month(api: Elexon, first: date) -> pa.Table:
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc) - timedelta(hours=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc)
    rows, t = [], start
    while t < end:  # weekly requests: the stream endpoint caps the range
        u = min(t + timedelta(days=7), end)
        rows += await api.get("/datasets/MID/stream", {"from": t.strftime("%Y-%m-%dT%H:%MZ"),
                                                       "to": u.strftime("%Y-%m-%dT%H:%MZ")})
        t = u
    fetched = datetime.now(timezone.utc)
    dedup = {}
    for r in rows:
        d = date.fromisoformat(r["settlementDate"])
        if (d.year, d.month) == (first.year, first.month):
            dedup[(r["startTime"], r["dataProvider"])] = r
    recs = sorted(dedup.values(), key=lambda r: (r["startTime"], r["dataProvider"]))
    return pa.table({
        "start_time": pa.array([_ts(r["startTime"]) for r in recs], SCHEMA.field("start_time").type),
        "settlement_date": pa.array([date.fromisoformat(r["settlementDate"]) for r in recs], pa.date32()),
        "sp": pa.array([r["settlementPeriod"] for r in recs], pa.int16()),
        "provider": pa.array([r["dataProvider"] for r in recs], pa.string()),
        "price": pa.array([r.get("price") for r in recs], pa.float64()),
        "volume": pa.array([r.get("volume") for r in recs], pa.float64()),
        "fetched_at": pa.array([fetched] * len(recs), SCHEMA.field("fetched_at").type),
    }, schema=SCHEMA)


async def run(start: date, end: date) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    months, m = [], start.replace(day=1)
    while m <= end:
        months.append(m)
        m = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
    current = datetime.now(timezone.utc).date().replace(day=1)
    async with Elexon(concurrency=8) as api:
        async def one(m: date) -> None:
            path = DATA / f"{m:%Y-%m}.parquet"
            if path.exists() and m < current - timedelta(days=1):  # past months are final once held
                return
            t = await fetch_month(api, m)
            tmp = path.with_suffix(".tmp")
            pq.write_table(t, tmp, compression="zstd")
            tmp.replace(path)
            print(f"  {m:%Y-%m}: {t.num_rows} rows", flush=True)
        await asyncio.gather(*(one(m) for m in months))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2025-04-01")
    p.add_argument("--to", dest="end")
    a = p.parse_args()
    asyncio.run(run(date.fromisoformat(a.start),
                    date.fromisoformat(a.end) if a.end else datetime.now(timezone.utc).date()))
