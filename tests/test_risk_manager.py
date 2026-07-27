import pytest

from app.risk_manager import RiskConfig, RiskManager


def test_trade_plan_created_with_valid_risk_profile():
    manager = RiskManager(RiskConfig(capital=50000, risk_per_trade_pct=0.005))
    plan = manager.create_trade_plan(entry_price=100.0, stop_loss_price=95.0)

    assert plan.entry_price == 100.0
    assert plan.stop_loss_price == 95.0
    assert plan.take_profit_price == 110.0
    assert plan.position_size == 250.0 / 5.0


def test_trade_rejected_when_daily_loss_limit_reached():
    manager = RiskManager(RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_daily_loss_pct=0.01))
    manager.daily_loss = 500.0
    assert not manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)


def test_trade_rejected_when_max_drawdown_exceeded():
    manager = RiskManager(RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05))
    manager.peak_equity = 50000.0
    manager.equity = 47000.0  # 6% drawdown from peak, over the 5% limit
    assert not manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)


def test_trade_allowed_when_drawdown_under_limit():
    manager = RiskManager(RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05))
    manager.peak_equity = 50000.0
    manager.equity = 48000.0  # 4% drawdown, under the 5% limit
    assert manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)


def test_peak_equity_tracks_new_highs_and_drawdown_is_measured_from_peak():
    manager = RiskManager(RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_drawdown_pct=0.05))
    manager.register_trade_result(pnl=10000.0, notional=0.0)  # equity now 60000, new peak
    assert manager.peak_equity == 60000.0

    manager.register_trade_result(pnl=-3500.0, notional=0.0)  # equity 56500, ~5.83% off peak
    assert not manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)


def test_position_size_capped_by_max_position_pct():
    # risk sizing alone would want 10000/1.0 = 10000 units (notional 1,000,000 on 50k capital);
    # the notional cap should clamp this down to max_position_pct of capital instead.
    manager = RiskManager(RiskConfig(capital=50000, risk_per_trade_pct=0.2, max_position_pct=0.1))
    plan = manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0)

    assert plan.notional <= 50000 * 0.1 + 1e-6
    assert plan.position_size == (50000 * 0.1) / 100.0


def test_trade_rejected_when_capital_fully_allocated():
    # risk_per_trade_pct=0.2 makes every trade clamp to exactly max_position_pct (10%) of
    # capital, so 10 trades fully allocate the account and the 11th must be rejected.
    manager = RiskManager(
        RiskConfig(
            capital=50000,
            risk_per_trade_pct=0.2,
            max_position_pct=0.1,
            max_open_positions=20,
            max_trades_per_day=20,
        )
    )
    for _ in range(10):
        manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0)

    assert manager.allocated_capital == pytest.approx(50000.0, rel=1e-6)
    assert not manager.validate_trade(entry_price=100.0, stop_loss_price=99.0)


def test_allocated_capital_released_on_trade_close():
    manager = RiskManager(RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_position_pct=0.1))
    plan = manager.create_trade_plan(entry_price=100.0, stop_loss_price=95.0)
    assert manager.allocated_capital == pytest.approx(plan.notional)

    manager.register_trade_result(pnl=10.0, notional=plan.notional)
    assert manager.allocated_capital == pytest.approx(0.0)


def test_cooldown_triggered_after_consecutive_losses():
    manager = RiskManager(
        RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_consecutive_losses=3, cooldown_period=5)
    )
    for _ in range(3):
        manager.register_trade_result(pnl=-10.0, notional=0.0)

    assert manager.cooldown_remaining == 5
    assert not manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)

    for _ in range(4):
        manager.tick()
    assert not manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)  # 1 tick left

    manager.tick()
    assert manager.cooldown_remaining == 0
    assert manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)


def test_consecutive_loss_streak_reset_by_a_win():
    manager = RiskManager(
        RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_consecutive_losses=3, cooldown_period=5)
    )
    manager.register_trade_result(pnl=-10.0, notional=0.0)
    manager.register_trade_result(pnl=-10.0, notional=0.0)
    manager.register_trade_result(pnl=5.0, notional=0.0)
    manager.register_trade_result(pnl=-10.0, notional=0.0)

    assert manager.consecutive_losses == 1
    assert manager.cooldown_remaining == 0
    assert manager.validate_trade(entry_price=100.0, stop_loss_price=95.0)


def test_same_side_entry_blocked_within_min_spacing():
    manager = RiskManager(
        RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_open_positions=10, min_entry_spacing_ticks=3)
    )
    manager.create_trade_plan(entry_price=100.0, stop_loss_price=95.0, side="buy")

    assert not manager.validate_trade(entry_price=101.0, stop_loss_price=96.0, side="buy")

    manager.tick()
    manager.tick()
    assert not manager.validate_trade(entry_price=101.0, stop_loss_price=96.0, side="buy")  # 1 tick short

    manager.tick()
    assert manager.validate_trade(entry_price=101.0, stop_loss_price=96.0, side="buy")


def test_min_entry_spacing_only_applies_to_the_same_side():
    manager = RiskManager(
        RiskConfig(capital=50000, risk_per_trade_pct=0.005, max_open_positions=10, min_entry_spacing_ticks=3)
    )
    manager.create_trade_plan(entry_price=100.0, stop_loss_price=95.0, side="buy")

    # Opposite side is unaffected by the buy-side spacing timer.
    assert manager.validate_trade(entry_price=100.0, stop_loss_price=105.0, side="sell")


def test_compounding_disabled_by_default_position_size_stays_fixed():
    manager = RiskManager(RiskConfig(capital=10000, risk_per_trade_pct=0.01, max_position_pct=1.0))
    plan_before = manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0, side="buy")
    manager.register_trade_result(pnl=plan_before.notional, notional=plan_before.notional)  # equity doubles

    # Without compounding, position size should be identical regardless of the equity gain.
    plan_after = manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0, side="buy")
    assert plan_after.position_size == pytest.approx(plan_before.position_size)


def test_compounding_enabled_grows_position_size_after_a_gain():
    manager = RiskManager(
        RiskConfig(capital=10000, risk_per_trade_pct=0.01, max_position_pct=1.0, compounding_enabled=True)
    )
    plan_before = manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0, side="buy")
    manager.register_trade_result(pnl=10000.0, notional=plan_before.notional)  # equity: 10000 -> 20000

    plan_after = manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0, side="buy")
    # Equity doubled, so risk_amount (and therefore position size) should double too.
    assert plan_after.position_size == pytest.approx(plan_before.position_size * 2, rel=1e-6)


def test_compounding_enabled_shrinks_position_size_after_a_loss():
    manager = RiskManager(
        RiskConfig(
            capital=10000, risk_per_trade_pct=0.01, max_position_pct=1.0,
            max_drawdown_pct=1.0, max_daily_loss_pct=1.0,  # neutralized to isolate sizing behavior only
            compounding_enabled=True,
        )
    )
    plan_before = manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0, side="buy")
    manager.register_trade_result(pnl=-5000.0, notional=plan_before.notional)  # equity: 10000 -> 5000
    manager.daily_loss = 0.0  # isolate sizing behavior; daily-loss-limit behavior has its own test

    plan_after = manager.create_trade_plan(entry_price=100.0, stop_loss_price=99.0, side="buy")
    assert plan_after.position_size == pytest.approx(plan_before.position_size * 0.5, rel=1e-6)


def test_compounding_uses_equity_for_daily_loss_and_available_capital_limits():
    manager = RiskManager(
        RiskConfig(
            capital=10000, risk_per_trade_pct=0.01, max_position_pct=1.0,
            max_daily_loss_pct=0.5, max_drawdown_pct=1.0,  # drawdown check neutralized to isolate daily-loss behavior
            compounding_enabled=True,
        )
    )
    manager.register_trade_result(pnl=-9000.0, notional=0.0)  # equity now 1000
    manager.daily_loss = 400.0  # 40% of the ORIGINAL 10000 capital, but 40% of current equity (1000) is only 400 too
    # max_daily_loss_pct=0.5 of current equity (1000) = 500; daily_loss (400) is under that -> still allowed
    assert manager.validate_trade(entry_price=100.0, stop_loss_price=99.0)
    manager.daily_loss = 600.0  # now over 50% of current equity (1000) -> blocked
    assert not manager.validate_trade(entry_price=100.0, stop_loss_price=99.0)
