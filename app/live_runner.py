from __future__ import annotations

import os
import signal
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from app.binance_client import SymbolFilters, round_step_size
from app.binance_futures_client import BinanceFuturesClient
from app.data_feed import BinanceDataFeed
from app.fx import get_usd_inr_rate
from app.notifications import TelegramNotifier
from app.order_executor import (
    FuturesLiveOrderExecutor,
    FuturesTestnetOrderExecutor,
    LiveOrderExecutor,
    OrderExecutor,
    SimulatedOrderExecutor,
    TestnetOrderExecutor,
)
from app.risk_manager import RiskConfig, RiskManager
from app.state_store import ClosedTradeRecord, OpenTradeState, StateStore
from app.strategy import TAEStrategy, TAEStrategyConfig
from app.telemetry import Telemetry

DEFAULT_SYMBOL_FILTERS = SymbolFilters(step_size=0.000001, tick_size=0.01, min_notional=0.0)

HELP_TEXT = (
    "\U0001F916 Commands:\n"
    "/status - open positions, PnL, and risk counters\n"
    "/pnl - cumulative PnL, win/loss counts, and average PnL per trade\n"
    "/trades or /openpositions - list currently open trades\n"
    "/history [N] - last N closed trades with entry/exit/reason/pnl (default 5, max 20)\n"
    "/price - current market price\n"
    "/pause - stop opening new positions (existing ones still managed)\n"
    "/resume - resume opening new positions after a /pause\n"
    "/kill or /stop - shut the bot down remotely (graceful, sends a final summary)\n"
    "/help - show this message"
)


def _extract_fill(order: dict, fallback_price: float, fallback_quantity: float = 0.0) -> tuple[float, float, float]:
    """Returns (fill_price, filled_quantity, total_fee) from an order response's fills.
    filled_quantity is the ACTUAL amount the exchange filled, which for a market order on a
    liquid pair is essentially always the full requested amount, but should never be assumed --
    a thin book or a rejected/partially-filled order means less (or nothing) actually filled."""
    fills = order.get("fills") or []
    if not fills:
        return fallback_price, fallback_quantity, 0.0
    total_qty = sum(float(f["qty"]) for f in fills)
    total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
    total_fee = sum(float(f.get("commission", 0.0)) for f in fills)
    price = total_cost / total_qty if total_qty > 0 else fallback_price
    return price, total_qty, total_fee


class LiveRunner:
    """Runs the TAE strategy continuously against live Binance market data, mirroring
    the backtester's exit logic (partial exit, trailing stop, take-profit) in real time.
    Orders are filled via a pluggable OrderExecutor — by default a local simulation
    against live mainnet prices (no exchange account, no real or testnet orders placed).
    State survives restarts via SQLite, and every open/close is reported to Telegram."""

    def __init__(
        self,
        risk_config: RiskConfig,
        strategy_config: Optional[TAEStrategyConfig] = None,
        symbol: str = "BTC/USDT",
        timeframe: str = "5m",
        poll_interval_seconds: int = 300,
        warmup_candles: int = 100,
        stop_loss_pct: float = 0.01,
        take_profit_pct: float = 0.03,
        partial_exit_profit_pct: float = 0.01,
        partial_exit_qty_pct: float = 0.5,
        trailing_stop_pct: float = 0.005,
        db_path: str = "live_state.db",
        long_only: bool = False,
        summary_interval_seconds: int = 3600,
        command_poll_interval_seconds: int = 5,
        notifier: Optional[TelegramNotifier] = None,
        telemetry: Optional[Telemetry] = None,
        order_executor: Optional[OrderExecutor] = None,
        data_feed: Optional[BinanceDataFeed] = None,
        store: Optional[StateStore] = None,
        symbol_filters: Optional[SymbolFilters] = None,
        fx_rate_provider: Optional[Callable[[], Optional[float]]] = None,
        futures_client: Optional[BinanceFuturesClient] = None,
        market_type: str = "spot",
        mark_price_divergence_pct: float = 0.01,
        mark_price_divergence_max_ticks: int = 3,
        kill_switch_file_path: str = "KILL_SWITCH",
    ):
        self._fx_rate_provider = fx_rate_provider or get_usd_inr_rate
        self.symbol = symbol
        self.timeframe = timeframe
        self.poll_interval_seconds = poll_interval_seconds
        self.warmup_candles = warmup_candles
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.partial_exit_profit_pct = partial_exit_profit_pct
        self.partial_exit_qty_pct = partial_exit_qty_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.long_only = long_only
        self.summary_interval_seconds = summary_interval_seconds
        self.command_poll_interval_seconds = command_poll_interval_seconds
        self.futures_client = futures_client
        self.market_type = market_type
        self.mark_price_divergence_pct = mark_price_divergence_pct
        self.mark_price_divergence_max_ticks = mark_price_divergence_max_ticks
        self.kill_switch_file_path = kill_switch_file_path

        self.data_feed = data_feed or BinanceDataFeed()
        self.order_executor = order_executor or SimulatedOrderExecutor()
        self.notifier = notifier or TelegramNotifier()
        self.telemetry = telemetry or Telemetry(enabled=True)
        self.strategy = TAEStrategy(strategy_config)
        self.risk_manager = RiskManager(risk_config)
        self.store = store or StateStore(db_path)
        self._symbol_filters = symbol_filters or DEFAULT_SYMBOL_FILTERS

        self._current_day = datetime.now(timezone.utc).date().isoformat()
        self.cumulative_pnl = 0.0
        self.cumulative_funding_paid = 0.0
        self.closed_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self._funding_last_checked_ms = int(time.time() * 1000)
        self._mark_price_divergence_ticks = 0
        self._restore_state()
        if self.futures_client is not None:
            self._revalidate_futures_positions_on_restart()
        self._last_candle_open_time: Optional[int] = None
        self._update_offset: Optional[int] = None
        self._stop_requested = False
        self._trading_paused = False
        self._drawdown_halt_notified = False

    def _log(self, message: str) -> None:
        self.telemetry.log(message)

    def _release_reserved_trade_slot(self, plan) -> None:
        """Undoes create_trade_plan()'s capital/daily-trade reservation for an order that
        never actually resulted in an open position (skipped, rejected, or failed to place)."""
        self.risk_manager.allocated_margin = max(0.0, self.risk_manager.allocated_margin - plan.margin_required)
        self.risk_manager.notional_exposure = max(0.0, self.risk_manager.notional_exposure - plan.notional)
        if self.risk_manager.open_positions == 0:
            self.risk_manager.current_side = None
        self.risk_manager.daily_trades = max(0, self.risk_manager.daily_trades - 1)
        self._save_risk_state()

    def _refresh_symbol_filters(self) -> bool:
        """Re-fetches exchange lot-size/tick-size/min-notional filters for self.symbol. Exchange
        filters can change server-side (e.g. Binance re-tiering a pair's LOT_SIZE precision)
        without this process restarting, and a stale filter causes every order to be rejected
        until the filters are refreshed -- this lets that self-heal instead of requiring a
        manual restart. Returns whether the refresh succeeded."""
        client = getattr(self.order_executor, "client", None) or self.futures_client
        if client is None:
            return False
        try:
            self._symbol_filters = client.get_symbol_filters(self.symbol)
            self._log(f"Refreshed symbol filters for {self.symbol}: {self._symbol_filters}")
            return True
        except Exception as exc:
            self._log(f"Failed to refresh symbol filters: {exc}")
            return False

    def _place_order_with_filter_refresh(
        self,
        symbol: str,
        side: str,
        quantity: float,
        reference_price: float,
        reduce_only: bool = False,
        recompute_quantity: Optional[Callable[[], float]] = None,
    ) -> tuple[dict, float]:
        """Places an order; if it's rejected, refreshes symbol filters and retries exactly once.
        `recompute_quantity`, when given, re-derives the quantity from `self._symbol_filters`
        after the refresh (e.g. re-rounding to a step size that just changed) -- otherwise the
        same quantity is resubmitted as-is. Returns (order, quantity actually submitted) on
        success; re-raises the retry's exception (or the original, if no refresh was possible)
        on failure."""
        try:
            order = self.order_executor.place_order(
                symbol, side, quantity, reference_price=reference_price, reduce_only=reduce_only
            )
            return order, quantity
        except Exception as first_exc:
            # Printed (not just self._log, which is silent without --verbose) so the exact
            # rejected quantity/step size is visible in journalctl -- the exchange's error message
            # alone doesn't say what we actually sent, which is the first thing needed to tell a
            # genuinely stale filter apart from some other rejection reason entirely.
            print(
                f"Order rejected, attempting filter refresh + retry: symbol={symbol} side={side} "
                f"qty={quantity} step_size={self._symbol_filters.step_size} error={first_exc}"
            )
            if not self._refresh_symbol_filters():
                raise
            if recompute_quantity is not None:
                quantity = recompute_quantity()
            try:
                order = self.order_executor.place_order(
                    symbol, side, quantity, reference_price=reference_price, reduce_only=reduce_only
                )
            except Exception as retry_exc:
                print(
                    f"Retry after filter refresh also failed: symbol={symbol} side={side} "
                    f"qty={quantity} step_size={self._symbol_filters.step_size} error={retry_exc}"
                )
                raise
            self._log(f"Order placed on retry after refreshing symbol filters (qty={quantity})")
            return order, quantity

    def _render_table(self, headers: list[str], rows: list[list[str]]) -> str:
        """Renders a compact, right-aligned monospace table for Telegram, wrapped in a
        Markdown code block so column alignment survives Telegram's non-monospace default
        font on mobile."""
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))
        lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))]
        for row in rows:
            lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        return "```\n" + "\n".join(lines) + "\n```"

    def _mode_label(self) -> str:
        if isinstance(self.order_executor, (LiveOrderExecutor, FuturesLiveOrderExecutor)):
            suffix = " (FUTURES, LEVERAGED)" if isinstance(self.order_executor, FuturesLiveOrderExecutor) else ""
            return f"\U0001F534 LIVE — REAL MONEY{suffix}"
        if isinstance(self.order_executor, FuturesTestnetOrderExecutor):
            return "futures testnet"
        if isinstance(self.order_executor, TestnetOrderExecutor):
            return "testnet"
        return "simulated (no real orders)"

    def _fmt_usdt(self, amount: float, signed: bool = False) -> str:
        number = f"{amount:+,.2f}" if signed else f"{amount:,.2f}"
        rate = self._fx_rate_provider()
        if rate is None:
            return f"{number} USDT"
        inr = amount * rate
        inr_str = f"{inr:+,.2f}" if signed else f"{inr:,.2f}"
        return f"{number} USDT (≈ ₹{inr_str})"

    def _restore_state(self) -> None:
        state = self.store.load_risk_state()
        if not state:
            return
        self.risk_manager.daily_loss = state["daily_loss"]
        self.risk_manager.daily_trades = state["daily_trades"]
        self.risk_manager.open_positions = state["open_positions"]
        # allocated_margin falls back to the legacy allocated_capital column so DBs written
        # before this migration don't lose their in-flight allocation on the first restart.
        self.risk_manager.allocated_margin = state.get("allocated_margin") or state.get("allocated_capital") or 0.0
        self.risk_manager.notional_exposure = state.get("notional_exposure") or 0.0
        self.risk_manager.consecutive_losses = state["consecutive_losses"]
        self.risk_manager.cooldown_remaining = state["cooldown_remaining"]
        self.risk_manager.drawdown_halt_remaining = state.get("drawdown_halt_remaining") or 0
        self._current_day = state["current_day"]
        self.cumulative_pnl = state.get("cumulative_pnl", 0.0) or 0.0
        self.cumulative_funding_paid = state.get("cumulative_funding_paid", 0.0) or 0.0
        self.closed_trades = state.get("closed_trades", 0) or 0
        self.winning_trades = state.get("winning_trades", 0) or 0
        self.losing_trades = state.get("losing_trades", 0) or 0
        if state.get("equity") is not None:
            self.risk_manager.equity = state["equity"]
        if state.get("peak_equity") is not None:
            self.risk_manager.peak_equity = state["peak_equity"]

        # If capital (or, under compounding, equity) was lowered while trades opened
        # under a larger reference were still open, the persisted allocation can
        # exceed the new reference capital, making available capital permanently
        # negative and blocking all new trades. Clamp it so the account self-heals
        # as those positions close naturally.
        reference_capital = self.risk_manager._reference_capital()
        if self.risk_manager.allocated_margin > reference_capital:
            self._log(
                f"WARNING: persisted allocated_margin ({self.risk_manager.allocated_margin:.2f}) exceeds "
                f"reference capital ({reference_capital:.2f}) — capital/equity was likely reduced "
                "while positions were open. Clamping so new trades aren't permanently blocked."
            )
            self.risk_manager.allocated_margin = reference_capital
            self._save_risk_state()

        # Recover which side is currently open so opposite-signal reversal detection keeps
        # working across a restart, not just for the lifetime of one process.
        open_trades = self.store.list_open_trades()
        if open_trades:
            self.risk_manager.current_side = open_trades[-1].side

    def _save_risk_state(self) -> None:
        self.store.save_risk_state(
            self.risk_manager,
            self._current_day,
            cumulative_pnl=self.cumulative_pnl,
            closed_trades=self.closed_trades,
            winning_trades=self.winning_trades,
            losing_trades=self.losing_trades,
            cumulative_funding_paid=self.cumulative_funding_paid,
        )

    def _get_current_price(self, fallback_price: float) -> float:
        """Mark price when trading futures (what the exchange itself uses for unrealized PnL
        and liquidation), falling back to the candle-close-derived price otherwise/on failure."""
        if self.futures_client is not None:
            mark_price = self.futures_client.get_mark_price(self.symbol)
            if mark_price:
                return mark_price
        return fallback_price

    def _estimate_margin_ratio(self, position_risk: dict) -> Optional[float]:
        """Distance-based proxy for closeness to liquidation: 0.0 at entry price, approaching
        1.0 as mark price approaches the liquidation price. Deliberately avoids depending on
        Binance's own maintMargin/marginRatio field naming (which varies by endpoint version) --
        cross-check this proxy against the account's real margin ratio during testnet validation."""
        try:
            entry = float(position_risk["entryPrice"])
            mark = float(position_risk["markPrice"])
            liq = float(position_risk["liquidationPrice"])
        except (KeyError, TypeError, ValueError):
            return None
        total_distance = abs(entry - liq)
        if liq <= 0 or total_distance <= 0:
            return None
        remaining_distance = abs(mark - liq)
        return max(0.0, min(1.0, 1 - remaining_distance / total_distance))

    def _emergency_close_all(self, reason: str) -> None:
        """Cancels any resting orders and market-closes every open position immediately, then
        pauses new entries (does not stop the process -- /status and /resume stay reachable)."""
        self._log(f"EMERGENCY CLOSE triggered: {reason}")
        if self.futures_client is not None:
            try:
                self.futures_client.cancel_all_open_orders(self.symbol)
            except Exception as exc:
                self._log(f"cancel_all_open_orders failed during emergency close: {exc}")
                self.notifier.send(
                    f"⚠️ Failed to cancel open orders during emergency close (positions are still "
                    f"being closed below) — check for resting orders manually:\n{exc}"
                )
        for trade in self.store.list_open_trades():
            current_price = self._get_current_price(trade.entry_execution_price)
            self._close_trade(trade, current_price, reason)
        self._trading_paused = True
        self._save_risk_state()
        self.notifier.send(
            f"\U0001F6A8 EMERGENCY CLOSE: {reason}\n"
            f"All positions closed, new entries paused. Send /resume after review, or /kill to stop."
        )

    def _revalidate_futures_positions_on_restart(self) -> None:
        """On restart, re-checks any recovered open position against the CURRENT mark price
        and liquidation buffer -- a position that was safe when the process last ran may not
        still be, and it shouldn't simply resume being monitored without this check."""
        if not self.store.list_open_trades():
            return
        position_risk = self.futures_client.get_position_risk(self.symbol)
        if position_risk is None:
            return
        margin_ratio = self._estimate_margin_ratio(position_risk)
        if margin_ratio is not None and self.risk_manager.check_margin_ratio(margin_ratio) == "force_close":
            self._emergency_close_all("restart_liquidation_check")

    def _ensure_futures_account_configured(self) -> bool:
        """Explicitly sets and verifies one-way position mode, isolated margin, and configured
        leverage before the bot is allowed to start. Returns False (refuse to start) on any
        failure -- futures orders must never be placed against unverified account settings."""
        try:
            self.futures_client.set_position_mode(dual_side=False)
            self.futures_client.set_margin_type(self.symbol, self.risk_manager.config.margin_type)
            self.futures_client.set_leverage(self.symbol, self.risk_manager.config.leverage)
            mode = self.futures_client.get_position_mode()
            if mode.get("dualSidePosition") is not False:
                raise ValueError(f"Position mode did not stick: {mode}")
        except Exception as exc:
            self.notifier.send(f"\U0001F6D1 Futures account setup failed, refusing to start: {exc}")
            self._log(f"Futures account setup failed, refusing to start: {exc}")
            return False
        return True

    def _check_futures_safety(self) -> None:
        """Per-poll futures safety checks: margin-ratio warn/force-close, and a mark-price
        vs. last-trade-price divergence kill switch sustained over several consecutive polls."""
        if self.futures_client is None:
            return

        if self.store.list_open_trades():
            position_risk = self.futures_client.get_position_risk(self.symbol)
            if position_risk is not None:
                margin_ratio = self._estimate_margin_ratio(position_risk)
                if margin_ratio is not None:
                    status = self.risk_manager.check_margin_ratio(margin_ratio)
                    if status == "force_close":
                        self._emergency_close_all(f"margin_ratio_breach ({margin_ratio:.1%})")
                        return
                    if status == "warn":
                        self.notifier.send(
                            f"⚠️ Margin ratio warning: {margin_ratio:.1%} on {self.symbol}"
                        )

        last_price = self.data_feed.get_latest_price(self.symbol)
        mark_price = self.futures_client.get_mark_price(self.symbol)
        if not last_price or not mark_price or last_price <= 0:
            return
        divergence = abs(mark_price - last_price) / last_price
        if divergence <= self.mark_price_divergence_pct:
            self._mark_price_divergence_ticks = 0
            return
        self._mark_price_divergence_ticks += 1
        if self._mark_price_divergence_ticks >= self.mark_price_divergence_max_ticks:
            ticks = self._mark_price_divergence_ticks
            self._mark_price_divergence_ticks = 0
            self._emergency_close_all(f"mark_price_divergence ({divergence:.2%} sustained for {ticks} polls)")

    def _check_funding_payments(self) -> None:
        """Folds any new funding payments (positive or negative) since the last check into
        equity/PnL -- funding accrues independent of trade close events and is otherwise
        invisible to the bot's own bookkeeping."""
        if self.futures_client is None:
            return
        try:
            payments = self.futures_client.get_funding_payments(self.symbol, self._funding_last_checked_ms)
        except Exception as exc:
            self._log(f"Failed to fetch funding payments: {exc}")
            self.notifier.send(f"⚠️ Failed to fetch funding payments (will retry next poll):\n{exc}")
            return
        self._funding_last_checked_ms = int(time.time() * 1000)
        if not payments:
            return
        total = sum(float(payment.get("income", 0.0) or 0.0) for payment in payments)
        if total == 0.0:
            return
        self.risk_manager.apply_funding_payment(total)
        self.cumulative_funding_paid += total
        self._save_risk_state()
        self._log(f"Funding payments applied: {total:+.4f} ({len(payments)} entries)")
        self.notifier.send(f"\U0001F4B8 Funding: {self._fmt_usdt(total, signed=True)} ({len(payments)} payment(s))")

    def _check_kill_switch_file(self) -> None:
        """A Telegram-independent stop mechanism: if a sentinel file is present, stop exactly
        like the /kill command would, distinguishable in the log from a Telegram-issued kill."""
        if os.path.exists(self.kill_switch_file_path):
            self._stop_requested = True
            self._log(f"Kill switch file detected at {self.kill_switch_file_path!r}, stopping")

    def _maybe_roll_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self._current_day:
            self._current_day = today
            self.risk_manager.reset_daily_limits()
            self._log(f"New day {today}: reset daily loss and trade counters")
            self._save_risk_state()

    def _fetch_price_history(self) -> tuple[list, Optional[int]]:
        candles = self.data_feed.get_recent_candles(self.symbol, interval=self.timeframe, limit=self.warmup_candles)
        if len(candles) < 2:
            return [], None
        closed = candles[:-1]  # the last candle from Binance's klines endpoint is still forming
        prices = [float(c[4]) for c in closed]
        last_open_time = int(closed[-1][0])
        return prices, last_open_time

    def _current_drawdown_pct(self) -> float:
        peak = self.risk_manager.peak_equity
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - self.risk_manager.equity) / peak)

    def _check_drawdown_halt_notification(self) -> None:
        """Sends exactly one Telegram alert per halt episode the first time the max-drawdown
        circuit breaker starts blocking trades -- without this, a halted account looks
        identical to a quiet market: validate_trade() just returns False silently, and the
        matching log line only shows with --verbose."""
        halted = self.risk_manager.is_drawdown_halted()
        if halted and not self._drawdown_halt_notified:
            self._drawdown_halt_notified = True
            drawdown = self._current_drawdown_pct()
            recovery = self.risk_manager.config.drawdown_recovery_period
            recovery_note = (
                f"Will auto-resume in {self.risk_manager.drawdown_halt_remaining} ticks "
                f"(peak resets, drawdown recalculated fresh)."
                if recovery > 0
                else "No auto-recovery configured -- this will persist until equity recovers "
                "or the config changes."
            )
            self._log(f"Max drawdown circuit breaker tripped at {drawdown:.1%}")
            self.notifier.send(
                f"\U0001F9CA Max drawdown circuit breaker tripped ({drawdown:.1%} from peak "
                f"{self._fmt_usdt(self.risk_manager.peak_equity)}) -- new trades blocked.\n{recovery_note}"
            )
        elif not halted and self._drawdown_halt_notified:
            self._drawdown_halt_notified = False
            self._log("Max drawdown circuit breaker cleared, trading resumed")
            self.notifier.send("✅ Max drawdown circuit breaker cleared -- trading resumed.")

    def _open_position(self, signal, entry_price: float) -> None:
        stop_loss_price = (
            entry_price * (1 - self.stop_loss_pct) if signal.side == "buy" else entry_price * (1 + self.stop_loss_pct)
        )
        if not self.risk_manager.validate_trade(entry_price, stop_loss_price, side=signal.side):
            self._log(f"Risk blocked trade: entry={entry_price:.2f} side={signal.side}")
            return

        plan = self.risk_manager.create_trade_plan(entry_price, stop_loss_price, side=signal.side)
        quantity = round_step_size(plan.position_size, self._symbol_filters.step_size)
        if quantity <= 0 or quantity * entry_price < self._symbol_filters.min_notional:
            self._log(f"Order below exchange minimums, skipping: qty={quantity}")
            self._release_reserved_trade_slot(plan)
            return

        order_side = "BUY" if signal.side == "buy" else "SELL"
        try:
            order, quantity = self._place_order_with_filter_refresh(
                self.symbol,
                order_side,
                quantity,
                reference_price=entry_price,
                recompute_quantity=lambda: round_step_size(plan.position_size, self._symbol_filters.step_size),
            )
        except Exception as exc:
            # create_trade_plan() above already reserved capital and a daily-trade slot for
            # this order -- if it never actually got placed, that reservation must be released,
            # or it permanently blocks future trades with capital tied to a trade that doesn't exist.
            print(f"Order placement failed, releasing reserved capital: {exc}")
            self._log(f"Order placement failed, releasing reserved capital: {exc}")
            self._release_reserved_trade_slot(plan)
            self.notifier.send(
                f"\U0001F6D1 Order failed: {signal.side.upper()} {self.symbol} — trade skipped, "
                f"reserved capital released.\n{exc}"
            )
            return

        fill_price, filled_quantity, entry_fee = _extract_fill(order, fallback_price=entry_price, fallback_quantity=quantity)
        if filled_quantity <= 0:
            self._log(f"Order placed but nothing filled (qty=0), releasing reserved capital")
            self._release_reserved_trade_slot(plan)
            return
        self.cumulative_pnl -= entry_fee

        risk_per_unit = abs(entry_price - stop_loss_price)
        reward_ratio = self.risk_manager.config.reward_ratio
        take_profit_price = (
            entry_price + risk_per_unit * reward_ratio
            if signal.side == "buy"
            else entry_price - risk_per_unit * reward_ratio
        )

        trade_state = OpenTradeState(
            id=None,
            symbol=self.symbol,
            side=signal.side,
            entry_price=entry_price,
            entry_execution_price=fill_price,
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            quantity=filled_quantity,
            remaining_quantity=filled_quantity,
            trailing_stop=stop_loss_price,
            partial_exit_done=False,
            entry_order_id=str(order.get("orderId", "")),
            opened_at=datetime.now(timezone.utc).isoformat(),
            realized_pnl_so_far=-entry_fee,
            total_fees=entry_fee,
            leverage=self.risk_manager.config.leverage,
            margin_type=self.risk_manager.config.margin_type,
            margin_required=plan.margin_required,
            notional=plan.notional,
            liquidation_price=plan.liquidation_price,
        )
        self.store.add_open_trade(trade_state)
        self.risk_manager.open_positions += 1
        self._save_risk_state()
        self._log(
            f"Opened {signal.side} {filled_quantity} {self.symbol} @ {fill_price:.2f} "
            f"(SL={stop_loss_price:.2f} TP={take_profit_price:.2f}, fee={entry_fee:.4f})"
        )
        self.notifier.send(
            f"\U0001F4C8 {signal.side.upper()} {self.symbol}\n"
            f"Entry: {fill_price:.2f}\nStop: {stop_loss_price:.2f}\nTarget: {take_profit_price:.2f}\n"
            f"Qty: {filled_quantity}"
        )

    def _take_partial_exit(self, trade: OpenTradeState, current_price: float) -> None:
        partial_qty = round_step_size(trade.quantity * self.partial_exit_qty_pct, self._symbol_filters.step_size)
        if partial_qty <= 0 or partial_qty >= trade.remaining_quantity:
            return

        close_side = "SELL" if trade.side == "buy" else "BUY"
        try:
            order, partial_qty = self._place_order_with_filter_refresh(
                self.symbol,
                close_side,
                partial_qty,
                reference_price=current_price,
                reduce_only=True,
                recompute_quantity=lambda: round_step_size(
                    trade.quantity * self.partial_exit_qty_pct, self._symbol_filters.step_size
                ),
            )
        except Exception as exc:
            # Nothing has been mutated yet -- the trade stays open unchanged and this partial
            # exit will simply be retried on the next poll if the price condition still holds.
            print(f"Partial exit order failed, will retry next poll: {exc}")
            self._log(f"Partial exit order failed, will retry next poll: {exc}")
            self.notifier.send(f"⚠️ Partial exit failed for {trade.symbol}, will retry next poll:\n{exc}")
            return

        partial_price, filled_qty, partial_fee = _extract_fill(order, fallback_price=current_price, fallback_quantity=partial_qty)
        if filled_qty <= 0:
            self._log("Partial exit order placed but nothing filled, will retry next poll")
            return
        if trade.side == "buy":
            partial_pnl = filled_qty * (partial_price - trade.entry_execution_price) - partial_fee
            trailing_stop = max(trade.stop_loss, current_price * (1 - self.trailing_stop_pct))
        else:
            partial_pnl = filled_qty * (trade.entry_execution_price - partial_price) - partial_fee
            trailing_stop = min(trade.stop_loss, current_price * (1 + self.trailing_stop_pct))

        trade.remaining_quantity -= filled_qty
        trade.partial_exit_done = True
        trade.trailing_stop = trailing_stop
        trade.realized_pnl_so_far += partial_pnl
        trade.total_fees += partial_fee
        self.store.update_open_trade(
            trade.id,
            remaining_quantity=trade.remaining_quantity,
            partial_exit_done=1,
            trailing_stop=trailing_stop,
            realized_pnl_so_far=trade.realized_pnl_so_far,
            total_fees=trade.total_fees,
        )
        self.store.add_trade_history(
            ClosedTradeRecord(
                id=None,
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_execution_price,
                exit_price=partial_price,
                quantity=filled_qty,
                pnl=partial_pnl,
                exit_reason="partial_profit",
                opened_at=trade.opened_at,
                closed_at=datetime.now(timezone.utc).isoformat(),
                fees=partial_fee,
            )
        )
        self.cumulative_pnl += partial_pnl
        self._save_risk_state()
        self._log(f"Partial exit {filled_qty} {trade.symbol} @ {partial_price:.2f} pnl={partial_pnl:.2f}")
        self.notifier.send(
            f"⚖️ Partial exit {trade.symbol} @ {partial_price:.2f}\n"
            f"PnL: {self._fmt_usdt(partial_pnl, signed=True)}\n"
            f"Running total: {self._fmt_usdt(self.cumulative_pnl, signed=True)}"
        )

    def _close_trade(self, trade: OpenTradeState, current_price: float, exit_reason: str) -> None:
        close_side = "SELL" if trade.side == "buy" else "BUY"
        try:
            order, _ = self._place_order_with_filter_refresh(
                self.symbol,
                close_side,
                trade.remaining_quantity,
                reference_price=current_price,
                reduce_only=True,
                recompute_quantity=lambda: round_step_size(
                    trade.remaining_quantity, self._symbol_filters.step_size
                ),
            )
        except Exception as exc:
            # Nothing has been mutated yet -- the trade stays open and the close is retried
            # on the next poll (the stop/target condition that triggered this is still true).
            print(f"Close order failed, will retry next poll: {exc}")
            self._log(f"Close order failed, will retry next poll: {exc}")
            self.notifier.send(
                f"⚠️ Close order failed for {trade.symbol} ({exit_reason}), will retry next poll:\n{exc}"
            )
            return

        # filled_qty should equal trade.remaining_quantity for a market order on a liquid pair
        # in virtually every case; PnL is computed on whatever actually filled either way. A
        # partial fill leaving genuine residual quantity open at the exchange is not handled
        # (the trade is always treated as fully closed below) -- an accepted, very low-probability
        # gap at this bot's tiny position sizes, worth revisiting if position sizes scale up a lot.
        exit_price, filled_qty, exit_fee = _extract_fill(
            order, fallback_price=current_price, fallback_quantity=trade.remaining_quantity
        )
        if trade.side == "buy":
            pnl = filled_qty * (exit_price - trade.entry_execution_price) - exit_fee
        else:
            pnl = filled_qty * (trade.entry_execution_price - exit_price) - exit_fee

        # The trade's true result includes the entry fee and any partial-exit profit
        # banked earlier in its lifetime, not just this final segment's fill.
        total_trade_pnl = trade.realized_pnl_so_far + pnl
        total_trade_fees = trade.total_fees + exit_fee

        notional = trade.notional or (trade.quantity * trade.entry_execution_price)
        margin = trade.margin_required or notional
        self.risk_manager.register_trade_result(total_trade_pnl, notional=notional, margin=margin)
        self.store.remove_open_trade(trade.id)
        self.store.add_trade_history(
            ClosedTradeRecord(
                id=None,
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_execution_price,
                exit_price=exit_price,
                quantity=filled_qty,
                pnl=pnl,
                exit_reason=exit_reason,
                opened_at=trade.opened_at,
                closed_at=datetime.now(timezone.utc).isoformat(),
                fees=exit_fee,  # this segment's own fee, matching pnl above -- the whole-trade
                # total (entry + partial + this) is shown separately in the Telegram summary below
            )
        )

        self.cumulative_pnl += pnl
        self.closed_trades += 1
        if total_trade_pnl > 0:
            self.winning_trades += 1
        elif total_trade_pnl < 0:
            self.losing_trades += 1
        self._save_risk_state()

        self._log(
            f"Closed {trade.side} {trade.symbol} @ {exit_price:.2f} reason={exit_reason} "
            f"final_segment_pnl={pnl:.2f} total_trade_pnl={total_trade_pnl:.2f} "
            f"cumulative_pnl={self.cumulative_pnl:.2f}"
        )
        win_rate = (self.winning_trades / self.closed_trades * 100) if self.closed_trades else 0.0
        table = self._render_table(
            ["Field", "Value"],
            [
                ["Entry", f"{trade.entry_execution_price:.2f}"],
                ["Exit", f"{exit_price:.2f}"],
                ["Qty", f"{trade.quantity:.6f}"],
                ["Fees", f"{total_trade_fees:.4f}"],
                ["PnL", f"{total_trade_pnl:+.4f}"],
            ],
        )
        self.notifier.send(
            f"✅ Closed {trade.side.upper()} {trade.symbol} ({exit_reason})\n"
            f"{table}\n"
            f"Capital: {self._fmt_usdt(self.risk_manager._reference_capital())}\n"
            f"Running PnL: {self._fmt_usdt(self.cumulative_pnl, signed=True)} over {self.closed_trades} trades "
            f"({win_rate:.1f}% win rate)",
            parse_mode="Markdown",
        )

    def _check_open_trades(self, current_price: float) -> None:
        for trade in self.store.list_open_trades():
            if trade.side == "buy":
                if not trade.partial_exit_done and current_price >= trade.entry_price * (
                    1 + self.partial_exit_profit_pct
                ):
                    self._take_partial_exit(trade, current_price)

                if current_price >= trade.take_profit:
                    self._close_trade(trade, current_price, "take_profit")
                elif current_price <= trade.trailing_stop:
                    self._close_trade(trade, current_price, "trailing_stop")
            else:
                if not trade.partial_exit_done and current_price <= trade.entry_price * (
                    1 - self.partial_exit_profit_pct
                ):
                    self._take_partial_exit(trade, current_price)

                if current_price <= trade.take_profit:
                    self._close_trade(trade, current_price, "take_profit")
                elif current_price >= trade.trailing_stop:
                    self._close_trade(trade, current_price, "trailing_stop")

    def _futures_status_lines(self) -> str:
        """Leverage/margin/position-mode line, plus live margin ratio + liquidation price
        when a position is open. Empty string outside futures mode."""
        if self.futures_client is None:
            return ""
        lines = (
            f"\nLeverage: {self.risk_manager.config.leverage:g}x ({self.risk_manager.config.margin_type}, one-way)"
        )
        open_trades = self.store.list_open_trades()
        if open_trades:
            position_risk = self.futures_client.get_position_risk(self.symbol)
            if position_risk is not None:
                margin_ratio = self._estimate_margin_ratio(position_risk)
                if margin_ratio is not None:
                    lines += f"\nMargin ratio: {margin_ratio:.1%}"
                liquidation_price = open_trades[0].liquidation_price
                if liquidation_price:
                    lines += f"\nLiquidation price (est.): {liquidation_price:.2f}"
        return lines

    def _status_message(self, prefix: str = "\U0001F4CA Status") -> str:
        open_trades = self.store.list_open_trades()
        win_rate = (self.winning_trades / self.closed_trades * 100) if self.closed_trades else 0.0
        return (
            f"{prefix}\n"
            f"Symbol: {self.symbol}\n"
            f"Mode: {self._mode_label()}\n"
            f"Open positions: {len(open_trades)}\n"
            f"Closed trades: {self.closed_trades} (W:{self.winning_trades} L:{self.losing_trades}, "
            f"{win_rate:.1f}% win rate)\n"
            f"Cumulative PnL: {self._fmt_usdt(self.cumulative_pnl, signed=True)}\n"
            f"Capital: {self._fmt_usdt(self.risk_manager._reference_capital())}"
            f"{' (compounding ON)' if self.risk_manager.config.compounding_enabled else ''}\n"
            f"Daily trades used: {self.risk_manager.daily_trades}/{self.risk_manager.config.max_trades_per_day}\n"
            f"Cooldown remaining: {self.risk_manager.cooldown_remaining} ticks\n"
            f"Drawdown from peak: {self._current_drawdown_pct():.1%} of {self.risk_manager.config.max_drawdown_pct:.0%} limit"
            f"{' 🧊 HALTED' if self.risk_manager.is_drawdown_halted() else ''}"
            f"{self._futures_status_lines()}"
        )

    def _open_trades_message(self) -> str:
        open_trades = self.store.list_open_trades()
        if not open_trades:
            return "No open trades right now."
        lines = ["\U0001F4C8 Open trades:"]
        for trade in open_trades:
            lines.append(
                f"{trade.side.upper()} {trade.symbol} qty={trade.remaining_quantity:.6f} "
                f"entry={trade.entry_execution_price:.2f} trailing_stop={trade.trailing_stop:.2f} "
                f"target={trade.take_profit:.2f}"
            )
        return "\n".join(lines)

    def _pnl_message(self) -> str:
        win_rate = (self.winning_trades / self.closed_trades * 100) if self.closed_trades else 0.0
        avg_pnl = (self.cumulative_pnl / self.closed_trades) if self.closed_trades else 0.0
        return (
            f"\U0001F4B0 PnL\n"
            f"Cumulative PnL: {self._fmt_usdt(self.cumulative_pnl, signed=True)}\n"
            f"Closed trades: {self.closed_trades} (W:{self.winning_trades} L:{self.losing_trades}, "
            f"{win_rate:.1f}% win rate)\n"
            f"Avg PnL per closed trade: {self._fmt_usdt(avg_pnl, signed=True)}"
        )

    _EXIT_REASON_ABBREVIATIONS = {
        "take_profit": "TP",
        "trailing_stop": "TS",
        "partial_profit": "PE",
    }

    def _recent_trades_message(self, limit: int = 5) -> str:
        records = self.store.list_recent_trade_history(limit=limit)
        if not records:
            return "No closed trades yet."

        headers = ["Side", "Entry", "Exit", "Fees", "PnL", "Rsn"]
        rows = [
            [
                record.side.upper(),
                f"{record.entry_price:.2f}",
                f"{record.exit_price:.2f}",
                f"{record.fees:.3f}",
                f"{record.pnl:+.2f}",
                self._EXIT_REASON_ABBREVIATIONS.get(record.exit_reason, record.exit_reason),
            ]
            for record in records
        ]
        table = self._render_table(headers, rows)
        return f"\U0001F4CB Last {len(records)} closed trade(s):\n{table}"

    def _send_started_message(self) -> None:
        mode = self._mode_label()
        open_trades = self.store.list_open_trades()
        resumed = self.closed_trades > 0 or bool(open_trades)
        header = f"\U0001F7E2 Bot started ({'resumed' if resumed else 'fresh'} state)"
        self._log(
            f"Starting live paper trading ({mode}) for {self.symbol} on {self.timeframe} candles "
            f"({len(open_trades)} open position(s) recovered from state)"
        )
        message = (
            f"{header}\n"
            f"Symbol: {self.symbol}\nTimeframe: {self.timeframe}\nMode: {mode}\n"
            f"Capital: {self._fmt_usdt(self.risk_manager._reference_capital())}"
            f"{' (compounding ON)' if self.risk_manager.config.compounding_enabled else ''}\n"
            f"Cumulative PnL so far: {self._fmt_usdt(self.cumulative_pnl, signed=True)} over {self.closed_trades} trades\n"
            f"Open positions recovered: {len(open_trades)}"
            f"{self._futures_status_lines()}"
        )
        if open_trades:
            for trade in open_trades:
                message += (
                    f"\n{trade.side.upper()} {trade.symbol} qty={trade.remaining_quantity:.6f} "
                    f"entry={trade.entry_execution_price:.2f} trailing_stop={trade.trailing_stop:.2f} "
                    f"target={trade.take_profit:.2f}"
                )
        self.notifier.send(message)

    def _send_stopped_message(self) -> None:
        self._log("Bot stopping")
        self.notifier.send(self._status_message(prefix="\U0001F534 Bot stopped"))

    def _handle_command(self, text: str) -> None:
        command = text.strip().lower().split()[0] if text.strip() else ""
        if command in ("/start", "/help"):
            self.notifier.send(HELP_TEXT)
        elif command == "/status":
            self.notifier.send(self._status_message())
        elif command == "/pnl":
            self.notifier.send(self._pnl_message())
        elif command in ("/trades", "/openpositions"):
            self.notifier.send(self._open_trades_message())
        elif command == "/history":
            parts = text.strip().split()
            limit = 5
            if len(parts) > 1 and parts[1].isdigit():
                limit = max(1, min(int(parts[1]), 20))
            self.notifier.send(self._recent_trades_message(limit=limit), parse_mode="Markdown")
        elif command == "/price":
            price = self.data_feed.get_latest_price(self.symbol)
            self.notifier.send(f"{self.symbol}: {price:.2f}" if price is not None else "Price unavailable right now.")
        elif command == "/pause":
            self._trading_paused = True
            self.notifier.send(
                "⏸ Trading paused. Existing positions are still monitored and will "
                "exit normally; no new positions will open until /resume."
            )
        elif command == "/resume":
            self._trading_paused = False
            self.notifier.send("▶️ Trading resumed. New signals can open positions again.")
        elif command in ("/kill", "/stop"):
            self._stop_requested = True
            self.notifier.send("\U0001F6D1 Kill switch received. Shutting down...")
        elif command:
            self.notifier.send("Unknown command. Send /help to see what I understand.")

    def _poll_commands(self) -> None:
        updates = self.notifier.get_updates(offset=self._update_offset, timeout=0)
        for update in updates:
            self._update_offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat_id = str(message.get("chat", {}).get("id", ""))
            text = message.get("text") or ""
            if not text or chat_id != str(self.notifier.chat_id):
                continue
            self._handle_command(text)

    def _install_signal_handlers(self) -> None:
        def _request_stop(signum, frame):
            self._stop_requested = True

        signal.signal(signal.SIGTERM, _request_stop)
        signal.signal(signal.SIGINT, _request_stop)

    def run_once(self) -> None:
        self._maybe_roll_day()
        self._check_funding_payments()
        prices, last_open_time = self._fetch_price_history()
        if not prices:
            self._log("No price data available")
            return

        current_price = prices[-1]
        exit_price = self._get_current_price(current_price)
        self._check_open_trades(exit_price)
        self._check_futures_safety()
        self._check_drawdown_halt_notification()
        if self._trading_paused:
            # _check_futures_safety may have just triggered an emergency close and paused
            # trading -- don't fall through into signal evaluation in the same poll.
            return

        if last_open_time == self._last_candle_open_time:
            return  # no new closed candle since last check
        self._last_candle_open_time = last_open_time

        signal = self.strategy.generate_signal(prices, self.symbol)
        if signal is None:
            self._log("No trading signal generated")
            return

        if self.long_only and signal.side == "sell":
            self._log("Sell signal skipped: long_only is enabled (spot can't short)")
            return

        if self.risk_manager.current_side and signal.side != self.risk_manager.current_side:
            self._log(
                f"Opposite signal ({signal.side}) while {self.risk_manager.current_side} is open "
                "-- closing first, no naked reverse"
            )
            for trade in self.store.list_open_trades():
                self._close_trade(trade, exit_price, "signal_reversal")
            return  # next poll re-evaluates and may open the new side fresh, fully risk-checked

        if self.risk_manager.open_positions >= self.risk_manager.config.max_open_positions:
            return

        self._open_position(signal, entry_price=current_price)

    def run_forever(self) -> None:
        if self.futures_client is not None and not self._ensure_futures_account_configured():
            return  # refuse to start -- futures orders must never run against unverified settings

        self._install_signal_handlers()
        self._send_started_message()

        last_run_at = 0.0
        last_summary_at = time.time()

        while not self._stop_requested:
            self._check_kill_switch_file()
            if self._stop_requested:
                break

            try:
                self._poll_commands()
            except Exception as exc:
                # Always printed (not just via self._log, which is silent without --verbose) so
                # errors are never invisible in journalctl once real money is involved. Also
                # notified -- if this exception IS Telegram being unreachable, notifier.send()
                # itself just swallows the failure and returns False rather than raising, so this
                # never masks the original error or loops back into itself.
                print(f"Error polling Telegram commands: {exc}")
                self._log(f"Error polling Telegram commands: {exc}")
                self.notifier.send(f"\U0001F6D1 Error polling Telegram commands:\n{exc}")

            now = time.time()
            if now - last_run_at >= self.poll_interval_seconds:
                last_run_at = now
                try:
                    self.run_once()
                except Exception as exc:  # keep the loop alive across transient API errors
                    print(f"Error in run_once: {exc}")
                    self._log(f"Error in run_once: {exc}")
                    self.notifier.send(f"\U0001F6D1 Error in trading loop (will retry next poll):\n{exc}")
                self.risk_manager.tick()
                # tick() decays cooldown_remaining/drawdown_halt_remaining purely in memory --
                # without a save here, a restart during an active cooldown or drawdown-recovery
                # countdown reloads whatever was last persisted (from the trade event that
                # started it), silently discarding however much time had actually elapsed since.
                self._save_risk_state()

            now = time.time()
            if now - last_summary_at >= self.summary_interval_seconds:
                last_summary_at = now
                self.notifier.send(self._status_message(prefix="⏱ Periodic update"))

            time.sleep(self.command_poll_interval_seconds)

        self._send_stopped_message()
