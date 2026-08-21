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
    compounding_enabled: bool = False
    # Futures-only fields. leverage=1.0 makes margin_required == notional, identical to today's
    # spot math -- these are inert for spot use and only take effect when leverage > 1.
    leverage: float = 1.0
    max_leverage: float = 5.0
    margin_type: str = "ISOLATED"
    liquidation_buffer_pct: float = 0.08
    max_position_notional: float = float("inf")  # inert unless explicitly set (e.g. for futures)
    maintenance_margin_rate: float = 0.004
    margin_ratio_warn_pct: float = 0.40
    margin_ratio_force_close_pct: float = 0.65


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
    margin_required: float
    liquidation_price: float


class RiskManager:
    def __init__(self, config: RiskConfig):
        if config.leverage > config.max_leverage:
            raise ValueError(
                f"leverage ({config.leverage}) exceeds max_leverage ({config.max_leverage}) -- refusing to start"
            )
        self.config = config
        self.daily_loss = 0.0
        self.daily_trades = 0
        self.open_positions = 0
        self.allocated_margin = 0.0
        self.notional_exposure = 0.0
        self.current_side: Optional[str] = None
        self.consecutive_losses = 0
        self.cooldown_remaining = 0
        self.ticks_since_buy_entry = 10**9
        self.ticks_since_sell_entry = 10**9
        self.equity = config.capital
        self.peak_equity = config.capital

    def validate_trade(
        self,
        entry_price: float,
        stop_loss_price: float,
        side: str = "buy",
        current_price: Optional[float] = None,
    ) -> bool:
        side = side.lower()
        if self._reference_capital() <= 0:
            return False
        if self.daily_trades >= self.config.max_trades_per_day:
            return False
        if self.open_positions >= self.config.max_open_positions:
            return False
        if self._daily_loss_exceeded():
            return False
        if self._max_drawdown_exceeded():
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
        if self.notional_exposure >= self.config.max_position_notional:
            return False
        if self.cooldown_remaining > 0:
            return False
        if side == "buy" and self.ticks_since_buy_entry < self.config.min_entry_spacing_ticks:
            return False
        if side == "sell" and self.ticks_since_sell_entry < self.config.min_entry_spacing_ticks:
            return False
        if not self._liquidation_buffer_ok(entry_price, stop_loss_price, side):
            return False
        return True

    def _reference_capital(self) -> float:
        """The capital base used for position sizing and risk limits: the static
        configured capital, or -- if compounding is enabled -- the account's current
        tracked equity, so position sizes grow and shrink with real realized P&L."""
        return self.equity if self.config.compounding_enabled else self.config.capital

    def _available_capital(self) -> float:
        return self._reference_capital() - self.allocated_margin

    def _estimate_liquidation_price(self, entry_price: float, side: str) -> float:
        """Isolated-margin liquidation estimate: distance from entry is approximately
        entry * (1/leverage - maintenance_margin_rate). At leverage=1.0 this is a very
        wide (effectively irrelevant) distance, matching spot's lack of liquidation risk."""
        move = entry_price * (1 / self.config.leverage - self.config.maintenance_margin_rate)
        return entry_price - move if side == "buy" else entry_price + move

    def _liquidation_buffer_ok(self, entry_price: float, stop_loss_price: float, side: str) -> bool:
        """Reject trades whose own stop-loss sits too close to estimated liquidation --
        the stop must have meaningful room to trigger before liquidation would."""
        liq_price = self._estimate_liquidation_price(entry_price, side)
        liq_distance = abs(entry_price - liq_price)
        stop_distance = abs(entry_price - stop_loss_price)
        return stop_distance <= liq_distance * (1 - self.config.liquidation_buffer_pct)

    def check_margin_ratio(self, margin_ratio: float) -> str:
        """Returns 'ok' / 'warn' / 'force_close' against configured thresholds. Stays
        exchange-agnostic -- the caller (LiveRunner) is responsible for computing
        margin_ratio from the futures client's live position data."""
        if margin_ratio >= self.config.margin_ratio_force_close_pct:
            return "force_close"
        if margin_ratio >= self.config.margin_ratio_warn_pct:
            return "warn"
        return "ok"

    def apply_funding_payment(self, amount: float) -> None:
        """Funding accrues independent of trade close events; folds straight into equity
        (amount is negative when funding is paid out, positive when received)."""
        self.equity += amount
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

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

        reference_capital = self._reference_capital()
        risk_amount = reference_capital * self.config.risk_per_trade_pct
        position_size = risk_amount / risk_per_unit

        # Hard cap on margin used per trade, independent of stop distance: a tight stop
        # should never translate into an oversized position. margin_required = notional /
        # leverage, so at leverage=1.0 this is identical to spot's old notional-based cap.
        # max_position_notional is folded in as a second, absolute ceiling on the same
        # margin figure (via its notional-to-margin equivalent) rather than a separate
        # post-hoc check -- validate_trade() only confirms *some* capital is available, not
        # the exact size create_trade_plan() will land on, so any additional constraint here
        # must clamp the size down, never raise, or it could fail a trade validate_trade()
        # already approved.
        remaining_notional_budget = self.config.max_position_notional - self.notional_exposure
        if remaining_notional_budget <= 0:
            raise ValueError("max_position_notional already fully allocated")
        max_margin = min(
            reference_capital * self.config.max_position_pct,
            self._available_capital(),
            remaining_notional_budget / self.config.leverage,
        )
        if max_margin <= 0:
            raise ValueError("No available capital to open a new position")
        notional = position_size * entry_price
        margin_required = notional / self.config.leverage
        if margin_required > max_margin:
            margin_required = max_margin
            notional = margin_required * self.config.leverage
            position_size = notional / entry_price
            risk_amount = position_size * risk_per_unit

        if side == "buy":
            take_profit_price = entry_price + (risk_per_unit * self.config.reward_ratio)
        else:
            take_profit_price = entry_price - (risk_per_unit * self.config.reward_ratio)
        reward_amount = position_size * abs(take_profit_price - entry_price)
        liquidation_price = self._estimate_liquidation_price(entry_price, side)

        self.daily_trades += 1
        self.allocated_margin += margin_required
        self.notional_exposure += notional
        self.current_side = side
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
            margin_required=margin_required,
            liquidation_price=liquidation_price,
        )

    def register_trade_result(self, pnl: float, notional: float = 0.0, margin: float = 0.0) -> None:
        self.daily_loss += max(-pnl, 0.0)
        self.open_positions = max(0, self.open_positions - 1)
        self.allocated_margin = max(0.0, self.allocated_margin - margin)
        self.notional_exposure = max(0.0, self.notional_exposure - notional)
        if self.open_positions == 0:
            self.current_side = None

        self.equity += pnl
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

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
        max_daily_loss = self._reference_capital() * self.config.max_daily_loss_pct
        return self.daily_loss >= max_daily_loss

    def _max_drawdown_exceeded(self) -> bool:
        if self.peak_equity <= 0:
            return False
        drawdown = (self.peak_equity - self.equity) / self.peak_equity
        return drawdown >= self.config.max_drawdown_pct
