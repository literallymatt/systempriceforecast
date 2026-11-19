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
    seeds: int = 1                      # average this many models with different seeds
    niv_stack: bool = False             # add out-of-fold NIV regression / P(short) predictions as features
    side_stack: bool = False            # also add out-of-fold "price if short" / "price if long" predictions


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
    "lgbm_recent365": Exp("lgbm_recent365", weight_recent=365),
    "lgbm_leaves31": Exp("lgbm_leaves31", params={"num_leaves": 31}),
    "lgbm_leaves127": Exp("lgbm_leaves127", params={"num_leaves": 127, "min_data_in_leaf": 300}),
    "lgbm_slow": Exp("lgbm_slow", params={"learning_rate": 0.015}, rounds=3000),
    "lgbm_fast": Exp("lgbm_fast", params={"learning_rate": 0.06}, rounds=700),
    "lgbm_bag3": Exp("lgbm_bag3", seeds=3),
    "lgbm_est_anchor": Exp("lgbm_est_anchor", anchor="bm0_est_price"),
    "lgbm_stack": Exp("lgbm_stack", niv_stack=True),
    "lgbm_2023": Exp("lgbm_2023", train_from="2023-01-15"),
    "lgbm_2023_stack": Exp("lgbm_2023_stack", train_from="2023-01-15", niv_stack=True),
    "lgbm_side": Exp("lgbm_side", train_from="2023-01-15", niv_stack=True, side_stack=True),
    "lgbm_ff05": Exp("lgbm_ff05", train_from="2023-01-15", niv_stack=True,
                     params={"feature_fraction": 0.5, "min_data_in_leaf": 200}),
    "lgbm_2023_stack_bag3": Exp("lgbm_2023_stack_bag3", train_from="2023-01-15", niv_stack=True, seeds=3),
}

# Open-data additions (Nov 2026, spf.extra_features), by feature-name prefix
NEW_GROUPS = {"weather": ["wx_"], "dayahead": ["da_"], "mel": ["mel_", "hr_"], "remit": ["remit_"],
              "syswarn": ["syswarn_"]}
NEW_ALL = [p for v in NEW_GROUPS.values() for p in v]
EXPS["base_stack"] = Exp("base_stack", train_from="2023-01-15", niv_stack=True, drop=NEW_ALL)
EXPS["open_all"] = Exp("open_all", train_from="2023-01-15", niv_stack=True)
for _g, _p in NEW_GROUPS.items():
    EXPS[f"open_no_{_g}"] = Exp(f"open_no_{_g}", train_from="2023-01-15", niv_stack=True, drop=_p)
    EXPS[f"base_plus_{_g}"] = Exp(f"base_plus_{_g}", train_from="2023-01-15", niv_stack=True,
                                  drop=[x for x in NEW_ALL if x not in _p])
EXPS["open_all_bag3"] = Exp("open_all_bag3", train_from="2023-01-15", niv_stack=True, seeds=3)


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


NIV_PARAMS = {"learning_rate": 0.06, "num_leaves": 63, "min_data_in_leaf": 100, "feature_fraction": 0.7,
              "bagging_fraction": 0.8, "bagging_freq": 1, "verbose": -1, "num_threads": 0}


def add_niv_stack(train: pd.DataFrame, test: pd.DataFrame, feats: list[str], folds: int = 4,
                  side: bool = False, anchor: str = "mid_l0"):
    """NIV regression and P(short) per horizon. Training rows get out-of-fold predictions from models fitted on
    the other contiguous time blocks; test rows get predictions from models fitted on all training rows."""
    train, test = train.copy(), test.copy()
    models: dict[str, lgb.Booster] = {}
    blocks = np.array_split(np.arange(len(train)), folds)
    for h in HORIZONS:
        lab = train[f"niv_y{h}"]
        ok = lab.notna().to_numpy()
        for name, obj, y in ((f"nivhat{h}", "l2", lab), (f"pshort{h}", "binary", (lab > 0).astype(float))):
            oof = np.full(len(train), np.nan)
            for b in blocks:
                fit = np.ones(len(train), bool)
                fit[b] = False
                fit &= ok
                m = lgb.train({**NIV_PARAMS, "objective": obj}, lgb.Dataset(train[feats][fit], y[fit]), 400)
                oof[b] = m.predict(train[feats].iloc[b])
            train[name] = oof
            m = lgb.train({**NIV_PARAMS, "objective": obj}, lgb.Dataset(train[feats][ok], y[ok]), 400)
            test[name] = m.predict(test[feats])
            models[name] = m
        if not side:
            continue
        a_tr = train[anchor].fillna(train["ssp_l1"])
        a_te = test[anchor].fillna(test["ssp_l1"])
        resid = train[f"y{h}"] - a_tr
        for name, mask in ((f"pifshort{h}", lab > 0), (f"piflong{h}", lab <= 0)):
            ok2 = (mask & resid.notna()).to_numpy()
            oof = np.full(len(train), np.nan)
            for b in blocks:
                fit = np.ones(len(train), bool)
                fit[b] = False
                fit &= ok2
                m = lgb.train({**NIV_PARAMS, "objective": "l1"}, lgb.Dataset(train[feats][fit], resid[fit]), 400)
                oof[b] = m.predict(train[feats].iloc[b])
            train[name] = oof + a_tr.to_numpy()
            m = lgb.train({**NIV_PARAMS, "objective": "l1"}, lgb.Dataset(train[feats][ok2], resid[ok2]), 400)
            test[name] = m.predict(test[feats]) + a_te.to_numpy()
            models[name] = m
    return train, test, models


def stack_names(exp: "Exp") -> list[str]:
    names = []
    if exp.niv_stack:
        names += [f"nivhat{h}" for h in HORIZONS] + [f"pshort{h}" for h in HORIZONS]
    if exp.side_stack:
        names += [f"pifshort{h}" for h in HORIZONS] + [f"piflong{h}" for h in HORIZONS]
    return names


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
        if exp.niv_stack:
            train, test, _ = add_niv_stack(train, test, feats, side=exp.side_stack, anchor=exp.anchor or "mid_l0")
        hfeats = feats + stack_names(exp)
        for h in HORIZONS:
            y = f"y{h}"
            tr = train[train[y].notna()]
            anchor_tr = tr[exp.anchor].fillna(tr["ssp_l1"]) if exp.anchor else 0.0
            target = tr[y] - anchor_tr
            w = None
            if exp.weight_recent > 0:
                age = (m0 - tr.index).total_seconds() / 86400
                w = np.asarray(0.5 ** (age / exp.weight_recent))
            params = {"objective": exp.objective, "learning_rate": 0.03, "num_leaves": 63, "min_data_in_leaf": 100,
                      "feature_fraction": 0.7, "bagging_fraction": 0.8, "bagging_freq": 1, "lambda_l2": 1.0,
                      "verbose": -1, "num_threads": 0, **exp.params}
            preds = []
            for seed in range(exp.seeds):
                model = lgb.train({**params, "seed": seed}, lgb.Dataset(tr[hfeats], target, weight=w),
                                  num_boost_round=exp.rounds)
                preds.append(model.predict(test[hfeats]))
            anchor_te = test[exp.anchor].fillna(test["ssp_l1"]) if exp.anchor else 0.0
            pm[h] = np.mean(preds, axis=0) + anchor_te
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
