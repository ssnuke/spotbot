"""Step 15: repeats the percentile/threshold calibration using several earlier cutoffs, to see
how much the numbers move as more history accumulates. No new backtest run needed."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CUTOFFS = ["2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31", "2026-08-30"]


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[f] if f == c else sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main():
    with open(ROOT / "data_cache" / "sample_size.json") as f:
        min_trades = json.load(f)["recommended_min_trades"]
    with open(ROOT / "calibration_windows.csv") as f:
        calib = [r for r in csv.DictReader(f) if int(r["trade_count"]) >= min_trades]

    rows = []
    for cutoff in CUTOFFS:
        subset = [r for r in calib if r["window_end"] <= cutoff]
        pf = sorted(float(r["profit_factor"]) for r in subset)
        wr = sorted(float(r["win_rate"]) for r in subset)
        rev = sorted(float(r["reversal_exit_share"]) for r in subset)
        rows.append({
            "cutoff": cutoff, "n_windows": len(subset),
            "pf_p25": round(percentile(pf, 25), 3), "pf_p50": round(percentile(pf, 50), 3), "pf_p75": round(percentile(pf, 75), 3),
            "wr_p25": round(percentile(wr, 25), 2), "wr_p50": round(percentile(wr, 50), 2), "wr_p75": round(percentile(wr, 75), 2),
            "rev_p25": round(percentile(rev, 25), 2), "rev_p50": round(percentile(rev, 50), 2), "rev_p75": round(percentile(rev, 75), 2),
        })

    with open(ROOT / "threshold_stability.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # movement between the FIRST (2022 cutoff) and LAST (full history) calibration
    first, last = rows[0], rows[-1]
    movement = {
        k: round(abs(last[k] - first[k]), 3)
        for k in ["pf_p25", "pf_p50", "pf_p75", "wr_p25", "wr_p50", "wr_p75", "rev_p25", "rev_p50", "rev_p75"]
    }
    out = {"cutoffs": rows, "movement_2022_to_full": movement}
    with open(ROOT / "data_cache" / "stability_summary.json", "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
