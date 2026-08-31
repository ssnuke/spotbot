"""Regime Health Monitor -- OBSERVABILITY ONLY.

Computes a real-time "how healthy is the strategy's current trading environment compared with
its own historical behavior" snapshot from the live account's actual closed-trade history, using
thresholds frozen in `app/regime_calibration.json` (produced by the research/regime_monitor/
calibration package and reviewed before this file was written -- see
research/regime_monitor/REGIME_THRESHOLD_REPORT.md for the full derivation).

This module NEVER touches trading decisions. It has no access to, and makes no calls into,
RiskManager, the order executor, or the strategy's signal generation. It only reads closed-trade
history via StateStore.list_trade_history_since() and returns a descriptive snapshot. Nothing
here changes position size, leverage, cooldowns, or whether a trade is taken.
"""
from __future__ import annotations

import bisect
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_CALIBRATION_PATH = Path(__file__).resolve().parent / "regime_calibration.json"
_WINDOW_DAYS = 30
_MIN_TRADES_FOR_SNAPSHOT = 20  # far below the 100-trade calibration floor -- see docstring on
# compute_regime_snapshot() for why a live account needs a much lower bar than 6 years of backtest
_HISTORY_LOOKBACK_DAYS = 60  # covers the 30d window plus the 14d trend/hysteresis/hard-rule lookback
_TREND_LOOKBACK_DAYS = [7, 14]
_HARD_RULE_SUSTAINED_DAYS = 14
_PF_CAP = 999.0

BANDS_IN_ORDER = ["HEALTHY", "NORMAL", "CAUTION", "STRESS", "EXTREME_STRESS"]


@dataclass
class RegimeSnapshot:
    as_of: str
    insufficient_history: bool
    days_of_history_available: float
    trades_in_window: int = 0
    band: Optional[str] = None
    stress_score: Optional[float] = None
    trend: Optional[str] = None  # IMPROVING / STABLE / DETERIORATING / None
    days_in_current_state: Optional[int] = None
    profit_factor: Optional[float] = None
    profit_factor_percentile: Optional[float] = None
    win_rate: Optional[float] = None
    win_rate_percentile: Optional[float] = None
    reversal_share: Optional[float] = None
    reversal_share_percentile: Optional[float] = None
    reversal_density: Optional[float] = None
    reversal_density_label: Optional[str] = None  # LOW / MEDIUM / HIGH
    expectancy: Optional[float] = None
    current_drawdown_pct: Optional[float] = None
    hard_flag_active: bool = False
    hard_flag_days_sustained: int = 0
    historical_context: dict = field(default_factory=dict)


def _load_calibration() -> dict:
    with open(_CALIBRATION_PATH) as f:
        return json.load(f)


def _percentile_rank(sorted_ref: list, value: float) -> float:
    return bisect.bisect_right(sorted_ref, value) / len(sorted_ref) * 100


def _window_trade_stats(trades: list) -> Optional[dict]:
    """trades: list of ClosedTradeRecord-like objects (pnl, exit_reason, closed_at: str)."""
    n = len(trades)
    if n == 0:
        return None
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (_PF_CAP if gross_win > 0 else 0.0)
    reversal = [t for t in trades if t.exit_reason == "signal_reversal"]
    return {
        "trade_count": n,
        "win_rate": len(wins) / n * 100,
        "profit_factor": min(pf, _PF_CAP),
        "reversal_exit_share": len(reversal) / n * 100,
        "expectancy": sum(t.pnl for t in trades) / n,
    }


def _reversal_density(trades: list) -> float:
    """Mean signal_reversal exits per trailing 4h bucket, anchored at every trade close --
    same definition used in the calibration (research/regime_monitor/build_dataset.py)."""
    exit_dts = sorted(datetime.fromisoformat(t.closed_at) for t in trades)
    reversal_dts = sorted(datetime.fromisoformat(t.closed_at) for t in trades if t.exit_reason == "signal_reversal")
    if not reversal_dts or not exit_dts:
        return 0.0
    counts = []
    i = 0
    for anchor in exit_dts:
        while i < len(reversal_dts) and reversal_dts[i] < anchor - timedelta(hours=4):
            i += 1
        counts.append(sum(1 for j in range(i, len(reversal_dts)) if anchor - timedelta(hours=4) <= reversal_dts[j] <= anchor))
    return sum(counts) / len(counts) if counts else 0.0


def _current_drawdown_pct(store) -> float:
    """How far below its all-time peak the account's REAL equity sits right now -- context only,
    never a score input (see Step 20 in the report). Deliberately reads the actual persisted
    `equity`/`peak_equity` from risk_state (the same figures RiskManager's own max_drawdown_pct
    halt is computed from) rather than reconstructing a "wealth" curve from raw trade PnL
    starting at 0: on a young or currently-losing account, cumulative PnL can go negative
    relative to a small early peak (e.g. peak +$0.50 from one early win, now -$3 after a losing
    streak), which would make a from-zero reconstruction produce a nonsensical >100% figure.
    Real equity is always anchored to the account's actual starting capital, so this stays a
    proper 0-100% figure."""
    state = store.load_risk_state()
    if not state:
        return 0.0
    equity = state.get("equity")
    peak_equity = state.get("peak_equity")
    if not equity or not peak_equity or peak_equity <= 0:
        return 0.0
    return max(0.0, min(100.0, (peak_equity - equity) / peak_equity * 100))


def _stress_score(pf: float, reversal_share: float, calib: dict) -> float:
    ref = calib["reference_distributions"]
    weights = calib["score_weights"]
    pf_badness = 100 - _percentile_rank(ref["profit_factor"], pf)
    reversal_badness = _percentile_rank(ref["reversal_exit_share"], reversal_share)
    return weights["pf_weight"] * pf_badness + weights["reversal_weight"] * reversal_badness


def _classify_band(score: float, cutoffs: dict) -> str:
    if score < cutoffs["p25"]:
        return "HEALTHY"
    if score < cutoffs["p50"]:
        return "NORMAL"
    if score < cutoffs["p75"]:
        return "CAUTION"
    if score < cutoffs["p90"]:
        return "STRESS"
    return "EXTREME_STRESS"


def _density_label(density: float, calib: dict) -> str:
    ref = sorted(calib["reference_distributions"]["reversal_density"])
    p33 = ref[int(len(ref) * 0.33)]
    p66 = ref[int(len(ref) * 0.66)]
    if density < p33:
        return "LOW"
    if density < p66:
        return "MEDIUM"
    return "HIGH"


def _daily_window_series(all_trades: list, now: datetime, days_back: int, calib: dict) -> list:
    """One entry per day for the trailing `days_back` days, each describing that day's own
    trailing 30-day window -- used for trend/hysteresis/hard-rule persistence checks."""
    series = []
    for d in range(days_back, -1, -1):
        end = now - timedelta(days=d)
        start = end - timedelta(days=_WINDOW_DAYS)
        window_trades = [t for t in all_trades if start <= datetime.fromisoformat(t.closed_at) <= end]
        stats = _window_trade_stats(window_trades)
        if stats is None or stats["trade_count"] < _MIN_TRADES_FOR_SNAPSHOT:
            series.append({"date": end.date().isoformat(), "stats": None})
            continue
        score = _stress_score(stats["profit_factor"], stats["reversal_exit_share"], calib)
        band = _classify_band(score, calib["band_cutoffs"])
        hard_rule = calib["hard_rule"]
        hard_hit = (
            stats["win_rate"] <= hard_rule["win_rate_max"]
            and stats["profit_factor"] <= hard_rule["profit_factor_max"]
            and stats["reversal_exit_share"] >= hard_rule["reversal_share_min"]
        )
        series.append({"date": end.date().isoformat(), "stats": stats, "score": score, "band": band, "hard_hit": hard_hit})
    return series


def _hysteresis_state(series: list, calib: dict) -> tuple:
    """Confirms band elevation using the frozen enter/exit persistence rule. Returns
    (confirmed_elevated: bool, days_in_current_confirmed_state: int)."""
    enter_days = calib["hysteresis"]["enter_days"]
    exit_days = calib["hysteresis"]["exit_days"]
    state = "NOT_ELEVATED"
    consec_elevated = consec_not = 0
    days_in_state = 0
    for entry in series:
        if entry["stats"] is None:
            continue
        elevated = entry["band"] in ("STRESS", "EXTREME_STRESS")
        if elevated:
            consec_elevated += 1
            consec_not = 0
        else:
            consec_not += 1
            consec_elevated = 0
        if state == "NOT_ELEVATED" and consec_elevated >= enter_days:
            state = "ELEVATED"
            days_in_state = 0
        elif state == "ELEVATED" and consec_not >= exit_days:
            state = "NOT_ELEVATED"
            days_in_state = 0
        days_in_state += 1
    return state == "ELEVATED", days_in_state


def _trend(series: list, calib: dict) -> Optional[str]:
    """Looks up "today" and "7 days ago" by DATE, not list position -- a day with too few
    trades is dropped from `series` as a None-stats entry, which would silently misalign a
    positional lookup (e.g. valid[-8]) whenever any gap exists."""
    by_date = {e["date"]: e for e in series if e["stats"] is not None}
    dates = sorted(by_date)
    if not dates:
        return None
    today = dates[-1]
    seven_ago = (datetime.fromisoformat(today) - timedelta(days=7)).date().isoformat()
    if seven_ago not in by_date:
        return None
    delta_7d = by_date[today]["score"] - by_date[seven_ago]["score"]
    band = calib["trend"]
    if delta_7d < band["noise_band_low"]:
        return "IMPROVING"
    if delta_7d > band["noise_band_high"]:
        return "DETERIORATING"
    return "STABLE"


def _hard_flag_sustained_days(series: list) -> int:
    count = 0
    for entry in reversed(series):
        if entry["stats"] is not None and entry.get("hard_hit"):
            count += 1
        else:
            break
    return count


def compute_regime_snapshot(store, now: Optional[datetime] = None) -> RegimeSnapshot:
    """The one public entry point. `store` is an app.state_store.StateStore (or anything with
    the same list_trade_history_since() signature). Read-only: fetches closed-trade history and
    returns a descriptive snapshot. Never writes to the store, never touches risk/strategy state.

    _MIN_TRADES_FOR_SNAPSHOT (20) is deliberately far below the 100-trade floor used for the
    6-year backtest calibration (research/regime_monitor/sample_size.py) -- that floor was chosen
    because it changed nothing in a dataset where even the quietest 30-day window had 141 trades.
    A live account's early days will have far fewer trades than that, and 20 is a pragmatic
    "enough to not be pure noise" floor for a still-young live history, not a recalibrated value.
    """
    now = now or datetime.now(timezone.utc)
    calib = _load_calibration()

    since = now - timedelta(days=_HISTORY_LOOKBACK_DAYS)
    all_trades = store.list_trade_history_since(since.isoformat())

    current_window_start = now - timedelta(days=_WINDOW_DAYS)
    current_trades = [t for t in all_trades if current_window_start <= datetime.fromisoformat(t.closed_at) <= now]

    if not all_trades:
        days_available = 0.0
    else:
        earliest = min(datetime.fromisoformat(t.closed_at) for t in all_trades)
        days_available = (now - earliest).total_seconds() / 86400

    stats = _window_trade_stats(current_trades)
    if stats is None or stats["trade_count"] < _MIN_TRADES_FOR_SNAPSHOT:
        return RegimeSnapshot(
            as_of=now.isoformat(),
            insufficient_history=True,
            days_of_history_available=round(days_available, 1),
            trades_in_window=stats["trade_count"] if stats else 0,
        )

    score = _stress_score(stats["profit_factor"], stats["reversal_exit_share"], calib)
    band = _classify_band(score, calib["band_cutoffs"])
    density = _reversal_density(current_trades)
    drawdown = _current_drawdown_pct(store)
    ref = calib["reference_distributions"]

    # days_back=16 -> series already ends with an entry for "today" (d=0), computed the same way
    # as `stats` above -- do not recompute/append a separate "today" entry on top of it.
    series = _daily_window_series(all_trades, now, days_back=16, calib=calib)
    _, days_in_state = _hysteresis_state(series, calib)
    trend = _trend(series, calib)
    hard_rule = calib["hard_rule"]
    sustained_days = _hard_flag_sustained_days(series)
    hard_flag_active = sustained_days >= hard_rule["sustained_days"]

    band_context = calib["band_median_stats"].get(band, {})
    hist = calib["historical_summary"]
    historical_context = {
        "band_pct_of_history": band_context.get("pct_of_days"),
        "band_median_pf": band_context.get("median_profit_factor"),
        "band_median_win_rate": band_context.get("median_win_rate"),
        "band_median_reversal_share": band_context.get("median_reversal_share"),
        "elevated_episodes_in_6yr_history": hist["elevated_episodes_total"],
        "hard_rule_historical_episodes": hist["hard_rule_episodes_q25_75"],
        "hard_rule_recovery_rate_30_60_90d_pct": [
            hist["hard_rule_recovery_rate_30d_pct"],
            hist["hard_rule_recovery_rate_60d_pct"],
            hist["hard_rule_recovery_rate_90d_pct"],
        ],
        "hard_rule_median_recovery_days": hist["hard_rule_median_recovery_days"],
    }

    return RegimeSnapshot(
        as_of=now.isoformat(),
        insufficient_history=False,
        days_of_history_available=round(days_available, 1),
        trades_in_window=stats["trade_count"],
        band=band,
        stress_score=round(score, 1),
        trend=trend,
        days_in_current_state=days_in_state,
        profit_factor=round(stats["profit_factor"], 3),
        profit_factor_percentile=round(_percentile_rank(ref["profit_factor"], stats["profit_factor"]), 1),
        win_rate=round(stats["win_rate"], 1),
        win_rate_percentile=round(_percentile_rank(ref["win_rate"], stats["win_rate"]), 1),
        reversal_share=round(stats["reversal_exit_share"], 1),
        reversal_share_percentile=round(_percentile_rank(ref["reversal_exit_share"], stats["reversal_exit_share"]), 1),
        reversal_density=round(density, 2),
        reversal_density_label=_density_label(density, calib),
        expectancy=round(stats["expectancy"], 4),
        current_drawdown_pct=round(drawdown, 2),
        hard_flag_active=hard_flag_active,
        hard_flag_days_sustained=sustained_days,
        historical_context=historical_context,
    )


def format_snapshot_message(snap: RegimeSnapshot) -> str:
    """Renders the Telegram /regime message. Pure formatting -- no trading logic."""
    if snap.insufficient_history:
        return (
            "\U0001F4CA Regime Health\n\n"
            f"Not enough live trade history yet ({snap.trades_in_window} trades, "
            f"~{snap.days_of_history_available:.1f} days of live history).\n"
            f"Needs at least {_MIN_TRADES_FOR_SNAPSHOT} trades in the trailing {_WINDOW_DAYS} days "
            "to compute a snapshot. This will fill in as REV-2C keeps trading live."
        )

    band_emoji = {"HEALTHY": "\U0001F7E2", "NORMAL": "\U0001F7E2", "CAUTION": "\U0001F7E1",
                  "STRESS": "\U0001F7E0", "EXTREME_STRESS": "\U0001F534"}
    trend_arrow = {"IMPROVING": "↑ IMPROVING", "DETERIORATING": "↓ DETERIORATING",
                   "STABLE": "→ STABLE", None: "not enough history yet"}
    hc = snap.historical_context
    hard_flag_text = (
        f"\U0001F6A8 ACTIVE ({snap.hard_flag_days_sustained}d)" if snap.hard_flag_active else "not active"
    )

    lines = [
        "━" * 22,
        "       REGIME HEALTH",
        "━" * 22,
        "",
        f"State: {band_emoji.get(snap.band, '')} {snap.band}",
        f"Stress score: {snap.stress_score:.0f} / 100",
        f"Trend: {trend_arrow.get(snap.trend, snap.trend)}",
        f"Days in current state: {snap.days_in_current_state}",
        "",
        f"30D Profit Factor: {snap.profit_factor:.2f}  (historical percentile: {snap.profit_factor_percentile:.0f}th)",
        f"30D Win Rate: {snap.win_rate:.1f}%  (historical percentile: {snap.win_rate_percentile:.0f}th)",
        f"30D Reversal Share: {snap.reversal_share:.1f}%  (historical percentile: {snap.reversal_share_percentile:.0f}th)",
        f"4H Reversal Density: {snap.reversal_density_label} ({snap.reversal_density:.2f}/bucket)",
        f"30D Expectancy: ${snap.expectancy:+.3f}/trade",
        f"30D Trades: {snap.trades_in_window}",
        f"Current Drawdown (context): {snap.current_drawdown_pct:.1f}%",
        "",
        f"JULY_LIKE_FAILURE flag: {hard_flag_text}",
        "",
        f"Historically, {snap.band} windows had a median PF of {hc.get('band_median_pf')} and "
        f"win rate of {hc.get('band_median_win_rate')}% ({hc.get('band_pct_of_history')}% of the "
        "6-year backtest history).",
        f"Historical JULY_LIKE_FAILURE episodes: {hc.get('hard_rule_historical_episodes')}, "
        f"recovered {hc.get('hard_rule_recovery_rate_30_60_90d_pct')[2]:.0f}% of the time within "
        f"90 days (median {hc.get('hard_rule_median_recovery_days')}d).",
        "",
        "━" * 22,
        "Descriptive only -- does not change trading behavior.",
    ]
    return "\n".join(lines)
