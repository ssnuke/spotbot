"""Step 8/10/11: continuous 0-100 Regime Stress Score, built from percentile-rank "badness"
transforms -- NOT a binary if/else rule -- with weights justified by the Step 9 correlation
analysis rather than picked arbitrarily.

WEIGHT JUSTIFICATION (from correlations.csv):
  - profit_factor is highly correlated with win_rate (r=0.835), expectancy (r=0.911), and
    cooldown_rate (r=-0.811) -- these four are largely measuring the SAME underlying "trade
    quality" phenomenon. Using profit_factor alone as the quality pillar captures most of what
    win_rate/expectancy/cooldown_rate would each add; including all four at full weight would
    count that one phenomenon roughly four times.
  - reversal_exit_share is almost perfectly correlated with reversal_density (r=0.955) -- these
    are the same "whipsaw character" phenomenon measured two ways. reversal_density is therefore
    EXCLUDED from the score (kept as a descriptive/contextual stat only, answering Step 19's
    question: it adds negligible incremental information here).
  - max_drawdown_pct has only moderate correlation with everything else (strongest: -0.550 vs
    reversal_exit_share) and is explicitly kept OUT of the score per Step 20 (drawdown is an
    outcome/context measure, not a leading-quality measure).

RESULT: two pillars, deliberately close to independent:
  pf_badness        = 100 - percentile_rank(profit_factor)      [lower PF = worse]
  reversal_badness  = percentile_rank(reversal_exit_share)      [higher share = worse]
  stress_score = 0.55 * pf_badness + 0.45 * reversal_badness

Percentile ranks are computed against the CALIBRATION dataset's empirical distribution (the same
one percentiles.json reports), so a score of 80 means "this window's PF/reversal mix is worse
than roughly 80% of REV-2C's own 6-year history" -- self-referential by design, not compared to
some other strategy or asset.
"""
from __future__ import annotations

import bisect
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PF_WEIGHT = 0.55
REVERSAL_WEIGHT = 0.45


def build_reference(calib_rows, metric):
    return sorted(float(r[metric]) for r in calib_rows if r.get(metric) not in (None, ""))


def percentile_rank(sorted_ref, value):
    """% of the reference distribution <= value, i.e. this value's own percentile position."""
    idx = bisect.bisect_right(sorted_ref, value)
    return idx / len(sorted_ref) * 100


def score_row(row, pf_ref, rev_ref):
    if not row.get("profit_factor") or not row.get("reversal_exit_share"):
        return None
    pf = float(row["profit_factor"])
    rev = float(row["reversal_exit_share"])
    pf_pctile = percentile_rank(pf_ref, pf)
    rev_pctile = percentile_rank(rev_ref, rev)
    pf_badness = 100 - pf_pctile
    reversal_badness = rev_pctile
    score = PF_WEIGHT * pf_badness + REVERSAL_WEIGHT * reversal_badness
    return round(score, 2), round(pf_badness, 1), round(reversal_badness, 1)


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
        calib_rows = [r for r in csv.DictReader(f) if int(r["trade_count"]) >= min_trades]
    with open(ROOT / "rolling_windows_daily.csv") as f:
        daily_rows = [r for r in csv.DictReader(f) if r["trade_count"] and int(r["trade_count"]) >= min_trades]

    pf_ref = build_reference(calib_rows, "profit_factor")
    rev_ref = build_reference(calib_rows, "reversal_exit_share")

    # score the calibration set itself (for band-boundary derivation)
    calib_scores = []
    for r in calib_rows:
        s = score_row(r, pf_ref, rev_ref)
        if s:
            calib_scores.append({**r, "stress_score": s[0], "pf_badness": s[1], "reversal_badness": s[2]})

    scores_sorted = sorted(c["stress_score"] for c in calib_scores)
    band_cuts = {
        "p25": round(percentile(scores_sorted, 25), 2),
        "p50": round(percentile(scores_sorted, 50), 2),
        "p75": round(percentile(scores_sorted, 75), 2),
        "p90": round(percentile(scores_sorted, 90), 2),
        "p95": round(percentile(scores_sorted, 95), 2),
    }

    def classify(score):
        if score < band_cuts["p25"]:
            return "HEALTHY"
        if score < band_cuts["p50"]:
            return "NORMAL"
        if score < band_cuts["p75"]:
            return "CAUTION"
        if score < band_cuts["p90"]:
            return "STRESS"
        return "EXTREME_STRESS"

    for c in calib_scores:
        c["band"] = classify(c["stress_score"])

    band_counts = {}
    for c in calib_scores:
        band_counts[c["band"]] = band_counts.get(c["band"], 0) + 1

    # score the daily rolling dataset too (needed by episodes.py / early_warning.py / trend etc.)
    daily_scores = []
    for r in daily_rows:
        s = score_row(r, pf_ref, rev_ref)
        if s:
            daily_scores.append({
                "window_start": r["window_start"], "window_end": r["window_end"],
                "trade_count": r["trade_count"], "profit_factor": r["profit_factor"],
                "win_rate": r["win_rate"], "reversal_exit_share": r["reversal_exit_share"],
                "expectancy": r["expectancy"], "reversal_density": r["reversal_density"],
                "cooldown_rate": r["cooldown_rate"], "max_drawdown_pct": r["max_drawdown_pct"],
                "stress_score": s[0], "pf_badness": s[1], "reversal_badness": s[2],
                "band": classify(s[0]),
            })

    with open(ROOT / "stress_score_history.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(daily_scores[0].keys()))
        w.writeheader()
        w.writerows(daily_scores)

    out = {
        "weights": {"pf_weight": PF_WEIGHT, "reversal_weight": REVERSAL_WEIGHT},
        "reference_dataset": "calibration_windows.csv (non-overlapping 30d, min_trades filtered)",
        "score_distribution_calibration": {
            "p05": round(percentile(scores_sorted, 5), 2), "p10": round(percentile(scores_sorted, 10), 2),
            "p25": band_cuts["p25"], "p50": band_cuts["p50"], "p75": band_cuts["p75"],
            "p90": band_cuts["p90"], "p95": band_cuts["p95"],
        },
        "band_cutoffs": band_cuts,
        "band_counts_calibration": band_counts,
        "band_share_pct_calibration": {k: round(v / len(calib_scores) * 100, 1) for k, v in band_counts.items()},
    }
    with open(ROOT / "data_cache" / "stress_score_summary.json", "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
