from app.backtester import BacktestConfig, Backtester


def test_backtest_report_contains_key_metrics():
    config = BacktestConfig(start_capital=10000.0, timeframe="1m")
    backtester = Backtester(config=config)
    result = backtester.run([
        [0, 0, 0, 0, 100.0, 100.0],
        [0, 0, 0, 0, 101.0, 101.0],
        [0, 0, 0, 0, 102.0, 102.0],
        [0, 0, 0, 0, 103.0, 103.0],
        [0, 0, 0, 0, 104.0, 104.0],
        [0, 0, 0, 0, 105.0, 105.0],
        [0, 0, 0, 0, 106.0, 106.0],
        [0, 0, 0, 0, 107.0, 107.0],
        [0, 0, 0, 0, 108.0, 108.0],
        [0, 0, 0, 0, 109.0, 109.0],
        [0, 0, 0, 0, 110.0, 110.0],
    ])
    report = backtester.generate_report(result)
    assert "win_rate" in report
    assert "sharpe_like_ratio" in report
    assert "max_drawdown" in report
