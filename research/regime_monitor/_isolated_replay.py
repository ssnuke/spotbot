"""Shared helper: fresh-capital isolated backtest replay for one date window, used wherever an
episode's own dollar-drawdown / recovery-time / N-day-later outcome needs to be measured on a
comparable, non-compounding-distorted basis (same technique as the three earlier one-off reports
in research/reversal_experiments/)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.reversal_experiments.engine import run_backtest, BASELINE_USD  # noqa: E402
from research.reversal_experiments.shortlist import SHORTLIST  # noqa: E402

SRC_DATA = Path(__file__).resolve().parent.parent / "reversal_experiments" / "data"
WARMUP_PAD_DAYS = 3
REV2C = SHORTLIST[2]

_candles_cache = None
_funding_cache = None


def _load_all():
    global _candles_cache, _funding_cache
    if _candles_cache is None:
        with open(SRC_DATA / "SOL_USDT_full_5m.json") as f:
            _candles_cache = json.load(f)
        with open(SRC_DATA / "SOL_USDT_full_funding.json") as f:
            _funding_cache = json.load(f)
    return _candles_cache, _funding_cache


def replay(ref_start: datetime, forward_days: int, data_end_dt: datetime):
    """Fresh BASELINE_USD backtest starting WARMUP_PAD_DAYS before ref_start, running through
    ref_start + forward_days (capped at data_end_dt). Returns (trades_sorted, curve) where curve
    is a list of {"dt","wealth","peak"} points, one per trade close."""
    all_candles, all_funding = _load_all()
    warmup_start = ref_start - timedelta(days=WARMUP_PAD_DAYS)
    forward_end = min(ref_start + timedelta(days=forward_days), data_end_dt)
    ws_ms, fe_ms = int(warmup_start.timestamp() * 1000), int(forward_end.timestamp() * 1000)
    candles = [c for c in all_candles if ws_ms <= c[0] <= fe_ms]
    funding = [fr for fr in all_funding if ws_ms <= int(fr["fundingTime"]) <= fe_ms]

    result = run_backtest(candles, funding, REV2C, start_capital=BASELINE_USD)
    trades = sorted(result.trades, key=lambda t: t.exit_index)

    wealth = BASELINE_USD
    peak = BASELINE_USD
    curve = []
    for t in trades:
        wealth += t.pnl
        peak = max(peak, wealth)
        curve.append({"dt": datetime.fromisoformat(t.exit_timestamp), "wealth": wealth, "peak": peak})
    return trades, curve, forward_end
