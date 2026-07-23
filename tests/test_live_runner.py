from app.live_runner import LiveRunner
from app.order_executor import SimulatedOrderExecutor
from app.risk_manager import RiskConfig
from app.state_store import StateStore
from app.telemetry import Telemetry


class FakeDataFeed:
    def __init__(self, candles, latest_price=None):
        self.candles = candles
        self.latest_price = latest_price

    def get_recent_candles(self, symbol, interval="5m", limit=100):
        return self.candles[-limit:]

    def get_latest_price(self, symbol):
        return self.latest_price


class FakeNotifier:
    def __init__(self, chat_id="12345"):
        self.bot_token = "fake-token"
        self.chat_id = chat_id
        self.sent = []
        self._pending_updates = []

    def send(self, message):
        self.sent.append(message)
        return True

    def queue_update(self, text, chat_id=None):
        self._pending_updates.append(
            {"update_id": len(self._pending_updates) + 1, "message": {"chat": {"id": chat_id or self.chat_id}, "text": text}}
        )

    def get_updates(self, offset=None, timeout=0):
        if offset is None:
            updates = self._pending_updates
        else:
            updates = [u for u in self._pending_updates if u["update_id"] >= offset]
        return updates


def _make_candles(closes, start_time_ms=1_700_000_000_000, step_ms=300_000):
    return [
        [start_time_ms + i * step_ms, c, c, c, c, 1.0]
        for i, c in enumerate(closes)
    ]


def _make_runner(candles, **overrides):
    kwargs = dict(
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_open_positions=3),
        symbol="BTC/USDT",
        poll_interval_seconds=1,
        db_path=":memory:",
        order_executor=SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.0),
        data_feed=FakeDataFeed(candles),
        store=StateStore(":memory:"),
        telemetry=Telemetry(enabled=False, logger=None),
    )
    kwargs.update(overrides)
    return LiveRunner(**kwargs)


def test_live_runner_initializes_with_defaults():
    runner = _make_runner(_make_candles([100.0, 101.0]))
    assert runner.symbol == "BTC/USDT"
    assert runner.poll_interval_seconds == 1
    assert runner.cumulative_pnl == 0.0


def test_run_once_skips_when_no_new_closed_candle():
    candles = _make_candles([100.0] * 30)
    runner = _make_runner(candles)
    runner.run_once()
    trades_after_first = runner.store.list_open_trades()

    runner.run_once()  # same candle set again, no new closed candle
    assert runner.store.list_open_trades() == trades_after_first


def test_open_position_persists_and_updates_risk_manager():
    # Strong sustained uptrend so TAEStrategy fires a buy signal on the last candle.
    closes = [100.0 + i * 0.5 for i in range(60)]
    runner = _make_runner(_make_candles(closes))
    runner.run_once()

    assert runner.risk_manager.open_positions == 1
    open_trades = runner.store.list_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0].side == "buy"
    assert runner.risk_manager.allocated_capital > 0


def test_closing_a_trade_updates_cumulative_pnl_and_persists():
    # Uptrend to open a buy; the data feed then "advances" to a crashed price to force a close.
    uptrend = [100.0 + i * 0.5 for i in range(60)]
    data_feed = FakeDataFeed(_make_candles(uptrend + [uptrend[-1]]))
    runner = _make_runner(candles=[], data_feed=data_feed)
    runner.run_once()
    assert runner.risk_manager.open_positions == 1

    data_feed.candles = _make_candles(uptrend + [50.0, 50.0])
    runner.run_once()  # crashed price triggers the stop-loss check
    assert runner.risk_manager.open_positions == 0
    assert runner.store.list_open_trades() == []
    assert runner.cumulative_pnl < 0
    assert runner.closed_trades == 1
    assert runner.losing_trades == 1

    persisted = runner.store.load_risk_state()
    assert persisted["cumulative_pnl"] == runner.cumulative_pnl
    assert persisted["closed_trades"] == 1


def test_status_command_replies_with_summary():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    runner.closed_trades = 3
    runner.winning_trades = 2
    runner.losing_trades = 1
    runner.cumulative_pnl = 42.5

    runner._handle_command("/status")

    assert len(notifier.sent) == 1
    assert "Closed trades: 3" in notifier.sent[0]
    assert "42.5" in notifier.sent[0]


def test_unknown_command_gets_a_helpful_reply():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)

    runner._handle_command("/nonsense")

    assert len(notifier.sent) == 1
    assert "Unknown command" in notifier.sent[0]


def test_price_command_uses_data_feed_latest_price():
    notifier = FakeNotifier()
    data_feed = FakeDataFeed(_make_candles([100.0] * 5), latest_price=12345.67)
    runner = _make_runner(candles=[], notifier=notifier, data_feed=data_feed)

    runner._handle_command("/price")

    assert notifier.sent == ["BTC/USDT: 12345.67"]


def test_poll_commands_ignores_messages_from_other_chats():
    notifier = FakeNotifier(chat_id="12345")
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    notifier.queue_update("/status", chat_id="99999")  # different chat, should be ignored

    runner._poll_commands()

    assert notifier.sent == []


def test_poll_commands_advances_offset_so_updates_are_not_reprocessed():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    notifier.queue_update("/help")

    runner._poll_commands()
    assert len(notifier.sent) == 1

    runner._poll_commands()  # no new updates, offset already advanced
    assert len(notifier.sent) == 1


def test_started_message_reports_fresh_vs_resumed_state():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    runner._send_started_message()
    assert "fresh state" in notifier.sent[0]

    notifier.sent.clear()
    runner.closed_trades = 5
    runner._send_started_message()
    assert "resumed state" in notifier.sent[0]
