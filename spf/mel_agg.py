"""Declared available capacity: maximum export limits (MEL) in force at each forecast time, by technology.

Elexon's MELS stream holds the MEL segments that ended up in effect: a redeclaration replaces the
earlier declaration from its notification time onwards, and the superseded part is not kept. So the
honest "as known at tau" quantity is the MEL in force at the instant tau - MEL_LAG (every segment in
force then was notified by then), not the MEL that would later apply to SP+1..SP+3. Redeclarations
mostly apply from now on, so the level in force now is the best known view of the next periods.

For each forecast (one per settlement period, tau = start + ISSUE_MIN), per technology:

- mel_<tech>: summed MEL in force at tau - MEL_LAG (MW);
- mel_<tech>_d60: change over the previous hour (units declaring themselves unavailable show up here);
- mel_total, mel_total_d60.

Features subtract the PN aggregates (spf.pn_agg) to give headroom: capacity declared available above
what units have notified they will generate.

Output: data/features/mel_agg/<settlement date>.parquet

    uv run python -m spf.mel_agg --from 2023-01-01
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

from spf.bm_live import ISSUE_MIN  # noqa: E402
from spf.ingest import _bounds, _iso  # noqa: E402
from spf.pn_agg import _tech_map  # noqa: E402

OUT = Path("data/features/mel_agg")
MEL_LAG = 5  # minutes; MELS appear within a minute or two of notification
TECHS = {"gas": "gas", "battery": "battery", "pumped storage": "pumped_storage", "wind": "wind",
         "interconnector": "interconnector", "nuclear": "nuclear", "biomass": "biomass", "hydro": "hydro",
         "coal": "coal"}


def _tech_group(t: str) -> str:
    t = (t or "").lower()
    for k, v in TECHS.items():
        if k in t:
            return v
    return "other"


def aggregate(rows: list[dict], d: date, tech: dict[str, str]) -> pd.DataFrame:
    s, e = _bounds(d)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame()
    df = df.drop_duplicates(["nationalGridBmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo"])
    t0 = pd.to_datetime(df.timeFrom, utc=True).dt.tz_convert(None).dt.as_unit("ns").to_numpy()
    t1 = pd.to_datetime(df.timeTo, utc=True).dt.tz_convert(None).dt.as_unit("ns").to_numpy()
    nt = pd.to_datetime(df.notificationTime, utc=True).dt.tz_convert(None).dt.as_unit("ns").to_numpy()
    ngc = df.nationalGridBmUnit.fillna("")
    fallback = np.where(ngc.str.startswith("I_"), "interconnector", "other")
    grp = np.array([_tech_group(tech.get(u)) if u in tech else f for u, f in zip(ngc, fallback)])
    lf, lt = df.levelFrom.to_numpy(float), df.levelTo.to_numpy(float)
    starts = pd.date_range(s, e, freq="30min", inclusive="left")
    out = []
    for st in starts:
        tau = st + pd.Timedelta(minutes=ISSUE_MIN)
        row = {"start_time": st}
        for name, c in (("", tau - pd.Timedelta(minutes=MEL_LAG)), ("_m60", tau - pd.Timedelta(minutes=MEL_LAG + 60))):
            c64 = np.datetime64(c.as_unit("ns").tz_convert(None))
            m = (t0 <= c64) & (c64 < t1) & (nt <= c64)
            frac = (c64 - t0[m]) / np.maximum(t1[m] - t0[m], np.timedelta64(1, "s"))
            lvl = lf[m] + (lt[m] - lf[m]) * frac.astype(float)
            ser = pd.Series(lvl).groupby(grp[m]).sum()
            for g, v in ser.items():
                row[f"mel_{g}{name}"] = v
            row[f"mel_total{name}"] = float(lvl.sum())
            row[f"mel_units{name}"] = int(m.sum())
        out.append(row)
    res = pd.DataFrame(out)
    for c in [c for c in res.columns if c.startswith("mel_") and not c.endswith("_m60")]:
        if f"{c}_m60" in res:
            res[f"{c}_d60"] = res[c] - res[f"{c}_m60"]
    res = res.drop(columns=[c for c in res.columns if c.endswith("_m60")])
    res.insert(0, "settlement_date", d)
    return res


async def run(a: date, b: date, force: bool) -> None:
    tech = _tech_map()
    days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
    todo = [d for d in days if force or not (OUT / f"{d.isoformat()}.parquet").exists()]
    print(f"mel_agg: {len(todo)} days", flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(4)
    done = 0
    async with Elexon(concurrency=12) as api:
        async def one(d: date) -> None:
            nonlocal done
            async with sem:
                s, e = _bounds(d)
                s = s - timedelta(hours=1)  # the first periods' "an hour ago" instants
                parts, t = [], s
                while t < e:
                    u = min(t + timedelta(hours=3), e)
                    parts.append(api.get("/datasets/MELS/stream", {"from": _iso(t), "to": _iso(u)}))
                    t = u
                try:
                    res = await asyncio.gather(*parts)
                    df = await asyncio.to_thread(aggregate, [r for p in res for r in p], d, tech)
                    df.to_parquet(OUT / f"{d.isoformat()}.parquet", index=False)
                except Exception as ex:  # noqa: BLE001
                    print(f"{d} FAILED {ex!r}", flush=True)
                    return
                done += 1
                if done % 50 == 0:
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
