from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, date
from typing import List, Optional

from app.regime_filter import RegimeFilter, RegimeFilterConfig
from app.risk_manager import RiskConfig, RiskManager
from app.strategy import BaseStrategy, MomentumStrategy, TAEStrategy, TAEStrategyConfig
from app.telemetry import Telemetry


@dataclass
class BacktestTrade:
    timestamp: str
    symbol: str
    side: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    entry_execution_price: float = 0.0
    exit_execution_price: float = 0.0
    fees: float = 0.0
    pnl: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    trailing_stop: float = 0.0
    partial_exits: List[dict] = field(default_factory=list)
    margin_required: float = 0.0
    notional: float = 0.0
    liquidation_price: float = 0.0
    funding_cost: float = 0.0


@dataclass
class BacktestResult:
    trades: List[BacktestTrade] = field(default_factory=list)
    final_capital: float = 0.0
    equity_curve: List[float] = field(default_factory=list)
    max_drawdown_halt_date: Optional[str] = None  # set once, the first time the max-drawdown
    # circuit breaker starts permanently blocking new trades for the rest of the run


@dataclass
class BacktestReport:
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    sharpe_like_ratio: float
    max_drawdown: float

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "total_pnl": self.total_pnl,
            "sharpe_like_ratio": self.sharpe_like_ratio,
            "max_drawdown": self.max_drawdown,
        }


@dataclass
class BacktestConfig:
    start_capital: float = 50000.0
    timeframe: str = "1m"
    risk_per_trade_pct: float = 0.005
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.05
    max_trades_per_day: int = 8
    max_open_positions: int = 3
    max_position_pct: float = 0.1
    max_consecutive_losses: int = 0
    cooldown_period: int = 0
    min_entry_spacing_ticks: int = 0
    drawdown_recovery_period: int = 0
    compounding_enabled: bool = False
    reward_ratio: float = 2.0
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.02
    partial_exit_profit_pct: float = 0.01
    partial_exit_qty_pct: float = 0.5
    trailing_stop_pct: float = 0.005
    use_atr_stop: bool = False
    atr_period: int = 14
    atr_multiplier: float = 2.0
    trade_fee_pct: float = 0.0
    slippage_pct: float = 0.0
    htf_filter_enabled: bool = False
    htf_filter_mode: str = "both"
    htf_short_period: int = 9
    htf_long_period: int = 21
    long_only: bool = False  # skip sell/short signals -- required for real spot execution, which can't short
    telemetry_enabled: bool = True
    # Futures-only fields. leverage=1.0 makes this identical to spot backtesting -- inert
    # unless explicitly set higher.
    leverage: float = 1.0
    max_leverage: float = 5.0
    margin_type: str = "ISOLATED"
    liquidation_buffer_pct: float = 0.08
    max_position_notional: float = float("inf")
    maintenance_margin_rate: float = 0.004
    funding_enabled: bool = False


class Backtester:
    def __init__(
        self,
        config: BacktestConfig | None = None,
        strategy_config: TAEStrategyConfig | None = None,
        telemetry: Telemetry | None = None,
    ):
        self.config = config or BacktestConfig()
        self.telemetry = telemetry or Telemetry(enabled=self.config.telemetry_enabled)
        self.risk_manager = RiskManager(
            RiskConfig(
                capital=self.config.start_capital,
                risk_per_trade_pct=self.config.risk_per_trade_pct,
                max_daily_loss_pct=self.config.max_daily_loss_pct,
                max_drawdown_pct=self.config.max_drawdown_pct,
                reward_ratio=self.config.reward_ratio,
                max_trades_per_day=self.config.max_trades_per_day,
                max_open_positions=self.config.max_open_positions,
                max_position_pct=self.config.max_position_pct,
                max_consecutive_losses=self.config.max_consecutive_losses,
                cooldown_period=self.config.cooldown_period,
                min_entry_spacing_ticks=self.config.min_entry_spacing_ticks,
                drawdown_recovery_period=self.config.drawdown_recovery_period,
                compounding_enabled=self.config.compounding_enabled,
                leverage=self.config.leverage,
                max_leverage=self.config.max_leverage,
                margin_type=self.config.margin_type,
                liquidation_buffer_pct=self.config.liquidation_buffer_pct,
                max_position_notional=self.config.max_position_notional,
                maintenance_margin_rate=self.config.maintenance_margin_rate,
            )
        )
        self.strategy: BaseStrategy = TAEStrategy(strategy_config)
        if strategy_config is None:
            self.strategy = MomentumStrategy()

    def _log(self, message: str) -> None:
        self.telemetry.log(message)

    def run(
        self,
        candles: List[List[float]],
        symbol: str = "BTC/USDT",
        funding_rates: Optional[List[dict]] = None,
    ) -> BacktestResult:
        prices = [float(candle[4]) for candle in candles]
        highs = [float(candle[2]) for candle in candles]
        lows = [float(candle[3]) for candle in candles]
        regime_filter = RegimeFilter(
            candles,
            RegimeFilterConfig(
                enabled=self.config.htf_filter_enabled,
                mode=self.config.htf_filter_mode,
                short_period=self.config.htf_short_period,
                long_period=self.config.htf_long_period,
            ),
        )
        result = BacktestResult(final_capital=self.config.start_capital)
        equity = self.config.start_capital
        result.equity_curve.append(equity)
        entry_price = None
        stop_loss_price = None
        take_profit_price = None

        current_day: date | None = None
        warmup_period = getattr(self.strategy, "warmup_period", 0)
        for index in range(warmup_period, len(prices)):
            self.risk_manager.tick()
            candle_day = self._get_candle_date(candles[index])
            if current_day is None:
                current_day = candle_day
            elif candle_day != current_day:
                current_day = candle_day
                self.risk_manager.reset_daily_limits()
                self._log(f"New day {current_day.isoformat()}: reset daily loss and daily trade count")
            window = prices[: index + 1]
            signal = self.strategy.generate_signal(window, symbol)
            if signal is None:
                continue

            if self.config.long_only and signal.side == "sell":
                continue

            if not regime_filter.allows(index, signal.side):
                self._log(f"Signal at candle {index} rejected by higher-timeframe trend filter: side={signal.side}")
                continue

            self._log(
                f"Signal detected at candle {index}: side={signal.side}, price={prices[index]:.4f}, confidence={signal.confidence:.2f}"
            )

            entry_price = prices[index]
            if self.config.use_atr_stop:
                atr = self._calculate_atr(candles, index, self.config.atr_period)
                if atr <= 0:
                    continue
                stop_distance = atr * self.config.atr_multiplier
                if signal.side == "buy":
                    stop_loss_price = entry_price - stop_distance
                    take_profit_price = entry_price + stop_distance * self.config.reward_ratio
                else:
                    stop_loss_price = entry_price + stop_distance
                    take_profit_price = entry_price - stop_distance * self.config.reward_ratio
            else:
                if signal.side == "buy":
                    stop_loss_price = entry_price * (1 - self.config.stop_loss_pct)
                    take_profit_price = entry_price * (1 + self.config.take_profit_pct)
                else:
                    stop_loss_price = entry_price * (1 + self.config.stop_loss_pct)
                    take_profit_price = entry_price * (1 - self.config.take_profit_pct)
            if stop_loss_price <= 0:
                continue
            if result.max_drawdown_halt_date is None and self.risk_manager._max_drawdown_exceeded():
                # Records the FIRST breach only. With the default drawdown_recovery_period=0 this
                # is effectively permanent -- equity and peak_equity never move again without a
                # new trade, so nothing here can recover on its own for the rest of the run. With
                # a nonzero recovery period configured, trading can resume later (the reference
                # peak resets), but this field still marks when the breach first happened.
                # Record it once, visibly -- silently truncating the effective backtest period
                # is the failure mode this guards against.
                result.max_drawdown_halt_date = self._get_candle_date(candles[index]).isoformat()
                print(
                    f"\n*** Max drawdown ({self.risk_manager.config.max_drawdown_pct * 100:.1f}%) breached on "
                    f"{result.max_drawdown_halt_date} -- no further trades will open for the rest of this "
                    f"backtest window. ***\n"
                )
            if not self.risk_manager.validate_trade(entry_price, stop_loss_price, side=signal.side):
                self._log(
                    f"Trade rejected by risk manager at candle {index}: entry={entry_price:.4f}, stop_loss={stop_loss_price:.4f}, side={signal.side}, daily_trades={self.risk_manager.daily_trades}, daily_loss={self.risk_manager.daily_loss:.2f}"
                )
                continue

            plan = self.risk_manager.create_trade_plan(entry_price, stop_loss_price, side=signal.side)
            entry_execution_price = self._apply_slippage(entry_price, signal.side, is_entry=True)
            entry_fee = self._calculate_fee(plan.position_size, entry_execution_price)
            trade = BacktestTrade(
                timestamp=str(index),
                symbol=symbol,
                side=signal.side,
                entry_price=entry_price,
                entry_execution_price=entry_execution_price,
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                quantity=plan.position_size,
                fees=entry_fee,
                margin_required=plan.margin_required,
                notional=plan.notional,
                liquidation_price=plan.liquidation_price,
            )
            result.trades.append(trade)
            self.risk_manager.open_positions += 1
            self._log(
                f"Opened trade: entry={entry_price:.4f}, stop_loss={stop_loss_price:.4f}, take_profit={take_profit_price:.4f}, qty={plan.position_size:.6f}, fee={entry_fee:.4f}"
            )

            exit_price = None
            exit_reason = "eod"
            trailing_stop = stop_loss_price
            remaining_quantity = plan.position_size
            realized_pnl = -entry_fee
            exit_execution_price = 0.0
            exit_index = index
            for future_index in range(index + 1, len(prices)):
                exit_index = future_index
                candle_high = highs[future_index]
                candle_low = lows[future_index]

                # Adverse-first: OHLC data can't tell us the true order price moved within a
                # candle. If a candle's range would have hit both a stop/liquidation AND a
                # profit target, assume the adverse outcome happened first -- overstating a
                # backtest's performance by assuming lucky intra-candle ordering is worse than
                # understating it. Liquidation is checked before the stop since it's strictly
                # farther from entry (enforced by the liquidation-buffer check at trade-open),
                # so it can only trigger when the stop would also have triggered anyway.
                if signal.side == "buy":
                    liquidated = (
                        self.config.leverage > 1.0 and plan.liquidation_price > 0 and candle_low <= plan.liquidation_price
                    )
                    stopped = candle_low <= trailing_stop
                    take_profit_hit = candle_high >= take_profit_price
                    partial_hit = (
                        not trade.partial_exits
                        and candle_high >= entry_price * (1 + self.config.partial_exit_profit_pct)
                    )
                else:
                    liquidated = (
                        self.config.leverage > 1.0 and plan.liquidation_price > 0 and candle_high >= plan.liquidation_price
                    )
                    stopped = candle_high >= trailing_stop
                    take_profit_hit = candle_low <= take_profit_price
                    partial_hit = (
                        not trade.partial_exits
                        and candle_low <= entry_price * (1 - self.config.partial_exit_profit_pct)
                    )

                if liquidated:
                    exit_price = plan.liquidation_price
                    exit_reason = "liquidation"
                    break
                if stopped:
                    exit_price = trailing_stop
                    exit_reason = "trailing_stop"
                    break

                if partial_hit:
                    # Fills at the trigger level itself (not the candle's high/low), matching
                    # how take-profit and stop exits are filled -- consistent with the rest of
                    # the model's slippage-is-applied-once-on-top-of-the-level convention.
                    if signal.side == "buy":
                        partial_trigger_price = entry_price * (1 + self.config.partial_exit_profit_pct)
                    else:
                        partial_trigger_price = entry_price * (1 - self.config.partial_exit_profit_pct)
                    partial_qty = remaining_quantity * self.config.partial_exit_qty_pct
                    partial_exec = self._apply_slippage(partial_trigger_price, signal.side, is_entry=False)
                    if signal.side == "buy":
                        realized_pnl += partial_qty * (partial_exec - entry_execution_price)
                    else:
                        realized_pnl += partial_qty * (entry_execution_price - partial_exec)
                    partial_fee = self._calculate_fee(partial_qty, partial_exec)
                    realized_pnl -= partial_fee
                    trade.fees += partial_fee
                    remaining_quantity -= partial_qty
                    if signal.side == "buy":
                        trailing_stop = max(stop_loss_price, candle_high * (1 - self.config.trailing_stop_pct))
                    else:
                        trailing_stop = min(stop_loss_price, candle_low * (1 + self.config.trailing_stop_pct))
                    trade.partial_exits.append({
                        "price": partial_trigger_price,
                        "quantity": partial_qty,
                        "reason": "partial_profit",
                        "execution_price": partial_exec,
                        "fee": partial_fee,
                    })
                    # The partial exit just tightened the trailing stop -- if this same
                    # candle's range would now also breach the NEW level, the remainder
                    # closes here too rather than waiting for a future candle to notice.
                    if signal.side == "buy" and candle_low <= trailing_stop:
                        exit_price = trailing_stop
                        exit_reason = "trailing_stop"
                        break
                    if signal.side == "sell" and candle_high >= trailing_stop:
                        exit_price = trailing_stop
                        exit_reason = "trailing_stop"
                        break

                if take_profit_hit:
                    exit_price = take_profit_price
                    exit_reason = "take_profit"
                    break

            if exit_price is None:
                exit_price = prices[-1]
                exit_reason = "eod"

            exit_execution_price = self._apply_slippage(exit_price, signal.side, is_entry=False)
            if signal.side == "buy":
                realized_pnl += remaining_quantity * (exit_execution_price - entry_execution_price)
            else:
                realized_pnl += remaining_quantity * (entry_execution_price - exit_execution_price)
            exit_fee = self._calculate_fee(remaining_quantity, exit_execution_price)
            realized_pnl -= exit_fee
            trade.fees += exit_fee
            trade.exit_execution_price = exit_execution_price

            if self.config.funding_enabled and funding_rates:
                funding_cost = self._funding_cost_for_window(
                    signal.side,
                    plan.notional,
                    self._get_candle_time_ms(candles[index]),
                    self._get_candle_time_ms(candles[exit_index]),
                    funding_rates,
                )
                realized_pnl -= funding_cost
                trade.funding_cost = funding_cost

            trade.exit_price = exit_price
            trade.exit_reason = exit_reason
            trade.trailing_stop = trailing_stop
            trade.pnl = realized_pnl
            equity += trade.pnl
            result.equity_curve.append(equity)
            self.risk_manager.register_trade_result(trade.pnl, notional=plan.notional, margin=plan.margin_required)
            self._log(
                f"Closed trade: exit={exit_price:.4f}, reason={exit_reason}, pnl={trade.pnl:.4f}, total_fees={trade.fees:.4f}, equity={equity:.2f}"
            )

        result.final_capital = equity
        return result

    def _calculate_fee(self, quantity: float, price: float) -> float:
        return abs(quantity * price) * self.config.trade_fee_pct

    def _apply_slippage(self, price: float, side: str, is_entry: bool) -> float:
        if self.config.slippage_pct == 0.0:
            return price
        if side == "buy":
            return price * (1 + self.config.slippage_pct)
        return price * (1 - self.config.slippage_pct)

    def _calculate_atr(self, candles: List[List[float]], current_index: int, period: int) -> float:
        if current_index <= 0 or period <= 0:
            return 0.0

        start = max(1, current_index - period + 1)
        true_ranges: list[float] = []
        for i in range(start, current_index + 1):
            high = float(candles[i][2])
            low = float(candles[i][3])
            prev_close = float(candles[i - 1][4]) if i - 1 >= 0 else float(candles[i][4])
            true_range = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(true_range)

        if not true_ranges:
            return 0.0
        return sum(true_ranges) / len(true_ranges)

    def _get_candle_time_ms(self, candle: List[float]) -> int:
        timestamp = candle[0]
        if isinstance(timestamp, str) and timestamp.isdigit():
            # A pure-digit string is an epoch timestamp, not a real date string -- checked
            # before fromisoformat() because Python 3.11+ parses some bare digit strings as
            # valid-looking (but wrong) dates instead of raising.
            timestamp = int(timestamp)
        if isinstance(timestamp, str):
            return int(datetime.fromisoformat(timestamp).timestamp() * 1000)
        if isinstance(timestamp, (int, float)):
            value = int(timestamp)
            return value if len(str(value)) == 13 else value * 1000
        raise ValueError("Unsupported candle timestamp format")

    def _funding_cost_for_window(
        self,
        side: str,
        notional: float,
        start_time_ms: int,
        end_time_ms: int,
        funding_rates: List[dict],
    ) -> float:
        """Sums funding cost for every funding event within [start_time_ms, end_time_ms] the
        position was open through. Uses the trade's entry notional for the whole window
        (doesn't shrink it for partial exits) -- a reasonable approximation given funding
        events are relatively infrequent (every 8h) versus this bot's short holding periods.
        Positive funding rate: longs pay shorts (cost to a buy, income to a sell), and
        vice-versa for negative rates -- the standard Binance funding convention."""
        cost = 0.0
        for entry in funding_rates:
            funding_time = int(entry.get("fundingTime", 0))
            if funding_time < start_time_ms or funding_time > end_time_ms:
                continue
            rate = float(entry.get("fundingRate", 0.0))
            cost += notional * rate if side == "buy" else -notional * rate
        return cost

    def _get_candle_date(self, candle: List[float]) -> date:
        timestamp = candle[0]
        if isinstance(timestamp, str) and timestamp.isdigit():
            # A pure-digit string is an epoch timestamp, not a real date string -- checked
            # before fromisoformat() because Python 3.11+ parses some bare digit strings as
            # valid-looking (but wrong) dates instead of raising.
            timestamp = int(timestamp)
        if isinstance(timestamp, str):
            return datetime.fromisoformat(timestamp).date()
        if isinstance(timestamp, (int, float)):
            if len(str(int(timestamp))) == 13:
                timestamp = int(timestamp) / 1000
            return datetime.fromtimestamp(timestamp).date()
        raise ValueError("Unsupported candle timestamp format")

    def generate_report(self, result: BacktestResult) -> BacktestReport:
        if not result.trades:
            return BacktestReport(0, 0, 0, 0.0, 0.0, 0.0, 0.0)

        pnl_values = [trade.pnl for trade in result.trades]
        winning_trades = sum(1 for pnl in pnl_values if pnl > 0)
        losing_trades = sum(1 for pnl in pnl_values if pnl < 0)
        win_rate = winning_trades / len(pnl_values) if pnl_values else 0.0
        total_pnl = sum(pnl_values)

        if len(pnl_values) > 1:
            mean_pnl = sum(pnl_values) / len(pnl_values)
            std_pnl = (sum((p - mean_pnl) ** 2 for p in pnl_values) / len(pnl_values)) ** 0.5
            sharpe_like_ratio = mean_pnl / std_pnl if std_pnl > 0 else 0.0
        else:
            sharpe_like_ratio = 0.0

        equity_curve = result.equity_curve
        max_drawdown = 0.0
        peak = equity_curve[0] if equity_curve else 0.0
        for value in equity_curve:
            if value > peak:
                peak = value
            drawdown = (peak - value) / peak if peak > 0 else 0.0
            max_drawdown = max(max_drawdown, drawdown)

        return BacktestReport(
            total_trades=len(pnl_values),
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=win_rate,
            total_pnl=total_pnl,
            sharpe_like_ratio=sharpe_like_ratio,
            max_drawdown=max_drawdown,
        )

    def export_results(self, result: BacktestResult, output_path: str, format: str = "json") -> None:
        if format == "json":
            with open(output_path, "w", encoding="utf-8") as handle:
                json.dump(asdict(result), handle, indent=2)
        elif format == "csv":
            with open(output_path, "w", newline="", encoding="utf-8") as handle:
                # Derived from the dataclass itself (not hand-listed) so a future field added to
                # BacktestTrade can't silently go missing from this export again.
                fieldnames = [f.name for f in fields(BacktestTrade)]
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                for trade in result.trades:
                    writer.writerow(asdict(trade))
        else:
            raise ValueError("Unsupported export format")
