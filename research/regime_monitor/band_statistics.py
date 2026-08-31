"""Step 22: for every band, what did a window in that band actually look like historically?
Also (feeds Step 25 chart 13) recovery time bucketed by which band an episode peaked in."""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BANDS = ["HEALTHY", "NORMAL", "CAUTION", "STRESS", "EXTREME_STRESS"]


def median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 3) if vals else None


def main():
    with open(ROOT / "stress_score_history.csv") as f:
        rows = list(csv.DictReader(f))

    band_stats = []
    for band in BANDS:
        sub = [r for r in rows if r["band"] == band]
        if not sub:
            continue
        band_stats.append({
            "band": band,
            "n_days": len(sub),
            "pct_of_days": round(len(sub) / len(rows) * 100, 1),
            "median_profit_factor": median(float(r["profit_factor"]) for r in sub),
            "median_win_rate": median(float(r["win_rate"]) for r in sub),
            "median_reversal_share": median(float(r["reversal_exit_share"]) for r in sub),
            "median_expectancy": median(float(r["expectancy"]) for r in sub),
            "median_max_drawdown_pct": median(float(r["max_drawdown_pct"]) for r in sub),
            "median_cooldown_rate": median(float(r["cooldown_rate"]) for r in sub),
        })
    with open(ROOT / "regime_band_statistics.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(band_stats[0].keys()))
        w.writeheader()
        w.writerows(band_stats)
    print(json.dumps(band_stats, indent=2))

    # recovery time by the band the episode PEAKED in
    with open(ROOT / "historical_episodes.csv") as f:
        episodes = list(csv.DictReader(f))

    def band_of_score(score):
        with open(ROOT / "data_cache" / "stress_score_summary.json") as f2:
            cuts = json.load(f2)["band_cutoffs"]
        if score < cuts["p25"]:
            return "HEALTHY"
        if score < cuts["p50"]:
            return "NORMAL"
        if score < cuts["p75"]:
            return "CAUTION"
        if score < cuts["p90"]:
            return "STRESS"
        return "EXTREME_STRESS"

    recovery_rows = []
    for ep in episodes:
        peak_band = band_of_score(float(ep["peak_stress_score"]))
        recovery_rows.append({
            "episode_start": ep["episode_start"], "peak_band": peak_band,
            "recovery_time_days": ep["recovery_time_days"], "max_drawdown_pct": ep["max_drawdown_pct"],
        })
    with open(ROOT / "recovery_analysis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(recovery_rows[0].keys()))
        w.writeheader()
        w.writerows(recovery_rows)

    by_band = {}
    for r in recovery_rows:
        by_band.setdefault(r["peak_band"], []).append(r)
    print("\nRecovery time by peak band:")
    for band, items in by_band.items():
        rts = [float(i["recovery_time_days"]) for i in items if i["recovery_time_days"]]
        print(f"  {band}: n={len(items)}, median_recovery={median(rts)}d")


if __name__ == "__main__":
    main()
