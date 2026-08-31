"""Step 25: charts. Consolidated to 7 figures (not 14) per the instruction to skip charts whose
conclusion is already obvious from a better one -- e.g. the four core-metric histograms share one
grid instead of four near-identical files, and "score vs subsequent drawdown" already covers what
a separate "reversal density vs future expectancy" scatter would show even less clearly (that
question is answered numerically, and conclusively, in reversal_density_check.json instead: a
scatter of two variables with a -0.03 partial correlation is just a cloud)."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402

ROOT = Path(__file__).resolve().parent
CHARTS = ROOT / "charts"
plt.rcParams.update({"figure.facecolor": "white", "axes.facecolor": "white", "font.size": 10})


def load_csv(name):
    with open(ROOT / name) as f:
        return list(csv.DictReader(f))


def chart_distributions_grid():
    rows = load_csv("calibration_windows.csv")
    fig, axes = plt.subplots(2, 2, figsize=(10, 8))
    specs = [
        ("profit_factor", "Profit Factor (30d, non-overlapping windows)", axes[0][0]),
        ("win_rate", "Win Rate % (30d)", axes[0][1]),
        ("reversal_exit_share", "Reversal-Exit Share % (30d)", axes[1][0]),
        ("expectancy", "Expectancy $/trade (30d)", axes[1][1]),
    ]
    for metric, title, ax in specs:
        vals = [float(r[metric]) for r in rows]
        ax.hist(vals, bins=18, color="#2c6e8f", edgecolor="white")
        ax.axvline(sorted(vals)[len(vals)//2], color="#c9583f", linestyle="--", linewidth=1.5, label="median")
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=8)
    fig.suptitle("Historical distributions -- 72 non-overlapping 30-day calibration windows", fontsize=12)
    fig.tight_layout()
    fig.savefig(CHARTS / "01_distributions_grid.png", dpi=140)
    plt.close(fig)


def chart_score_distribution():
    with open(ROOT / "data_cache" / "stress_score_summary.json") as f:
        summary = json.load(f)
    rows = load_csv("calibration_windows.csv")
    with open(ROOT / "stress_score_history.csv") as f:
        daily = list(csv.DictReader(f))
    calib_dates = {r["window_end"] for r in rows}
    calib_scores = [float(r["stress_score"]) for r in daily if r["window_end"] in calib_dates]

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.hist(calib_scores, bins=20, color="#6b7285", edgecolor="white")
    for band, cut in summary["band_cutoffs"].items():
        ax.axvline(cut, color="#c9583f", linestyle="--", linewidth=1)
        ax.text(cut, ax.get_ylim()[1] * 0.95, band, rotation=90, fontsize=8, va="top", ha="right", color="#c9583f")
    ax.set_title("Stress-score distribution (calibration windows) with band cutoffs")
    ax.set_xlabel("Stress score (0-100)")
    fig.tight_layout()
    fig.savefig(CHARTS / "02_stress_score_distribution.png", dpi=140)
    plt.close(fig)


def chart_score_timeline():
    with open(ROOT / "stress_score_history.csv") as f:
        rows = list(csv.DictReader(f))
    dates = [datetime.fromisoformat(r["window_end"]) for r in rows]
    scores = [float(r["stress_score"]) for r in rows]

    episodes = load_csv("historical_episodes.csv")

    fig, ax = plt.subplots(figsize=(13, 5))
    ax.plot(dates, scores, color="#2c6e8f", linewidth=1)
    for ep in episodes:
        s = datetime.fromisoformat(ep["episode_start"])
        e = datetime.fromisoformat(ep["episode_end"])
        ax.axvspan(s, e, color="#c9583f", alpha=0.15)
    for cut_name, cut_val in [("STRESS", 76.82), ("EXTREME", 88.74)]:
        ax.axhline(cut_val, color="#b3791c", linestyle=":", linewidth=1)
    ax.set_ylim(0, 100)
    ax.set_title("Stress score over time (shaded = merged STRESS/EXTREME episodes)")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    fig.tight_layout()
    fig.savefig(CHARTS / "03_stress_score_timeline.png", dpi=140)
    plt.close(fig)


def chart_score_vs_drawdown():
    with open(ROOT / "stress_score_history.csv") as f:
        rows = list(csv.DictReader(f))
    scores = [float(r["stress_score"]) for r in rows]
    dds = [float(r["max_drawdown_pct"]) for r in rows]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.scatter(scores, dds, s=8, alpha=0.35, color="#2c6e8f")
    ax.set_xlabel("Stress score (that day's trailing 30d window)")
    ax.set_ylabel("Max drawdown % (that same window, local)")
    ax.set_title("Stress score vs. same-window drawdown\n(context, not score input -- see Step 20)")
    fig.tight_layout()
    fig.savefig(CHARTS / "04_score_vs_drawdown.png", dpi=140)
    plt.close(fig)


def chart_early_warning_lag():
    with open(ROOT / "data_cache" / "early_warning_lags.json") as f:
        lags = json.load(f)
    labels = [l["episode_start"][:7] for l in lags]
    vals = [l["days_first_stress_to_peak_dd"] for l in lags]
    colors = ["#24905a" if v > 0 else "#c93f3f" for v in vals]

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar(labels, vals, color=colors)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("Days from first STRESS flag to episode's peak drawdown\n(positive = warned in advance)")
    ax.set_title("Early-warning lag per historical episode")
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", fontsize=8)
    fig.tight_layout()
    fig.savefig(CHARTS / "05_early_warning_lag.png", dpi=140)
    plt.close(fig)


def chart_threshold_stability():
    rows = load_csv("threshold_stability.csv")
    cutoffs = [r["cutoff"][:4] for r in rows]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.5))
    specs = [("pf_p25", "pf_p50", "pf_p75", "Profit Factor", axes[0]),
             ("wr_p25", "wr_p50", "wr_p75", "Win Rate %", axes[1]),
             ("rev_p25", "rev_p50", "rev_p75", "Reversal Share %", axes[2])]
    for p25k, p50k, p75k, title, ax in specs:
        ax.plot(cutoffs, [float(r[p25k]) for r in rows], marker="o", label="P25")
        ax.plot(cutoffs, [float(r[p50k]) for r in rows], marker="o", label="P50")
        ax.plot(cutoffs, [float(r[p75k]) for r in rows], marker="o", label="P75")
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=8)
    fig.suptitle("Threshold stability as the calibration cutoff advances")
    fig.tight_layout()
    fig.savefig(CHARTS / "06_threshold_stability.png", dpi=140)
    plt.close(fig)


def chart_band_frequency_and_recovery():
    band_stats = load_csv("regime_band_statistics.csv")
    recovery = load_csv("recovery_analysis.csv")

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    bands = [r["band"] for r in band_stats]
    pcts = [float(r["pct_of_days"]) for r in band_stats]
    colors = {"HEALTHY": "#24905a", "NORMAL": "#6b7285", "CAUTION": "#b3791c", "STRESS": "#c9583f", "EXTREME_STRESS": "#8f1d1d"}
    axes[0].bar(bands, pcts, color=[colors[b] for b in bands])
    axes[0].set_title("Share of days in each band (daily rolling)")
    axes[0].set_ylabel("% of days")
    plt.setp(axes[0].get_xticklabels(), rotation=30, ha="right", fontsize=8)

    by_band = {}
    for r in recovery:
        if r["recovery_time_days"]:
            by_band.setdefault(r["peak_band"], []).append(float(r["recovery_time_days"]))
    band_order = [b for b in ["STRESS", "EXTREME_STRESS"] if b in by_band]
    axes[1].boxplot([by_band[b] for b in band_order], tick_labels=band_order)
    axes[1].set_title("Recovery time (days) by episode peak band")
    axes[1].set_ylabel("Days")
    fig.tight_layout()
    fig.savefig(CHARTS / "07_band_frequency_and_recovery.png", dpi=140)
    plt.close(fig)


def main():
    CHARTS.mkdir(exist_ok=True)
    chart_distributions_grid()
    chart_score_distribution()
    chart_score_timeline()
    chart_score_vs_drawdown()
    chart_early_warning_lag()
    chart_threshold_stability()
    chart_band_frequency_and_recovery()
    print(f"Wrote 7 charts to {CHARTS}")


if __name__ == "__main__":
    main()
