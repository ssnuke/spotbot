"""Steps 16, 17, 18 combined -- all operate on the already-computed daily stress_score_history.csv
time series, so no new backtest run is needed.

Step 16 (early warning): for each score-based episode, measure the lag between the score first
crossing into STRESS / EXTREME_STRESS and the date of that episode's actual max-drawdown trough.
Step 17 (trend): score_today vs 7d-ago vs 14d-ago, classified IMPROVING/STABLE/DETERIORATING using
a threshold derived from the historical distribution of week-over-week score changes (not a
guessed number).
Step 18 (hysteresis): tests a few consecutive-day entry/exit persistence rules against the raw
daily band series and reports how much day-to-day flapping each removes, and the added detection
delay.
"""
from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_score_series():
    with open(ROOT / "stress_score_history.csv") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_dt"] = datetime.fromisoformat(r["window_end"])
        r["_score"] = float(r["stress_score"])
    rows.sort(key=lambda r: r["_dt"])
    return rows


# ---------- Step 17: trend ----------

def compute_trend(rows):
    by_date = {r["_dt"].date(): r["_score"] for r in rows}
    dates = sorted(by_date)
    deltas_7d = []
    for i, d in enumerate(dates):
        prior = d - __import__("datetime").timedelta(days=7)
        if prior in by_date:
            deltas_7d.append(by_date[d] - by_date[prior])
    deltas_7d_sorted = sorted(deltas_7d)
    # threshold: the noise band is the middle 50% (P25-P75) of week-over-week changes: a move
    # inside that band is "normal wiggle", not a real trend -- STABLE. Outside it is a real trend.
    p25 = deltas_7d_sorted[int(len(deltas_7d_sorted) * 0.25)]
    p75 = deltas_7d_sorted[int(len(deltas_7d_sorted) * 0.75)]

    trend_rows = []
    for d in dates:
        prior7 = d - __import__("datetime").timedelta(days=7)
        prior14 = d - __import__("datetime").timedelta(days=14)
        if prior7 not in by_date or prior14 not in by_date:
            continue
        delta7 = by_date[d] - by_date[prior7]
        if delta7 < p25:
            trend = "IMPROVING"  # score falling = getting healthier
        elif delta7 > p75:
            trend = "DETERIORATING"
        else:
            trend = "STABLE"
        trend_rows.append({
            "date": d.isoformat(), "score_today": by_date[d], "score_7d_ago": by_date[prior7],
            "score_14d_ago": by_date[prior14], "delta_7d": round(delta7, 2), "trend": trend,
        })
    return trend_rows, {"noise_band_p25": round(p25, 2), "noise_band_p75": round(p75, 2)}


# ---------- Step 18: hysteresis ----------

def band_of(score, cuts):
    if score < cuts["p25"]:
        return "HEALTHY"
    if score < cuts["p50"]:
        return "NORMAL"
    if score < cuts["p75"]:
        return "CAUTION"
    if score < cuts["p90"]:
        return "STRESS"
    return "EXTREME_STRESS"


def is_elevated(band):
    return band in ("STRESS", "EXTREME_STRESS")


def simulate_hysteresis(rows, enter_days, exit_days):
    """A persistence state machine over the raw elevated/not-elevated series."""
    state = "NOT_ELEVATED"
    consec_elevated = 0
    consec_not = 0
    transitions = 0
    days_elevated_confirmed = 0
    detection_delays = []
    pending_entry_start = None
    for r in rows:
        elevated_today = is_elevated(r["band"])
        if elevated_today:
            consec_elevated += 1
            consec_not = 0
            if pending_entry_start is None:
                pending_entry_start = r["_dt"]
        else:
            consec_not += 1
            consec_elevated = 0
            pending_entry_start = None

        if state == "NOT_ELEVATED" and consec_elevated >= enter_days:
            state = "ELEVATED"
            transitions += 1
            detection_delays.append(enter_days - 1)
        elif state == "ELEVATED" and consec_not >= exit_days:
            state = "NOT_ELEVATED"
            transitions += 1
        if state == "ELEVATED":
            days_elevated_confirmed += 1
    return transitions, days_elevated_confirmed, detection_delays


def hysteresis_sweep(rows, cuts):
    raw_transitions = sum(
        1 for i in range(1, len(rows)) if is_elevated(rows[i]["band"]) != is_elevated(rows[i - 1]["band"])
    )
    candidates = [(1, 1), (2, 2), (3, 3), (2, 5), (3, 7)]
    results = [{"enter_days": 1, "exit_days": 1, "transitions": raw_transitions, "note": "raw (no hysteresis)"}]
    for enter_d, exit_d in candidates:
        t, days_elev, delays = simulate_hysteresis(rows, enter_d, exit_d)
        results.append({
            "enter_days": enter_d, "exit_days": exit_d, "transitions": t,
            "pct_of_raw_transitions": round(t / raw_transitions * 100, 1) if raw_transitions else None,
            "avg_detection_delay_days": round(statistics.mean(delays), 1) if delays else 0,
        })
    return results


# ---------- Step 16: early warning ----------

def early_warning(rows):
    with open(ROOT / "historical_episodes.csv") as f:
        episodes = list(csv.DictReader(f))

    score_by_date = {r["_dt"].date(): r for r in rows}
    dates_sorted = sorted(score_by_date)
    lags = []
    for ep in episodes:
        ep_start = datetime.fromisoformat(ep["episode_start"]).date()
        ep_end = datetime.fromisoformat(ep["episode_end"]).date()
        window = [d for d in dates_sorted if ep_start <= d <= ep_end]
        if not window:
            continue
        first_stress_date = next((d for d in window if score_by_date[d]["band"] in ("STRESS", "EXTREME_STRESS")), None)
        first_extreme_date = next((d for d in window if score_by_date[d]["band"] == "EXTREME_STRESS"), None)
        peak_dd_row = max(window, key=lambda d: float(score_by_date[d].get("max_drawdown_pct") or 0))
        if first_stress_date:
            lags.append({
                "episode_start": ep["episode_start"], "episode_end": ep["episode_end"],
                "days_first_stress_to_peak_dd": (peak_dd_row - first_stress_date).days,
                "days_first_extreme_to_peak_dd": (peak_dd_row - first_extreme_date).days if first_extreme_date else None,
            })
    return lags


def main():
    rows = load_score_series()

    with open(ROOT / "data_cache" / "stress_score_summary.json") as f:
        cuts = json.load(f)["band_cutoffs"]

    print("Step 17: trend classification...")
    trend_rows, noise_band = compute_trend(rows)
    with open(ROOT / "data_cache" / "trend_history.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(trend_rows[0].keys()))
        w.writeheader()
        w.writerows(trend_rows)
    trend_counts = {}
    for t in trend_rows:
        trend_counts[t["trend"]] = trend_counts.get(t["trend"], 0) + 1
    print(f"  noise band (week-over-week score delta): {noise_band}")
    print(f"  trend distribution: {trend_counts}")

    print("Step 18: hysteresis sweep...")
    hyst = hysteresis_sweep(rows, cuts)
    with open(ROOT / "data_cache" / "hysteresis_sweep.json", "w") as f:
        json.dump(hyst, f, indent=2)
    print(json.dumps(hyst, indent=2))

    print("Step 16: early-warning lag...")
    lags = early_warning(rows)
    with open(ROOT / "data_cache" / "early_warning_lags.json", "w") as f:
        json.dump(lags, f, indent=2)
    valid_lags = [l["days_first_stress_to_peak_dd"] for l in lags if l["days_first_stress_to_peak_dd"] is not None]
    print(f"  {len(lags)} episodes with a STRESS flag; median lag to peak DD: "
          f"{statistics.median(valid_lags) if valid_lags else None} days")
    print(json.dumps(lags, indent=2))


if __name__ == "__main__":
    main()
