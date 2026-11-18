"""Poll Elexon and log the first time each period's system price and MID is seen.

Elexon's system-price endpoint only serves the latest version of a price (it is republished about a
day later), and MID carries no publication time at all, so the real-time publication lag can only be
measured by watching. Appends to data/publication_watch.csv; safe to stop and restart.

    uv run python -m spf.watch_publication --minutes 90
"""
from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bmdash.elexon import Elexon

OUT = Path("data/publication_watch.csv")
FIELDS = ["dataset", "start_time", "provider", "first_seen", "created", "value", "volume"]


def _seen() -> set[tuple]:
    if not OUT.exists():
        return set()
    with OUT.open() as f:
        return {(r["dataset"], r["start_time"], r["provider"]) for r in csv.DictReader(f)}


async def poll_once(api: Elexon, seen: set[tuple]) -> list[dict]:
    now = datetime.now(timezone.utc)
    today = now.date()
    prices, mid = await asyncio.gather(
        api.get(f"/balancing/settlement/system-prices/{today.isoformat()}", {"format": "json"}),
        api.get("/datasets/MID", {"from": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%MZ"),
                                  "to": (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%MZ"), "format": "json"}),
    )
    stamp = now.isoformat(timespec="seconds")
    new = []
    for r in prices:
        k = ("system_price", r["startTime"], "")
        if k not in seen:
            new.append({"dataset": k[0], "start_time": k[1], "provider": "", "first_seen": stamp,
                        "created": r.get("createdDateTime"), "value": r.get("systemSellPrice"),
                        "volume": r.get("netImbalanceVolume")})
            seen.add(k)
    for r in mid:
        k = ("mid", r["startTime"], r["dataProvider"])
        if k not in seen:
            new.append({"dataset": "mid", "start_time": k[1], "provider": k[2], "first_seen": stamp,
                        "created": "", "value": r.get("price"), "volume": r.get("volume")})
            seen.add(k)
    return new


async def main(minutes: float, every: float) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    first = not OUT.exists()
    seen = _seen()
    end = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    async with Elexon(concurrency=4) as api:
        while datetime.now(timezone.utc) < end:
            try:
                new = await poll_once(api, seen)
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
                print(datetime.now(timezone.utc).strftime("%H:%M:%S"), len(new), "new rows", flush=True)
            await asyncio.sleep(every)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=90)
    p.add_argument("--every", type=float, default=30, help="seconds between polls")
    a = p.parse_args()
    asyncio.run(main(a.minutes, a.every))
