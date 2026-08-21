from datetime import datetime

import pytest

from app.backtester import BacktestConfig, Backtester


def test_backtester_initializes_with_defaults():
    config = BacktestConfig(start_capital=10000.0, timeframe="1m")
    backtester = Backtester(config=config)
    assert backtester.config.start_capital == 10000.0
    assert backtester.config.timeframe == "1m"


def test_long_only_skips_sell_signals():
    # A monotonic downtrend only ever produces sell (short) signals from MomentumStrategy.
    downtrend = [100.0 - i * 0.5 for i in range(30)]
    candles = [[0, p, p, p, p, p] for p in downtrend]

    result_both_directions = Backtester(config=BacktestConfig(start_capital=10000.0, timeframe="1m")).run(candles)
    assert any(t.side == "sell" for t in result_both_directions.trades)

    result_long_only = Backtester(
        config=BacktestConfig(start_capital=10000.0, timeframe="1m", long_only=True)
    ).run(candles)
    assert result_long_only.trades == []  # every signal here was a sell, all filtered out


def test_backtester_applies_fees_and_slippage():
    config = BacktestConfig(
        start_capital=10000.0,
        timeframe="1m",
        trade_fee_pct=0.001,
        slippage_pct=0.001,
    )
    backtester = Backtester(config=config)
    prices = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 104.0, 105.0]
    result = backtester.run([[0, p, p, p, p, p] for p in prices])

    assert len(result.trades) >= 1
    trade = result.trades[0]
    assert trade.fees >= 0.0
    assert trade.entry_execution_price != trade.entry_price
    assert trade.exit_execution_price != 0.0
    assert trade.pnl <= (trade.quantity * (trade.exit_price - trade.entry_price))


def test_backtester_uses_atr_stop_and_position_sizing():
    config = BacktestConfig(
        start_capital=10000.0,
        timeframe="1m",
        use_atr_stop=True,
        atr_period=14,
        atr_multiplier=2.0,
        trade_fee_pct=0.0,
        slippage_pct=0.0,
        max_position_pct=1.0,
    )
    backtester = Backtester(config=config)
    candles = []
    close_price = 100.0
    for _ in range(20):
        high = close_price + 1.0
        low = close_price - 1.0
        candles.append([0, close_price, high, low, close_price, 0])
        close_price += 1.0

    result = backtester.run(candles)
    assert len(result.trades) >= 1
    trade = result.trades[0]
    expected_atr = 2.0
    expected_stop_distance = expected_atr * config.atr_multiplier
    assert abs(trade.entry_price - trade.stop_loss - expected_stop_distance) < 1e-6
    assert trade.quantity == config.start_capital * config.risk_per_trade_pct / expected_stop_distance


def test_backtester_releases_open_position_after_trade_close():
    config = BacktestConfig(start_capital=10000.0, timeframe="1m")
    backtester = Backtester(config=config)
    prices = [100.0] * 25
    prices.extend([110.0] * 25)
    prices.extend([90.0] * 25)

    result = backtester.run([[0, p, p, p, p, p] for p in prices])
    assert len(result.trades) >= 2
    assert backtester.risk_manager.open_positions == 0


def test_backtester_resets_daily_limits_between_days():
    config = BacktestConfig(
        start_capital=10000.0,
        timeframe="1m",
        risk_per_trade_pct=0.003,
        max_daily_loss_pct=0.03,
        max_trades_per_day=4,
        max_open_positions=3,
        use_atr_stop=False,
        trade_fee_pct=0.0,
        slippage_pct=0.0,
    )
    backtester = Backtester(config=config)
    candles = []
    for day in range(2):
        close_price = 100.0
        for i in range(8):
            high = close_price + 1.0
            low = close_price - 1.0
            candles.append([f"2025-01-{day + 1:02d}T00:0{i}:00", close_price, high, low, close_price, 0])
            close_price += 1.0

    result = backtester.run(candles)

    assert len(result.trades) >= 4
    assert any("New day 2025-01-02" in entry for entry in backtester.telemetry.entries)


def test_leverage_reduces_margin_required_for_same_notional():
    prices = [100.0] * 25
    prices.extend([110.0] * 25)
    candles = [[0, p, p, p, p, p] for p in prices]

    unleveraged = Backtester(
        config=BacktestConfig(start_capital=10000.0, timeframe="1m", max_position_pct=1.0, leverage=1.0)
    ).run(candles)
    leveraged = Backtester(
        config=BacktestConfig(start_capital=10000.0, timeframe="1m", max_position_pct=1.0, leverage=2.0)
    ).run(candles)

    assert len(unleveraged.trades) >= 1 and len(leveraged.trades) >= 1
    # Same max_position_pct notional cap either way, but leverage=2.0 ties up half the margin.
    assert leveraged.trades[0].notional == pytest.approx(unleveraged.trades[0].notional, rel=0.05)
    assert leveraged.trades[0].margin_required == pytest.approx(leveraged.trades[0].notional / 2.0)


def test_liquidation_closes_position_before_stop_loss_at_high_leverage():
    # At 10x leverage the estimated liquidation distance is ~9.6% (default maintenance_margin_rate),
    # so a 5% stop comfortably passes the liquidation-buffer check at trade-open. A single-candle
    # ~25% crash then jumps straight past both the 5% stop AND the ~9.6% liquidation distance --
    # since the liquidation check runs first each candle, it must win, not the stop-loss.
    config = BacktestConfig(
        start_capital=10000.0, timeframe="1m", max_position_pct=1.0,
        leverage=10.0, max_leverage=10.0, stop_loss_pct=0.05,
        trade_fee_pct=0.0, slippage_pct=0.0, max_trades_per_day=100,
    )
    backtester = Backtester(config=config)
    uptrend = [100.0 + i * 0.5 for i in range(30)]
    crash = [uptrend[-1] * 0.75] * 10  # a ~25% instant drop, well past the ~9.6% liquidation distance
    candles = [[0, p, p, p, p, p] for p in uptrend + crash]

    result = backtester.run(candles)

    # The steady uptrend opens and take-profits several trades before the crash; only the
    # trade(s) still open when the crash hits should show a liquidation exit.
    liquidated_trades = [t for t in result.trades if t.exit_reason == "liquidation"]
    assert liquidated_trades
    assert all(t.pnl < 0 for t in liquidated_trades)


def test_funding_cost_applied_when_enabled():
    config = BacktestConfig(
        start_capital=10000.0, timeframe="1m", max_position_pct=1.0, leverage=2.0,
        funding_enabled=True, trade_fee_pct=0.0, slippage_pct=0.0,
    )
    backtester = Backtester(config=config)
    prices = [100.0] * 25
    prices.extend([110.0] * 25)
    candles = [
        [f"2025-01-01T{(i // 12):02d}:{(i % 12) * 5:02d}:00", p, p, p, p, 0]
        for i, p in enumerate(prices)
    ]
    # One funding event at a fixed positive rate, timestamped inside the trade's holding window
    # (entry is at the price jump, index 25 = "2025-01-01T02:05:00"; the trade then holds through
    # the flat 110.0 segment to EOD at index 49 = "2025-01-01T04:05:00").
    funding_rates = [{"fundingTime": int(datetime.fromisoformat("2025-01-01T03:00:00").timestamp() * 1000), "fundingRate": 0.001}]

    result = backtester.run(candles, funding_rates=funding_rates)

    assert len(result.trades) >= 1
    trade = result.trades[0]
    assert trade.funding_cost == pytest.approx(trade.notional * 0.001)  # long pays positive funding


def test_export_results_csv_includes_every_backtest_trade_field(tmp_path):
    # Regression guard: export_results' CSV fieldnames must be derived from BacktestTrade
    # itself, not a hand-maintained list that silently drops newly-added fields.
    import csv as csv_module
    from dataclasses import fields as dataclass_fields

    from app.backtester import BacktestTrade

    prices = [100.0] * 25
    prices.extend([110.0] * 25)
    candles = [[0, p, p, p, p, p] for p in prices]
    result = Backtester(config=BacktestConfig(start_capital=10000.0, timeframe="1m")).run(candles)
    assert result.trades

    output_path = tmp_path / "trades.csv"
    Backtester().export_results(result, str(output_path), format="csv")

    with open(output_path, newline="", encoding="utf-8") as handle:
        header = next(csv_module.reader(handle))

    assert set(header) == {f.name for f in dataclass_fields(BacktestTrade)}


def _entry_setup_candles():
    """Four flat warmup candles (satisfies MomentumStrategy's warmup_period=4) followed by a
    jump that fires a buy signal on the 5th candle (index 4), entry_price=110."""
    return [[0, 100.0, 100.0, 100.0, 100.0, 0] for _ in range(4)] + [[0, 110.0, 110.0, 110.0, 110.0, 0]]


def test_take_profit_triggers_on_intrabar_high_even_if_close_is_below_it():
    # entry=110, take_profit=110*1.03=113.3. The next candle's CLOSE (112.0) never reaches
    # it -- only the HIGH (114.0) does. Close-only logic would have missed this entirely.
    config = BacktestConfig(
        start_capital=10000.0, timeframe="1m", stop_loss_pct=0.01, take_profit_pct=0.03,
        partial_exit_profit_pct=0.5, max_position_pct=1.0, trade_fee_pct=0.0, slippage_pct=0.0,
    )
    candles = _entry_setup_candles()
    candles.append([0, 110.0, 114.0, 112.0, 112.0, 0])  # high touches TP, close stays well below it

    result = Backtester(config=config).run(candles)

    assert result.trades
    trade = result.trades[0]
    assert trade.entry_price == pytest.approx(110.0)
    assert trade.exit_reason == "take_profit"
    assert trade.exit_price == pytest.approx(113.3)


def test_trailing_stop_triggers_on_intrabar_low_even_if_close_is_above_it():
    # entry=110, stop=110*0.99=108.9. The next candle's CLOSE (110.2) stays above it -- only
    # the LOW (108.0) breaches it. Close-only logic would have let this trade ride through.
    config = BacktestConfig(
        start_capital=10000.0, timeframe="1m", stop_loss_pct=0.01, take_profit_pct=0.03,
        partial_exit_profit_pct=0.5, max_position_pct=1.0, trade_fee_pct=0.0, slippage_pct=0.0,
    )
    candles = _entry_setup_candles()
    candles.append([0, 110.0, 110.5, 108.0, 110.2, 0])  # low breaches stop, close stays above it

    result = Backtester(config=config).run(candles)

    assert result.trades
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_price == pytest.approx(108.9)


def test_adverse_outcome_wins_when_both_stop_and_target_are_touched_same_candle():
    # A single candle's range spans BOTH the stop (108.9) and the take-profit (113.3).
    # OHLC data can't say which happened first -- the model must assume the adverse
    # outcome (stop) happened first, not silently pick the favorable one.
    config = BacktestConfig(
        start_capital=10000.0, timeframe="1m", stop_loss_pct=0.01, take_profit_pct=0.03,
        partial_exit_profit_pct=0.5, max_position_pct=1.0, trade_fee_pct=0.0, slippage_pct=0.0,
    )
    candles = _entry_setup_candles()
    candles.append([0, 110.0, 114.0, 108.0, 111.0, 0])  # low breaches stop AND high breaches TP

    result = Backtester(config=config).run(candles)

    assert result.trades
    trade = result.trades[0]
    assert trade.exit_reason == "trailing_stop"
    assert trade.exit_price == pytest.approx(108.9)


def test_liquidation_wins_over_stop_when_both_touched_same_candle():
    # At 10x leverage the estimated liquidation price (~99.44) sits well below the 5% stop
    # (104.5). A candle whose low reaches down through both must register as a liquidation,
    # not a stop-loss -- liquidation is checked first because it's always the farther level.
    config = BacktestConfig(
        start_capital=10000.0, timeframe="1m", stop_loss_pct=0.05, take_profit_pct=0.03,
        partial_exit_profit_pct=0.5, max_position_pct=1.0, trade_fee_pct=0.0, slippage_pct=0.0,
        leverage=10.0, max_leverage=10.0,
    )
    candles = _entry_setup_candles()
    candles.append([0, 110.0, 110.5, 95.0, 100.0, 0])  # low breaches both liquidation and stop

    result = Backtester(config=config).run(candles)

    assert result.trades
    trade = result.trades[0]
    assert trade.exit_reason == "liquidation"
    assert trade.pnl < 0


def test_funding_disabled_by_default_leaves_cost_at_zero():
    prices = [100.0] * 25
    prices.extend([110.0] * 25)
    candles = [[0, p, p, p, p, p] for p in prices]
    funding_rates = [{"fundingTime": 0, "fundingRate": 0.01}]

    result = Backtester(config=BacktestConfig(start_capital=10000.0, timeframe="1m")).run(
        candles, funding_rates=funding_rates
    )

    assert len(result.trades) >= 1
    assert result.trades[0].funding_cost == 0.0
