"""Feature frame: one row per forecast, built only from what was published before the forecast time.

A forecast is issued at tau = start of the current period SP0 + ISSUE_MIN minutes and predicts the
system price for SP+1, SP+2 and SP+3. Every input is joined "as of" tau with a margin for its
measured publication delay (see results/data-availability.md):

| input | rule at tau |
|---|---|
| system price, NIV (bmdash) | periods whose end + PRICE_LAG <= tau (SP-1 and earlier) |
| MID (APX) | periods whose start + MID_LAG <= tau |
| IMBALNGC, MELNGC, NDF, TSDF, LOLPDRM, WINDFOR | latest version with publishTime <= tau - MARGIN |
| INDO, FUELINST outturns | publishTime <= tau - MARGIN |
| live BM state (spf.bm_live) | acceptances issued by tau - BOALF_LAG, against final PNs; SP0..SP+2 only |

    uv run python -m spf.features      # writes data/features/frame.parquet
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

os.environ.setdefault("BMDASH_DATA", str(Path("data/bmdash").resolve()))

import holidays  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from bmdash import store  # noqa: E402

from spf import bm_live  # noqa: E402

warnings.simplefilter("ignore", pd.errors.PerformanceWarning)

ISSUE_MIN = bm_live.ISSUE_MIN  # 25
PRICE_LAG = 21   # minutes after period end; measured max 19.5 (createdDateTime) + API delay
MID_LAG = 20     # minutes after period start; measured (see data-availability.md)
MARGIN = 3       # minutes between a stated publishTime and the data being fetchable
HH = pd.Timedelta(minutes=30)
EL = Path("data/elexon")
OUT = Path("data/features/frame.parquet")


def _read(ds: str, cols: list[str] | None = None) -> pd.DataFrame:
    files = sorted((EL / ds).glob("*.parquet"))
    parts = [pd.read_parquet(f) for f in files]
    parts = [p[cols] if cols else p for p in parts if len(p) and (cols is None or set(cols) <= set(p.columns))]
    df = pd.concat(parts, ignore_index=True)
    for c in ("publishTime", "startTime"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True).dt.as_unit("ns")
    return df


def _asof(base: pd.DataFrame, versions: pd.DataFrame, target_col: str, value_cols: list[str],
          prefix: str, max_lead: pd.Timedelta | None = None) -> pd.DataFrame:
    """For each row of base (tau, target_col), the latest version with publishTime <= tau - MARGIN."""
    v = versions.rename(columns={"startTime": target_col})
    if max_lead is not None:
        v = v[(v[target_col] - v.publishTime) <= max_lead]
    v = v.sort_values("publishTime")
    left = base[["tau", target_col]].copy()
    left["_avail"] = left.tau - pd.Timedelta(minutes=MARGIN)
    left = left.reset_index().sort_values("_avail")
    m = pd.merge_asof(left, v[["publishTime", target_col, *value_cols]], left_on="_avail",
                      right_on="publishTime", by=target_col, direction="backward")
    m = m.set_index("sp0").sort_index()
    out = m[value_cols].add_prefix(prefix)
    out[f"{prefix}age_min"] = (m.tau - m.publishTime).dt.total_seconds() / 60
    return out


def build(start: str = "2024-01-01") -> pd.DataFrame:
    con = store.connect()
    sp = con.sql("""select start_time, ssp, sbp, niv, price_derivation_code, accepted_offer_volume,
                           accepted_bid_volume, adj_sell_volume, adj_buy_volume
                    from system_prices order by start_time""").df()
    sp["start_time"] = pd.to_datetime(sp.start_time, utc=True).dt.as_unit("ns")
    sp = sp.drop_duplicates("start_time", keep="last").set_index("start_time")
    grid = pd.date_range(max(sp.index.min(), pd.Timestamp(start, tz="UTC")), sp.index.max(), freq=HH)
    sp = sp.reindex(grid)
    f = pd.DataFrame(index=grid)
    f.index.name = "sp0"
    f["tau"] = f.index + pd.Timedelta(minutes=ISSUE_MIN)

    # ---- targets
    for h in (1, 2, 3):
        f[f"y{h}"] = sp.ssp.shift(-h)
        f[f"niv_y{h}"] = sp.niv.shift(-h)

    # ---- price history: latest known period is SP-k0
    k0 = 1
    while 30 * (1 - k0) + PRICE_LAG > ISSUE_MIN:  # period SP-k ends at sp0 + 30*(1-k)
        k0 += 1
    for k in range(k0, k0 + 6):
        f[f"ssp_l{k}"] = sp.ssp.shift(k)
        f[f"niv_l{k}"] = sp.niv.shift(k)
    f["offer_vol_l1"] = sp.accepted_offer_volume.shift(k0)
    f["bid_vol_l1"] = sp.accepted_bid_volume.shift(k0)
    f["pdc_l1"] = sp.price_derivation_code.shift(k0).astype("category").cat.codes
    known = sp.ssp.shift(k0)
    for w in (6, 48, 336):
        f[f"ssp_mean{w}"] = known.rolling(w, min_periods=w // 2).mean()
        f[f"ssp_std{w}"] = known.rolling(w, min_periods=w // 2).std()
    f["niv_mean6"] = sp.niv.shift(k0).rolling(6, min_periods=3).mean()
    f["short_share48"] = (sp.niv.shift(k0) > 0).rolling(48, min_periods=24).mean()
    for h in (1, 2, 3):
        f[f"ssp_yday_h{h}"] = sp.ssp.shift(48 - h)
        f[f"ssp_lweek_h{h}"] = sp.ssp.shift(336 - h)
        # mean price at this time of day over the last 7 days
        f[f"ssp_tod7_h{h}"] = pd.concat([sp.ssp.shift(48 * d - h) for d in range(1, 8)], axis=1).mean(axis=1)

    # ---- MID (APX)
    mid = pd.concat(pd.read_parquet(p) for p in sorted(Path("data/mid").glob("*.parquet")))
    apx = mid[mid.provider == "APXMIDP"].copy()
    apx["start_time"] = pd.to_datetime(apx.start_time, utc=True).dt.as_unit("ns")
    apx = apx.drop_duplicates("start_time", keep="last").set_index("start_time").reindex(grid)
    apx.loc[apx.volume <= 0, "price"] = np.nan
    m0 = 0
    while 30 * m0 + MID_LAG > ISSUE_MIN:  # latest period whose MID is out by tau
        m0 -= 1
    for k in range(0, 6):
        f[f"mid_l{k}"] = apx.price.shift(k - m0)
        f[f"midvol_l{k}"] = apx.volume.shift(k - m0)
    f["mid_mean48"] = apx.price.shift(-m0).rolling(48, min_periods=24).mean()
    f["ssp_minus_mid_l1"] = f[f"ssp_l{k0}"] - apx.price.shift(k0)
    f["ssp_minus_mid_mean48"] = (sp.ssp.shift(k0) - apx.price.shift(k0)).rolling(48, min_periods=24).mean()

    # ---- NESO / Elexon forecasts, as of tau, for SP0..SP+3
    lead = pd.Timedelta(hours=6)
    specs = {
        "IMBALNGC": (["imbalance"], "imbal"),
        "MELNGC": (["margin"], "margin"),
        "TSDF": (["demand"], "tsdf"),
        "NDF": (["demand"], "ndf"),
    }
    for ds, (cols, name) in specs.items():
        v = _read(ds, ["publishTime", "startTime", *cols])
        for h in range(0, 4):
            base = pd.DataFrame({"tau": f.tau, "target": f.index + h * HH})
            a = _asof(base, v, "target", cols, f"{name}_h{h}_", lead)
            f[f"{name}_h{h}"] = a[f"{name}_h{h}_{cols[0]}"]
            if h == 1:
                f[f"{name}_age"] = a[f"{name}_h{h}_age_min"]
        # revision over the last hour for SP+1
        base = pd.DataFrame({"tau": f.tau - pd.Timedelta(hours=1), "target": f.index + HH})
        prev = _asof(base, v, "target", cols, "p_", pd.Timedelta(hours=8))
        f[f"{name}_rev1h_h1"] = f[f"{name}_h1"] - prev[f"p_{cols[0]}"].to_numpy()
    lol = _read("LOLPDRM", ["publishTime", "startTime", "lossOfLoadProbability", "deratedMargin"])
    for h in (1, 2, 3):
        base = pd.DataFrame({"tau": f.tau, "target": f.index + h * HH})
        a = _asof(base, lol, "target", ["lossOfLoadProbability", "deratedMargin"], "x_", pd.Timedelta(hours=9))
        f[f"lolp_h{h}"] = a["x_lossOfLoadProbability"]
        f[f"drm_h{h}"] = a["x_deratedMargin"]
    wf = _read("WINDFOR", ["publishTime", "startTime", "generation"])
    for h in (0, 1, 2, 3):
        base = pd.DataFrame({"tau": f.tau, "target": (f.index + h * HH).floor("h")})
        a = _asof(base, wf, "target", ["generation"], "x_", pd.Timedelta(hours=48))
        f[f"windfor_h{h}"] = a["x_generation"]

    # ---- outturns: INDO (demand) and FUELINST (generation by fuel), latest as of tau
    indo = _read("INDO", ["publishTime", "startTime", "demand"]).sort_values("publishTime")
    left = pd.DataFrame({"_avail": f.tau - pd.Timedelta(minutes=MARGIN)}).reset_index()
    m = pd.merge_asof(left.sort_values("_avail"), indo.rename(columns={"startTime": "indo_start"}),
                      left_on="_avail", right_on="publishTime").set_index("sp0").sort_index()
    f["indo_last"] = m.demand
    f["indo_age"] = (f.tau - m.indo_start).dt.total_seconds() / 60
    # demand forecast error for the latest outturn period: NDF as published an hour before that period
    base = pd.DataFrame({"tau": m.indo_start - pd.Timedelta(minutes=60 - MARGIN), "target": m.indo_start})
    base = base.dropna()
    nd = _read("NDF", ["publishTime", "startTime", "demand"])
    a = _asof(base, nd, "target", ["demand"], "x_", pd.Timedelta(hours=6))
    f["ndf_err_last"] = f.indo_last - a["x_demand"].reindex(f.index)

    fuel = _read("FUELINST", ["publishTime", "startTime", "fuelType", "generation"])
    fuel["fuel"] = np.where(fuel.fuelType.str.startswith("INT"), "INT", fuel.fuelType)
    wide = fuel.pivot_table(index="publishTime", columns="fuel", values="generation", aggfunc="sum").sort_index()
    wide = wide[["CCGT", "OCGT", "WIND", "NUCLEAR", "PS", "BIOMASS", "INT", "NPSHYD"]].add_prefix("fi_")
    wide.index = wide.index.as_unit("ns")
    left = pd.DataFrame({"_avail": f.tau - pd.Timedelta(minutes=MARGIN)}).reset_index().sort_values("_avail")
    now = pd.merge_asof(left, wide.reset_index(), left_on="_avail", right_on="publishTime").set_index("sp0").sort_index()
    left30 = left.assign(_avail=left._avail - pd.Timedelta(minutes=30))
    ago = pd.merge_asof(left30, wide.reset_index(), left_on="_avail", right_on="publishTime").set_index("sp0").sort_index()
    for c in wide.columns:
        f[c] = now[c]
        f[f"{c}_d30"] = now[c] - ago[c]
    f["fi_age"] = (f.tau - now.publishTime).dt.total_seconds() / 60
    f["wind_err_now"] = f.fi_WIND - f.windfor_h0
    f["gas_total"] = f.fi_CCGT + f.fi_OCGT

    # ---- live BM state
    bm_files = sorted(Path("data/features/bm_live").glob("*.parquet"))
    if not bm_files:
        print("no live BM features yet: skipping them")
        bm_files = []
    bm = pd.concat([pd.read_parquet(p) for p in bm_files] or [pd.DataFrame(
        columns=["start_time", "cutoff", "offer_mwh", "bid_mwh", "offer_mwh_so", "bid_mwh_so"])])
    bm["start_time"] = pd.to_datetime(bm.start_time, utc=True).dt.as_unit("ns")
    bm["net_mwh"] = bm.offer_mwh + bm.bid_mwh
    bm["net_mwh_noso"] = bm.net_mwh - bm.offer_mwh_so - bm.bid_mwh_so
    bmcols = ["offer_mwh", "bid_mwh", "net_mwh", "net_mwh_noso", "offer_mwh_so", "bid_mwh_so", "n_offer_units",
              "n_bid_units", "n_acceptances", "offer_vwap", "offer_pmax", "bid_vwap", "bid_pmin",
              "recent_offer_mwh", "recent_bid_mwh"]
    for k in range(3):
        b = bm[bm.cutoff == f"k{k}"].drop_duplicates("start_time").set_index("start_time").reindex(columns=bmcols)
        b = b.reindex(f.index + k * HH)
        b.index = f.index
        for c in bmcols:
            f[f"bm{k}_{c}"] = b[c]
    f["bm_have"] = f.bm0_net_mwh.notna().astype(int)

    # ---- calendar (local time)
    loc = (f.index + HH).tz_convert("Europe/London")  # SP+1 local time
    f["hour"] = loc.hour + loc.minute / 60
    f["dow"] = loc.dayofweek
    f["month"] = loc.month
    hol = holidays.UK(subdiv="ENG", years=range(2023, 2028))
    f["holiday"] = pd.Index(loc.date).isin(list(hol.keys())).astype(int)
    f["weekend"] = (f.dow >= 5).astype(int) | f.holiday
    return f


def main() -> None:
    f = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    f.to_parquet(OUT)
    print(OUT, f.shape)
    cover = f.notna().mean().sort_values()
    print(cover.head(25).round(3).to_string())


if __name__ == "__main__":
    main()
