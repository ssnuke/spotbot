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
    def __init__(self, trades, risk_state=None):
        self.trades = trades
        self.risk_state = risk_state

    def list_trade_history_since(self, since_iso):
        return [t for t in self.trades if t.closed_at >= since_iso]

    def load_risk_state(self):
        return self.risk_state


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


def test_current_drawdown_zero_when_no_risk_state_available():
    # A store with no risk_state row yet (e.g. a truly fresh account) must not error or return
    # a nonsensical figure -- it should read as "no drawdown known", not "0% is definitely right".
    store = FakeStore([], risk_state=None)
    assert regime_monitor._current_drawdown_pct(store) == 0.0


def test_current_drawdown_reads_real_equity_not_reconstructed_from_trade_pnl():
    # Regression test: the real bug this replaced. A young/currently-losing live account can
    # have cumulative trade PnL go negative relative to a small early peak (e.g. peak +$0.50 from
    # one early win, now -$3 after a losing streak) -- reconstructing "wealth" from raw trade PnL
    # starting at 0 produced a nonsensical >100% drawdown in that case. Reading the account's
    # REAL equity/peak_equity (the same figures RiskManager's own drawdown halt uses) instead
    # keeps this bounded to a proper 0-100% figure regardless of how the trade history looks.
    store = FakeStore([], risk_state={"equity": 121.0, "peak_equity": 130.0})
    dd = regime_monitor._current_drawdown_pct(store)
    assert dd == pytest.approx((130.0 - 121.0) / 130.0 * 100)
    assert 0.0 <= dd <= 100.0


def test_current_drawdown_is_bounded_even_with_bad_inputs():
    store = FakeStore([], risk_state={"equity": -5.0, "peak_equity": 0.5})
    dd = regime_monitor._current_drawdown_pct(store)
    assert 0.0 <= dd <= 100.0


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
