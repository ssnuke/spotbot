"""Step 14: freeze thresholds using ONLY calibration windows through a cutoff, then evaluate
those frozen thresholds against daily rolling windows AFTER the cutoff, without re-deriving
anything from the later data. No new backtest run needed -- window-level metrics are already in
calibration_windows.csv / rolling_windows_daily.csv, so this is pure date-based re-slicing."""
from __future__ import annotations

import bisect
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CUTOFF = "2025-12-31"
PF_WEIGHT, REVERSAL_WEIGHT = 0.55, 0.45


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] if f == c else sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def percentile_rank(sorted_ref, value):
    return bisect.bisect_right(sorted_ref, value) / len(sorted_ref) * 100


def classify(score, cuts):
    if score < cuts["p25"]:
        return "HEALTHY"
    if score < cuts["p50"]:
        return "NORMAL"
    if score < cuts["p75"]:
        return "CAUTION"
    if score < cuts["p90"]:
        return "STRESS"
    return "EXTREME_STRESS"


def main():
    with open(ROOT / "data_cache" / "sample_size.json") as f:
        min_trades = json.load(f)["recommended_min_trades"]

    with open(ROOT / "calibration_windows.csv") as f:
        calib_all = [r for r in csv.DictReader(f) if int(r["trade_count"]) >= min_trades]
    calib_train = [r for r in calib_all if r["window_end"] <= CUTOFF]

    pf_ref = sorted(float(r["profit_factor"]) for r in calib_train)
    rev_ref = sorted(float(r["reversal_exit_share"]) for r in calib_train)

    train_scores = []
    for r in calib_train:
        pf_b = 100 - percentile_rank(pf_ref, float(r["profit_factor"]))
        rev_b = percentile_rank(rev_ref, float(r["reversal_exit_share"]))
        train_scores.append(PF_WEIGHT * pf_b + REVERSAL_WEIGHT * rev_b)
    train_scores.sort()
    cuts = {p: round(percentile(train_scores, int(p[1:])), 2) for p in ["p25", "p50", "p75", "p90"]}

    with open(ROOT / "rolling_windows_daily.csv") as f:
        daily = [r for r in csv.DictReader(f) if r["trade_count"] and int(r["trade_count"]) >= min_trades]
    validation = [r for r in daily if r["window_end"] > CUTOFF]

    results = []
    for r in validation:
        pf_b = 100 - percentile_rank(pf_ref, float(r["profit_factor"]))
        rev_b = percentile_rank(rev_ref, float(r["reversal_exit_share"]))
        score = round(PF_WEIGHT * pf_b + REVERSAL_WEIGHT * rev_b, 2)
        band = classify(score, cuts)
        results.append({
            "window_end": r["window_end"], "trade_count": r["trade_count"],
            "profit_factor": r["profit_factor"], "win_rate": r["win_rate"],
            "reversal_exit_share": r["reversal_exit_share"], "stress_score": score, "band": band,
        })

    with open(ROOT / "out_of_sample_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    band_counts = {}
    for r in results:
        band_counts[r["band"]] = band_counts.get(r["band"], 0) + 1

    july_window = [r for r in results if "2026-07" <= r["window_end"] <= "2026-08-10"]
    recovery_window = [r for r in results if "2026-08-11" <= r["window_end"] <= "2026-08-30"]

    summary = {
        "calibration_cutoff": CUTOFF,
        "calibration_windows_n": len(calib_train),
        "frozen_thresholds": cuts,
        "validation_windows_n": len(results),
        "validation_period": f"{results[0]['window_end']} to {results[-1]['window_end']}" if results else None,
        "band_counts_validation": band_counts,
        "band_share_pct_validation": {k: round(v / len(results) * 100, 1) for k, v in band_counts.items()},
        "july_2026_window_bands": [{"date": r["window_end"], "band": r["band"], "score": r["stress_score"]} for r in july_window],
        "august_recovery_window_bands": [{"date": r["window_end"], "band": r["band"], "score": r["stress_score"]} for r in recovery_window],
    }
    with open(ROOT / "data_cache" / "out_of_sample_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
