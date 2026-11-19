"""Live forecasting: train on all history, then forecast SP+1..SP+3 from the current period.

    uv run python -m spf.live train [--exp lgbm_stack]   # fit and save models to data/models/
    uv run python -m spf.live predict                    # refresh today's data and forecast

Run `predict` at least ISSUE_MIN (25) minutes into a settlement period: that is the issue time the models
were trained and backtested for. Earlier than that, it forecasts from the previous period instead (so
the "SP+1" it reports may already be under way), and says so.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("BMDASH_DATA", str(Path("data/bmdash").resolve()))

import lightgbm as lgb  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from bmdash import ingest as bmi  # noqa: E402
from bmdash import store  # noqa: E402
from bmdash.elexon import Elexon  # noqa: E402

from spf import bm_live, features, ingest, mid, pn_agg  # noqa: E402
from spf.model import EXPS, HORIZONS, add_niv_stack, load_frame, stack_names  # noqa: E402
from spf.model import features as feature_list  # noqa: E402

MODELS = Path("data/models")
LONDON = bmi.LONDON


def train(exp_name: str) -> None:
    exp = EXPS[exp_name]
    df = load_frame()
    df = df[df.index >= pd.Timestamp(exp.train_from, tz="UTC")]
    feats = feature_list(df, exp)
    MODELS.mkdir(parents=True, exist_ok=True)
    stack_anchor = exp.anchor or "mid_l0"
    if exp.niv_stack:
        df, _, models = add_niv_stack(df, df.iloc[:1], feats, side=exp.side_stack, anchor=stack_anchor)
        for name, m in models.items():
            m.save_model(MODELS / f"{name}.txt")
    hfeats = feats + stack_names(exp)
    params = {"objective": exp.objective, "learning_rate": 0.03, "num_leaves": 63, "min_data_in_leaf": 100,
              "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
              "verbose": -1, "num_threads": 0, **exp.params}
    for h in HORIZONS:
        tr = df[df[f"y{h}"].notna()]
        anchor = tr[exp.anchor].fillna(tr["ssp_l1"]) if exp.anchor else 0.0
        for seed in range(exp.seeds):
            m = lgb.train({**params, "seed": seed}, lgb.Dataset(tr[hfeats], tr[f"y{h}"] - anchor), exp.rounds)
            m.save_model(MODELS / f"price{h}_s{seed}.txt")
    meta = {"exp": exp_name, "features": feats, "price_features": hfeats, "anchor": exp.anchor,
            "seeds": exp.seeds, "niv_stack": exp.niv_stack, "stack": stack_names(exp), "stack_anchor": stack_anchor, "trained_at": datetime.now(timezone.utc).isoformat(),
            "trained_to": str(df.index.max())}
    (MODELS / "meta.json").write_text(json.dumps(meta, indent=1))
    print("saved models for", exp_name, "trained to", meta["trained_to"])


async def refresh(days: list) -> None:
    """Re-fetch the recent days of every input: bmdash acceptances and prices, and this repo's datasets."""
    async with Elexon(concurrency=12) as api:
        for d in days:
            acc, prices = await asyncio.gather(bmi._fetch_acceptances(api, d), bmi._fetch_prices(api, d))
            store.write_day("acceptances", d, acc)
            store.write_day("system_prices", d, prices)
        await asyncio.gather(*(ingest.forecast_day(api, ds, d) for ds in ingest.FORECASTS for d in days))
        await asyncio.gather(*(ingest.bm_day(api, ds, d) for ds in ("PN", "BOD") for d in days))
    await mid.run(days[0], days[-1])


def predict() -> pd.DataFrame:
    meta = json.loads((MODELS / "meta.json").read_text())
    now = datetime.now(timezone.utc)
    today = now.astimezone(LONDON).date()
    days = [today - timedelta(days=1), today]
    for d in days:  # force today's monthly MID file to be refreshed
        p = mid.DATA / f"{d:%Y-%m}.parquet"
        if p.exists():
            p.unlink()
    asyncio.run(refresh(days))
    asyncio.run(pn_agg.run(days[0], days[-1], force=True))
    for d in days:
        df = bm_live.day_features(d)
        if df is not None:
            bm_live.OUT.mkdir(parents=True, exist_ok=True)
            df.to_parquet(bm_live.OUT / f"{d.isoformat()}.parquet", index=False)
    frame = features.build(start=(today - timedelta(days=12)).isoformat(), end_extra=4)
    sp_now = pd.Timestamp(now).floor("30min")
    sp0 = sp_now if now >= sp_now + timedelta(minutes=features.ISSUE_MIN) else sp_now - pd.Timedelta(minutes=30)
    if sp0 != sp_now:
        print(f"Note: less than {features.ISSUE_MIN} min into the current period; forecasting from {sp0:%H:%M} UTC.")
    row = frame.loc[[sp0]]
    obj = [c for c in row.columns if row[c].dtype == object]
    row[obj] = row[obj].apply(pd.to_numeric, errors="coerce")
    stack_anchor = row[meta["stack_anchor"]].fillna(row["ssp_l1"]).to_numpy()
    for name in meta["stack"]:
        pred = lgb.Booster(model_file=str(MODELS / f"{name}.txt")).predict(row[meta["features"]])
        row[name] = pred + stack_anchor if name.startswith("pif") else pred
    anchor = row[meta["anchor"]].fillna(row["ssp_l1"]).iloc[0] if meta["anchor"] else 0.0
    out = []
    for h in HORIZONS:
        p = np.mean([lgb.Booster(model_file=str(MODELS / f"price{h}_s{s}.txt")).predict(row[meta["price_features"]])[0]
                     for s in range(meta["seeds"])])
        start = sp0 + h * pd.Timedelta(minutes=30)
        out.append({"horizon": f"SP+{h}", "period_start_utc": start,
                    "settlement_period": _sp_number(start), "forecast_gbp_mwh": round(p + anchor, 2),
                    "p_short": round(float(row[f"pshort{h}"].iloc[0]), 3) if meta["niv_stack"] else None})
    res = pd.DataFrame(out)
    log = Path("data/live_forecasts.csv")
    res.assign(issued_at=now.isoformat(timespec="seconds"), sp0=sp0).to_csv(log, mode="a", header=not log.exists(),
                                                                          index=False)
    print(f"Issued {now:%Y-%m-%d %H:%M} UTC from SP0 starting {sp0:%H:%M} UTC; latest MID {anchor:.2f}")
    print(res.to_string(index=False))
    return res


def _sp_number(start: pd.Timestamp) -> int:
    local = start.tz_convert(LONDON)
    day_start = pd.Timestamp(local.date(), tz=LONDON)
    return int((start - day_start.tz_convert("UTC")).total_seconds() // 1800) + 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--exp", default="lgbm_stack")
    sub.add_parser("predict")
    a = p.parse_args()
    train(a.exp) if a.cmd == "train" else predict()
