from app.risk_manager import RiskConfig, RiskManager
from app.state_store import OpenTradeState, StateStore


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
