"""Walk-forward backtest of system price models.

Every test month is predicted by models trained only on forecasts whose outcomes were known before
that month started (expanding window, retrained monthly). Development uses the DEV months; the TEST
months are held out for the final score only.

    uv run python -m spf.model --exp lgbm_mid
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

FRAME = Path("data/features/frame.parquet")
PRED = Path("data/predictions")
HH = pd.Timedelta(minutes=30)
DEV = ("2025-04-01", "2026-05-01")
TEST = ("2026-05-01", "2026-12-01")
HORIZONS = (1, 2, 3)
NON_FEATURES = {"tau"} | {f"y{h}" for h in HORIZONS} | {f"niv_y{h}" for h in HORIZONS}


@dataclass
class Exp:
    name: str
    anchor: str | None = "mid_l0"       # model predicts y - anchor
    objective: str = "l1"
    drop: list[str] = field(default_factory=list)   # feature name prefixes to leave out
    keep: list[str] | None = None                    # if set, only these prefixes
    params: dict = field(default_factory=dict)
    rounds: int = 1500
    train_from: str = "2024-01-15"
    weight_recent: float = 0.0          # >0: exponential weight with this half-life in days


EXPS = {
    "lgbm_mid": Exp("lgbm_mid"),
    "lgbm_last": Exp("lgbm_last", anchor="ssp_l1"),
    "lgbm_raw": Exp("lgbm_raw", anchor=None),
    "lgbm_mid_l2": Exp("lgbm_mid_l2", objective="l2"),
    "lgbm_mid_huber": Exp("lgbm_mid_huber", objective="huber", params={"alpha": 30.0}),
    "lgbm_mid_nobm": Exp("lgbm_mid_nobm", drop=["bm0_", "bm1_", "bm2_", "bm_have"]),
    "lgbm_mid_hist": Exp("lgbm_mid_hist", keep=["ssp_", "niv_", "mid", "hour", "dow", "month", "holiday",
                                                "weekend", "offer_vol", "bid_vol", "pdc", "short_share"]),
    "lgbm_mid_recent": Exp("lgbm_mid_recent", weight_recent=180),
}


def load_frame() -> pd.DataFrame:
    df = pd.read_parquet(FRAME)
    obj = [c for c in df.columns if df[c].dtype == object]
    df[obj] = df[obj].apply(pd.to_numeric, errors="coerce")
    return df


def features(df: pd.DataFrame, exp: Exp) -> list[str]:
    cols = [c for c in df.columns if c not in NON_FEATURES]
    if exp.keep is not None:
        cols = [c for c in cols if any(c.startswith(k) for k in exp.keep)]
    return [c for c in cols if not any(c.startswith(d) for d in exp.drop)]


def months(a: str, b: str) -> list[pd.Timestamp]:
    return list(pd.date_range(a, b, freq="MS", tz="UTC", inclusive="left"))


def fit_predict(df: pd.DataFrame, exp: Exp, test_months: list[pd.Timestamp]) -> pd.DataFrame:
    feats = features(df, exp)
    out = []
    for m0 in test_months:
        m1 = m0 + pd.offsets.MonthBegin(1)
        # an outcome for SP+3 is published ~2 h after the forecast: keep a margin before the test month
        train = df[(df.index >= pd.Timestamp(exp.train_from, tz="UTC")) & (df.index < m0 - pd.Timedelta(hours=3))]
        test = df[(df.index >= m0) & (df.index < m1)]
        if test.empty:
            continue
        pm = {}
        for h in HORIZONS:
            y = f"y{h}"
            tr = train[train[y].notna()]
            anchor_tr = tr[exp.anchor].fillna(tr["ssp_l1"]) if exp.anchor else 0.0
            target = tr[y] - anchor_tr
            w = None
            if exp.weight_recent > 0:
                age = (m0 - tr.index).total_seconds() / 86400
                w = 0.5 ** (age / exp.weight_recent)
            params = {"objective": exp.objective, "learning_rate": 0.03, "num_leaves": 63, "min_data_in_leaf": 100,
                      "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
                      "verbose": -1, "num_threads": 0, **exp.params}
            model = lgb.train(params, lgb.Dataset(tr[feats], target, weight=w), num_boost_round=exp.rounds)
            anchor_te = test[exp.anchor].fillna(test["ssp_l1"]) if exp.anchor else 0.0
            pm[h] = model.predict(test[feats]) + anchor_te
        o = pd.DataFrame({f"pred{h}": pm[h] for h in HORIZONS}, index=test.index)
        out.append(o)
        print(f"  {m0:%Y-%m} done", flush=True)
    return pd.concat(out)


def score(df: pd.DataFrame, pred: pd.DataFrame) -> pd.DataFrame:
    rows = []
    j = df.join(pred, how="inner")
    for h in HORIZONS:
        g = j[j[f"y{h}"].notna()]
        cands = {"model": g[f"pred{h}"], "last known price": g["ssp_l1"], "latest MID": g["mid_l0"].fillna(g["ssp_l1"])}
        for name, p in cands.items():
            e = p - g[f"y{h}"]
            rows.append({"horizon": f"SP+{h}", "forecast": name, "n": int(e.notna().sum()),
                         "MAE": e.abs().mean(), "RMSE": np.sqrt((e ** 2).mean())})
    return pd.DataFrame(rows)


def run(exp: Exp, window: tuple[str, str], tag: str) -> pd.DataFrame:
    df = load_frame()
    pred = fit_predict(df, exp, months(*window))
    PRED.mkdir(parents=True, exist_ok=True)
    pred.to_parquet(PRED / f"{exp.name}_{tag}.parquet")
    s = score(df, pred)
    print(s.round(2).to_string())
    log = Path("results/experiments.jsonl")
    log.parent.mkdir(exist_ok=True)
    with log.open("a") as f:
        f.write(json.dumps({"exp": exp.name, "window": tag,
                            "mae": {r.horizon: round(r.MAE, 3) for r in s[s.forecast == "model"].itertuples()},
                            "rmse": {r.horizon: round(r.RMSE, 3) for r in s[s.forecast == "model"].itertuples()},
                            "config": {k: v for k, v in exp.__dict__.items() if k != "name"}}) + "\n")
    return s


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--exp", nargs="+", default=["lgbm_mid"])
    p.add_argument("--test", action="store_true", help="score on the held-out TEST months")
    a = p.parse_args()
    for name in a.exp:
        print("==", name)
        run(EXPS[name], TEST if a.test else DEV, "test" if a.test else "dev")
