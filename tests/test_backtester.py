from app.backtester import BacktestConfig, Backtester


def test_backtester_initializes_with_defaults():
    config = BacktestConfig(start_capital=10000.0, timeframe="1m")
    backtester = Backtester(config=config)
    assert backtester.config.start_capital == 10000.0
    assert backtester.config.timeframe == "1m"


def test_backtester_applies_fees_and_slippage():
    config = BacktestConfig(
        start_capital=10000.0,
        timeframe="1m",
        trade_fee_pct=0.001,
        slippage_pct=0.001,
    )
    backtester = Backtester(config=config)
    prices = [100.0, 100.5, 101.0, 101.5, 102.0, 102.5, 103.0, 104.0, 105.0]
    result = backtester.run([[0, 0, 0, 0, p, p] for p in prices])

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

    result = backtester.run([[0, 0, 0, 0, p, p] for p in prices])
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
