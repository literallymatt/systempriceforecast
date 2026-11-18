"""Summarise measured publication delays from data/publication_watch.csv into results/data-availability.md.

    uv run python -m spf.availability
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from spf import bm_live, features

WATCH = Path("data/publication_watch.csv")
OUT = Path("results/data-availability.md")


def load() -> pd.DataFrame:
    w = pd.read_csv(WATCH, dtype=str)
    w["first_seen"] = pd.to_datetime(w.first_seen, utc=True)
    starts = sorted(set(w.loc[w.dataset == "watch_start", "first_seen"]) | {w.first_seen.min()})
    w = w[w.dataset != "watch_start"].copy()
    s = pd.Series(starts)
    w["watch_start"] = s.iloc[s.searchsorted(w.first_seen, side="right") - 1].to_numpy()
    # items seen in the first poll after a (re)start were published before it: their delay is unknown
    w["valid"] = (w.first_seen - w.watch_start) > pd.Timedelta(seconds=15)
    w["start"] = pd.to_datetime(w.start_time, utc=True, errors="coerce")
    w["created_t"] = pd.to_datetime(w.created, utc=True, errors="coerce")
    return w


def describe(x: pd.Series) -> dict:
    return {"n": int(x.count()), "min": x.min(), "median": x.median(), "p95": x.quantile(0.95), "max": x.max()}


def main() -> None:
    w = load()
    rows = []
    sp = w[(w.dataset == "system_price") & w.valid]
    rows.append({"item": "System price: first seen − period end (min)",
                 **describe((sp.first_seen - (sp.start + pd.Timedelta(minutes=30))).dt.total_seconds() / 60)})
    rows.append({"item": "System price: first seen − createdDateTime (min)",
                 **describe((sp.first_seen - sp.created_t).dt.total_seconds() / 60)})
    mid = w[(w.dataset == "mid") & (w.provider == "APXMIDP") & w.valid & (w.start > w.watch_start)]
    rows.append({"item": "MID (APX): first seen − period start (min)",
                 **describe((mid.first_seen - mid.start).dt.total_seconds() / 60)})
    upd = w[(w.dataset == "mid_update") & (w.provider == "APXMIDP")]
    b = w[(w.dataset == "boalf") & (w.created_t > w.watch_start)]
    rows.append({"item": "BOALF: first seen − acceptance time (min)",
                 **describe((b.first_seen - b.created_t).dt.total_seconds() / 60)})
    im = w[(w.dataset == "imbalngc") & w.valid]
    rows.append({"item": "IMBALNGC: first seen − publishTime (min)",
                 **describe((im.first_seen - im.created_t).dt.total_seconds() / 60)})
    for ds in ["melngc", "ndf", "tsdf", "indo", "lolpdrm", "windfor"]:
        x = w[(w.dataset == ds) & w.valid]
        rows.append({"item": f"{ds.upper()}: first seen − publishTime (min)",
                     **describe((x.first_seen - x.created_t).dt.total_seconds() / 60)})
    fu = w[(w.dataset == "fuelinst") & w.valid]
    rows.append({"item": "FUELINST: first seen − publishTime (min)",
                 **describe((fu.first_seen - fu.created_t).dt.total_seconds() / 60)})
    t = pd.DataFrame(rows)
    span = f"{w.first_seen.min():%Y-%m-%d %H:%M} – {w.first_seen.max():%Y-%m-%d %H:%M} UTC"

    L = ["# Data availability: what is known at forecast time\n",
         f"Measured by polling Elexon every 20–30 s ({span}) with `spf.watch_publication`. "
         "Resolution is about ±30 s. Items already present when the watcher (re)started are excluded.\n",
         "| measurement | n | min | median | p95 | max |", "|---|---:|---:|---:|---:|---:|"]
    for r in t.itertuples():
        fmt = lambda v: "" if pd.isna(v) else f"{v:.1f}"  # noqa: E731
        L.append(f"| {r.item} | {r.n} | {fmt(r.min)} | {fmt(r.median)} | {fmt(r.p95)} | {fmt(r.max)} |")
    L.append(f"\nMID values changed after first publication in {len(upd)} of {len(mid)} watched periods.\n")
    L.append("## Rules used by the backtest\n")
    L.append(f"Forecasts are issued at SP0 start + {features.ISSUE_MIN} min.\n")
    L.append(f"- System price / NIV: a period is known once its end + {features.PRICE_LAG} min has passed "
             "(SP−1 at issue time).")
    L.append(f"- MID: known once its period start + {features.MID_LAG} min has passed (SP0 at issue time).")
    L.append(f"- BOALF acceptances: used if issued at least {bm_live.BOALF_LAG} min before issue time.")
    L.append(f"- Datasets with a publishTime (IMBALNGC, MELNGC, NDF, TSDF, WINDFOR, LOLPDRM, INDO, FUELINST): "
             f"latest version with publishTime ≤ issue time − {features.MARGIN} min.")
    L.append("- PN and BOD: final at gate closure (period start − 1 h), so used for SP0..SP+2 only; nothing "
             "about SP+3's PN or BOD is used.")
    L.append("- Every NESO forecast is the version published at the time, never a later revision "
             "(all versions are stored).\n")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
