"""Live Balancing Mechanism state: what NESO had accepted, as known at the forecast time.

For each settlement period p and each forecast that needs it, the accepted volume so far is computed
from BOALF acceptances issued before a cutoff, against the units' final physical notifications (FPN):

- every acceptance is sampled on a 1-minute grid (its levels are piecewise linear);
- at each minute, a unit's instructed level is the one from its latest acceptance issued by the cutoff;
- volume = (instructed level - FPN) integrated over the period: positive is offer volume (more
  generation / less demand), negative is bid volume. Split by NESO's SO flag;
- each unit's volume is priced at its BOD pair +1 offer / pair -1 bid price for that period.

Forecasts are issued at SP0 start + ISSUE_MIN. Period p is SP0+k (k = 0, 1, 2) for the forecasts
issued in periods p, p-1, p-2, so each period gets features at three cutoffs:
cutoff_k = p - 30k min + ISSUE_MIN - BOALF_LAG. Plus a `final` row with every acceptance (for
diagnostics only, never as a feature). PN and BOD are final at gate closure (period start - 1 h), so
they are known at every one of these cutoffs. SP+3 (gate closure after the forecast) is not computed.

Output: data/features/bm_live/<settlement date>.parquet

    uv run python -m spf.bm_live --from 2024-01-01
"""
from __future__ import annotations

import argparse
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("BMDASH_DATA", str(Path("data/bmdash").resolve()))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ISSUE_MIN = 25
BOALF_LAG = 3  # minutes from acceptance to it being usable (measured max 1.7 min over 362 acceptances)
ACC = Path(os.environ["BMDASH_DATA"]) / "acceptances"
EL = Path("data/elexon")
OUT = Path("data/features/bm_live")
LONDON = ZoneInfo("Europe/London")
MIN_NS = 60_000_000_000


def _ns(ts) -> np.ndarray:
    return pd.to_datetime(ts, utc=True).dt.as_unit("ns").astype("int64").to_numpy()


def _minutes(ts) -> np.ndarray:
    return _ns(ts) // MIN_NS


def _expand(unit_codes, t0, t1, l0, l1, extra: dict[str, np.ndarray] | None = None) -> pd.DataFrame:
    """Sample piecewise-linear segments at minute midpoints in [t0, t1)."""
    n = np.maximum(t1 - t0, 0).astype(np.int64)
    keep = n > 0
    unit_codes, t0, t1, l0, l1, n = (a[keep] for a in (unit_codes, t0, t1, l0, l1, n))
    idx = np.repeat(np.arange(len(n)), n)
    offs = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
    frac = (offs + 0.5) / n[idx]
    out = {"unit": unit_codes[idx], "t": t0[idx] + offs, "level": l0[idx] + (l1[idx] - l0[idx]) * frac}
    for k, v in (extra or {}).items():
        out[k] = v[keep][idx]
    return pd.DataFrame(out)


def _bounds(d: date) -> tuple[int, int]:
    s = datetime(d.year, d.month, d.day, tzinfo=LONDON).astimezone(timezone.utc)
    n = d + timedelta(days=1)
    e = datetime(n.year, n.month, n.day, tzinfo=LONDON).astimezone(timezone.utc)
    return int(s.timestamp() // 60), int(e.timestamp() // 60)


def day_features(d: date) -> pd.DataFrame | None:
    files = [ACC / f"{x.isoformat()}.parquet" for x in (d - timedelta(days=1), d)]
    pn_path, bod_path = EL / "PN" / f"{d.isoformat()}.parquet", EL / "BOD" / f"{d.isoformat()}.parquet"
    if not files[1].exists() or not pn_path.exists() or not bod_path.exists():
        return None
    acc = pd.concat([pd.read_parquet(f) for f in files if f.exists()], ignore_index=True)
    pn = pd.read_parquet(pn_path)
    bod = pd.read_parquet(bod_path)
    d0, d1 = _bounds(d)

    units = pd.Index(pd.unique(pd.concat([acc.ngc_bm_unit, pn.nationalGridBmUnit]).dropna()))
    acc = acc[acc.ngc_bm_unit.notna()]
    a = _expand(units.get_indexer(acc.ngc_bm_unit), _minutes(acc.time_from), _minutes(acc.time_to),
                acc.level_from.to_numpy(float), acc.level_to.to_numpy(float),
                {"acc_t": _ns(acc.acceptance_time) / MIN_NS,
                 "acc_n": acc.acceptance_number.to_numpy(), "so": acc.so_flag.fillna(False).to_numpy(bool)})
    a = a[(a.t >= d0) & (a.t < d1)]
    p = _expand(units.get_indexer(pn.nationalGridBmUnit), _minutes(pn.timeFrom), _minutes(pn.timeTo),
                pn.levelFrom.to_numpy(float), pn.levelTo.to_numpy(float))
    p = p.drop_duplicates(["unit", "t"], keep="last").rename(columns={"level": "fpn"})
    a = a.merge(p, on=["unit", "t"], how="left")
    a["no_pn"] = a.fpn.isna()
    a["fpn"] = a.fpn.fillna(0.0)
    a["period"] = (a.t - d0) // 30  # 0-based period index within the day
    a = a.sort_values(["period", "unit", "t", "acc_t", "acc_n"]).reset_index(drop=True)

    # prices: pair +1 offer and pair -1 bid per unit and period
    bod = bod.assign(unit=units.get_indexer(bod.nationalGridBmUnit), period=bod.settlementPeriod - 1)
    offer = bod[bod.pairId == 1].groupby(["unit", "period"]).offer.first()
    bid = bod[bod.pairId == -1].groupby(["unit", "period"]).bid.first()

    rows = []
    for per, g in a.groupby("period", sort=True):
        start = d0 + 30 * per
        cutoffs = {f"k{k}": start - 30 * k + ISSUE_MIN - BOALF_LAG for k in range(3)}
        cutoffs["final"] = np.inf
        key = g.unit.to_numpy() * 100_000 + (g.t.to_numpy() - start)
        acc_t = g.acc_t.to_numpy()
        for name, c in cutoffs.items():
            m = acc_t <= c
            r = {"settlement_date": d, "period": int(per) + 1,
                 "start_time": pd.Timestamp(start * MIN_NS, tz="UTC"), "cutoff": name}
            if m.any():
                km = key[m]
                last = np.r_[km[1:] != km[:-1], True]  # last (latest-issued) row per unit-minute
                sel = g[m][last]
                delta = (sel.level - sel.fpn).to_numpy() / 60.0  # MWh per minute
                u = sel.unit.to_numpy()
                so = sel.so.to_numpy()
                per_unit = pd.DataFrame({"unit": u, "d": delta, "so": so}).groupby("unit").agg(
                    v=("d", "sum"), so=("so", "max"))
                per_unit["offer_p"] = offer.reindex(pd.MultiIndex.from_arrays(
                    [per_unit.index, np.full(len(per_unit), per)])).to_numpy()
                per_unit["bid_p"] = bid.reindex(pd.MultiIndex.from_arrays(
                    [per_unit.index, np.full(len(per_unit), per)])).to_numpy()
                off = per_unit[per_unit.v > 0.05]
                bids = per_unit[per_unit.v < -0.05]
                r.update({
                    "offer_mwh": off.v.sum(), "bid_mwh": bids.v.sum(),
                    "offer_mwh_so": off.v[off.so].sum(), "bid_mwh_so": bids.v[bids.so].sum(),
                    "n_offer_units": len(off), "n_bid_units": len(bids),
                    "n_acceptances": int(pd.unique(sel.acc_n).size),
                    "no_pn_share": float(sel.no_pn.mean()),
                })
                for side, df, col in (("offer", off, "offer_p"), ("bid", bids, "bid_p")):
                    e = df[~df.so & df[col].notna() & (df[col].abs() < 5000)]
                    w = e.v.abs()
                    r[f"{side}_vwap"] = float((e[col] * w).sum() / w.sum()) if w.sum() > 0 else np.nan
                    r[f"{side}_pmax"] = float(e[col].max()) if len(e) else np.nan
                    r[f"{side}_pmin"] = float(e[col].min()) if len(e) else np.nan
                # acceptances issued in the 30 minutes before the cutoff that touch this period
                if np.isfinite(c):
                    recent = sel[sel.acc_t > c - 30]
                    rd = pd.DataFrame({"unit": recent.unit, "d": (recent.level - recent.fpn) / 60.0}).groupby("unit").d.sum()
                    r["recent_offer_mwh"] = rd[rd > 0].sum()
                    r["recent_bid_mwh"] = rd[rd < 0].sum()
            rows.append(r)
    return pd.DataFrame(rows)


def _one(d: date) -> str:
    out = OUT / f"{d.isoformat()}.parquet"
    try:
        df = day_features(d)
    except Exception as e:  # report and carry on with other days
        return f"{d} FAILED {e!r}"
    if df is None:
        return f"{d} skipped (inputs missing)"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return f"{d} {len(df)} rows"


def main(a: date, b: date, force: bool, workers: int) -> None:
    days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
    todo = [d for d in days if force or not (OUT / f"{d.isoformat()}.parquet").exists()]
    print(f"{len(todo)} days", flush=True)
    with ProcessPoolExecutor(workers) as ex:
        for i, msg in enumerate(ex.map(_one, todo)):
            if "FAILED" in msg or "skipped" in msg or i % 50 == 0:
                print(msg, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="start", default="2024-01-01")
    p.add_argument("--to", dest="end")
    p.add_argument("--force", action="store_true")
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    x = p.parse_args()
    main(date.fromisoformat(x.start), date.fromisoformat(x.end) if x.end else datetime.now(timezone.utc).date(),
         x.force, x.workers)
