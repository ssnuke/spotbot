"""Step 6/7: historical percentiles for every key metric, computed on the (min-trade-filtered)
calibration dataset, with an explicit "which direction is bad" annotation per metric."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PCTS = [5, 10, 20, 25, 30, 40, 50, 60, 70, 75, 80, 90, 95]

# Step 6: explicit direction annotations -- NOT assumed silently.
METRIC_DIRECTIONS = {
    "profit_factor": "lower = worse",
    "win_rate": "lower = worse",
    "reversal_exit_share": "higher = worse",
    "expectancy": "lower = worse",
    "reversal_density": "higher = worse",
    "reversal_density_frac_high": "higher = worse",
    "cooldown_rate": "higher = worse",
    "max_drawdown_pct": "higher = worse",
    "trade_count": "context only -- neither direction is 'worse', used for sample-size filtering",
    "long_win_rate": "lower = worse",
    "short_win_rate": "lower = worse",
    "win_loss_ratio": "lower = worse",
    "drawdown_duration_days": "higher = worse (context only, see Step 20)",
    "recovery_time_days": "higher = worse (context only, see Step 20)",
    "total_return_pct": "lower = worse",
}


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def main():
    with open(ROOT / "data_cache" / "sample_size.json") as f:
        min_trades = json.load(f)["recommended_min_trades"]

    with open(ROOT / "calibration_windows.csv") as f:
        rows = [r for r in csv.DictReader(f) if int(r["trade_count"]) >= min_trades]

    print(f"{len(rows)} calibration windows at min_trades >= {min_trades}", file=__import__("sys").stderr)

    out = {"min_trades_applied": min_trades, "n_windows": len(rows), "metrics": {}}
    for metric, direction in METRIC_DIRECTIONS.items():
        vals = sorted(float(r[metric]) for r in rows if r.get(metric) not in (None, ""))
        entry = {"direction": direction, "n": len(vals)}
        for p in PCTS:
            entry[f"p{p:02d}"] = round(percentile(vals, p), 4) if vals else None
        out["metrics"][metric] = entry

    with open(ROOT / "percentiles.json", "w") as f:
        json.dump(out, f, indent=2)

    # human-readable table for the report
    lines = [f"{'Metric':<28}" + "".join(f"{'P'+str(p):>9}" for p in [10, 25, 50, 75, 90])]
    for metric in ["profit_factor", "win_rate", "reversal_exit_share", "expectancy",
                   "reversal_density", "cooldown_rate", "max_drawdown_pct", "trade_count"]:
        e = out["metrics"][metric]
        lines.append(f"{metric:<28}" + "".join(f"{e[f'p{p:02d}']:>9.3f}" for p in [10, 25, 50, 75, 90]))
    table = "\n".join(lines)
    with open(ROOT / "data_cache" / "percentile_table.txt", "w") as f:
        f.write(table)
    print(table)


if __name__ == "__main__":
    main()
