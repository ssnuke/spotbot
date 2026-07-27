from app.order_executor import LiveOrderExecutor, SimulatedOrderExecutor, TestnetOrderExecutor


class FakeBinanceClient:
    def __init__(self):
        self.calls = []

    def place_market_order(self, symbol, side, quantity):
        self.calls.append((symbol, side, quantity))
        return {"orderId": "FAKE", "fills": [{"qty": str(quantity), "price": "100.0", "commission": "0.01"}]}


def test_testnet_executor_delegates_to_client():
    client = FakeBinanceClient()
    executor = TestnetOrderExecutor(client=client)
    order = executor.place_order("SOL/USDT", "BUY", quantity=1.5, reference_price=76.0)
    assert client.calls == [("SOL/USDT", "BUY", 1.5)]
    assert order["orderId"] == "FAKE"


def test_live_executor_delegates_to_client():
    client = FakeBinanceClient()
    executor = LiveOrderExecutor(client=client)
    order = executor.place_order("SOL/USDT", "SELL", quantity=0.75, reference_price=76.0)
    assert client.calls == [("SOL/USDT", "SELL", 0.75)]
    assert order["orderId"] == "FAKE"


def test_buy_order_applies_positive_slippage():
    executor = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.001)
    order = executor.place_order("BTC/USDT", "BUY", quantity=1.0, reference_price=100.0)
    fill = order["fills"][0]
    assert float(fill["price"]) == 100.1


def test_sell_order_applies_negative_slippage():
    executor = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.001)
    order = executor.place_order("BTC/USDT", "SELL", quantity=1.0, reference_price=100.0)
    fill = order["fills"][0]
    assert float(fill["price"]) == 99.9


def test_commission_scales_with_notional_and_fee_pct():
    executor = SimulatedOrderExecutor(trade_fee_pct=0.001, slippage_pct=0.0)
    order = executor.place_order("BTC/USDT", "BUY", quantity=2.0, reference_price=100.0)
    fill = order["fills"][0]
    assert float(fill["commission"]) == 2.0 * 100.0 * 0.001


class FakeBookTickerFeed:
    def __init__(self, bid_price, ask_price):
        self.bid_price = bid_price
        self.ask_price = ask_price

    def get_book_ticker(self, symbol):
        return {"bid_price": self.bid_price, "ask_price": self.ask_price}


def test_buy_crosses_the_live_ask_not_the_reference_price():
    feed = FakeBookTickerFeed(bid_price=99.5, ask_price=100.5)
    executor = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.0, data_feed=feed)
    order = executor.place_order("BTC/USDT", "BUY", quantity=1.0, reference_price=100.0)
    assert float(order["fills"][0]["price"]) == 100.5


def test_sell_crosses_the_live_bid_not_the_reference_price():
    feed = FakeBookTickerFeed(bid_price=99.5, ask_price=100.5)
    executor = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.0, data_feed=feed)
    order = executor.place_order("BTC/USDT", "SELL", quantity=1.0, reference_price=100.0)
    assert float(order["fills"][0]["price"]) == 99.5


def test_falls_back_to_reference_price_when_book_ticker_unavailable():
    class NoDataFeed:
        def get_book_ticker(self, symbol):
            return None

    executor = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.001, data_feed=NoDataFeed())
    order = executor.place_order("BTC/USDT", "BUY", quantity=1.0, reference_price=100.0)
    assert float(order["fills"][0]["price"]) == 100.1
