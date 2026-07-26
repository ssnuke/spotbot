from app.risk_manager import RiskConfig, RiskManager
from app.state_store import ClosedTradeRecord, OpenTradeState, StateStore


def test_risk_state_round_trip():
    store = StateStore(":memory:")
    manager = RiskManager(RiskConfig(capital=50000))
    manager.daily_loss = 120.0
    manager.daily_trades = 3
    manager.open_positions = 1
    manager.allocated_capital = 4500.0
    manager.consecutive_losses = 2
    manager.cooldown_remaining = 5
    manager.equity = 46500.0
    manager.peak_equity = 51000.0

    store.save_risk_state(manager, current_day="2026-07-23")
    state = store.load_risk_state()

    assert state["daily_loss"] == 120.0
    assert state["daily_trades"] == 3
    assert state["open_positions"] == 1
    assert state["allocated_capital"] == 4500.0
    assert state["consecutive_losses"] == 2
    assert state["cooldown_remaining"] == 5
    assert state["current_day"] == "2026-07-23"
    assert state["equity"] == 46500.0
    assert state["peak_equity"] == 51000.0


def test_risk_state_upsert_overwrites_previous_row():
    store = StateStore(":memory:")
    manager = RiskManager(RiskConfig(capital=50000))
    store.save_risk_state(manager, current_day="2026-07-23")

    manager.daily_trades = 7
    store.save_risk_state(manager, current_day="2026-07-24")

    state = store.load_risk_state()
    assert state["daily_trades"] == 7
    assert state["current_day"] == "2026-07-24"


def test_open_trade_lifecycle():
    store = StateStore(":memory:")
    trade = OpenTradeState(
        id=None,
        symbol="BTC/USDT",
        side="buy",
        entry_price=100.0,
        entry_execution_price=100.0,
        stop_loss=99.0,
        take_profit=103.0,
        quantity=1.0,
        remaining_quantity=1.0,
        trailing_stop=99.0,
        partial_exit_done=False,
        entry_order_id="1",
        opened_at="2026-07-23T00:00:00",
    )
    trade_id = store.add_open_trade(trade)

    open_trades = store.list_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0].id == trade_id
    assert open_trades[0].partial_exit_done is False

    store.update_open_trade(trade_id, remaining_quantity=0.5, partial_exit_done=1, trailing_stop=99.5)
    updated = store.list_open_trades()[0]
    assert updated.remaining_quantity == 0.5
    assert updated.partial_exit_done is True
    assert updated.trailing_stop == 99.5

    store.remove_open_trade(trade_id)
    assert store.list_open_trades() == []


def _make_closed_trade(pnl, closed_at):
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
        closed_at=closed_at,
    )


def test_trade_history_round_trip():
    store = StateStore(":memory:")
    store.add_trade_history(_make_closed_trade(pnl=5.0, closed_at="2026-07-23T01:00:00"))

    records = store.list_recent_trade_history(limit=5)
    assert len(records) == 1
    assert records[0].pnl == 5.0
    assert records[0].exit_reason == "take_profit"
    assert records[0].symbol == "BTC/USDT"


def test_trade_history_returns_most_recent_first_and_respects_limit():
    store = StateStore(":memory:")
    for i in range(8):
        store.add_trade_history(_make_closed_trade(pnl=float(i), closed_at=f"2026-07-23T0{i}:00:00"))

    records = store.list_recent_trade_history(limit=5)
    assert len(records) == 5
    # most recently inserted (pnl=7) should come first
    assert [r.pnl for r in records] == [7.0, 6.0, 5.0, 4.0, 3.0]


def test_trade_history_empty_returns_empty_list():
    store = StateStore(":memory:")
    assert store.list_recent_trade_history(limit=5) == []
