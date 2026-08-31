"""Step 19: does reversal_density add predictive information beyond reversal_exit_share?
Two checks on the calibration dataset (non-overlapping, so consecutive rows are genuinely
sequential, distinct 30-day periods):
  1. Contemporaneous correlation (already in correlations.csv: r=0.955 -- near-total overlap).
  2. FORWARD test: does this window's reversal_density predict the NEXT window's expectancy
     any better than this window's reversal_exit_share alone does? (lag-1, non-overlapping,
     so this is a legitimate forward check, not a look-ahead one.)
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent


def main():
    with open(ROOT / "data_cache" / "sample_size.json") as f:
        min_trades = json.load(f)["recommended_min_trades"]
    with open(ROOT / "calibration_windows.csv") as f:
        rows = [r for r in csv.DictReader(f) if int(r["trade_count"]) >= min_trades]
    rows.sort(key=lambda r: r["window_start"])

    rev_share = np.array([float(r["reversal_exit_share"]) for r in rows[:-1]])
    rev_density = np.array([float(r["reversal_density"]) for r in rows[:-1]])
    next_expectancy = np.array([float(r["expectancy"]) for r in rows[1:]])

    r_share_next = np.corrcoef(rev_share, next_expectancy)[0, 1]
    r_density_next = np.corrcoef(rev_density, next_expectancy)[0, 1]

    # partial correlation of density with next_expectancy, controlling for share (via residuals)
    share_b, share_a = np.polyfit(rev_share, rev_density, 1)
    density_resid = rev_density - (share_b * rev_share + share_a)
    exp_b, exp_a = np.polyfit(rev_share, next_expectancy, 1)
    exp_resid = next_expectancy - (exp_b * rev_share + exp_a)
    partial_r = np.corrcoef(density_resid, exp_resid)[0, 1]

    out = {
        "contemporaneous_r_share_vs_density": 0.955,
        "forward_r_share_to_next_expectancy": round(float(r_share_next), 3),
        "forward_r_density_to_next_expectancy": round(float(r_density_next), 3),
        "partial_r_density_vs_next_expectancy_controlling_for_share": round(float(partial_r), 3),
        "conclusion": (
            "Negligible incremental value: reversal_density's forward correlation with next-"
            "window expectancy is not stronger than reversal_exit_share's, and the partial "
            "correlation (density's link to future expectancy AFTER removing what share already "
            "explains) is small. Density is kept as a descriptive/contextual stat on the "
            "dashboard, not as a separate scored input."
        ) if abs(partial_r) < 0.2 else (
            "Reversal_density adds meaningful incremental information beyond reversal_exit_share "
            "and should be considered as a secondary scored input."
        ),
    }
    with open(ROOT / "data_cache" / "reversal_density_check.json", "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
