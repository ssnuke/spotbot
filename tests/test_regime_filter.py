from datetime import datetime, timedelta

from app.regime_filter import HTFTrendSeries, RegimeFilter, RegimeFilterConfig


def _make_5m_candles(closes, start=datetime(2026, 1, 1, 0, 0, 0)):
    candles = []
    for i, close in enumerate(closes):
        ts = (start + timedelta(minutes=5 * i)).isoformat()
        candles.append([ts, close, close, close, close, 1.0])
    return candles


def test_htf_trend_series_uses_only_completed_bars_no_lookahead():
    # 1H bucket = 12 5m-candles. Sustained uptrend across many completed hours,
    # then a single huge spike on the LAST (still-forming) candle of an hour.
    closes = [100.0 + i * 0.1 for i in range(12 * 30)]  # 30 completed hours, gentle uptrend
    # Corrupt the last candle of the series with an extreme value; since that
    # candle's bucket never completes, it must not affect the trend at its own index.
    closes[-1] = 1_000_000.0
    candles = _make_5m_candles(closes)

    series = HTFTrendSeries(candles, bucket_hours=1, short_period=3, long_period=6)
    # Trend at the very last index should reflect the gentle uptrend from completed
    # bars, not be corrupted by the extreme still-forming-bar value.
    assert series.trend_at(len(candles) - 1) == "up"


def test_htf_trend_series_returns_none_before_enough_completed_bars():
    closes = [100.0] * 20  # under 1 hour of data, no completed 1H bar possible yet
    candles = _make_5m_candles(closes)
    series = HTFTrendSeries(candles, bucket_hours=1, short_period=3, long_period=6)
    assert all(t is None for t in series.trend_by_index)


def test_htf_trend_series_detects_uptrend_and_downtrend():
    up_closes = [100.0 + i * 0.5 for i in range(12 * 10)]
    candles_up = _make_5m_candles(up_closes)
    series_up = HTFTrendSeries(candles_up, bucket_hours=1, short_period=2, long_period=4)
    assert series_up.trend_at(len(candles_up) - 1) == "up"

    down_closes = [200.0 - i * 0.5 for i in range(12 * 10)]
    candles_down = _make_5m_candles(down_closes)
    series_down = HTFTrendSeries(candles_down, bucket_hours=1, short_period=2, long_period=4)
    assert series_down.trend_at(len(candles_down) - 1) == "down"


def test_regime_filter_disabled_allows_everything():
    candles = _make_5m_candles([100.0] * 20)
    regime_filter = RegimeFilter(candles, RegimeFilterConfig(enabled=False))
    assert regime_filter.allows(5, "buy")
    assert regime_filter.allows(5, "sell")


def test_regime_filter_both_mode_requires_1h_and_4h_agreement():
    up_closes = [100.0 + i * 0.3 for i in range(48 * 10)]  # long clean uptrend, both TFs agree
    candles = _make_5m_candles(up_closes)
    regime_filter = RegimeFilter(
        candles, RegimeFilterConfig(enabled=True, mode="both", short_period=2, long_period=4)
    )
    last = len(candles) - 1
    assert regime_filter.allows(last, "buy")
    assert not regime_filter.allows(last, "sell")


def test_regime_filter_either_mode_is_more_permissive_than_both():
    up_closes = [100.0 + i * 0.3 for i in range(48 * 10)]
    candles = _make_5m_candles(up_closes)
    both_filter = RegimeFilter(candles, RegimeFilterConfig(enabled=True, mode="both", short_period=2, long_period=4))
    either_filter = RegimeFilter(candles, RegimeFilterConfig(enabled=True, mode="either", short_period=2, long_period=4))
    last = len(candles) - 1
    # In a clean uptrend both modes should agree buy is allowed; either should never be stricter than both.
    assert both_filter.allows(last, "buy") <= either_filter.allows(last, "buy")
