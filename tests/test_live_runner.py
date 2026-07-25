from app.live_runner import LiveRunner
from app.order_executor import SimulatedOrderExecutor
from app.risk_manager import RiskConfig, RiskManager
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
        fx_rate_provider=lambda: None,  # disable INR conversion (and real network calls) by default
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


def test_pause_blocks_new_entries_but_resume_reallows_them():
    # Strong sustained uptrend so TAEStrategy fires a buy signal on the last candle.
    closes = [100.0 + i * 0.5 for i in range(60)]
    runner = _make_runner(_make_candles(closes))

    runner._handle_command("/pause")
    assert runner._trading_paused is True
    runner.run_once()
    assert runner.risk_manager.open_positions == 0
    assert runner.store.list_open_trades() == []

    runner._handle_command("/resume")
    assert runner._trading_paused is False
    runner._last_candle_open_time = None  # force the candle to be treated as "new" again
    runner.run_once()
    assert runner.risk_manager.open_positions == 1


def test_pause_does_not_stop_managing_existing_open_trades():
    uptrend = [100.0 + i * 0.5 for i in range(60)]
    data_feed = FakeDataFeed(_make_candles(uptrend + [uptrend[-1]]))
    runner = _make_runner(candles=[], data_feed=data_feed)
    runner.run_once()
    assert runner.risk_manager.open_positions == 1

    runner._handle_command("/pause")
    data_feed.candles = _make_candles(uptrend + [50.0, 50.0])  # crash below stop-loss
    runner.run_once()

    assert runner.risk_manager.open_positions == 0  # still closed despite being paused
    assert runner.store.list_open_trades() == []


def test_kill_command_requests_stop():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    assert runner._stop_requested is False

    runner._handle_command("/kill")

    assert runner._stop_requested is True
    assert any("Kill switch" in msg for msg in notifier.sent)


def test_price_command_uses_data_feed_latest_price():
    notifier = FakeNotifier()
    data_feed = FakeDataFeed(_make_candles([100.0] * 5), latest_price=12345.67)
    runner = _make_runner(candles=[], notifier=notifier, data_feed=data_feed)

    runner._handle_command("/price")

    assert notifier.sent == ["BTC/USDT: 12345.67"]


def test_openpositions_is_an_alias_for_trades():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)

    runner._handle_command("/openpositions")

    assert notifier.sent == ["No open trades right now."]


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


def test_status_message_includes_inr_conversion_when_rate_available():
    notifier = FakeNotifier()
    runner = _make_runner(
        _make_candles([100.0] * 5), notifier=notifier, fx_rate_provider=lambda: 83.0
    )
    runner.cumulative_pnl = 10.0

    message = runner._status_message()

    assert "+10.00 USDT (≈ ₹+830.00)" in message
    assert "50,000.00 USDT (≈ ₹4,150,000.00)" in message


def test_status_message_omits_inr_when_rate_unavailable():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier, fx_rate_provider=lambda: None)

    message = runner._status_message()

    assert "≈" not in message
    assert "USDT" in message


def test_reduced_capital_with_open_positions_does_not_permanently_block_new_trades():
    # Simulate 3 positions opened under a $50k capital (10% cap = $5k notional each),
    # then the operator lowering capital to $300 in config while those stay open.
    store = StateStore(":memory:")
    old_risk_config = RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_position_pct=0.1)
    persisted_manager = RiskManager(old_risk_config)
    persisted_manager.allocated_capital = 15000.0
    persisted_manager.open_positions = 3
    store.save_risk_state(persisted_manager, current_day="2026-07-23")

    runner = _make_runner(
        _make_candles([100.0] * 5),
        store=store,
        risk_config=RiskConfig(capital=300, risk_per_trade_pct=0.005, max_position_pct=0.1),
    )

    assert runner.risk_manager.allocated_capital == 300.0
    assert runner.risk_manager._available_capital() == 0.0


def test_restore_state_reloads_equity_and_peak_equity():
    store = StateStore(":memory:")
    old_risk_config = RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05)
    persisted_manager = RiskManager(old_risk_config)
    persisted_manager.equity = 46000.0
    persisted_manager.peak_equity = 52000.0
    store.save_risk_state(persisted_manager, current_day="2026-07-23")

    runner = _make_runner(
        _make_candles([100.0] * 5),
        store=store,
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05),
    )

    assert runner.risk_manager.equity == 46000.0
    assert runner.risk_manager.peak_equity == 52000.0
    # (52000 - 46000) / 52000 ~= 11.5% drawdown, over the 5% limit -> new trades blocked
    assert not runner.risk_manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)
