"""Build results/forecast-report.pdf: the final model's accuracy and errors, with charts.

    uv run python -m spf.report_pdf [--exp lgbm_2023_stack_bag3]

Reads the walk-forward predictions in data/predictions/<exp>_{dev,test}.parquet, the feature frame (for the
actual prices and the baselines) and results/experiments.jsonl. Charts are also saved to results/figures/.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
from reportlab import rl_config  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer,  # noqa: E402
                                Table, TableStyle)

# Plain binary streams (no ASCII85) and flat RGB images (no alpha masks): readable by every PDF viewer.
rl_config.useA85 = 0


def _register_fonts() -> None:
    """Embed DejaVu (shipped with matplotlib) so the PDF does not rely on the viewer's own Helvetica."""
    from matplotlib import font_manager
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    d = Path(font_manager.findfont("DejaVu Sans")).parent
    for name, f in (("DejaVu", "DejaVuSans.ttf"), ("DejaVu-Bold", "DejaVuSans-Bold.ttf"),
                    ("DejaVuMono", "DejaVuSansMono.ttf")):
        pdfmetrics.registerFont(TTFont(name, str(d / f)))
    addMapping("DejaVu", 0, 0, "DejaVu")
    addMapping("DejaVu", 1, 0, "DejaVu-Bold")
    addMapping("DejaVu", 0, 1, "DejaVu")
    addMapping("DejaVu", 1, 1, "DejaVu-Bold")


_register_fonts()
rl_config.canvas_basefontname = "DejaVu"

FIG = Path("results/figures")
OUT = Path("results/forecast-report.pdf")
H = (1, 2, 3)

# Reference palette, categorical slots 1-3 (validated all-pairs), light mode.
C_MODEL, C_MID, C_LAST = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
NAMES = {"model": "Model", "mid": "Latest MID", "last": "Last known price"}
COLS = {"model": C_MODEL, "mid": C_MID, "last": C_LAST}

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": MUTED, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "axes.titlesize": 10, "axes.titleweight": "bold", "axes.titlecolor": INK, "axes.titlelocation": "left",
    "legend.frameon": False, "legend.fontsize": 8.5, "lines.linewidth": 2, "figure.dpi": 200,
})


def load(exp: str) -> dict[str, pd.DataFrame]:
    f = pd.read_parquet("data/features/frame.parquet",
                        columns=["mid_l0", "ssp_l1", "niv_l1", "y1", "y2", "y3", "niv_y1", "niv_y2", "niv_y3"])
    f = f.apply(pd.to_numeric, errors="coerce")
    out = {}
    for w in ("dev", "test"):
        p = pd.read_parquet(f"data/predictions/{exp}_{w}.parquet")
        g = f.loc[p.index]
        rows = []
        for h in H:
            d = pd.DataFrame({"h": h, "actual": g[f"y{h}"], "model": p[f"pred{h}"],
                              "mid": g.mid_l0.fillna(g.ssp_l1), "last": g.ssp_l1,
                              "flip": np.sign(g[f"niv_y{h}"]) != np.sign(g.niv_l1)}, index=p.index)
            rows.append(d.dropna(subset=["actual", "model", "last"]))
        out[w] = pd.concat(rows)
    return out


def mae(d: pd.DataFrame, c: str) -> float:
    return float((d[c] - d.actual).abs().mean())


def save(fig, name: str) -> Path:
    FIG.mkdir(parents=True, exist_ok=True)
    path = FIG / f"{name}.png"
    fig.savefig(path, bbox_inches="tight", facecolor="white", dpi=170)
    plt.close(fig)
    PILImage.open(path).convert("RGB").save(path, optimize=True)  # drop the alpha channel
    return path


def bar_labels(ax, bars, fmt="{:.1f}"):
    for b in bars:
        ax.annotate(fmt.format(b.get_height()), (b.get_x() + b.get_width() / 2, b.get_height()),
                    xytext=(0, 2), textcoords="offset points", ha="center", va="bottom", fontsize=7.5, color=INK2)


def fig_headline(D) -> Path:
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.9), sharey=True)
    for ax, w, title in zip(axes, ("dev", "test"), ("Development year (Apr 2025 – Apr 2026)",
                                                     "Held-out months (May – Nov 2026)")):
        x = np.arange(3)
        for i, c in enumerate(("model", "mid", "last")):
            vals = [mae(D[w][D[w].h == h], c) for h in H]
            bars = ax.bar(x + (i - 1) * 0.27, vals, 0.25, color=COLS[c], label=NAMES[c])
            bar_labels(ax, bars)
        ax.set_xticks(x, [f"SP+{h}" for h in H])
        ax.set_title(title, fontsize=9)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Mean absolute error (£/MWh)")
    axes[0].legend(loc="upper left", ncol=1)
    fig.tight_layout()
    return save(fig, "01_headline_mae")


def fig_example(D) -> tuple[Path, str]:
    t = D["test"][D["test"].h == 1].copy()
    t["ae"] = (t.model - t.actual).abs()
    daily = t.ae.resample("D").mean()
    daily = daily[daily.index >= "2026-10-01"]
    # a typical (median-error) autumn stretch of 3 days
    day = (daily.rolling(3).mean() - daily.rolling(3).mean().median()).abs().idxmin()
    w = t[(t.index >= day - pd.Timedelta(days=2)) & (t.index < day + pd.Timedelta(days=1))]
    tgt = w.index + pd.Timedelta(minutes=30)  # SP+1 period start
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    ax.plot(tgt, w.actual, color=INK, lw=1.6, label="Actual system price")
    ax.plot(tgt, w.model, color=C_MODEL, lw=1.6, label="Model forecast (SP+1)")
    ax.plot(tgt, w.mid, color=C_MID, lw=1.2, alpha=0.9, label="Latest MID")
    ax.set_ylabel("£/MWh")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%a %d %b\n%H:%M", tz="Europe/London"))
    ax.set_title("Three typical autumn days: SP+1 forecast against the actual price")
    fig.tight_layout()
    label = f"{(day - pd.Timedelta(days=2)):%d %b} – {day:%d %b %Y}"
    return save(fig, "02_example_days"), label


def fig_month(D) -> Path:
    d = pd.concat([D["dev"], D["test"]])
    fig, axes = plt.subplots(1, 3, figsize=(7.2, 2.7), sharey=True)
    for ax, h in zip(axes, H):
        g = d[d.h == h]
        m = pd.DataFrame({c: (g[c] - g.actual).abs().groupby(g.index.tz_convert(None).to_period("M")).mean()
                          for c in ("model", "mid")})
        x = m.index.to_timestamp()
        ax.axvspan(pd.Timestamp("2026-05-01"), x.max() + pd.Timedelta(days=20), color="#f1f0ec", lw=0)
        ax.plot(x, m["mid"], color=C_MID, label=NAMES["mid"], marker="o", ms=3)
        ax.plot(x, m["model"], color=C_MODEL, label=NAMES["model"], marker="o", ms=3)
        ax.set_title(f"SP+{h}", fontsize=9)
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%b\n%y"))
        ax.xaxis.set_major_locator(matplotlib.dates.MonthLocator(bymonth=[4, 8, 12]))
    axes[0].set_ylabel("MAE (£/MWh)")
    axes[0].legend(loc="upper left")
    axes[2].text(pd.Timestamp("2026-05-10"), axes[2].get_ylim()[0] + 1, "held out", fontsize=7.5, color=INK2)
    fig.tight_layout()
    return save(fig, "03_mae_by_month")


def fig_hist(D) -> Path:
    t = D["test"][D["test"].h == 1]
    fig, ax = plt.subplots(figsize=(7.2, 2.7))
    bins = np.arange(-100, 101, 4)
    for c in ("mid", "model"):
        e = (t[c] - t.actual).clip(-100, 100)
        ax.hist(e, bins=bins, histtype="step", lw=2, color=COLS[c], label=f"{NAMES[c]}", density=True)
    ax.axvline(0, color=MUTED, lw=0.8)
    ax.set_xlabel("Forecast − actual (£/MWh), SP+1, held-out months; tails clipped at ±100")
    ax.set_ylabel("Share of forecasts")
    ax.set_yticks([])
    ax.legend(loc="upper left")
    ax.set_title("Distribution of SP+1 errors: the model's errors cluster much closer to zero")
    fig.tight_layout()
    return save(fig, "04_error_distribution")


def fig_scatter(D) -> Path:
    t = D["test"][D["test"].h == 1]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 3.3), sharey=True)
    lim = (-100, 400)
    for ax, c in zip(axes, ("model", "mid")):
        ax.hexbin(t[c].clip(*lim), t.actual.clip(*lim), gridsize=55, extent=lim + lim, mincnt=1, bins="log",
                  cmap="Blues" if c == "model" else "Oranges", linewidths=0)
        ax.plot(lim, lim, color=INK2, lw=0.8, ls="--")
        ax.set_xlim(lim)
        ax.set_ylim(lim)
        ax.set_xlabel(f"{NAMES[c]} (£/MWh)")
        ax.set_title(f"{NAMES[c]} vs actual, SP+1", fontsize=9)
        ax.set_aspect("equal")
    axes[0].set_ylabel("Actual price (£/MWh)")
    fig.tight_layout()
    return save(fig, "05_scatter")


def fig_bands(D) -> Path:
    t = D["test"]
    edges = [-np.inf, 0, 50, 100, 150, 250, np.inf]
    labels = ["< 0", "0–50", "50–100", "100–150", "150–250", "≥ 250"]
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.9), gridspec_kw={"width_ratios": [2.2, 1]})
    g = t[t.h == 1]
    band = pd.cut(g.actual, edges, labels=labels)
    m = pd.DataFrame({c: (g[c] - g.actual).abs().groupby(band, observed=False).mean() for c in ("model", "mid")})
    x = np.arange(len(labels))
    for i, c in enumerate(("model", "mid")):
        bars = axes[0].bar(x + (i - 0.5) * 0.38, m[c], 0.36, color=COLS[c], label=NAMES[c])
        bar_labels(axes[0], bars, "{:.0f}")
    axes[0].set_xticks(x, labels)
    axes[0].set_xlabel("Actual price band (£/MWh)")
    axes[0].set_ylabel("MAE (£/MWh), SP+1")
    axes[0].legend(loc="upper left")
    axes[0].grid(axis="x", visible=False)
    axes[0].set_title("Error by actual price level", fontsize=9)
    n = band.value_counts().reindex(labels)
    bars = axes[1].barh(x, n / n.sum() * 100, color=MUTED, height=0.6)
    axes[1].set_yticks(x, labels)
    axes[1].invert_yaxis()
    axes[1].set_xlabel("% of periods")
    axes[1].grid(axis="y", visible=False)
    axes[1].set_title("How often each band occurs", fontsize=9)
    for b, v in zip(bars, n / n.sum() * 100):
        axes[1].annotate(f"{v:.0f}%", (b.get_width(), b.get_y() + b.get_height() / 2), xytext=(2, 0),
                         textcoords="offset points", va="center", fontsize=7.5, color=INK2)
    fig.tight_layout()
    return save(fig, "06_mae_by_price_band")


def fig_hour(D) -> Path:
    t = D["test"]
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    for h, ls in zip(H, ("-", "--", ":")):
        g = t[t.h == h]
        hr = (g.index + pd.Timedelta(minutes=30 * h)).tz_convert("Europe/London").hour
        ax.plot(range(24), (g.model - g.actual).abs().groupby(hr).mean().reindex(range(24)), color=C_MODEL, ls=ls,
                label=f"Model SP+{h}")
    g = t[t.h == 1]
    hr = (g.index + pd.Timedelta(minutes=30)).tz_convert("Europe/London").hour
    ax.plot(range(24), (g.mid - g.actual).abs().groupby(hr).mean().reindex(range(24)), color=C_MID,
            label="Latest MID (SP+1)")
    ax.set_xticks(range(0, 24, 2))
    ax.set_xlabel("Hour of the target period (UK time)")
    ax.set_ylabel("MAE (£/MWh)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=4)
    ax.set_title("Error by time of day: highest over the evening peak (16:00–19:00)")
    fig.tight_layout()
    return save(fig, "07_mae_by_hour")


def fig_flip(D) -> Path:
    t = D["test"]
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    x = np.arange(3)
    for i, (flip, lab, col) in enumerate(((False, "System stays on the same side as SP−1", C_MODEL),
                                           (True, "System changes side (long ↔ short)", "#6250d6"))):
        vals = [mae(t[(t.h == h) & (t.flip == flip)], "model") for h in H]
        share = [float(t[t.h == h].flip.eq(flip).mean()) * 100 for h in H]
        bars = ax.bar(x + (i - 0.5) * 0.38, vals, 0.36, color=col, label=lab)
        for b, s in zip(bars, share):
            ax.annotate(f"{b.get_height():.1f}\n({s:.0f}% of periods)", (b.get_x() + b.get_width() / 2,
                        b.get_height()), xytext=(0, 2), textcoords="offset points", ha="center", va="bottom",
                        fontsize=7, color=INK2)
    ax.set_xticks(x, [f"SP+{h}" for h in H])
    ax.set_ylabel("Model MAE (£/MWh)")
    ax.set_ylim(0, ax.get_ylim()[1] * 1.25)
    ax.legend(loc="upper left")
    ax.grid(axis="x", visible=False)
    ax.set_title("Changes of side are the hardest periods")
    fig.tight_layout()
    return save(fig, "08_side_flip")


def fig_concentration(D) -> Path:
    t = D["test"]
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    for h, ls in zip(H, ("-", "--", ":")):
        e = np.sort((t[t.h == h].model - t[t.h == h].actual).abs().to_numpy())[::-1]
        ax.plot(np.arange(1, len(e) + 1) / len(e) * 100, np.cumsum(e) / e.sum() * 100, color=C_MODEL, ls=ls,
                label=f"SP+{h}")
    ax.plot([0, 100], [0, 100], color=MUTED, lw=0.8, ls="--")
    ax.set_xlabel("Worst x% of forecasts")
    ax.set_ylabel("% of total absolute error")
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.legend(loc="lower right")
    ax.set_title("A small share of periods carries much of the error")
    fig.tight_layout()
    return save(fig, "09_error_concentration")


STEPS = [("Latest MID price (baseline)", None, 20.50),
         ("LightGBM: prices, MID, NESO forecasts, outturns", "lgbm_mid_nobm", None),
         ("+ live balancing actions (BOALF)", None, 15.84),
         ("+ solar, cleaned frequency", None, 15.79),
         ("+ stacked NIV / P(short) forecasts", None, 14.96),
         ("+ training data from 2023", None, 14.53),
         ("+ scheduled output (PN) by technology", None, 13.89),
         ("+ 3-seed average (final model)", "lgbm_2023_stack_bag3", None)]


def fig_steps() -> Path:
    runs = {}
    for line in open("results/experiments.jsonl"):
        r = json.loads(line)
        if r["window"] == "dev":
            runs[r["exp"]] = r["mae"]["SP+1"]
    labels = [s[0] for s in STEPS]
    vals = [runs[s[1]] if s[1] else s[2] for s in STEPS]
    fig, ax = plt.subplots(figsize=(7.2, 2.9))
    y = np.arange(len(labels))
    bars = ax.barh(y, vals, color=[C_MID] + [C_MODEL] * (len(vals) - 1), height=0.62)
    for b, v in zip(bars, vals):
        ax.annotate(f"{v:.2f}", (v, b.get_y() + b.get_height() / 2), xytext=(3, 0), textcoords="offset points",
                    va="center", fontsize=7.5, color=INK2)
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("SP+1 MAE on the development year (£/MWh)")
    ax.set_xlim(0, max(vals) * 1.12)
    ax.grid(axis="y", visible=False)
    ax.set_title("What improved the forecast, step by step")
    fig.tight_layout()
    return save(fig, "10_improvement_steps")


def build(exp: str) -> None:
    D = load(exp)
    figs = {"head": fig_headline(D), "month": fig_month(D), "hist": fig_hist(D), "scatter": fig_scatter(D),
            "bands": fig_bands(D), "hour": fig_hour(D), "flip": fig_flip(D), "conc": fig_concentration(D),
            "steps": fig_steps()}
    figs["example"], ex_label = fig_example(D)

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["Normal"], fontName="DejaVu", fontSize=9.5, leading=13.5,
                          textColor=colors.HexColor(INK), spaceAfter=6)
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontName="DejaVu-Bold", fontSize=18, leading=22,
                        alignment=0, spaceAfter=4, textColor=colors.HexColor(INK))
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="DejaVu-Bold", fontSize=12.5, leading=16,
                        spaceBefore=10, spaceAfter=4, textColor=colors.HexColor(INK))
    sub = ParagraphStyle("s", parent=body, textColor=colors.HexColor(INK2), fontSize=9)
    cap = ParagraphStyle("c", parent=body, textColor=colors.HexColor(INK2), fontSize=8.3, leading=11,
                         spaceAfter=10)
    W = A4[0] - 3.6 * cm

    def img(path: Path, caption: str | None = None):
        im = Image(str(path))
        r = W / im.drawWidth
        im.drawWidth, im.drawHeight = W, im.drawHeight * r
        parts = [im] + ([Paragraph(caption, cap)] if caption else [])
        return KeepTogether(parts)

    def P(text):
        return Paragraph(text, body)

    t = D["test"]
    dv = D["dev"]
    m = {(w, c, h): mae(D[w][D[w].h == h], c) for w in ("dev", "test") for c in ("model", "mid", "last") for h in H}
    imp = {h: (1 - m["test", "model", h] / m["test", "mid", h]) * 100 for h in H}
    hit = {}
    for h in H:
        g = t[t.h == h]
        hit[h] = float((np.sign(g.model - g.mid) == np.sign(g.actual - g.mid)).mean()) * 100
    within10 = float(((t[t.h == 1].model - t[t.h == 1].actual).abs() <= 10).mean()) * 100
    within10_mid = float(((t[t.h == 1].mid - t[t.h == 1].actual).abs() <= 10).mean()) * 100
    g1 = t[t.h == 1]
    big = g1[g1.actual >= 250]

    tbl = [["", "SP+1", "SP+2", "SP+3"]]
    for w, wl in (("dev", "Development year"), ("test", "Held-out months")):
        for c in ("model", "mid", "last"):
            tbl.append([f"{wl}: {NAMES[c]}"] + [f"{m[w, c, h]:.1f}" for h in H])
    table = Table(tbl, colWidths=[W * 0.52] + [W * 0.16] * 3)
    style = [("FONT", (0, 0), (-1, -1), "DejaVu", 9), ("FONT", (0, 0), (-1, 0), "DejaVu-Bold", 9),
             ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(INK)),
             ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor(MUTED)),
             ("LINEBELOW", (0, 3), (-1, 3), 0.4, colors.HexColor(GRID)),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    for r in (1, 4):
        style.append(("FONT", (0, r), (-1, r), "DejaVu-Bold", 9))
        style.append(("TEXTCOLOR", (0, r), (-1, r), colors.HexColor(C_MODEL)))
    table.setStyle(TableStyle(style))

    s = []
    s += [Paragraph("GB system price forecast: accuracy and errors", h1),
          Paragraph(f"Model <font face='DejaVuMono'>{exp}</font> · forecasts of the next three settlement periods, "
                    f"issued 25 minutes into the current period · {len(dv) // 3:,} development and "
                    f"{len(t) // 3:,} held-out forecasts per horizon", sub), Spacer(1, 8)]
    s += [Paragraph("Key results", h2),
          P(f"On the held-out months (May – 18 Nov 2026), which were never used to make any modelling choice, the "
            f"model's average error is <b>{m['test', 'model', 1]:.1f}, {m['test', 'model', 2]:.1f} and "
            f"{m['test', 'model', 3]:.1f} £/MWh</b> at SP+1, SP+2 and SP+3. That is {imp[1]:.0f}%, {imp[2]:.0f}% and "
            f"{imp[3]:.0f}% lower than using the latest market index (MID) price, the best simple baseline."),
          P(f"At SP+1, {within10:.0f}% of forecasts land within £10/MWh of the actual price, against "
            f"{within10_mid:.0f}% for the MID price. The model also calls whether the price will settle above or "
            f"below MID correctly {hit[1]:.0f}% / {hit[2]:.0f}% / {hit[3]:.0f}% of the time."),
          P("Accuracy falls with the horizon because less is known: by SP+3 the scheduled output, offer prices and "
            "balancing actions for that period are not yet published. Errors are largest for price spikes and for "
            "periods when the system switches between short and long."),
          Spacer(1, 4), table, Spacer(1, 4),
          Paragraph("Mean absolute error, £/MWh. 'Last known price' is the most recent published system price (SP−1); "
                    "'latest MID' is the current period's market index price.", cap),
          img(figs["head"], "Figure 1. Mean absolute error by horizon. Both windows are walk-forward: each month is "
                            "forecast by a model trained only on data before it."),
          P("<b>Limitations.</b> Spikes and changes of side remain the main sources of error. The publication delays "
            "were measured over a few hours of one night; a system price published unusually late would leave the "
            "latest period unknown in live use. See results/forecast.md for the full method and the list of "
            "approaches that did not help."),
          PageBreak()]
    s += [Paragraph("What the forecasts look like", h2),
          img(figs["example"], f"Figure 2. {ex_label}, a stretch with typical (median) error for the autumn. The "
                               "model follows the actual price's moves away from MID, though it smooths the largest "
                               "jumps."),
          img(figs["scatter"], "Figure 3. Forecast against actual, SP+1, held-out months (darker = more periods; "
                               "clipped to −100…400). Points on the dashed line are perfect forecasts. The model's "
                               "cloud hugs the line more tightly; MID's is wider because it cannot see the "
                               "imbalance."),
          img(figs["hist"], "Figure 4. Distribution of SP+1 errors on the held-out months."),
          PageBreak()]
    s += [Paragraph("Where the errors come from", h2),
          img(figs["month"], "Figure 5. MAE by month for the model and the MID baseline. The shaded region is the "
                             "held-out window. Errors rise in autumn for every method as prices get higher and more "
                             "volatile; the model's lead over MID holds throughout."),
          img(figs["bands"], f"Figure 6. SP+1 error by the actual price level, held-out months. Ordinary prices "
                             f"(50–150) are forecast best. Spikes of £250 or more ({len(big)} periods, "
                             f"{len(big) / len(g1) * 100:.1f}%) are badly under-forecast: MAE "
                             f"{mae(big, 'model'):.0f}, bias {float((big.model - big.actual).mean()):.0f}."),
          img(figs["hour"], "Figure 7. MAE by hour of the target period (UK time), held-out months."),
          PageBreak()]
    s += [img(figs["flip"], "Figure 8. Model error when the system stays on the same side as the last known period "
                            "(SP−1) versus when it flips between long and short. Flips are hard to see coming and "
                            "carry much larger errors."),
          img(figs["conc"], "Figure 9. Error concentration, held-out months. Sorting forecasts from worst to best, "
                            "the curve shows how much of the total error the worst x% carry. The dashed diagonal is "
                            "what equal errors would look like."),
          Paragraph("How the model was built", h2),
          img(figs["steps"], "Figure 10. SP+1 error on the development year as each group of information was added."),
          P("The model is three LightGBM gradient-boosted tree ensembles, one per horizon, each averaged over three "
            "random seeds. Each predicts how far the system price will sit from the latest MID. A first-stage model "
            "forecasts the imbalance volume and the chance the system is short, and those forecasts feed the price "
            "model. All 240 inputs are free public data from Elexon Insights (including NESO's forecasts), each used "
            "only once it would really have been published at the forecast time; the publication delays were "
            "measured by watching Elexon live.")]

    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm, topMargin=1.6 * cm,
                            bottomMargin=1.6 * cm, title="GB system price forecast: accuracy and errors")

    def footer(canvas, d):
        canvas.setFont("DejaVu", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawRightString(A4[0] - 1.8 * cm, 1 * cm, f"Page {d.page}")

    doc.build(s, onFirstPage=footer, onLaterPages=footer)
    print(OUT)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--exp", default="lgbm_2023_stack_bag3")
    build(p.parse_args().exp)
