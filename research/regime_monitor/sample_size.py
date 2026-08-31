"""Step 5: minimum trade-count requirement. Analyzes the calibration dataset's trade_count
distribution and checks how PF/win-rate percentile estimates move as the minimum-trade filter
is varied, rather than assuming a number."""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CANDIDATES = [0, 30, 50, 75, 100]


def load_calibration():
    with open(ROOT / "calibration_windows.csv") as f:
        return [r for r in csv.DictReader(f) if r["trade_count"]]


def main():
    rows = load_calibration()
    counts = sorted(int(r["trade_count"]) for r in rows)
    n = len(counts)

    dist = {
        "n_windows": n,
        "min": counts[0], "max": counts[-1],
        "p05": counts[int(n * 0.05)], "p10": counts[int(n * 0.10)],
        "p25": counts[int(n * 0.25)], "p50": counts[int(n * 0.50)],
        "p75": counts[int(n * 0.75)], "p90": counts[int(n * 0.90)],
        "mean": round(statistics.mean(counts), 1),
    }

    results = []
    for thresh in CANDIDATES:
        kept = [r for r in rows if int(r["trade_count"]) >= thresh]
        excluded = n - len(kept)
        pf_vals = sorted(float(r["profit_factor"]) for r in kept)
        wr_vals = sorted(float(r["win_rate"]) for r in kept)
        results.append({
            "min_trades": thresh,
            "windows_kept": len(kept),
            "windows_excluded": excluded,
            "pct_excluded": round(excluded / n * 100, 1),
            "pf_p25": round(pf_vals[int(len(pf_vals) * 0.25)], 3) if pf_vals else None,
            "pf_p50": round(pf_vals[int(len(pf_vals) * 0.50)], 3) if pf_vals else None,
            "pf_p75": round(pf_vals[int(len(pf_vals) * 0.75)], 3) if pf_vals else None,
            "wr_p25": round(wr_vals[int(len(wr_vals) * 0.25)], 2) if wr_vals else None,
            "wr_p50": round(wr_vals[int(len(wr_vals) * 0.50)], 2) if wr_vals else None,
            "wr_p75": round(wr_vals[int(len(wr_vals) * 0.75)], 2) if wr_vals else None,
        })

    with open(ROOT / "sample_size_analysis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    # recommendation: the lowest threshold at which PF/WR percentiles have stopped moving
    # meaningfully vs the next-lower threshold (< 0.05 absolute PF move, < 1.5pp WR move)
    # AND keeps at least ~80% of windows (a 30-day window's trade count is itself informative --
    # a very quiet 30 days IS part of the regime, not just noise -- so we don't want to throw
    # away too many).
    recommended = results[0]["min_trades"]
    for i in range(1, len(results)):
        prev, cur = results[i - 1], results[i]
        pf_move = abs(cur["pf_p50"] - prev["pf_p50"])
        wr_move = abs(cur["wr_p50"] - prev["wr_p50"])
        if pf_move < 0.05 and wr_move < 1.5 and cur["pct_excluded"] <= 20:
            recommended = cur["min_trades"]
        else:
            break

    out = {"trade_count_distribution": dist, "threshold_sweep": results, "recommended_min_trades": recommended}
    with open(ROOT / "data_cache" / "sample_size.json", "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
