from app.backtester import BacktestConfig, Backtester


def test_trailing_exit_model_updates_stop_loss():
    config = BacktestConfig(start_capital=10000.0, timeframe="1m")
    backtester = Backtester(config=config)
    candles = []
    price = 100.0
    for _ in range(25):
        price += 0.5
        candles.append([0, price, price, price, price, price])
    result = backtester.run(candles)
    assert len(result.trades) >= 1
    assert any(trade.partial_exits for trade in result.trades)
