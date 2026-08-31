from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app import regime_monitor


@dataclass
class FakeTrade:
    pnl: float
    exit_reason: str
    closed_at: str


class FakeStore:
    def __init__(self, trades):
        self.trades = trades

    def list_trade_history_since(self, since_iso):
        return [t for t in self.trades if t.closed_at >= since_iso]


NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _make_trades(n, win_rate_pct, reversal_pct, win_pnl=2.0, loss_pnl=-1.0, start=None, spacing_hours=2):
    """n trades spread backward from `start` (default NOW), with the given win rate and
    reversal-exit share, evenly interleaved so no single stretch is all-wins or all-losses."""
    start = start or NOW
    n_wins = round(n * win_rate_pct / 100)
    n_reversal = round(n * reversal_pct / 100)
    trades = []
    for i in range(n):
        is_win = i < n_wins
        is_reversal = i < n_reversal
        pnl = win_pnl if is_win else loss_pnl
        reason = "signal_reversal" if is_reversal else "take_profit"
        closed_at = (start - timedelta(hours=spacing_hours * (n - i))).isoformat()
        trades.append(FakeTrade(pnl=pnl, exit_reason=reason, closed_at=closed_at))
    # interleave win/loss and reversal/non-reversal so it's not one long streak-then-streak block
    trades.sort(key=lambda t: t.closed_at)
    import random
    rng = random.Random(42)
    pnls = [t.pnl for t in trades]
    reasons = [t.exit_reason for t in trades]
    rng.shuffle(pnls)
    rng.shuffle(reasons)
    for t, pnl, reason in zip(trades, pnls, reasons):
        t.pnl = pnl
        t.exit_reason = reason
    return trades


def test_insufficient_history_returns_flagged_snapshot():
    store = FakeStore(_make_trades(5, win_rate_pct=50, reversal_pct=50))
    snap = regime_monitor.compute_regime_snapshot(store, now=NOW)
    assert snap.insufficient_history is True
    assert snap.band is None
    assert snap.trades_in_window == 5


def test_insufficient_history_with_zero_trades():
    store = FakeStore([])
    snap = regime_monitor.compute_regime_snapshot(store, now=NOW)
    assert snap.insufficient_history is True
    assert snap.days_of_history_available == 0.0


def test_snapshot_computes_expected_band_for_strong_trades():
    # High win rate, low reversal share, no losses -> should read as HEALTHY/NORMAL, not stressed.
    trades = _make_trades(120, win_rate_pct=70, reversal_pct=20, win_pnl=3.0, loss_pnl=-1.0)
    store = FakeStore(trades)
    snap = regime_monitor.compute_regime_snapshot(store, now=NOW)
    assert snap.insufficient_history is False
    assert snap.band in ("HEALTHY", "NORMAL")
    assert snap.profit_factor > 1.5
    assert snap.win_rate == pytest.approx(70, abs=1)
    assert 0 <= snap.profit_factor_percentile <= 100
    assert 0 <= snap.reversal_share_percentile <= 100


def test_snapshot_computes_expected_band_for_weak_whipsaw_trades():
    # Low win rate, very high reversal share, small wins / big losses -> should read as stressed.
    trades = _make_trades(120, win_rate_pct=25, reversal_pct=90, win_pnl=1.0, loss_pnl=-2.0)
    store = FakeStore(trades)
    snap = regime_monitor.compute_regime_snapshot(store, now=NOW)
    assert snap.insufficient_history is False
    assert snap.band in ("STRESS", "EXTREME_STRESS")
    assert snap.profit_factor < 1.0
    assert snap.reversal_share > 80


def test_hard_flag_requires_sustained_days_not_a_single_bad_window():
    calib = regime_monitor._load_calibration()
    rule = calib["hard_rule"]
    # One single day this bad should NOT be enough to set the flag -- only 20 trades (the
    # snapshot minimum), all on "day 0", with no history before it to sustain 14 days.
    trades = _make_trades(
        30, win_rate_pct=rule["win_rate_max"] - 5, reversal_pct=rule["reversal_share_min"] + 5,
        win_pnl=1.0, loss_pnl=-2.0,
    )
    store = FakeStore(trades)
    snap = regime_monitor.compute_regime_snapshot(store, now=NOW)
    assert snap.insufficient_history is False
    assert snap.hard_flag_active is False
    assert snap.hard_flag_days_sustained < rule["sustained_days"]


def test_hard_flag_activates_after_sustained_bad_stretch():
    calib = regime_monitor._load_calibration()
    rule = calib["hard_rule"]
    # 45 days of consistently bad trading (well past the 30d window + 14d sustain requirement),
    # spaced so every daily 30d-lookback window during the tail also reads as a hard-rule hit.
    trades = _make_trades(
        400, win_rate_pct=rule["win_rate_max"] - 10, reversal_pct=rule["reversal_share_min"] + 10,
        win_pnl=1.0, loss_pnl=-2.5, spacing_hours=3,
    )
    store = FakeStore(trades)
    snap = regime_monitor.compute_regime_snapshot(store, now=NOW)
    assert snap.insufficient_history is False
    assert snap.hard_flag_active is True
    assert snap.hard_flag_days_sustained >= rule["sustained_days"]


def test_reversal_density_zero_when_no_reversal_exits():
    trades = _make_trades(50, win_rate_pct=60, reversal_pct=0)
    assert regime_monitor._reversal_density(trades) == 0.0


def test_local_drawdown_zero_when_monotonically_winning():
    trades = [
        FakeTrade(pnl=1.0, exit_reason="take_profit", closed_at=(NOW - timedelta(hours=h)).isoformat())
        for h in range(10, 0, -1)
    ]
    assert regime_monitor._local_drawdown_pct(trades) == 0.0


def test_local_drawdown_positive_after_a_pullback_from_peak():
    times = [NOW - timedelta(hours=h) for h in range(5, 0, -1)]
    pnls = [2.0, 2.0, -1.0, -1.0, 0.5]  # peaks at 4.0, ends at 2.5 -> drawdown from peak
    trades = [FakeTrade(pnl=p, exit_reason="take_profit", closed_at=t.isoformat()) for p, t in zip(pnls, times)]
    dd = regime_monitor._local_drawdown_pct(trades)
    assert dd == pytest.approx((4.0 - 2.5) / 4.0 * 100)


def test_format_message_for_insufficient_history():
    snap = regime_monitor.RegimeSnapshot(
        as_of=NOW.isoformat(), insufficient_history=True, days_of_history_available=2.0, trades_in_window=8,
    )
    msg = regime_monitor.format_snapshot_message(snap)
    assert "Not enough live trade history" in msg
    assert "Regime Health" in msg


def test_format_message_for_full_snapshot_mentions_band_and_no_action_disclaimer():
    trades = _make_trades(120, win_rate_pct=70, reversal_pct=20, win_pnl=3.0, loss_pnl=-1.0)
    store = FakeStore(trades)
    snap = regime_monitor.compute_regime_snapshot(store, now=NOW)
    msg = regime_monitor.format_snapshot_message(snap)
    assert snap.band in msg
    assert "does not change trading behavior" in msg
    assert "JULY_LIKE_FAILURE" in msg
