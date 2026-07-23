from app.strategy import TAEStrategy, TAEStrategyConfig


def test_tae_strategy_generates_buy_signal_on_pullback():
    prices = [100.0 + index * 0.4 for index in range(30)]
    prices.extend([112.0, 111.2, 111.8, 112.4, 112.0, 112.8])

    strategy = TAEStrategy(
        TAEStrategyConfig(
            short_window=3,
            long_window=8,
            value_window=12,
            value_zone_pct=0.03,
            reversal_lookback=2,
            min_trend_distance_pct=0.001,
        )
    )

    signal = strategy.generate_signal(prices, "BTC/USDT")

    assert signal is not None
    assert signal.side == "buy"


def test_tae_strategy_allows_pullback_entry_in_trending_market():
    prices = [100.0, 100.8, 101.5, 101.0, 101.7, 102.2, 102.8, 102.3, 103.0, 103.5, 103.2, 103.8]

    strategy = TAEStrategy(
        TAEStrategyConfig(
            short_window=3,
            long_window=6,
            value_window=8,
            value_zone_pct=0.03,
            reversal_lookback=2,
            min_trend_distance_pct=0.001,
        )
    )

    signal = strategy.generate_signal(prices, "BTC/USDT")

    assert signal is not None
    assert signal.side == "buy"
