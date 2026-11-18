"""Snapshot Elexon system prices as currently served, keeping `createdDateTime`.

Elexon publishes a period's price shortly after it ends, then republishes it about a day later; the
API only ever serves the latest version. Periods from the last ~24 hours are still on their initial
version, so snapshots taken now and compared later measure (a) the initial publication lag and
(b) how much the day-after revision moves the price. Appends to data/price_snapshots/<taken>.parquet.

    uv run python -m spf.price_snapshot
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from bmdash.elexon import Elexon

OUT = Path("data/price_snapshots")


async def main(days: int = 3) -> Path:
    taken = datetime.now(timezone.utc)
    dates = [(taken - timedelta(days=i)).date() for i in range(days)]
    async with Elexon(concurrency=4) as api:
        res = await asyncio.gather(*(api.get(f"/balancing/settlement/system-prices/{d}", {"format": "json"})
                                     for d in dates))
    df = pd.DataFrame([r for rows in res for r in rows])
    df["snapshot_taken"] = taken
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{taken:%Y%m%dT%H%M%SZ}.parquet"
    df.to_parquet(path)
    print(path, len(df))
    return path


if __name__ == "__main__":
    asyncio.run(main())
