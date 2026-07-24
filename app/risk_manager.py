from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskConfig:
    capital: float
    risk_per_trade_pct: float = 0.005
    reward_ratio: float = 2.0
    max_daily_loss_pct: float = 0.01
    max_drawdown_pct: float = 0.05
    max_trades_per_day: int = 3
    max_open_positions: int = 1
    max_position_pct: float = 0.1
    max_consecutive_losses: int = 0
    cooldown_period: int = 0
    min_entry_spacing_ticks: int = 0


@dataclass
class TradePlan:
    side: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    position_size: float
    risk_amount: float
    reward_amount: float
    notional: float


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.daily_loss = 0.0
        self.daily_trades = 0
        self.open_positions = 0
        self.allocated_capital = 0.0
        self.consecutive_losses = 0
        self.cooldown_remaining = 0
        self.ticks_since_buy_entry = 10**9
        self.ticks_since_sell_entry = 10**9

    def validate_trade(
        self,
        entry_price: float,
        stop_loss_price: float,
        side: str = "buy",
        current_price: Optional[float] = None,
    ) -> bool:
        side = side.lower()
        if self.config.capital <= 0:
            return False
        if self.daily_trades >= self.config.max_trades_per_day:
            return False
        if self.open_positions >= self.config.max_open_positions:
            return False
        if self._daily_loss_exceeded():
            return False
        if current_price is not None and current_price <= 0:
            return False
        if side == "buy" and stop_loss_price >= entry_price:
            return False
        if side == "sell" and stop_loss_price <= entry_price:
            return False
        if side not in {"buy", "sell"}:
            return False
        if self._available_capital() <= 0:
            return False
        if self.cooldown_remaining > 0:
            return False
        if side == "buy" and self.ticks_since_buy_entry < self.config.min_entry_spacing_ticks:
            return False
        if side == "sell" and self.ticks_since_sell_entry < self.config.min_entry_spacing_ticks:
            return False
        return True

    def _available_capital(self) -> float:
        return self.config.capital - self.allocated_capital

    def tick(self) -> None:
        """Advance time by one bar/tick, decaying any active loss-streak cooldown and
        advancing the same-side entry-spacing counters."""
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
        self.ticks_since_buy_entry = min(self.ticks_since_buy_entry + 1, 10**9)
        self.ticks_since_sell_entry = min(self.ticks_since_sell_entry + 1, 10**9)

    def create_trade_plan(self, entry_price: float, stop_loss_price: float, side: str = "buy") -> TradePlan:
        if not self.validate_trade(entry_price, stop_loss_price, side=side):
            raise ValueError("Trade does not satisfy risk constraints")

        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit <= 0:
            raise ValueError("Invalid stop-loss distance")

        risk_amount = self.config.capital * self.config.risk_per_trade_pct
        position_size = risk_amount / risk_per_unit

        # Hard cap on notional exposure per trade, independent of stop distance:
        # a tight stop should never translate into an oversized position.
        max_notional = min(self.config.capital * self.config.max_position_pct, self._available_capital())
        if max_notional <= 0:
            raise ValueError("No available capital to open a new position")
        notional = position_size * entry_price
        if notional > max_notional:
            position_size = max_notional / entry_price
            notional = position_size * entry_price
            risk_amount = position_size * risk_per_unit

        if side == "buy":
            take_profit_price = entry_price + (risk_per_unit * self.config.reward_ratio)
        else:
            take_profit_price = entry_price - (risk_per_unit * self.config.reward_ratio)
        reward_amount = position_size * abs(take_profit_price - entry_price)

        self.daily_trades += 1
        self.allocated_capital += notional
        if side == "buy":
            self.ticks_since_buy_entry = 0
        else:
            self.ticks_since_sell_entry = 0
        return TradePlan(
            side=side,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            position_size=position_size,
            risk_amount=risk_amount,
            reward_amount=reward_amount,
            notional=notional,
        )

    def register_trade_result(self, pnl: float, notional: float = 0.0) -> None:
        self.daily_loss += max(-pnl, 0.0)
        self.open_positions = max(0, self.open_positions - 1)
        self.allocated_capital = max(0.0, self.allocated_capital - notional)

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        if self.config.max_consecutive_losses > 0 and self.consecutive_losses >= self.config.max_consecutive_losses:
            self.cooldown_remaining = self.config.cooldown_period
            self.consecutive_losses = 0

    def reset_daily_limits(self) -> None:
        self.daily_trades = 0
        self.daily_loss = 0.0

    def _daily_loss_exceeded(self) -> bool:
        max_daily_loss = self.config.capital * self.config.max_daily_loss_pct
        return self.daily_loss >= max_daily_loss
