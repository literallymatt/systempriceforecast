"""Poll Elexon and log the first time each item is seen, to measure real publication delays.

Elexon's system-price endpoint only serves the latest version of a price, MID carries no publication
time, and BOALF carries the acceptance time but not when it reached the API. So the delays that the
backtest relies on are measured by watching. Appends to data/publication_watch.csv (one row per new
item, plus `mid_update` rows when a MID value changes after it first appeared); safe to restart.

    uv run python -m spf.watch_publication --minutes 600
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bmdash.elexon import Elexon

OUT = Path("data/publication_watch.csv")
OTHER_PUBLISHED = ["MELNGC", "NDF", "TSDF", "INDO", "LOLPDRM", "WINDFOR"]
FIELDS = ["dataset", "start_time", "provider", "first_seen", "created", "value", "volume"]


def _load() -> tuple[set[tuple], dict[tuple, str]]:
    seen, mid_val = set(), {}
    if OUT.exists():
        with OUT.open() as f:
            for r in csv.DictReader(f):
                k = (r["dataset"], r["start_time"], r["provider"])
                seen.add(k)
                if r["dataset"] in ("mid", "mid_update"):
                    mid_val[("mid", r["start_time"], r["provider"])] = r["value"]
    return seen, mid_val


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%MZ")


async def poll_once(api: Elexon, seen: set[tuple], mid_val: dict[tuple, str]) -> list[dict]:
    now = datetime.now(timezone.utc)
    prices, mid, boalf, imbal, fuel = await asyncio.gather(
        api.get(f"/balancing/settlement/system-prices/{now.date().isoformat()}", {"format": "json"}),
        api.get("/datasets/MID", {"from": _iso(now - timedelta(hours=3)), "to": _iso(now + timedelta(hours=6)),
                                  "format": "json"}),
        api.get("/datasets/BOALF/stream", {"from": _iso(now - timedelta(hours=1)), "to": _iso(now + timedelta(hours=4))}),
        api.get("/datasets/IMBALNGC", {"boundary": "N", "publishDateTimeFrom": _iso(now - timedelta(hours=1)),
                                       "publishDateTimeTo": _iso(now + timedelta(minutes=5)), "format": "json"}),
        api.get("/datasets/FUELINST", {"publishDateTimeFrom": _iso(now - timedelta(minutes=30)),
                                       "publishDateTimeTo": _iso(now + timedelta(minutes=5)), "fuelType": "WIND",
                                       "format": "json"}),
    )
    pub = {"publishDateTimeFrom": _iso(now - timedelta(hours=1)), "publishDateTimeTo": _iso(now + timedelta(minutes=5)),
           "format": "json"}
    others = await asyncio.gather(*(api.get(f"/datasets/{ds}", {**pub, **({"boundary": "N"} if ds in ("MELNGC", "TSDF")
                                                                          else {})})
                                    for ds in OTHER_PUBLISHED), return_exceptions=True)
    stamp = now.isoformat(timespec="seconds")
    new = []

    def add(ds, start, provider, created, value, volume):
        k = (ds, start, provider)
        if k not in seen:
            seen.add(k)
            new.append({"dataset": ds, "start_time": start, "provider": provider, "first_seen": stamp,
                        "created": created, "value": value, "volume": volume})

    for r in prices:
        add("system_price", r["startTime"], "", r.get("createdDateTime"), r.get("systemSellPrice"),
            r.get("netImbalanceVolume"))
    for r in mid:
        k = ("mid", r["startTime"], r["dataProvider"])
        v = str(r.get("price"))
        if k in seen and mid_val.get(k) not in (None, v):
            new.append({"dataset": "mid_update", "start_time": r["startTime"], "provider": r["dataProvider"],
                        "first_seen": stamp, "created": "", "value": v, "volume": r.get("volume")})
        add(*k, "", v, r.get("volume"))
        mid_val[k] = v
    for r in boalf:  # provider column holds unit:acceptance number; created holds the acceptance time
        add("boalf", r["timeFrom"], f"{r.get('nationalGridBmUnit')}:{r['acceptanceNumber']}", r["acceptanceTime"],
            r.get("levelFrom"), r.get("levelTo"))
    for r in {x["publishTime"]: x for x in imbal}.values():
        add("imbalngc", r["publishTime"], "", r["publishTime"], "", "")
    for ds, rows in zip(OTHER_PUBLISHED, others):
        if isinstance(rows, Exception):
            continue
        for t in {x["publishTime"] for x in rows}:
            add(ds.lower(), t, "", t, "", "")
    for r in fuel:
        add("fuelinst", r["startTime"], "WIND", r["publishTime"], r.get("generation"), "")
    return new


async def main(minutes: float, every: float) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    first = not OUT.exists()
    seen, mid_val = _load()
    started = datetime.now(timezone.utc)
    end = started + timedelta(minutes=minutes)
    # mark the (re)start: items first seen in the first poll after it were published earlier
    with OUT.open("a", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
        if first:
            w.writeheader()
            first = False
        w.writerow({"dataset": "watch_start", "start_time": "", "provider": "",
                    "first_seen": started.isoformat(timespec="seconds"), "created": "", "value": "", "volume": ""})
    async with Elexon(concurrency=6) as api:
        while datetime.now(timezone.utc) < end:
            try:
                new = await poll_once(api, seen, mid_val)
            except Exception as e:  # keep watching through a failed poll
                print("poll failed:", e, flush=True)
                new = []
            if new:
                with OUT.open("a", newline="") as f:
                    w = csv.DictWriter(f, FIELDS)
                    if first:
                        w.writeheader()
                        first = False
                    w.writerows(new)
            await asyncio.sleep(every)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=600)
    p.add_argument("--every", type=float, default=30, help="seconds between polls")
    a = p.parse_args()
    asyncio.run(main(a.minutes, a.every))
