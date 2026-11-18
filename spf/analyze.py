"""Error analysis of a model's walk-forward predictions against the baselines.

    uv run python -m spf.analyze lgbm_mid_dev [--out results/model-dev.md]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from spf.model import HORIZONS, PRED, load_frame

BANDS = [-np.inf, 0, 50, 100, 150, 250, np.inf]
BAND_LABELS = ["< 0", "0–50", "50–100", "100–150", "150–250", "≥ 250"]


def md(df: pd.DataFrame, fmt: str = ".1f") -> str:
    def f(v):
        if isinstance(v, (float, np.floating)):
            return "" if np.isnan(v) else format(v, fmt)
        return str(v)
    head = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "|" + "|".join("---:" if pd.api.types.is_numeric_dtype(df[c]) else "---" for c in df.columns) + "|"
    return "\n".join([head, sep, *("| " + " | ".join(f(v) for v in r) + " |" for r in df.itertuples(index=False))])


def long_frame(df: pd.DataFrame, pred: pd.DataFrame) -> pd.DataFrame:
    j = df.join(pred, how="inner")
    out = []
    for h in HORIZONS:
        g = pd.DataFrame({
            "horizon": f"SP+{h}", "target_start": j.index + h * pd.Timedelta(minutes=30),
            "actual": j[f"y{h}"], "niv": j[f"niv_y{h}"], "model": j[f"pred{h}"],
            "last_known": j["ssp_l1"], "latest_mid": j["mid_l0"].fillna(j["ssp_l1"]),
            "niv_last": j["niv_l1"], "mid": j["mid_l0"],
        }, index=j.index)
        out.append(g[g.actual.notna()])
    return pd.concat(out)


def report(name: str) -> str:
    df = load_frame()
    pred = pd.read_parquet(PRED / f"{name}.parquet")
    L = long_frame(df, pred)
    L["err"] = L.model - L.actual
    L["ae"] = L.err.abs()
    L["band"] = pd.cut(L.actual, BANDS, labels=BAND_LABELS, right=False)
    loc = L.target_start.dt.tz_convert("Europe/London")
    L["hour"] = loc.dt.hour
    L["month"] = loc.dt.tz_localize(None).dt.to_period("M").astype(str)
    L["flip"] = np.sign(L.niv) != np.sign(L.niv_last)
    out = [f"# Error analysis: `{name}`\n",
           f"{L.index.min():%Y-%m-%d} to {L.index.max():%Y-%m-%d}, {len(L) // len(HORIZONS):,} forecasts per horizon.\n"]

    rows = []
    for h, g in L.groupby("horizon"):
        for c in ("model", "latest_mid", "last_known"):
            e = g[c] - g.actual
            rows.append({"horizon": h, "forecast": c, "MAE": e.abs().mean(), "RMSE": np.sqrt((e ** 2).mean()),
                         "median AE": e.abs().median(), "bias": e.mean()})
    out += ["## Overall (£/MWh)\n", md(pd.DataFrame(rows)), ""]

    t = L.groupby(["horizon", "band"], observed=True).agg(n=("ae", "size"), model=("ae", "mean")).reset_index()
    t["latest MID"] = L.groupby(["horizon", "band"], observed=True).apply(
        lambda g: (g.latest_mid - g.actual).abs().mean(), include_groups=False).to_numpy()
    t["bias"] = L.groupby(["horizon", "band"], observed=True).err.mean().to_numpy()
    out += ["## MAE by band of the actual price\n", md(t), ""]

    # direction: the model's implied side is whether it forecasts above or below the latest MID
    rows = []
    for h, g in L.groupby("horizon"):
        g = g[g.mid.notna()]
        short = g.niv > 0
        rows.append({"horizon": h, "model above/below latest MID": ((g.model > g.mid) == short).mean(),
                     "persist last NIV sign": ((g.niv_last > 0) == short).mean(),
                     "always short": short.mean()})
    out += ["## Direction hit rate (long vs short)\n", md(pd.DataFrame(rows), ".3f"), ""]

    t = L.groupby(["horizon", "flip"]).agg(n=("ae", "size"), MAE=("ae", "mean")).reset_index()
    t["flip"] = t.flip.map({True: "side changed vs SP−1", False: "same side as SP−1"})
    out += ["## Error when the system changes side\n", md(t), ""]

    t = L[L.horizon == "SP+1"].groupby("hour").agg(MAE=("ae", "mean"), mean_price=("actual", "mean")).reset_index()
    out += ["## SP+1 MAE by local hour\n", md(t), ""]

    t = L.groupby(["month", "horizon"]).agg(MAE=("ae", "mean")).unstack("horizon").droplevel(0, axis=1).reset_index()
    base = L[L.horizon == "SP+1"].groupby("month").apply(lambda g: (g.latest_mid - g.actual).abs().mean(),
                                                         include_groups=False)
    t["SP+1 latest MID"] = base.to_numpy()
    out += ["## MAE by month\n", md(t), ""]

    s = L.ae.sort_values(ascending=False)
    out.append(f"Worst 5% of forecasts carry {s.iloc[:len(s) // 20].sum() / s.sum():.0%} of total absolute error.\n")
    w = L.nlargest(10, "ae")[["horizon", "target_start", "actual", "model", "latest_mid", "last_known"]]
    w["target_start"] = w.target_start.dt.strftime("%Y-%m-%d %H:%M")
    out += ["## Ten worst misses\n", md(w), ""]
    return "\n".join(out)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("name")
    p.add_argument("--out")
    a = p.parse_args()
    text = report(a.name)
    Path(a.out or f"results/analysis-{a.name}.md").write_text(text, encoding="utf-8")
    print(a.out or f"results/analysis-{a.name}.md")
