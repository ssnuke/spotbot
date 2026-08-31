"""Step 1-4: reproduce REV-2C on the full historical dataset using the ACTUAL production math
(research/reversal_experiments/engine.py, which is the same engine already validated field-for-
field against config.json elsewhere in this repo's research), then build the two window datasets
the whole calibration pipeline depends on:

  rolling_windows_daily.csv   -- one row per DAY, each describing the trailing 30-day window
                                  ending that day. This is what a live monitor would actually see.
  calibration_windows.csv     -- NON-OVERLAPPING 30-day windows (see rationale below), used for
                                  every percentile/threshold/score-band calculation so a single
                                  extended regime cannot dominate the calibration by being counted
                                  ~30 times over via heavily-overlapping daily windows.

CALIBRATION SPACING CHOICE (Step 4): non-overlapping (30-day step), not weekly-spaced. A 7-day
step still shares 23 of 30 days (77%) between adjacent windows -- barely more independent than
daily. A 30-day step shares nothing between adjacent windows, so each calibration observation is
a genuinely distinct slice of history. The tradeoff (documented, not hidden) is fewer calibration
points (~72 vs ~2000+), which is exactly why Step 5's minimum-trade-count analysis and Step 15's
threshold-stability analysis both exist -- to check whether that smaller N still gives stable
percentiles.

DATA LIMITATION (Step 1): max_drawdown / drawdown_duration / recovery_time at the WINDOW level are
computed on the ONE continuous 2020-2026 compounding backtest's own equity curve, peak-to-trough
WITHIN each window. This means they reflect a single continuously-compounding account's path, not
an isolated fresh-capital replay -- appropriate for a health-monitor context (Step 20: drawdown is
used as context on "how much damage has happened", not as a score driver), but NOT comparable in
absolute dollar terms to the isolated fresh-₹1,00,000 episode replays used in the three earlier
one-off reports (recent_months / similar_periods / regime_survival). Episode-level 30/60/90-day
dollar outcomes in episodes.py use the isolated-replay method instead, for that reason.

CANONICAL DATA SOURCES (Step 1 findings, see README.md for the full writeup):
  - Trade outcomes, PnL, fees, funding, exit reasons, timestamps, cooldown flags:
    research/reversal_experiments/engine.py TradeRecord, from run_backtest() -- the SAME engine
    used for every research artifact produced in this session, itself built to mirror
    app/live_runner.py + app/risk_manager.py's real exit/risk logic and config.json's "risk"+
    "live"+"strategy" sections exactly (verified earlier).
  - Account equity: reconstructed by accumulating TradeRecord.pnl from a fixed starting capital
    (BASELINE_USD), same convention as every prior report.
  - app/state_store.py (the LIVE sqlite schema) is NOT used here -- the live account has only a
    few days of REV-2C history, nowhere near enough for calibration. This is a real limitation:
    the monitor's thresholds are calibrated on BACKTEST behavior, not the live account's own
    (currently tiny) trade history.
  - app/regime_filter.py exists in the codebase but is an unrelated HTF-EMA trend filter, not
    imported by app/strategy.py, app/live_runner.py, or the research engine -- a naming
    coincidence with "regime" only, not part of REV-2C's actual signal path.
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.reversal_experiments.engine import run_backtest, BASELINE_USD  # noqa: E402
from research.reversal_experiments.shortlist import SHORTLIST  # noqa: E402

ROOT = Path(__file__).resolve().parent
SRC_DATA = ROOT.parent / "reversal_experiments" / "data"
PF_CAP = 999.0
WINDOW_DAYS = 30
DAILY_STEP_DAYS = 1
CALIBRATION_STEP_DAYS = 30  # non-overlapping, see module docstring

REV2C = SHORTLIST[2]
assert REV2C.name == "REV-2C"


def load_trades():
    with open(SRC_DATA / "SOL_USDT_full_5m.json") as f:
        candles = json.load(f)
    with open(SRC_DATA / "SOL_USDT_full_funding.json") as f:
        funding = json.load(f)
    result = run_backtest(candles, funding, REV2C, start_capital=BASELINE_USD)
    trades = sorted(result.trades, key=lambda t: t.exit_index)
    for t in trades:
        t._exit_dt = datetime.fromisoformat(t.exit_timestamp)
        t._entry_dt = datetime.fromisoformat(t.entry_timestamp)
    data_start = datetime.fromtimestamp(candles[0][0] / 1000, tz=timezone.utc)
    data_end = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc)
    return trades, data_start, data_end


def build_equity_curve(trades):
    """Full continuous equity curve, one point per trade close, in chronological order."""
    wealth = BASELINE_USD
    peak = BASELINE_USD
    curve = []
    for t in trades:
        wealth += t.pnl
        peak = max(peak, wealth)
        curve.append({"dt": t._exit_dt, "wealth": wealth, "peak": peak})
    return curve


def _reversal_density(window_trades):
    """Mean number of signal_reversal exits per trailing 4-hour bucket, plus the fraction of
    4-hour buckets that hit the informal "5+ in 4h" threshold flagged in earlier research."""
    reversal_exits = sorted(t._exit_dt for t in window_trades if t.exit_reason == "signal_reversal")
    if not reversal_exits:
        return 0.0, 0.0
    bucket_counts = []
    i = 0
    n = len(reversal_exits)
    all_exits = sorted(t._exit_dt for t in window_trades)
    for anchor in all_exits:
        while i < n and reversal_exits[i] < anchor - timedelta(hours=4):
            i += 1
        count = sum(1 for j in range(i, n) if anchor - timedelta(hours=4) <= reversal_exits[j] <= anchor)
        bucket_counts.append(count)
    mean_density = sum(bucket_counts) / len(bucket_counts) if bucket_counts else 0.0
    frac_high = sum(1 for c in bucket_counts if c >= 5) / len(bucket_counts) if bucket_counts else 0.0
    return round(mean_density, 3), round(frac_high, 4)


def compute_window_row(window_trades, window_start, window_end, curve_slice):
    n = len(window_trades)
    row = {
        "window_start": window_start.date().isoformat(),
        "window_end": window_end.date().isoformat(),
        "trade_count": n,
    }
    if n == 0:
        return {**row, **{k: None for k in [
            "win_rate", "profit_factor", "expectancy", "reversal_exit_share", "reversal_density",
            "reversal_density_frac_high", "max_drawdown_pct", "drawdown_duration_days",
            "recovery_time_days", "cooldown_count", "cooldown_rate", "long_win_rate",
            "short_win_rate", "average_win", "average_loss", "win_loss_ratio",
            "total_return_usd", "total_return_pct",
        ]}}

    wins = [t for t in window_trades if t.pnl > 0]
    losses = [t for t in window_trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (PF_CAP if gross_win > 0 else 0.0)
    reversal = [t for t in window_trades if t.exit_reason == "signal_reversal"]
    cooldowns = [t for t in window_trades if t.triggered_cooldown]
    longs = [t for t in window_trades if t.side == "buy"]
    shorts = [t for t in window_trades if t.side == "sell"]
    long_wins = [t for t in longs if t.pnl > 0]
    short_wins = [t for t in shorts if t.pnl > 0]
    total_pnl = sum(t.pnl for t in window_trades)
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0

    rev_density, rev_density_frac_high = _reversal_density(window_trades)

    # window-local drawdown/duration/recovery: peak-to-trough on the equity path RESTRICTED to
    # this window's own curve slice (see module docstring for why this is window-local, not
    # measured against the continuous account's all-time peak)
    if curve_slice:
        local_peak = curve_slice[0]["wealth"]
        peak_dt = curve_slice[0]["dt"]
        worst_dd, worst_dd_dt, worst_dd_peak_dt = 0.0, None, None
        for pt in curve_slice:
            if pt["wealth"] > local_peak:
                local_peak = pt["wealth"]
                peak_dt = pt["dt"]
            dd = (local_peak - pt["wealth"]) / local_peak * 100 if local_peak > 0 else 0.0
            if dd > worst_dd:
                worst_dd, worst_dd_dt, worst_dd_peak_dt = dd, pt["dt"], peak_dt
        dd_duration = (worst_dd_dt - worst_dd_peak_dt).total_seconds() / 86400 if worst_dd_dt else 0.0
        recovery_days = None
        if worst_dd_dt is not None:
            recovered_target = local_peak
            for pt in curve_slice:
                if pt["dt"] >= worst_dd_dt and pt["wealth"] >= recovered_target:
                    recovery_days = (pt["dt"] - worst_dd_dt).total_seconds() / 86400
                    break
    else:
        worst_dd, dd_duration, recovery_days = 0.0, 0.0, None

    row.update({
        "win_rate": round(len(wins) / n * 100, 3),
        "profit_factor": round(min(pf, PF_CAP), 4),
        "expectancy": round(total_pnl / n, 5),
        "reversal_exit_share": round(len(reversal) / n * 100, 3),
        "reversal_density": rev_density,
        "reversal_density_frac_high": rev_density_frac_high,
        "max_drawdown_pct": round(worst_dd, 3),
        "drawdown_duration_days": round(dd_duration, 2),
        "recovery_time_days": round(recovery_days, 2) if recovery_days is not None else None,
        "cooldown_count": len(cooldowns),
        "cooldown_rate": round(len(cooldowns) / n, 4),
        "long_win_rate": round(len(long_wins) / len(longs) * 100, 3) if longs else None,
        "short_win_rate": round(len(short_wins) / len(shorts) * 100, 3) if shorts else None,
        "average_win": round(avg_win, 5),
        "average_loss": round(avg_loss, 5),
        "win_loss_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else None,
        "total_return_usd": round(total_pnl, 4),
        "total_return_pct": round(total_pnl / BASELINE_USD * 100, 3),
    })
    return row


def build_windows(trades, curve, data_start, data_end, step_days: int):
    """Two-pointer sweep: O(n) per pass instead of O(n * num_windows)."""
    exit_dts = [t._exit_dt for t in trades]
    rows = []
    lo = 0
    end = data_start + timedelta(days=WINDOW_DAYS)
    while end <= data_end:
        start = end - timedelta(days=WINDOW_DAYS)
        while lo < len(exit_dts) and exit_dts[lo] < start:
            lo += 1
        hi = lo
        while hi < len(exit_dts) and exit_dts[hi] < end:
            hi += 1
        window_trades = trades[lo:hi]
        curve_slice = [c for c in curve if start <= c["dt"] < end]
        rows.append(compute_window_row(window_trades, start, end, curve_slice))
        end += timedelta(days=step_days)
    return rows


def write_csv(path: Path, rows: list):
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def main():
    print("Loading candles/funding and running the full REV-2C backtest...", file=sys.stderr)
    trades, data_start, data_end = load_trades()
    print(f"{len(trades)} trades, {data_start.date()} -> {data_end.date()}", file=sys.stderr)

    curve = build_equity_curve(trades)

    print("Building daily rolling windows (30d window, 1d step)...", file=sys.stderr)
    daily_rows = build_windows(trades, curve, data_start, data_end, DAILY_STEP_DAYS)
    print(f"  {len(daily_rows)} daily rolling windows", file=sys.stderr)

    print("Building non-overlapping calibration windows (30d window, 30d step)...", file=sys.stderr)
    calib_rows = build_windows(trades, curve, data_start, data_end, CALIBRATION_STEP_DAYS)
    print(f"  {len(calib_rows)} calibration windows", file=sys.stderr)

    write_csv(ROOT / "rolling_windows_daily.csv", daily_rows)
    write_csv(ROOT / "calibration_windows.csv", calib_rows)

    with open(ROOT / "data_cache" / "meta.json", "w") as f:
        json.dump({
            "trade_count": len(trades),
            "data_from": data_start.isoformat(),
            "data_through": data_end.isoformat(),
            "baseline_usd": BASELINE_USD,
            "window_days": WINDOW_DAYS,
            "daily_step_days": DAILY_STEP_DAYS,
            "calibration_step_days": CALIBRATION_STEP_DAYS,
        }, f, indent=2)

    print("Wrote rolling_windows_daily.csv, calibration_windows.csv, data_cache/meta.json", file=sys.stderr)


if __name__ == "__main__":
    main()
