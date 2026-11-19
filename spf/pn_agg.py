"""Aggregate physical notifications (PN) of *all* BM units by technology, per settlement period.

PNs are final at gate closure (period start - 1 h), so at a forecast issued in SP0 the aggregates for
SP0, SP+1 and SP+2 are fixed and known; SP+3's are not and are never used. Scheduled changes between
periods (interconnectors ramping at hour boundaries, gas units, batteries) are a large source of
imbalance around the boundaries.

Output: data/features/pn_agg/<settlement date>.parquet with mean MW and end-of-period MW per tech.

    uv run python -m spf.pn_agg --from 2023-01-01
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("BMDASH_DATA", str(Path("data/bmdash").resolve()))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from bmdash.elexon import Elexon  # noqa: E402

from spf.ingest import _bounds, _iso  # noqa: E402

OUT = Path("data/features/pn_agg")


def _tech_map() -> dict[str, str]:
    u = pd.read_parquet(Path(os.environ["BMDASH_DATA"]) / "units.parquet")
    return dict(zip(u.ngc_bm_unit, u.tech))


def aggregate(rows: list[dict], d: date, tech: dict[str, str]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df = df[df.settlementDate == d.isoformat()].drop_duplicates(
        ["nationalGridBmUnit", "settlementPeriod", "timeFrom", "timeTo", "levelFrom", "levelTo"])
    t0 = pd.to_datetime(df.timeFrom, utc=True)
    t1 = pd.to_datetime(df.timeTo, utc=True)
    dur = (t1 - t0).dt.total_seconds() / 60
    df = df.assign(mwmin=(df.levelFrom + df.levelTo) / 2 * dur)  # trapezoid: MW x minutes
    ngc = df.nationalGridBmUnit.fillna("")
    fallback = pd.Series(np.where(ngc.str.startswith("I_"), "Interconnector", "Other"), index=df.index)
    df["tech"] = ngc.map(tech).fillna(fallback)
    df["gen"] = df.mwmin.clip(lower=0)
    df["dem"] = df.mwmin.clip(upper=0)
    # end-of-period level: the segment ending at the period end
    df["end_level"] = np.where(t1.dt.minute.isin([0, 30]), df.levelTo, np.nan)
    g = df.groupby(["settlementPeriod", "tech"]).agg(mw_mean=("mwmin", "sum"), gen=("gen", "sum"), dem=("dem", "sum"),
                                                     mw_end=("end_level", "sum"))
    for c in ("mw_mean", "gen", "dem"):
        g[c] = g[c] / 30.0
    out = g.reset_index().pivot(index="settlementPeriod", columns="tech")
    out.columns = [f"pn_{tech.lower().replace(' ', '_')}_{c}" for c, tech in out.columns]
    out["pn_total_gen"] = out.filter(like="_gen").sum(axis=1)
    out["pn_total_dem"] = out.filter(like="_dem").sum(axis=1)
    out["pn_total"] = out.filter(like="_mw_mean").sum(axis=1)
    s, _ = _bounds(d)
    out = out.reset_index()
    out["start_time"] = pd.Timestamp(s) + pd.to_timedelta((out.settlementPeriod - 1) * 30, unit="min")
    out.insert(0, "settlement_date", d)
    return out


async def run(a: date, b: date, force: bool) -> None:
    tech = _tech_map()
    days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
    todo = [d for d in days if force or not (OUT / f"{d.isoformat()}.parquet").exists()]
    print(f"{len(todo)} days", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(6)
    done = 0
    async with Elexon(concurrency=16) as api:
        async def one(d: date) -> None:
            nonlocal done
            async with sem:
                s, e = _bounds(d)
                parts, t = [], s
                while t < e:
                    u = min(t + timedelta(hours=6), e)
                    parts.append(api.get("/datasets/PN/stream", {"from": _iso(t), "to": _iso(u)}))
                    t = u
                try:
                    res = await asyncio.gather(*parts)
                    df = await asyncio.to_thread(aggregate, [r for p in res for r in p], d, tech)
                    df.to_parquet(OUT / f"{d.isoformat()}.parquet", index=False)
                except Exception as ex:
                    print(f"{d} FAILED {ex!r}", flush=True)
                    return
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(todo)}", flush=True)

        await asyncio.gather(*(one(d) for d in todo))
    print("done", done, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2023-01-01")
    p.add_argument("--to", dest="end")
    p.add_argument("--force", action="store_true")
    x = p.parse_args()
    asyncio.run(run(date.fromisoformat(x.start),
                    date.fromisoformat(x.end) if x.end else datetime.now(timezone.utc).date(), x.force))
