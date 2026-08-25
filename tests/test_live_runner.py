import pytest

from app.binance_client import SymbolFilters
from app.live_runner import LiveRunner
from app.order_executor import LiveOrderExecutor, SimulatedOrderExecutor, TestnetOrderExecutor
from app.risk_manager import RiskConfig, RiskManager
from app.state_store import ClosedTradeRecord, OpenTradeState, StateStore
from app.telemetry import Telemetry


class FakeBinanceClient:
    """Stands in for BinanceTestnetClient/BinanceLiveClient in tests -- no real network calls."""

    def place_market_order(self, symbol, side, quantity):
        return {"orderId": "FAKE", "fills": [{"qty": str(quantity), "price": "100.0", "commission": "0.0"}]}


class FakeFuturesClient:
    """Stands in for BinanceFuturesTestnetClient/BinanceFuturesLiveClient in tests -- no real
    network calls. Configurable so individual tests can force failures or specific
    mark-price/position-risk/funding scenarios."""

    def __init__(
        self,
        mark_price=100.0,
        position_risk=None,
        funding_payments=None,
        fail_position_mode=False,
        fail_margin_type=False,
        fail_leverage=False,
    ):
        self.mark_price = mark_price
        self.position_risk = position_risk
        self.funding_payments = funding_payments or []
        self.fail_position_mode = fail_position_mode
        self.fail_margin_type = fail_margin_type
        self.fail_leverage = fail_leverage
        self.cancel_all_calls = 0
        self.set_leverage_calls = []
        self.set_margin_type_calls = []

    def set_position_mode(self, dual_side=False):
        if self.fail_position_mode:
            raise RuntimeError("simulated position mode failure")
        return {"dualSidePosition": dual_side}

    def set_margin_type(self, symbol, margin_type):
        if self.fail_margin_type:
            raise RuntimeError("simulated margin type failure")
        self.set_margin_type_calls.append((symbol, margin_type))
        return {}

    def set_leverage(self, symbol, leverage):
        if self.fail_leverage:
            raise RuntimeError("simulated leverage failure")
        self.set_leverage_calls.append((symbol, leverage))
        return {}

    def get_position_mode(self):
        return {"dualSidePosition": False}

    def get_mark_price(self, symbol):
        return self.mark_price

    def get_position_risk(self, symbol):
        return self.position_risk

    def cancel_all_open_orders(self, symbol):
        self.cancel_all_calls += 1
        return {}

    def get_funding_payments(self, symbol, start_time_ms):
        return self.funding_payments

    def place_market_order(self, symbol, side, quantity, reduce_only=False):
        return {
            "orderId": "FAKE-FUT",
            "fills": [{"qty": str(quantity), "price": str(self.mark_price), "commission": "0.0"}],
        }


class RecordingOrderExecutor:
    """Wraps a SimulatedOrderExecutor and records every place_order call's reduce_only flag."""

    def __init__(self):
        self._inner = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.0)
        self.calls = []

    def place_order(self, symbol, side, quantity, reference_price, reduce_only=False):
        self.calls.append({"side": side, "quantity": quantity, "reduce_only": reduce_only})
        return self._inner.place_order(symbol, side, quantity, reference_price)


class FailingOrderExecutor:
    """Every order placement raises, simulating a rejected order / network failure."""

    def place_order(self, symbol, side, quantity, reference_price, reduce_only=False):
        raise RuntimeError("simulated exchange rejection")


class PartialFillOrderExecutor:
    """Fills only a fraction of every requested quantity, simulating thin liquidity."""

    def __init__(self, fill_fraction=0.5, trade_fee_pct=0.0, slippage_pct=0.0):
        self.fill_fraction = fill_fraction
        self._inner = SimulatedOrderExecutor(trade_fee_pct=trade_fee_pct, slippage_pct=slippage_pct)

    def place_order(self, symbol, side, quantity, reference_price, reduce_only=False):
        order = self._inner.place_order(symbol, side, quantity, reference_price)
        fill = order["fills"][0]
        fill["qty"] = str(float(fill["qty"]) * self.fill_fraction)
        return order


def _make_closed_trade_record(pnl):
    return ClosedTradeRecord(
        id=None,
        symbol="BTC/USDT",
        side="buy",
        entry_price=100.0,
        exit_price=101.0,
        quantity=1.0,
        pnl=pnl,
        exit_reason="take_profit",
        opened_at="2026-07-23T00:00:00",
        closed_at="2026-07-23T01:00:00",
    )


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

    def send(self, message, parse_mode=None):
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
    assert runner.risk_manager.allocated_margin > 0


def test_long_only_skips_sell_signals_and_never_opens_a_short():
    # Strong sustained downtrend so TAEStrategy fires a sell signal on the last candle --
    # spot accounts can't short, so this must never actually open a position.
    closes = [100.0 - i * 0.5 for i in range(60)]

    runner_both_directions = _make_runner(_make_candles(closes))
    runner_both_directions.run_once()
    assert runner_both_directions.risk_manager.open_positions == 1
    assert runner_both_directions.store.list_open_trades()[0].side == "sell"

    runner_long_only = _make_runner(_make_candles(closes), long_only=True)
    runner_long_only.run_once()
    assert runner_long_only.risk_manager.open_positions == 0
    assert runner_long_only.store.list_open_trades() == []


def test_failed_order_placement_releases_reserved_capital_and_daily_trade_slot():
    closes = [100.0 + i * 0.5 for i in range(60)]
    runner = _make_runner(_make_candles(closes), order_executor=FailingOrderExecutor())

    daily_trades_before = runner.risk_manager.daily_trades
    allocated_before = runner.risk_manager.allocated_margin

    runner.run_once()  # signal fires, order placement raises

    assert runner.store.list_open_trades() == []
    assert runner.risk_manager.open_positions == 0
    # The reservation create_trade_plan() made must be fully released, not left dangling.
    assert runner.risk_manager.daily_trades == daily_trades_before
    assert runner.risk_manager.allocated_margin == pytest.approx(allocated_before)


def test_failed_order_placement_does_not_permanently_block_future_trades():
    # A rejected order shouldn't eat into max_trades_per_day -- confirm a real trade can
    # still open afterward once a working executor is swapped in.
    closes = [100.0 + i * 0.5 for i in range(60)]
    failing_runner = _make_runner(
        _make_candles(closes),
        order_executor=FailingOrderExecutor(),
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_trades_per_day=1),
    )
    failing_runner.run_once()  # the only daily slot would be gone forever if not released
    assert failing_runner.risk_manager.daily_trades == 0

    failing_runner.order_executor = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.0)
    failing_runner._last_candle_open_time = None  # force the candle to be treated as "new" again
    failing_runner.run_once()

    assert failing_runner.risk_manager.open_positions == 1


def test_partial_fill_on_open_uses_actual_filled_quantity():
    closes = [100.0 + i * 0.5 for i in range(60)]
    runner = _make_runner(
        _make_candles(closes),
        order_executor=PartialFillOrderExecutor(fill_fraction=0.5),
    )
    runner.run_once()

    open_trade = runner.store.list_open_trades()[0]
    plan_quantity = open_trade.quantity / 0.5  # what would've been recorded before this fix
    assert open_trade.quantity == pytest.approx(plan_quantity * 0.5)
    assert open_trade.remaining_quantity == open_trade.quantity


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

    history = runner.store.list_recent_trade_history(limit=5)
    assert len(history) == 1
    assert history[0].side == "buy"
    assert history[0].exit_reason == "trailing_stop"
    assert history[0].pnl == runner.cumulative_pnl


def test_failed_close_order_leaves_trade_open_for_retry_next_poll():
    uptrend = [100.0 + i * 0.5 for i in range(60)]
    data_feed = FakeDataFeed(_make_candles(uptrend + [uptrend[-1]]))
    runner = _make_runner(candles=[], data_feed=data_feed)
    runner.run_once()
    assert runner.risk_manager.open_positions == 1

    runner.order_executor = FailingOrderExecutor()
    data_feed.candles = _make_candles(uptrend + [50.0, 50.0])  # would trigger stop-loss close
    runner.run_once()  # close order raises

    # Nothing was mutated -- the trade is still open, exactly as before, ready to retry.
    assert runner.risk_manager.open_positions == 1
    assert len(runner.store.list_open_trades()) == 1
    assert runner.closed_trades == 0


def test_closed_trade_message_renders_a_table_with_entry_exit_fees_pnl_capital():
    notifier = FakeNotifier()
    uptrend = [100.0 + i * 0.5 for i in range(60)]
    data_feed = FakeDataFeed(_make_candles(uptrend + [uptrend[-1]]))
    runner = _make_runner(
        candles=[], data_feed=data_feed, notifier=notifier,
        order_executor=SimulatedOrderExecutor(trade_fee_pct=0.001, slippage_pct=0.0),
        # The severe crash below is for exercising the close/table-render path, not for testing
        # drawdown behavior -- neutralized so it doesn't also trip the (unrelated) circuit breaker.
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_open_positions=3, max_drawdown_pct=0.99),
    )
    runner.run_once()
    open_trade = runner.store.list_open_trades()[0]

    data_feed.candles = _make_candles(uptrend + [50.0, 50.0])  # crash triggers stop-loss close
    notifier.sent.clear()
    runner.run_once()

    assert len(notifier.sent) == 1
    message = notifier.sent[0]
    assert "```" in message  # rendered as a Markdown table
    assert f"{open_trade.entry_execution_price:.2f}" in message  # Entry
    assert "50.00" in message  # Exit price (no slippage configured in this test)
    assert "Fees" in message and "PnL" in message
    assert "Capital:" in message

    history = runner.store.list_recent_trade_history(limit=1)
    assert history[0].fees > 0  # this segment's own exit fee was tracked, not left at the 0.0 default


def test_closed_trade_fees_total_sums_entry_partial_and_exit_fees():
    # The single "Closed trade" notification's Fees row is the WHOLE trade's total (entry +
    # partial + final exit fee combined) -- a different convention from /history, where each
    # row shows only that segment's own fee. Both need to independently be correct.
    notifier = FakeNotifier()
    uptrend = [100.0 + i * 0.5 for i in range(60)]
    data_feed = FakeDataFeed(_make_candles(uptrend + [uptrend[-1]]))
    runner = _make_runner(
        candles=[], data_feed=data_feed, notifier=notifier,
        order_executor=SimulatedOrderExecutor(trade_fee_pct=0.001, slippage_pct=0.0),
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_open_positions=1),
    )
    runner.run_once()
    entry_fee = runner.store.list_open_trades()[0].total_fees
    assert entry_fee > 0

    data_feed.candles = _make_candles(uptrend + [131.0, 131.0])  # +1%+ -> partial exit fires
    runner.run_once()
    partial_fee = runner.store.list_open_trades()[0].total_fees - entry_fee
    assert partial_fee > 0

    notifier.sent.clear()
    data_feed.candles = _make_candles(uptrend + [131.0, 128.7, 128.7])  # pullback -> trailing-stop close
    runner.run_once()

    fees_line = next(line for line in notifier.sent[0].split("\n") if line.startswith("Fees"))
    displayed_total_fees = float(fees_line.split()[1])
    final_segment_fee = runner.store.list_recent_trade_history(limit=1)[0].fees
    assert displayed_total_fees == pytest.approx(entry_fee + partial_fee + final_segment_fee, abs=1e-4)


def test_history_table_shows_fees_and_abbreviated_exit_reason():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    runner.store.add_trade_history(
        ClosedTradeRecord(
            id=None, symbol="BTC/USDT", side="buy", entry_price=100.0, exit_price=103.0,
            quantity=1.0, pnl=3.0, exit_reason="take_profit",
            opened_at="2026-07-23T00:00:00", closed_at="2026-07-23T01:00:00", fees=0.15,
        )
    )

    runner._handle_command("/history")

    message = notifier.sent[0]
    assert "0.150" in message  # fees column
    assert "+3.00" in message  # pnl column
    assert "TP" in message  # abbreviated take_profit
    assert "take_profit" not in message  # abbreviation replaced the full word


def test_entry_fee_is_deducted_from_cumulative_pnl_immediately():
    closes = [100.0 + i * 0.5 for i in range(60)]
    runner = _make_runner(
        _make_candles(closes),
        order_executor=SimulatedOrderExecutor(trade_fee_pct=0.001, slippage_pct=0.0),
    )
    runner.run_once()

    assert runner.risk_manager.open_positions == 1
    open_trade = runner.store.list_open_trades()[0]
    entry_notional = open_trade.quantity * open_trade.entry_execution_price
    expected_fee = entry_notional * 0.001
    assert open_trade.realized_pnl_so_far == pytest.approx(-expected_fee)
    assert runner.cumulative_pnl == pytest.approx(-expected_fee)


def test_win_loss_classification_uses_whole_trade_not_just_final_segment():
    # A partial exit banks a solid gain; the final segment alone is a small loss,
    # but the trade overall (partial + final) nets positive. It must count as a win.
    uptrend = [100.0 + i * 0.5 for i in range(60)]
    entry_price = uptrend[-1]  # 129.5

    data_feed = FakeDataFeed(_make_candles(uptrend + [uptrend[-1]]))
    # max_open_positions=1 so a partial exit (which doesn't free the position slot)
    # can't let the strategy open a second position on the same continuing uptrend.
    runner = _make_runner(
        candles=[], data_feed=data_feed,
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_open_positions=1),
    )
    runner.run_once()
    assert runner.risk_manager.open_positions == 1

    # Price rises 1%+ -> triggers the 50% partial exit at a solid profit.
    data_feed.candles = _make_candles(uptrend + [131.0, 131.0])
    runner.run_once()
    open_trade = runner.store.list_open_trades()[0]
    assert open_trade.partial_exit_done is True
    assert open_trade.realized_pnl_so_far > 0  # partial exit was profitable

    # Price then pulls back below entry -> trailing-stop closes the rest at a loss,
    # but not enough to erase the earlier partial gain.
    data_feed.candles = _make_candles(uptrend + [131.0, 128.7, 128.7])
    runner.run_once()

    assert runner.risk_manager.open_positions == 0
    history = runner.store.list_recent_trade_history(limit=5)
    assert len(history) == 2  # one partial_profit record, one final trailing_stop record
    final_record = history[0]  # most recent first
    assert final_record.exit_reason == "trailing_stop"
    assert final_record.pnl < 0  # final segment alone was a loss

    total_trade_pnl = sum(r.pnl for r in history)
    assert total_trade_pnl > 0  # but the whole trade was net positive

    assert runner.winning_trades == 1  # correctly classified as a win, not a loss
    assert runner.losing_trades == 0
    assert runner.cumulative_pnl == pytest.approx(total_trade_pnl)


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
    assert "Symbol:" in notifier.sent[0]  # /status includes symbol/open-position context


def test_pnl_command_is_distinct_from_status():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    runner.closed_trades = 4
    runner.winning_trades = 3
    runner.losing_trades = 1
    runner.cumulative_pnl = 40.0

    runner._handle_command("/pnl")

    assert len(notifier.sent) == 1
    assert "PnL" in notifier.sent[0]
    assert "Avg PnL per closed trade" in notifier.sent[0]
    assert "10.00" in notifier.sent[0]  # 40.0 / 4 trades average
    assert "Symbol:" not in notifier.sent[0]  # leaner than /status, no position/risk context
    assert "Daily trades used" not in notifier.sent[0]


def test_history_command_defaults_to_five_and_shows_most_recent_first():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    for i in range(7):
        runner.store.add_trade_history(_make_closed_trade_record(pnl=float(i)))

    runner._handle_command("/history")

    assert len(notifier.sent) == 1
    message = notifier.sent[0]
    assert "Last 5 closed trade" in message
    assert "```" in message  # rendered as a Markdown code-block table
    # most recently added trade (pnl=6.0) must appear before the older ones
    assert message.index("+6.00") < message.index("+5.00") < message.index("+4.00")


def test_history_command_accepts_custom_limit():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)
    for i in range(7):
        runner.store.add_trade_history(_make_closed_trade_record(pnl=float(i)))

    runner._handle_command("/history 2")

    assert "Last 2 closed trade" in notifier.sent[0]


def test_history_command_with_no_trades_yet():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)

    runner._handle_command("/history")

    assert notifier.sent == ["No closed trades yet."]


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


def test_started_message_lists_recovered_open_positions():
    notifier = FakeNotifier()
    store = StateStore(":memory:")
    store.add_open_trade(
        OpenTradeState(
            id=None,
            symbol="SOL/USDT",
            side="buy",
            entry_price=74.67,
            entry_execution_price=74.67,
            stop_loss=73.92,
            take_profit=76.12,
            quantity=0.301488,
            remaining_quantity=0.301488,
            trailing_stop=75.01,
            partial_exit_done=True,
            entry_order_id="1",
            opened_at="2026-07-26T00:00:00",
            realized_pnl_so_far=1.5,
        )
    )
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier, store=store)

    runner._send_started_message()

    assert "resumed state" in notifier.sent[0]
    assert "Open positions recovered: 1" in notifier.sent[0]
    assert "BUY SOL/USDT" in notifier.sent[0]
    assert "entry=74.67" in notifier.sent[0]
    assert "trailing_stop=75.01" in notifier.sent[0]
    assert "target=76.12" in notifier.sent[0]


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
    persisted_manager.allocated_margin = 15000.0
    persisted_manager.open_positions = 3
    store.save_risk_state(persisted_manager, current_day="2026-07-23")

    runner = _make_runner(
        _make_candles([100.0] * 5),
        store=store,
        risk_config=RiskConfig(capital=300, risk_per_trade_pct=0.005, max_position_pct=0.1),
    )

    assert runner.risk_manager.allocated_margin == 300.0
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


def test_mode_label_is_simulated_by_default():
    runner = _make_runner(_make_candles([100.0] * 5))
    assert runner._mode_label() == "simulated (no real orders)"


def test_mode_label_is_testnet_when_testnet_executor_used():
    runner = _make_runner(
        _make_candles([100.0] * 5),
        order_executor=TestnetOrderExecutor(client=FakeBinanceClient()),
    )
    assert runner._mode_label() == "testnet"


def test_mode_label_is_live_real_money_when_live_executor_used():
    runner = _make_runner(
        _make_candles([100.0] * 5),
        order_executor=LiveOrderExecutor(client=FakeBinanceClient()),
    )
    label = runner._mode_label()
    assert "LIVE" in label
    assert "REAL MONEY" in label


def test_status_and_started_messages_show_mode():
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier)

    assert "Mode: simulated" in runner._status_message()

    runner._send_started_message()
    assert "Mode: simulated" in notifier.sent[-1]


def test_daily_trades_not_consumed_when_order_below_exchange_minimum():
    # A min_notional far above any position this strategy would ever size forces the
    # below-exchange-minimum skip path in _open_position.
    closes = [100.0 + i * 0.5 for i in range(60)]
    runner = _make_runner(
        _make_candles(closes),
        symbol_filters=SymbolFilters(step_size=0.000001, tick_size=0.01, min_notional=1_000_000.0),
    )

    runner.run_once()

    assert runner.store.list_open_trades() == []  # order was skipped, never actually opened
    assert runner.risk_manager.daily_trades == 0  # and didn't silently eat a daily-trade slot


def _seed_open_trade(runner, side="buy", entry_price=190.0, stop_loss=100.0, take_profit=300.0, trailing_stop=100.0):
    """Directly seeds an open position, as if opened on a prior poll -- bounds are set far
    outside any test price series by default so _check_open_trades won't close it on its own."""
    trade = OpenTradeState(
        id=None, symbol="BTC/USDT", side=side, entry_price=entry_price, entry_execution_price=entry_price,
        stop_loss=stop_loss, take_profit=take_profit, quantity=1.0, remaining_quantity=1.0,
        trailing_stop=trailing_stop, partial_exit_done=False, entry_order_id="SEED",
        opened_at="2026-01-01T00:00:00", notional=entry_price, margin_required=entry_price,
    )
    runner.store.add_open_trade(trade)
    runner.risk_manager.open_positions = 1
    runner.risk_manager.current_side = side


def test_futures_startup_verification_success_configures_leverage_and_margin_type():
    futures_client = FakeFuturesClient()
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        risk_config=RiskConfig(capital=100, leverage=2.0, margin_type="ISOLATED"),
    )

    assert runner._ensure_futures_account_configured() is True
    assert futures_client.set_leverage_calls == [("BTC/USDT", 2.0)]
    assert futures_client.set_margin_type_calls == [("BTC/USDT", "ISOLATED")]


def test_futures_startup_verification_failure_refuses_to_start():
    notifier = FakeNotifier()
    futures_client = FakeFuturesClient(fail_leverage=True)
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        notifier=notifier,
        risk_config=RiskConfig(capital=100, leverage=2.0),
    )

    assert runner._ensure_futures_account_configured() is False
    assert any("refusing to start" in message for message in notifier.sent)

    # run_forever() must return immediately without ever sending a "bot started" message.
    runner.run_forever()
    assert not any("Bot started" in message for message in notifier.sent)


def test_opposite_signal_closes_position_before_reopening():
    prices = [200.0 - i * 0.5 for i in range(50)]
    prices += [176.2, 177.0, 177.6]  # bounce toward resistance
    prices += [177.4, 176.9, 176.2]  # reversal back down -- a "sell" signal on the last candle
    runner = _make_runner(_make_candles(prices))
    _seed_open_trade(runner, side="buy")  # bounds far outside this series, won't self-trigger a close

    runner.run_once()

    assert runner.store.list_open_trades() == []
    records = runner.store.list_recent_trade_history(limit=1)
    assert records[0].exit_reason == "signal_reversal"
    # The reversal close returns immediately -- no new (opposite-side) position opened same poll.
    assert runner.risk_manager.open_positions == 0


def test_close_orders_are_placed_reduce_only():
    executor = RecordingOrderExecutor()
    runner = _make_runner(_make_candles([100.0] * 5), order_executor=executor)
    _seed_open_trade(runner, side="buy", take_profit=100.0)  # current price already at/above target

    runner.run_once()

    assert executor.calls, "expected a close order to have been placed"
    assert executor.calls[-1]["reduce_only"] is True


def test_margin_ratio_force_close_triggers_emergency_close_and_pauses():
    # entry=100, liquidation=90 (distance 10), mark=93.5 (distance-to-liq 3.5) -> margin_ratio
    # = 1 - 3.5/10 = 0.65, exactly at the default force-close threshold.
    position_risk = {"entryPrice": "100.0", "markPrice": "93.5", "liquidationPrice": "90.0"}
    notifier = FakeNotifier()
    futures_client = FakeFuturesClient(mark_price=93.5, position_risk=position_risk)
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        notifier=notifier,
        risk_config=RiskConfig(capital=100, leverage=5.0, margin_ratio_force_close_pct=0.65),
    )
    _seed_open_trade(runner, side="buy", entry_price=100.0)

    runner._check_futures_safety()

    assert runner.store.list_open_trades() == []
    assert runner._trading_paused is True
    assert futures_client.cancel_all_calls == 1
    assert any("EMERGENCY CLOSE" in message for message in notifier.sent)


def test_margin_ratio_warning_does_not_close_position():
    # margin_ratio = 1 - 6/10 = 0.40, exactly at the default warn threshold, below force-close.
    position_risk = {"entryPrice": "100.0", "markPrice": "84.0", "liquidationPrice": "90.0"}
    notifier = FakeNotifier()
    futures_client = FakeFuturesClient(mark_price=84.0, position_risk=position_risk)
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        notifier=notifier,
        risk_config=RiskConfig(capital=100, leverage=5.0, margin_ratio_warn_pct=0.40, margin_ratio_force_close_pct=0.65),
    )
    _seed_open_trade(runner, side="buy", entry_price=100.0)

    runner._check_futures_safety()

    assert len(runner.store.list_open_trades()) == 1  # still open
    assert runner._trading_paused is False
    assert any("Margin ratio warning" in message for message in notifier.sent)


def test_mark_price_divergence_kill_requires_sustained_ticks():
    futures_client = FakeFuturesClient(mark_price=110.0)  # 10% above last price -> over 1% default
    notifier = FakeNotifier()
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        data_feed=FakeDataFeed(_make_candles([100.0] * 5), latest_price=100.0),
        notifier=notifier,
        risk_config=RiskConfig(capital=100, leverage=2.0),
    )
    _seed_open_trade(runner, side="buy", entry_price=100.0)

    runner._check_futures_safety()  # tick 1
    assert runner.store.list_open_trades()  # not yet -- default max_ticks is 3
    runner._check_futures_safety()  # tick 2
    assert runner.store.list_open_trades()
    runner._check_futures_safety()  # tick 3 -- sustained divergence, now triggers
    assert runner.store.list_open_trades() == []
    assert any("mark_price_divergence" in message for message in notifier.sent)


def test_mark_price_divergence_resets_on_a_normal_tick():
    futures_client = FakeFuturesClient(mark_price=110.0)
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        data_feed=FakeDataFeed(_make_candles([100.0] * 5), latest_price=100.0),
        risk_config=RiskConfig(capital=100, leverage=2.0),
    )
    _seed_open_trade(runner, side="buy", entry_price=100.0)

    runner._check_futures_safety()
    assert runner._mark_price_divergence_ticks == 1

    futures_client.mark_price = 100.05  # back within tolerance
    runner._check_futures_safety()
    assert runner._mark_price_divergence_ticks == 0
    assert runner.store.list_open_trades()  # never closed


def test_funding_payments_applied_to_equity_and_cumulative_total():
    futures_client = FakeFuturesClient(funding_payments=[{"income": "-1.5"}, {"income": "2.0"}])
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        risk_config=RiskConfig(capital=100, leverage=2.0),
    )
    equity_before = runner.risk_manager.equity

    runner._check_funding_payments()

    assert runner.cumulative_funding_paid == pytest.approx(0.5)
    assert runner.risk_manager.equity == pytest.approx(equity_before + 0.5)


def test_restart_revalidation_closes_already_breached_position():
    # Persist an open trade and risk state as if from a prior process, then construct a new
    # runner where the futures client reports the position already past the liquidation buffer.
    store = StateStore(":memory:")
    persisted_manager = RiskManager(RiskConfig(capital=100, leverage=5.0))
    store.save_risk_state(persisted_manager, current_day="2026-07-23")
    store.add_open_trade(
        OpenTradeState(
            id=None, symbol="BTC/USDT", side="buy", entry_price=100.0, entry_execution_price=100.0,
            stop_loss=90.0, take_profit=300.0, quantity=1.0, remaining_quantity=1.0, trailing_stop=90.0,
            partial_exit_done=False, entry_order_id="SEED", opened_at="2026-07-23T00:00:00",
        )
    )
    position_risk = {"entryPrice": "100.0", "markPrice": "93.5", "liquidationPrice": "90.0"}
    futures_client = FakeFuturesClient(mark_price=93.5, position_risk=position_risk)
    notifier = FakeNotifier()

    runner = _make_runner(
        _make_candles([100.0] * 5),
        store=store,
        futures_client=futures_client,
        market_type="futures",
        notifier=notifier,
        risk_config=RiskConfig(capital=100, leverage=5.0, margin_ratio_force_close_pct=0.65),
    )

    # The breach must be handled during construction, before run_forever ever starts polling.
    assert runner.store.list_open_trades() == []
    assert any("restart_liquidation_check" in message for message in notifier.sent)


def test_file_kill_switch_stops_the_loop(tmp_path):
    kill_file = tmp_path / "KILL_SWITCH"
    runner = _make_runner(_make_candles([100.0] * 5), kill_switch_file_path=str(kill_file))
    assert runner._stop_requested is False

    kill_file.write_text("stop")
    runner._check_kill_switch_file()


class _FakeFilterClient:
    """Stands in for the `.client` attribute on a real order executor -- exposes
    get_symbol_filters() so LiveRunner's self-healing refresh path has something to call."""

    def __init__(self, filters=None, should_fail=False):
        self.filters = filters
        self.should_fail = should_fail
        self.calls = 0

    def get_symbol_filters(self, symbol):
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("exchangeInfo unreachable")
        return self.filters


class FlakyOrderExecutor:
    """Rejects the first `fail_times` orders (simulating a stale-filter exchange rejection),
    then delegates to a real SimulatedOrderExecutor. Exposes `.client` so a filter refresh
    can succeed."""

    def __init__(self, fail_times=1, filters_after_refresh=None):
        self._inner = SimulatedOrderExecutor(trade_fee_pct=0.0, slippage_pct=0.0)
        self.fail_times = fail_times
        self.calls = 0
        self.client = _FakeFilterClient(filters_after_refresh)

    def place_order(self, symbol, side, quantity, reference_price, reduce_only=False):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("400 Client Error: Binance error -1013 — Filter failure: LOT_SIZE")
        return self._inner.place_order(symbol, side, quantity, reference_price)


class AlwaysFailingOrderExecutorWithClient:
    """Every order placement raises, but (unlike FailingOrderExecutor) exposes `.client` so
    a filter refresh is attempted -- used to confirm the retry is capped at exactly one."""

    def __init__(self, filters):
        self.client = _FakeFilterClient(filters)
        self.calls = 0

    def place_order(self, symbol, side, quantity, reference_price, reduce_only=False):
        self.calls += 1
        raise RuntimeError(f"still rejected (attempt {self.calls})")


def test_order_failure_self_heals_via_symbol_filter_refresh_and_retry():
    closes = [100.0 + i * 0.5 for i in range(60)]
    new_filters = SymbolFilters(step_size=0.01, tick_size=0.01, min_notional=0.0)
    executor = FlakyOrderExecutor(fail_times=1, filters_after_refresh=new_filters)
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles(closes), order_executor=executor, notifier=notifier)

    runner.run_once()

    assert executor.calls == 2  # failed once, then succeeded on the refresh-and-retry
    assert executor.client.calls == 1  # filters refreshed exactly once
    assert runner._symbol_filters is new_filters
    assert len(runner.store.list_open_trades()) == 1
    assert not any("Order failed" in m for m in notifier.sent)  # self-healed, no failure alert


def test_order_failure_with_no_refresh_path_notifies_and_releases_capital():
    closes = [100.0 + i * 0.5 for i in range(60)]
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles(closes), order_executor=FailingOrderExecutor(), notifier=notifier)

    daily_trades_before = runner.risk_manager.daily_trades
    runner.run_once()

    assert runner.store.list_open_trades() == []
    assert runner.risk_manager.daily_trades == daily_trades_before
    assert any("Order failed" in m and "BTC/USDT" in m for m in notifier.sent)


def test_order_failure_persisting_after_refresh_retries_exactly_once_and_notifies():
    closes = [100.0 + i * 0.5 for i in range(60)]
    filters = SymbolFilters(step_size=0.001, tick_size=0.01, min_notional=0.0)
    executor = AlwaysFailingOrderExecutorWithClient(filters)
    notifier = FakeNotifier()
    runner = _make_runner(_make_candles(closes), order_executor=executor, notifier=notifier)

    runner.run_once()

    assert executor.calls == 2  # original attempt + exactly one retry, never more
    assert executor.client.calls == 1
    assert runner.store.list_open_trades() == []
    assert any("attempt 2" in m for m in notifier.sent)


def test_failed_close_order_notifies_after_exhausting_retry():
    uptrend = [100.0 + i * 0.5 for i in range(60)]
    data_feed = FakeDataFeed(_make_candles(uptrend + [uptrend[-1]]))
    notifier = FakeNotifier()
    runner = _make_runner(candles=[], data_feed=data_feed, notifier=notifier)
    runner.run_once()
    assert runner.risk_manager.open_positions == 1

    notifier.sent.clear()
    runner.order_executor = FailingOrderExecutor()
    data_feed.candles = _make_candles(uptrend + [50.0, 50.0])  # would trigger stop-loss close
    runner.run_once()

    assert runner.risk_manager.open_positions == 1  # unchanged, ready to retry next poll
    assert any("Close order failed" in m for m in notifier.sent)


class _RaisingFundingFuturesClient(FakeFuturesClient):
    def get_funding_payments(self, symbol, start_time_ms):
        raise RuntimeError("income endpoint unreachable")


def test_funding_payment_fetch_failure_notifies():
    notifier = FakeNotifier()
    futures_client = _RaisingFundingFuturesClient()
    runner = _make_runner(
        _make_candles([100.0] * 5), futures_client=futures_client, market_type="futures", notifier=notifier
    )

    runner._check_funding_payments()

    assert any("Failed to fetch funding payments" in m for m in notifier.sent)


class _CancelFailingFuturesClient(FakeFuturesClient):
    def cancel_all_open_orders(self, symbol):
        raise RuntimeError("cancel endpoint unreachable")


def test_emergency_close_notifies_when_cancel_all_orders_fails():
    notifier = FakeNotifier()
    futures_client = _CancelFailingFuturesClient()
    runner = _make_runner(
        _make_candles([100.0] * 5),
        futures_client=futures_client,
        market_type="futures",
        notifier=notifier,
        risk_config=RiskConfig(capital=100, leverage=2.0),
    )

    runner._emergency_close_all("test_reason")

    assert any("Failed to cancel open orders" in m for m in notifier.sent)
    assert any("EMERGENCY CLOSE" in m for m in notifier.sent)


def test_cooldown_decay_persists_across_restart_not_just_trade_events():
    # Regression test: cooldown_remaining decays every tick() (once per poll), but
    # save_risk_state() used to only be called on trade events (open/close/funding/day-roll).
    # A restart during a quiet cooldown period (no trades, since cooldown blocks them) would
    # reload whatever was last saved -- typically close to the full cooldown_period, discarding
    # however many ticks had actually elapsed. run_forever() now saves once per poll specifically
    # to prevent this; this test drives that same tick+save pattern directly.
    store = StateStore(":memory:")
    runner = _make_runner(
        _make_candles([100.0] * 5),
        store=store,
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, cooldown_period=100),
    )
    runner.risk_manager.cooldown_remaining = 100
    runner._save_risk_state()  # the "trade event" save that originally set cooldown=100

    for _ in range(30):
        runner.risk_manager.tick()
        runner._save_risk_state()  # what run_forever's loop now does every poll

    assert runner.risk_manager.cooldown_remaining == 70

    restarted = _make_runner(_make_candles([100.0] * 5), store=store)
    assert restarted.risk_manager.cooldown_remaining == 70  # decayed value, not the stale 100


def test_drawdown_halt_remaining_persists_across_restart():
    store = StateStore(":memory:")
    runner = _make_runner(
        _make_candles([100.0] * 5),
        store=store,
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, drawdown_recovery_period=2016),
    )
    runner.risk_manager.peak_equity = 50000.0
    runner.risk_manager.equity = 47000.0
    runner._check_drawdown_halt_notification()  # starts the countdown at 2016
    assert runner.risk_manager.drawdown_halt_remaining == 2016
    runner._save_risk_state()

    for _ in range(500):
        runner.risk_manager.tick()
    assert runner.risk_manager.drawdown_halt_remaining == 1516
    runner._save_risk_state()

    restarted = _make_runner(_make_candles([100.0] * 5), store=store)
    assert restarted.risk_manager.drawdown_halt_remaining == 1516


def test_run_forever_persists_tick_decay_every_poll_not_just_trade_events():
    # Confirms run_forever() itself (not just the underlying save/restore mechanism) calls
    # _save_risk_state() after tick() every poll -- a StopAfterOneCandleFeed with no trading
    # signal (flat prices) means no trade event would otherwise trigger a save.
    runner_holder = {}

    class StopAfterOnePoll(FakeDataFeed):
        def get_recent_candles(self, symbol, interval="5m", limit=100):
            candles = super().get_recent_candles(symbol, interval, limit)
            runner_holder["runner"]._stop_requested = True
            return candles

    data_feed = StopAfterOnePoll(_make_candles([100.0] * 30))
    runner = _make_runner(
        [], data_feed=data_feed, command_poll_interval_seconds=0,
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, cooldown_period=100),
    )
    runner.risk_manager.cooldown_remaining = 5
    runner_holder["runner"] = runner

    runner.run_forever()

    persisted = runner.store.load_risk_state()
    assert persisted["cooldown_remaining"] == 4  # decayed by tick() and saved, despite no trade


def test_run_forever_notifies_when_poll_commands_fails():
    runner_holder = {}

    class RaisingNotifier(FakeNotifier):
        def get_updates(self, offset=None, timeout=0):
            runner_holder["runner"]._stop_requested = True
            raise RuntimeError("simulated telegram outage")

    notifier = RaisingNotifier()
    runner = _make_runner(_make_candles([100.0] * 5), notifier=notifier, command_poll_interval_seconds=0)
    runner_holder["runner"] = runner

    runner.run_forever()

    assert any("Error polling Telegram commands" in m for m in notifier.sent)


def test_drawdown_halt_sends_exactly_one_alert_per_episode():
    notifier = FakeNotifier()
    runner = _make_runner(
        _make_candles([100.0] * 5),
        notifier=notifier,
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05),
    )
    runner.risk_manager.peak_equity = 50000.0
    runner.risk_manager.equity = 47000.0  # 6% drawdown, over the 5% limit

    runner._check_drawdown_halt_notification()
    runner._check_drawdown_halt_notification()  # a second check right after, same poll cycle

    alerts = [m for m in notifier.sent if "circuit breaker tripped" in m]
    assert len(alerts) == 1
    assert "6.0%" in alerts[0]


def test_drawdown_halt_clears_and_notifies_once_recovered():
    notifier = FakeNotifier()
    runner = _make_runner(
        _make_candles([100.0] * 5),
        notifier=notifier,
        risk_config=RiskConfig(
            capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05, drawdown_recovery_period=1
        ),
    )
    runner.risk_manager.peak_equity = 50000.0
    runner.risk_manager.equity = 47000.0

    runner._check_drawdown_halt_notification()  # trips the breaker, starts the countdown
    assert any("circuit breaker tripped" in m for m in notifier.sent)

    runner.risk_manager.tick()  # countdown reaches zero -- peak resets, halt clears
    notifier.sent.clear()
    runner._check_drawdown_halt_notification()

    assert any("circuit breaker cleared" in m for m in notifier.sent)
    assert not runner.risk_manager.is_drawdown_halted()


def test_status_message_shows_drawdown_from_peak():
    runner = _make_runner(
        _make_candles([100.0] * 5),
        risk_config=RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05),
    )
    runner.risk_manager.peak_equity = 50000.0
    runner.risk_manager.equity = 47000.0

    message = runner._status_message()

    assert "Drawdown from peak: 6.0% of 5% limit" in message
    assert "HALTED" in message


def test_run_forever_notifies_when_run_once_fails():
    runner_holder = {}

    class RaisingDataFeed:
        def get_recent_candles(self, symbol, interval="5m", limit=100):
            runner_holder["runner"]._stop_requested = True
            raise RuntimeError("simulated market-data outage")

        def get_latest_price(self, symbol):
            return None

    notifier = FakeNotifier()
    runner = _make_runner(
        [], data_feed=RaisingDataFeed(), notifier=notifier, command_poll_interval_seconds=0
    )
    runner_holder["runner"] = runner

    runner.run_forever()

    assert any("Error in trading loop" in m for m in notifier.sent)

    assert runner._stop_requested is True
