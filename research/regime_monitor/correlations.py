"""Step 9: correlation matrix among the candidate stress-score inputs, on the calibration
dataset, to check for double-counting before any weights are assigned."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
METRICS = ["profit_factor", "win_rate", "reversal_exit_share", "expectancy",
           "reversal_density", "cooldown_rate", "max_drawdown_pct"]


def main():
    with open(ROOT / "data_cache" / "sample_size.json") as f:
        min_trades = json.load(f)["recommended_min_trades"]
    with open(ROOT / "calibration_windows.csv") as f:
        rows = [r for r in csv.DictReader(f) if int(r["trade_count"]) >= min_trades]

    data = {m: np.array([float(r[m]) for r in rows]) for m in METRICS}
    n = len(METRICS)
    corr = np.zeros((n, n))
    for i, a in enumerate(METRICS):
        for j, b in enumerate(METRICS):
            corr[i, j] = np.corrcoef(data[a], data[b])[0, 1]

    with open(ROOT / "correlations.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([""] + METRICS)
        for i, m in enumerate(METRICS):
            w.writerow([m] + [round(x, 3) for x in corr[i]])

    # flag pairs with |r| >= 0.6 (excluding the diagonal) as "likely counting the same thing"
    strong_pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            if abs(corr[i, j]) >= 0.6:
                strong_pairs.append({"a": METRICS[i], "b": METRICS[j], "r": round(float(corr[i, j]), 3)})
    strong_pairs.sort(key=lambda p: -abs(p["r"]))

    out = {"n_windows": len(rows), "metrics": METRICS, "matrix": corr.round(3).tolist(), "strong_pairs": strong_pairs}
    with open(ROOT / "data_cache" / "correlations.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"{len(rows)} windows")
    header = "".join(f"{m[:10]:>12}" for m in METRICS)
    print(f"{'':22}{header}")
    for i, m in enumerate(METRICS):
        print(f"{m:<22}" + "".join(f"{corr[i,j]:>12.3f}" for j in range(n)))
    print("\nStrong pairs (|r| >= 0.6):")
    for p in strong_pairs:
        print(f"  {p['a']} <-> {p['b']}: r={p['r']}")


if __name__ == "__main__":
    main()
