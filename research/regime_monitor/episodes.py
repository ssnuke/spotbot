"""Step 12: merges adjacent/nearby STRESS-or-worse daily windows (from the continuous score) into
discrete episodes, then replays each episode's own fresh-capital outcome (30/60/90d).

Step 13: separately, tests the "hard July-like failure" AND-rule (win_rate<=Q, PF<=Q,
reversal_share>=Q simultaneously) at several defensible percentile pairs, NOT just Q25/Q75, and
reports episode count / avg max DD / median recovery / recovery rate / false-alarm rate for each.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolated_replay import replay  # noqa: E402

ROOT = Path(__file__).resolve().parent
EPISODE_MERGE_GAP_DAYS = 14
FORWARD_DAYS = 90


def load_meta():
    with open(ROOT / "data_cache" / "meta.json") as f:
        return json.load(f)


def parse_dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# ---------- Step 12: score-based episodes ----------

def score_based_episodes(data_end_dt):
    with open(ROOT / "stress_score_history.csv") as f:
        rows = list(csv.DictReader(f))

    hit_days = [parse_dt(r["window_end"]) for r in rows if r["band"] in ("STRESS", "EXTREME_STRESS")]
    hit_days.sort()

    episodes = []
    cur_start = cur_end = None
    for d in hit_days:
        if cur_end is None:
            cur_start = cur_end = d
        elif (d - cur_end).days <= EPISODE_MERGE_GAP_DAYS:
            cur_end = d
        else:
            episodes.append((cur_start - timedelta(days=30), cur_end))
            cur_start = cur_end = d
    if cur_end is not None:
        episodes.append((cur_start - timedelta(days=30), cur_end))

    by_window_end = {r["window_end"]: r for r in rows}
    results = []
    for ep_start, ep_end in episodes:
        ep_rows = [r for r in rows if ep_start.date().isoformat() <= r["window_end"] <= ep_end.date().isoformat()]
        if not ep_rows:
            continue
        peak_score = max(float(r["stress_score"]) for r in ep_rows)
        avg_score = statistics.mean(float(r["stress_score"]) for r in ep_rows)
        worst_row = max(ep_rows, key=lambda r: float(r["stress_score"]))

        replay_len = (ep_end - ep_start).days + FORWARD_DAYS
        trades, curve, forward_end = replay(ep_start, replay_len, data_end_dt)
        ep_trades = [t for t in trades if ep_start <= datetime.fromisoformat(t.exit_timestamp) <= ep_end]
        if not ep_trades:
            continue
        wins = [t for t in ep_trades if t.pnl > 0]
        losses = [t for t in ep_trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        pf = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
        reversal = [t for t in ep_trades if t.exit_reason == "signal_reversal"]

        ep_curve_idx = [i for i, c in enumerate(curve) if ep_start <= c["dt"] <= ep_end]
        worst_dd, trough_idx = 0.0, None
        for i in ep_curve_idx:
            dd = (curve[i]["peak"] - curve[i]["wealth"]) / curve[i]["peak"] * 100 if curve[i]["peak"] > 0 else 0.0
            if dd > worst_dd:
                worst_dd, trough_idx = dd, i
        recovery_days = None
        if trough_idx is not None:
            target_peak = curve[trough_idx]["peak"]
            for c in curve[trough_idx:]:
                if c["wealth"] >= target_peak:
                    recovery_days = (c["dt"] - curve[trough_idx]["dt"]).total_seconds() / 86400
                    break

        end_wealth = curve[ep_curve_idx[-1]]["wealth"] if ep_curve_idx else None
        end_dt = curve[ep_curve_idx[-1]]["dt"] if ep_curve_idx else ep_end

        def wealth_after(days):
            cutoff = end_dt + timedelta(days=days)
            if cutoff > data_end_dt:
                return None
            cands = [c for c in curve if c["dt"] <= cutoff]
            return cands[-1]["wealth"] if cands else None

        from research.reversal_experiments.engine import BASELINE_USD  # noqa: E402
        results.append({
            "episode_start": ep_start.date().isoformat(),
            "episode_end": ep_end.date().isoformat(),
            "duration_days": (ep_end - ep_start).days,
            "peak_stress_score": round(peak_score, 1),
            "average_stress_score": round(avg_score, 1),
            "win_rate": round(len(wins) / len(ep_trades) * 100, 2),
            "profit_factor": round(min(pf, 999.0), 3),
            "reversal_share": round(len(reversal) / len(ep_trades) * 100, 2),
            "expectancy": round(sum(t.pnl for t in ep_trades) / len(ep_trades), 4),
            "max_drawdown_pct": round(worst_dd, 2),
            "recovery_time_days": round(recovery_days, 1) if recovery_days is not None else None,
            "cooldown_count": sum(1 for t in ep_trades if t.triggered_cooldown),
            "wealth_30d": round(wealth_after(30), 2) if wealth_after(30) else None,
            "wealth_60d": round(wealth_after(60), 2) if wealth_after(60) else None,
            "wealth_90d": round(wealth_after(90), 2) if wealth_after(90) else None,
            "baseline": round(BASELINE_USD, 2),
        })
    return results


# ---------- Step 13: hard-rule threshold candidates ----------

def hard_rule_candidates(data_end_dt):
    with open(ROOT / "data_cache" / "sample_size.json") as f:
        min_trades = json.load(f)["recommended_min_trades"]
    with open(ROOT / "calibration_windows.csv") as f:
        calib = [r for r in csv.DictReader(f) if int(r["trade_count"]) >= min_trades]
    with open(ROOT / "rolling_windows_daily.csv") as f:
        daily = [r for r in csv.DictReader(f) if r["trade_count"] and int(r["trade_count"]) >= min_trades]

    def pct(vals, p):
        s = sorted(vals)
        k = (len(s) - 1) * (p / 100)
        f, c = int(k), min(int(k) + 1, len(s) - 1)
        return s[f] if f == c else s[f] + (s[c] - s[f]) * (k - f)

    wr = [float(r["win_rate"]) for r in calib]
    pf = [float(r["profit_factor"]) for r in calib]
    rev = [float(r["reversal_exit_share"]) for r in calib]

    candidates = [(20, 80), (25, 75), (30, 70)]
    out = []
    for lo, hi in candidates:
        wr_t, pf_t, rev_t = pct(wr, lo), pct(pf, lo), pct(rev, hi)
        hit_ends = sorted(
            parse_dt(r["window_end"]) for r in daily
            if float(r["win_rate"]) <= wr_t and float(r["profit_factor"]) <= pf_t and float(r["reversal_exit_share"]) >= rev_t
        )
        episodes = []
        cur_s = cur_e = None
        for d in hit_ends:
            if cur_e is None:
                cur_s = cur_e = d
            elif (d - cur_e).days <= EPISODE_MERGE_GAP_DAYS:
                cur_e = d
            else:
                episodes.append((cur_s - timedelta(days=30), cur_e))
                cur_s = cur_e = d
        if cur_e is not None:
            episodes.append((cur_s - timedelta(days=30), cur_e))

        dds, recoveries, rec30, rec60, rec90, complete_n = [], [], [], [], [], 0
        from research.reversal_experiments.engine import BASELINE_USD  # noqa: E402
        for ep_start, ep_end in episodes:
            replay_len = (ep_end - ep_start).days + FORWARD_DAYS
            trades, curve, forward_end = replay(ep_start, replay_len, data_end_dt)
            idxs = [i for i, c in enumerate(curve) if ep_start <= c["dt"] <= ep_end]
            if not idxs:
                continue
            worst_dd, trough_i = 0.0, None
            for i in idxs:
                dd = (curve[i]["peak"] - curve[i]["wealth"]) / curve[i]["peak"] * 100 if curve[i]["peak"] > 0 else 0.0
                if dd > worst_dd:
                    worst_dd, trough_i = dd, i
            dds.append(worst_dd)
            recovery_days = None
            if trough_i is not None:
                target = curve[trough_i]["peak"]
                for c in curve[trough_i:]:
                    if c["wealth"] >= target:
                        recovery_days = (c["dt"] - curve[trough_i]["dt"]).total_seconds() / 86400
                        break
            if recovery_days is not None:
                recoveries.append(recovery_days)
            complete = forward_end >= ep_end + timedelta(days=FORWARD_DAYS)
            if complete:
                complete_n += 1
                end_dt = curve[idxs[-1]]["dt"]

                def wealth_after(days):
                    cutoff = end_dt + timedelta(days=days)
                    cands = [c for c in curve if c["dt"] <= cutoff]
                    return cands[-1]["wealth"] if cands else None
                rec30.append(wealth_after(30) is not None and wealth_after(30) >= BASELINE_USD)
                rec60.append(wealth_after(60) is not None and wealth_after(60) >= BASELINE_USD)
                rec90.append(wealth_after(90) is not None and wealth_after(90) >= BASELINE_USD)

        # false-alarm proxy: episodes whose peak severity never breached a MUCH deeper 90th-pct
        # drawdown level -- i.e. flagged but the damage stayed mild
        mild_threshold = pct([float(r["max_drawdown_pct"]) for r in calib], 50)
        false_alarms = sum(1 for d in dds if d < mild_threshold)

        out.append({
            "lo_pct": lo, "hi_pct": hi,
            "win_rate_threshold": round(wr_t, 2), "pf_threshold": round(pf_t, 3), "reversal_threshold": round(rev_t, 2),
            "episode_count": len(episodes),
            "episodes_with_full_90d_horizon": complete_n,
            "avg_max_drawdown_pct": round(statistics.mean(dds), 2) if dds else None,
            "median_recovery_days": round(statistics.median(recoveries), 1) if recoveries else None,
            "recovery_rate_30d_pct": round(sum(rec30) / len(rec30) * 100, 1) if rec30 else None,
            "recovery_rate_60d_pct": round(sum(rec60) / len(rec60) * 100, 1) if rec60 else None,
            "recovery_rate_90d_pct": round(sum(rec90) / len(rec90) * 100, 1) if rec90 else None,
            "mild_false_alarms": false_alarms,
            "false_alarm_rate_pct": round(false_alarms / len(dds) * 100, 1) if dds else None,
        })
    return out


def main():
    meta = load_meta()
    data_end_dt = parse_dt(meta["data_through"][:10])

    print("Building score-based episodes (Step 12)...", file=sys.stderr)
    episodes = score_based_episodes(data_end_dt)
    with open(ROOT / "historical_episodes.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(episodes[0].keys()))
        w.writeheader()
        w.writerows(episodes)
    print(f"  {len(episodes)} episodes -> historical_episodes.csv", file=sys.stderr)
    for e in episodes:
        print(f"    {e['episode_start']} -> {e['episode_end']}: peak={e['peak_stress_score']} "
              f"dd={e['max_drawdown_pct']}% recovery={e['recovery_time_days']}d", file=sys.stderr)

    print("Testing hard-rule threshold candidates (Step 13)...", file=sys.stderr)
    candidates = hard_rule_candidates(data_end_dt)
    with open(ROOT / "threshold_candidates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(candidates[0].keys()))
        w.writeheader()
        w.writerows(candidates)
    print(json.dumps(candidates, indent=2))


if __name__ == "__main__":
    main()
