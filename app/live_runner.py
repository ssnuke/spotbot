from __future__ import annotations

import signal
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from app.binance_client import SymbolFilters, round_step_size
from app.data_feed import BinanceDataFeed
from app.fx import get_usd_inr_rate
from app.notifications import TelegramNotifier
from app.order_executor import OrderExecutor, SimulatedOrderExecutor
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


def _extract_fill(order: dict, fallback_price: float) -> tuple[float, float]:
    """Returns (fill_price, total_fee) from an order response's fills."""
    fills = order.get("fills") or []
    if not fills:
        return fallback_price, 0.0
    total_qty = sum(float(f["qty"]) for f in fills)
    total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
    total_fee = sum(float(f.get("commission", 0.0)) for f in fills)
    price = total_cost / total_qty if total_qty > 0 else fallback_price
    return price, total_fee


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
        summary_interval_seconds: int = 3600,
        command_poll_interval_seconds: int = 5,
        notifier: Optional[TelegramNotifier] = None,
        telemetry: Optional[Telemetry] = None,
        order_executor: Optional[OrderExecutor] = None,
        data_feed: Optional[BinanceDataFeed] = None,
        store: Optional[StateStore] = None,
        symbol_filters: Optional[SymbolFilters] = None,
        fx_rate_provider: Optional[Callable[[], Optional[float]]] = None,
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
        self.summary_interval_seconds = summary_interval_seconds
        self.command_poll_interval_seconds = command_poll_interval_seconds

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
        self.closed_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self._restore_state()
        self._last_candle_open_time: Optional[int] = None
        self._update_offset: Optional[int] = None
        self._stop_requested = False
        self._trading_paused = False

    def _log(self, message: str) -> None:
        self.telemetry.log(message)

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
        self.risk_manager.allocated_capital = state["allocated_capital"]
        self.risk_manager.consecutive_losses = state["consecutive_losses"]
        self.risk_manager.cooldown_remaining = state["cooldown_remaining"]
        self._current_day = state["current_day"]
        self.cumulative_pnl = state.get("cumulative_pnl", 0.0) or 0.0
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
        if self.risk_manager.allocated_capital > reference_capital:
            self._log(
                f"WARNING: persisted allocated_capital ({self.risk_manager.allocated_capital:.2f}) exceeds "
                f"reference capital ({reference_capital:.2f}) — capital/equity was likely reduced "
                "while positions were open. Clamping so new trades aren't permanently blocked."
            )
            self.risk_manager.allocated_capital = reference_capital
            self._save_risk_state()

    def _save_risk_state(self) -> None:
        self.store.save_risk_state(
            self.risk_manager,
            self._current_day,
            cumulative_pnl=self.cumulative_pnl,
            closed_trades=self.closed_trades,
            winning_trades=self.winning_trades,
            losing_trades=self.losing_trades,
        )

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
            self.risk_manager.allocated_capital = max(0.0, self.risk_manager.allocated_capital - plan.notional)
            self._save_risk_state()
            return

        order_side = "BUY" if signal.side == "buy" else "SELL"
        order = self.order_executor.place_order(self.symbol, order_side, quantity, reference_price=entry_price)
        fill_price, entry_fee = _extract_fill(order, fallback_price=entry_price)
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
            quantity=quantity,
            remaining_quantity=quantity,
            trailing_stop=stop_loss_price,
            partial_exit_done=False,
            entry_order_id=str(order.get("orderId", "")),
            opened_at=datetime.now(timezone.utc).isoformat(),
            realized_pnl_so_far=-entry_fee,
        )
        self.store.add_open_trade(trade_state)
        self.risk_manager.open_positions += 1
        self._save_risk_state()
        self._log(
            f"Opened {signal.side} {quantity} {self.symbol} @ {fill_price:.2f} "
            f"(SL={stop_loss_price:.2f} TP={take_profit_price:.2f}, fee={entry_fee:.4f})"
        )
        self.notifier.send(
            f"\U0001F4C8 {signal.side.upper()} {self.symbol}\n"
            f"Entry: {fill_price:.2f}\nStop: {stop_loss_price:.2f}\nTarget: {take_profit_price:.2f}\n"
            f"Qty: {quantity}"
        )

    def _take_partial_exit(self, trade: OpenTradeState, current_price: float) -> None:
        partial_qty = round_step_size(trade.quantity * self.partial_exit_qty_pct, self._symbol_filters.step_size)
        if partial_qty <= 0 or partial_qty >= trade.remaining_quantity:
            return

        close_side = "SELL" if trade.side == "buy" else "BUY"
        order = self.order_executor.place_order(self.symbol, close_side, partial_qty, reference_price=current_price)
        partial_price, partial_fee = _extract_fill(order, fallback_price=current_price)
        if trade.side == "buy":
            partial_pnl = partial_qty * (partial_price - trade.entry_execution_price) - partial_fee
            trailing_stop = max(trade.stop_loss, current_price * (1 - self.trailing_stop_pct))
        else:
            partial_pnl = partial_qty * (trade.entry_execution_price - partial_price) - partial_fee
            trailing_stop = min(trade.stop_loss, current_price * (1 + self.trailing_stop_pct))

        trade.remaining_quantity -= partial_qty
        trade.partial_exit_done = True
        trade.trailing_stop = trailing_stop
        trade.realized_pnl_so_far += partial_pnl
        self.store.update_open_trade(
            trade.id,
            remaining_quantity=trade.remaining_quantity,
            partial_exit_done=1,
            trailing_stop=trailing_stop,
            realized_pnl_so_far=trade.realized_pnl_so_far,
        )
        self.store.add_trade_history(
            ClosedTradeRecord(
                id=None,
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_execution_price,
                exit_price=partial_price,
                quantity=partial_qty,
                pnl=partial_pnl,
                exit_reason="partial_profit",
                opened_at=trade.opened_at,
                closed_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.cumulative_pnl += partial_pnl
        self._save_risk_state()
        self._log(f"Partial exit {partial_qty} {trade.symbol} @ {partial_price:.2f} pnl={partial_pnl:.2f}")
        self.notifier.send(
            f"⚖️ Partial exit {trade.symbol} @ {partial_price:.2f}\n"
            f"PnL: {self._fmt_usdt(partial_pnl, signed=True)}\n"
            f"Running total: {self._fmt_usdt(self.cumulative_pnl, signed=True)}"
        )

    def _close_trade(self, trade: OpenTradeState, current_price: float, exit_reason: str) -> None:
        close_side = "SELL" if trade.side == "buy" else "BUY"
        order = self.order_executor.place_order(
            self.symbol, close_side, trade.remaining_quantity, reference_price=current_price
        )
        exit_price, exit_fee = _extract_fill(order, fallback_price=current_price)
        if trade.side == "buy":
            pnl = trade.remaining_quantity * (exit_price - trade.entry_execution_price) - exit_fee
        else:
            pnl = trade.remaining_quantity * (trade.entry_execution_price - exit_price) - exit_fee

        # The trade's true result includes the entry fee and any partial-exit profit
        # banked earlier in its lifetime, not just this final segment's fill.
        total_trade_pnl = trade.realized_pnl_so_far + pnl

        notional = trade.quantity * trade.entry_execution_price
        self.risk_manager.register_trade_result(total_trade_pnl, notional=notional)
        self.store.remove_open_trade(trade.id)
        self.store.add_trade_history(
            ClosedTradeRecord(
                id=None,
                symbol=trade.symbol,
                side=trade.side,
                entry_price=trade.entry_execution_price,
                exit_price=exit_price,
                quantity=trade.remaining_quantity,
                pnl=pnl,
                exit_reason=exit_reason,
                opened_at=trade.opened_at,
                closed_at=datetime.now(timezone.utc).isoformat(),
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
        self.notifier.send(
            f"✅ Closed {trade.side.upper()} {trade.symbol} @ {exit_price:.2f} ({exit_reason})\n"
            f"Trade PnL: {self._fmt_usdt(total_trade_pnl, signed=True)}\n"
            f"Running PnL: {self._fmt_usdt(self.cumulative_pnl, signed=True)} over {self.closed_trades} trades "
            f"({win_rate:.1f}% win rate)"
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

    def _status_message(self, prefix: str = "\U0001F4CA Status") -> str:
        open_trades = self.store.list_open_trades()
        win_rate = (self.winning_trades / self.closed_trades * 100) if self.closed_trades else 0.0
        return (
            f"{prefix}\n"
            f"Symbol: {self.symbol}\n"
            f"Open positions: {len(open_trades)}\n"
            f"Closed trades: {self.closed_trades} (W:{self.winning_trades} L:{self.losing_trades}, "
            f"{win_rate:.1f}% win rate)\n"
            f"Cumulative PnL: {self._fmt_usdt(self.cumulative_pnl, signed=True)}\n"
            f"Capital: {self._fmt_usdt(self.risk_manager._reference_capital())}"
            f"{' (compounding ON)' if self.risk_manager.config.compounding_enabled else ''}\n"
            f"Daily trades used: {self.risk_manager.daily_trades}/{self.risk_manager.config.max_trades_per_day}\n"
            f"Cooldown remaining: {self.risk_manager.cooldown_remaining} ticks"
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

    def _recent_trades_message(self, limit: int = 5) -> str:
        records = self.store.list_recent_trade_history(limit=limit)
        if not records:
            return "No closed trades yet."
        lines = [f"\U0001F4CB Last {len(records)} closed trade(s):"]
        for record in records:
            lines.append(
                f"{record.side.upper()} {record.symbol} entry={record.entry_price:.2f} "
                f"exit={record.exit_price:.2f} ({record.exit_reason}) "
                f"pnl={self._fmt_usdt(record.pnl, signed=True)}"
            )
        return "\n".join(lines)

    def _send_started_message(self) -> None:
        mode = "simulated (no real orders)" if isinstance(self.order_executor, SimulatedOrderExecutor) else "testnet"
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
            self.notifier.send(self._recent_trades_message(limit=limit))
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
        prices, last_open_time = self._fetch_price_history()
        if not prices:
            self._log("No price data available")
            return

        current_price = prices[-1]
        self._check_open_trades(current_price)

        if last_open_time == self._last_candle_open_time:
            return  # no new closed candle since last check
        self._last_candle_open_time = last_open_time

        if self._trading_paused:
            return  # existing positions above are still managed; just no new entries

        if self.risk_manager.open_positions >= self.risk_manager.config.max_open_positions:
            return

        signal = self.strategy.generate_signal(prices, self.symbol)
        if signal is None:
            self._log("No trading signal generated")
            return

        self._open_position(signal, entry_price=current_price)

    def run_forever(self) -> None:
        self._install_signal_handlers()
        self._send_started_message()

        last_run_at = 0.0
        last_summary_at = time.time()

        while not self._stop_requested:
            try:
                self._poll_commands()
            except Exception as exc:
                self._log(f"Error polling Telegram commands: {exc}")

            now = time.time()
            if now - last_run_at >= self.poll_interval_seconds:
                last_run_at = now
                try:
                    self.run_once()
                except Exception as exc:  # keep the loop alive across transient API errors
                    self._log(f"Error in run_once: {exc}")
                self.risk_manager.tick()

            now = time.time()
            if now - last_summary_at >= self.summary_interval_seconds:
                last_summary_at = now
                self.notifier.send(self._status_message(prefix="⏱ Periodic update"))

            time.sleep(self.command_poll_interval_seconds)

        self._send_stopped_message()
