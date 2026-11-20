"""Features from the open-data inputs added in Nov 2026, each as known at the forecast time tau.

| prefix | source | rule at tau |
|---|---|---|
| wx_ | NOAA GFS 0.5 deg runs (spf.weather) | latest run with avail_time <= tau |
| da_ | N2EX day-ahead hourly prices (spf.dayahead) | cleared the day before delivery |
| mel_, hr_ | MEL in force by technology (spf.mel_agg), minus PN | MEL at tau - 5 min; PN final at gate closure |
| remit_ | REMIT outage messages, every revision (spf.ingest REMIT) | latest revision with publishTime <= tau - MARGIN |
| syswarn_ | NESO system warnings (spf.ingest SYSWARN) | publishTime <= tau - MARGIN |
| eu_ | Continental day-ahead prices, FR NL BE DE-LU NO2 DK1 (spf.eu_dayahead) | auction day D known from D-1 14:00 CET |

Each block is skipped (with a message) if its data has not been fetched, so the frame still builds.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HH = pd.Timedelta(minutes=30)
EL = Path("data/elexon")
MAX_EVENT = pd.Timedelta(days=180)  # longer "outages" are closures and mothballing, not news


def _np(x) -> np.ndarray:
    """Naive UTC datetime64[ns] array from a tz-aware Series or Index."""
    x = pd.Series(x) if isinstance(x, pd.Index) else x
    return x.dt.tz_convert(None).dt.as_unit("ns").to_numpy()


def _read_days(ds: str) -> pd.DataFrame:
    files = sorted((EL / ds).glob("*.parquet"))
    parts = [pd.read_parquet(f) for f in files]
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def remit(sp0: pd.DatetimeIndex, tau: pd.Series, margin: int) -> pd.DataFrame:
    df = _read_days("REMIT")
    if df.empty:
        print("no REMIT data: skipping remit_ features")
        return pd.DataFrame(index=sp0)
    df = df[df.eventType.isin(["Production unavailability", "Transmission unavailability"])].copy()
    for c in ("publishTime", "eventStartTime", "eventEndTime"):
        df[c] = pd.to_datetime(df[c], utc=True).dt.as_unit("ns")
    df = df.drop_duplicates(["mrid", "revisionNumber"]).sort_values(["mrid", "publishTime"])
    # each revision is the current word on its event from its publishTime until the next revision's
    df["known_to"] = df.groupby("mrid").publishTime.shift(-1).fillna(pd.Timestamp("2200-01-01", tz="UTC").as_unit("ns"))
    df["first_pub"] = df.groupby("mrid").publishTime.transform("min")
    active = df.eventStatus.fillna("Active").eq("Active")
    short = (df.eventEndTime - df.eventStartTime) <= MAX_EVENT
    df = df[active & short & df.unavailableCapacity.notna()]
    unpl = df.unavailabilityType.eq("Unplanned").to_numpy()
    gas = df.fuelType.fillna("").str.contains("Gas").to_numpy()
    trans = df.eventType.eq("Transmission unavailability").to_numpy()
    pub, kto, first = _np(df.publishTime), _np(df.known_to), _np(df.first_pub)
    es, ee = _np(df.eventStartTime), _np(df.eventEndTime)
    mw = df.unavailableCapacity.to_numpy(float)

    names = ["remit_unpl_mw_h1", "remit_pl_mw_h1", "remit_gas_unpl_mw_h1", "remit_trans_mw_h1",
             "remit_unpl_mw_h3", "remit_unpl_new1h", "remit_unpl_new3h", "remit_unpl_started3h",
             "remit_unpl_n_h1"]
    out = {n: np.full(len(sp0), np.nan) for n in names}
    avail = _np(tau - pd.Timedelta(minutes=margin))
    tgt1 = _np(sp0 + HH + HH / 2)
    tgt3 = _np(sp0 + 3 * HH + HH / 2)
    # process a day at a time against the revisions that could matter that day
    day = pd.Series(np.arange(len(sp0)), index=sp0).groupby(sp0.floor("D"))
    for d, rows in day:
        ii = rows.to_numpy()
        a0, a1 = avail[ii].min(), avail[ii].max()
        cand = (pub <= a1) & (kto > a0) & (ee > tgt1[ii].min()) & (es <= tgt3[ii].max())
        if not cand.any():
            for n in names:
                out[n][ii] = 0.0
            continue
        p, k, s, e, m = pub[cand], kto[cand], es[cand], ee[cand], mw[cand]
        u, g, tr, f1 = unpl[cand], gas[cand], trans[cand], first[cand]
        A = avail[ii][:, None]
        cur = (p <= A) & (k > A)                       # this revision is the latest known at tau
        on1 = cur & (s <= tgt1[ii][:, None]) & (e > tgt1[ii][:, None])
        on3 = cur & (s <= tgt3[ii][:, None]) & (e > tgt3[ii][:, None])
        out["remit_unpl_mw_h1"][ii] = (on1 & u & ~tr) @ m
        out["remit_pl_mw_h1"][ii] = (on1 & ~u & ~tr) @ m
        out["remit_gas_unpl_mw_h1"][ii] = (on1 & u & g) @ m
        out["remit_trans_mw_h1"][ii] = (on1 & tr) @ m
        out["remit_unpl_mw_h3"][ii] = (on3 & u & ~tr) @ m
        out["remit_unpl_n_h1"][ii] = (on1 & u & ~tr).sum(axis=1)
        for name, hrs in (("remit_unpl_new1h", 1), ("remit_unpl_new3h", 3)):
            new = cur & u & ~tr & (f1 > A - np.timedelta64(hrs, "h")) & (e > A)
            out[name][ii] = new @ m
        started = cur & u & ~tr & (s > A - np.timedelta64(3, "h")) & (s <= A) & (e > A)
        out["remit_unpl_started3h"][ii] = started @ m
    return pd.DataFrame(out, index=sp0)


SYSWARN_KINDS = {  # name -> warningType values (2023-2026 counts: SO-SO 1287, IT outage 295, other 133, ...)
    "margin": ("ELECTRICITY MARGIN NOTICE", "CAPACITY MARKET NOTICE"),
    "nrapm": ("NEGATIVE RESERVE ACTIVE POWER MARGIN",),
    "dfs": ("DEMAND FLEXIBILITY SERVICE",),
    "other": ("OTHER",),
    "sosotrade": ("SO-SO TRADES",),
}


def syswarn(sp0: pd.DatetimeIndex, tau: pd.Series, margin: int) -> pd.DataFrame:
    df = _read_days("SYSWARN")
    if df.empty:
        print("no SYSWARN data: skipping syswarn_ features")
        return pd.DataFrame(index=sp0)
    df["publishTime"] = pd.to_datetime(df.publishTime, utc=True).dt.as_unit("ns")
    wtype = df.warningType.fillna("").str.upper().str.strip()
    out = pd.DataFrame(index=sp0)
    avail = _np(tau - pd.Timedelta(minutes=margin))
    for name, key in SYSWARN_KINDS.items():
        t = np.sort(_np(df.publishTime[wtype.isin(key)]))
        n_now = np.searchsorted(t, avail, side="right")
        n_12h = np.searchsorted(t, avail - np.timedelta64(12, "h"), side="right")
        out[f"syswarn_{name}_12h"] = n_now - n_12h
        last = np.where(n_now > 0, t[np.maximum(n_now - 1, 0)], np.datetime64("NaT"))
        out[f"syswarn_{name}_age_h"] = np.minimum((avail - last) / np.timedelta64(1, "h"), 24 * 7)
    return out


def mel(f: pd.DataFrame) -> pd.DataFrame:
    files = sorted(Path("data/features/mel_agg").glob("*.parquet"))
    if not files:
        print("no MEL aggregates: skipping mel_/hr_ features")
        return pd.DataFrame(index=f.index)
    m = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    m["start_time"] = pd.to_datetime(m.start_time, utc=True).dt.as_unit("ns")
    m = m.drop_duplicates("start_time").set_index("start_time").drop(columns=["settlement_date"])
    m = m.reindex(f.index)
    m = m.drop(columns=[c for c in m.columns if c.startswith(("mel_coal", "mel_interconnector", "mel_units"))])
    out = m.copy()
    for k in (0, 1, 2):
        def pn(c, k=k):
            return f.get(f"{c}_h{k}", np.nan)
        out[f"hr_gas_h{k}"] = m.mel_gas - pn("pn_gas_mw_mean")
        out[f"hr_battery_h{k}"] = m.mel_battery - pn("pn_battery_mw_mean")
        out[f"hr_ps_h{k}"] = m.mel_pumped_storage - pn("pn_pumped_storage_mw_mean")
        out[f"hr_flex_h{k}"] = out[f"hr_gas_h{k}"] + out[f"hr_battery_h{k}"] + out[f"hr_ps_h{k}"]
        out[f"hr_total_h{k}"] = m.mel_total - pn("pn_total_gen")
    return out


def build(f: pd.DataFrame, margin: int) -> pd.DataFrame:
    from spf import dayahead, weather

    parts = []
    if any(weather.OUT.glob("*.parquet")):
        parts.append(weather.features(f.index, f.tau))
    else:
        print("no GFS data: skipping wx_ features")
    if dayahead.OUT.exists():
        da = dayahead.features(f.index, f.tau)
        da["da_minus_mid"] = da.da_h1 - f.mid_l0
        parts.append(da)
    else:
        print("no day-ahead prices: skipping da_ features")
    from spf import eu_dayahead
    if eu_dayahead.OUT.exists():
        gb = parts[-1] if parts and "da_h1" in parts[-1] else None
        parts.append(eu_dayahead.features(f.index, f.tau, gb))
    else:
        print("no continental day-ahead prices: skipping eu_ features")
    parts.append(mel(f))
    parts.append(remit(f.index, f.tau, margin))
    parts.append(syswarn(f.index, f.tau, margin))
    out = pd.concat(parts, axis=1)
    # weather against NESO's own forecasts: disagreement hints at forecast error
    if "wx_wind_cf_h1" in out and "windfor_h1" in f:
        out["wx_wind_vs_neso_h1"] = out.wx_wind_cf_h1 - f.windfor_h1 / f.windfor_h1.rolling(48 * 28, min_periods=48).max()
    return out
