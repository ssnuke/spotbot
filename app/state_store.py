from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class OpenTradeState:
    id: Optional[int]
    symbol: str
    side: str
    entry_price: float
    entry_execution_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    remaining_quantity: float
    trailing_stop: float
    partial_exit_done: bool
    entry_order_id: str
    opened_at: str
    realized_pnl_so_far: float = 0.0
    total_fees: float = 0.0
    leverage: float = 1.0
    margin_type: str = "ISOLATED"
    margin_required: float = 0.0
    notional: float = 0.0
    liquidation_price: float = 0.0


@dataclass
class ClosedTradeRecord:
    id: Optional[int]
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    exit_reason: str
    opened_at: str
    closed_at: str
    fees: float = 0.0


class StateStore:
    """Persists risk-manager state and open positions to SQLite so the live runner
    can be killed and restarted without losing track of open trades or risk counters."""

    def __init__(self, db_path: str = "live_state.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS risk_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                daily_loss REAL, daily_trades INTEGER, open_positions INTEGER,
                allocated_capital REAL, consecutive_losses INTEGER,
                cooldown_remaining INTEGER, current_day TEXT,
                cumulative_pnl REAL DEFAULT 0.0, closed_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0, losing_trades INTEGER DEFAULT 0
            )"""
        )
        self._ensure_pnl_columns()
        self._ensure_futures_risk_columns()
        self._ensure_drawdown_halt_column()
        self._ensure_reversal_confirmation_columns()
        self._ensure_activity_columns()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS open_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, entry_price REAL, entry_execution_price REAL,
                stop_loss REAL, take_profit REAL, quantity REAL, remaining_quantity REAL,
                trailing_stop REAL, partial_exit_done INTEGER, entry_order_id TEXT, opened_at TEXT,
                realized_pnl_so_far REAL DEFAULT 0.0
            )"""
        )
        self._ensure_open_trade_columns()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, entry_price REAL, exit_price REAL, quantity REAL,
                pnl REAL, exit_reason TEXT, opened_at TEXT, closed_at TEXT,
                fees REAL DEFAULT 0.0
            )"""
        )
        self._ensure_trade_history_columns()
        self.conn.commit()

    def _ensure_open_trade_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(open_trades)")}
        if "realized_pnl_so_far" not in existing:
            self.conn.execute("ALTER TABLE open_trades ADD COLUMN realized_pnl_so_far REAL DEFAULT 0.0")
        if "total_fees" not in existing:
            self.conn.execute("ALTER TABLE open_trades ADD COLUMN total_fees REAL DEFAULT 0.0")
        futures_defaults = {
            "leverage": "REAL DEFAULT 1.0",
            "margin_type": "TEXT DEFAULT 'ISOLATED'",
            "margin_required": "REAL DEFAULT 0.0",
            "notional": "REAL DEFAULT 0.0",
            "liquidation_price": "REAL DEFAULT 0.0",
        }
        for column, ddl in futures_defaults.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE open_trades ADD COLUMN {column} {ddl}")
        self.conn.commit()

    def _ensure_trade_history_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(trade_history)")}
        if "fees" not in existing:
            self.conn.execute("ALTER TABLE trade_history ADD COLUMN fees REAL DEFAULT 0.0")
        self.conn.commit()

    def _ensure_pnl_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(risk_state)")}
        migrations = {
            "cumulative_pnl": "ALTER TABLE risk_state ADD COLUMN cumulative_pnl REAL DEFAULT 0.0",
            "closed_trades": "ALTER TABLE risk_state ADD COLUMN closed_trades INTEGER DEFAULT 0",
            "winning_trades": "ALTER TABLE risk_state ADD COLUMN winning_trades INTEGER DEFAULT 0",
            "losing_trades": "ALTER TABLE risk_state ADD COLUMN losing_trades INTEGER DEFAULT 0",
            "equity": "ALTER TABLE risk_state ADD COLUMN equity REAL",
            "peak_equity": "ALTER TABLE risk_state ADD COLUMN peak_equity REAL",
        }
        for column, ddl in migrations.items():
            if column not in existing:
                self.conn.execute(ddl)
        self.conn.commit()

    def _ensure_futures_risk_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(risk_state)")}
        migrations = {
            "allocated_margin": "ALTER TABLE risk_state ADD COLUMN allocated_margin REAL DEFAULT 0.0",
            "notional_exposure": "ALTER TABLE risk_state ADD COLUMN notional_exposure REAL DEFAULT 0.0",
            "cumulative_funding_paid": "ALTER TABLE risk_state ADD COLUMN cumulative_funding_paid REAL DEFAULT 0.0",
        }
        for column, ddl in migrations.items():
            if column not in existing:
                self.conn.execute(ddl)
        self.conn.commit()

    def _ensure_drawdown_halt_column(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(risk_state)")}
        if "drawdown_halt_remaining" not in existing:
            self.conn.execute("ALTER TABLE risk_state ADD COLUMN drawdown_halt_remaining INTEGER DEFAULT 0")
        self.conn.commit()

    def _ensure_reversal_confirmation_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(risk_state)")}
        migrations = {
            "pending_reversal_side": "ALTER TABLE risk_state ADD COLUMN pending_reversal_side TEXT DEFAULT NULL",
            "pending_reversal_streak": "ALTER TABLE risk_state ADD COLUMN pending_reversal_streak INTEGER DEFAULT 0",
        }
        for column, ddl in migrations.items():
            if column not in existing:
                self.conn.execute(ddl)
        self.conn.commit()

    def _ensure_activity_columns(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(risk_state)")}
        migrations = {
            "last_poll_at": "ALTER TABLE risk_state ADD COLUMN last_poll_at TEXT",
            "trading_paused": "ALTER TABLE risk_state ADD COLUMN trading_paused INTEGER DEFAULT 0",
        }
        for column, ddl in migrations.items():
            if column not in existing:
                self.conn.execute(ddl)
        self.conn.commit()

    def save_risk_state(
        self,
        risk_manager,
        current_day: str,
        cumulative_pnl: float = 0.0,
        closed_trades: int = 0,
        winning_trades: int = 0,
        losing_trades: int = 0,
        cumulative_funding_paid: float = 0.0,
        pending_reversal_side: Optional[str] = None,
        pending_reversal_streak: int = 0,
        trading_paused: bool = False,
    ) -> None:
        self.conn.execute(
            """INSERT INTO risk_state (id, daily_loss, daily_trades, open_positions, allocated_capital,
                consecutive_losses, cooldown_remaining, current_day, cumulative_pnl, closed_trades,
                winning_trades, losing_trades, equity, peak_equity, allocated_margin, notional_exposure,
                cumulative_funding_paid, drawdown_halt_remaining, pending_reversal_side, pending_reversal_streak,
                last_poll_at, trading_paused)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                daily_loss=excluded.daily_loss, daily_trades=excluded.daily_trades,
                open_positions=excluded.open_positions, allocated_capital=excluded.allocated_capital,
                consecutive_losses=excluded.consecutive_losses, cooldown_remaining=excluded.cooldown_remaining,
                current_day=excluded.current_day, cumulative_pnl=excluded.cumulative_pnl,
                closed_trades=excluded.closed_trades, winning_trades=excluded.winning_trades,
                losing_trades=excluded.losing_trades, equity=excluded.equity,
                peak_equity=excluded.peak_equity, allocated_margin=excluded.allocated_margin,
                notional_exposure=excluded.notional_exposure,
                cumulative_funding_paid=excluded.cumulative_funding_paid,
                drawdown_halt_remaining=excluded.drawdown_halt_remaining,
                pending_reversal_side=excluded.pending_reversal_side,
                pending_reversal_streak=excluded.pending_reversal_streak,
                last_poll_at=excluded.last_poll_at, trading_paused=excluded.trading_paused""",
            (
                risk_manager.daily_loss,
                risk_manager.daily_trades,
                risk_manager.open_positions,
                risk_manager.allocated_margin,  # kept in the legacy allocated_capital column too
                risk_manager.consecutive_losses,
                risk_manager.cooldown_remaining,
                current_day,
                cumulative_pnl,
                closed_trades,
                winning_trades,
                losing_trades,
                risk_manager.equity,
                risk_manager.peak_equity,
                risk_manager.allocated_margin,
                risk_manager.notional_exposure,
                cumulative_funding_paid,
                risk_manager.drawdown_halt_remaining,
                pending_reversal_side,
                pending_reversal_streak,
                # Stamped fresh on every save so the dashboard can tell a live bot from one
                # that's silently stopped polling -- this is the same cadence as the rest of
                # this row (every poll cycle plus every trade event), not a separate heartbeat.
                datetime.now(timezone.utc).isoformat(),
                1 if trading_paused else 0,
            ),
        )
        self.conn.commit()

    def load_risk_state(self) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM risk_state WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        return dict(zip(columns, row))

    def add_open_trade(self, trade: OpenTradeState) -> int:
        cur = self.conn.execute(
            """INSERT INTO open_trades (symbol, side, entry_price, entry_execution_price, stop_loss,
                take_profit, quantity, remaining_quantity, trailing_stop, partial_exit_done,
                entry_order_id, opened_at, realized_pnl_so_far, total_fees, leverage, margin_type,
                margin_required, notional, liquidation_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.symbol,
                trade.side,
                trade.entry_price,
                trade.entry_execution_price,
                trade.stop_loss,
                trade.take_profit,
                trade.quantity,
                trade.remaining_quantity,
                trade.trailing_stop,
                int(trade.partial_exit_done),
                trade.entry_order_id,
                trade.opened_at,
                trade.realized_pnl_so_far,
                trade.total_fees,
                trade.leverage,
                trade.margin_type,
                trade.margin_required,
                trade.notional,
                trade.liquidation_price,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_open_trade(self, trade_id: int, **fields) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(trade_id)
        self.conn.execute(f"UPDATE open_trades SET {assignments} WHERE id = ?", values)
        self.conn.commit()

    def remove_open_trade(self, trade_id: int) -> None:
        self.conn.execute("DELETE FROM open_trades WHERE id = ?", (trade_id,))
        self.conn.commit()

    def list_open_trades(self) -> List[OpenTradeState]:
        cur = self.conn.execute("SELECT * FROM open_trades")
        columns = [d[0] for d in cur.description]
        trades = []
        for row in cur.fetchall():
            data = dict(zip(columns, row))
            trades.append(
                OpenTradeState(
                    id=data["id"],
                    symbol=data["symbol"],
                    side=data["side"],
                    entry_price=data["entry_price"],
                    entry_execution_price=data["entry_execution_price"],
                    stop_loss=data["stop_loss"],
                    take_profit=data["take_profit"],
                    quantity=data["quantity"],
                    remaining_quantity=data["remaining_quantity"],
                    trailing_stop=data["trailing_stop"],
                    partial_exit_done=bool(data["partial_exit_done"]),
                    entry_order_id=data["entry_order_id"],
                    opened_at=data["opened_at"],
                    realized_pnl_so_far=data.get("realized_pnl_so_far") or 0.0,
                    total_fees=data.get("total_fees") or 0.0,
                    leverage=data.get("leverage") or 1.0,
                    margin_type=data.get("margin_type") or "ISOLATED",
                    margin_required=data.get("margin_required") or 0.0,
                    notional=data.get("notional") or 0.0,
                    liquidation_price=data.get("liquidation_price") or 0.0,
                )
            )
        return trades

    def add_trade_history(self, record: ClosedTradeRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO trade_history (symbol, side, entry_price, exit_price, quantity,
                pnl, exit_reason, opened_at, closed_at, fees)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.symbol,
                record.side,
                record.entry_price,
                record.exit_price,
                record.quantity,
                record.pnl,
                record.exit_reason,
                record.opened_at,
                record.closed_at,
                record.fees,
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_trade_history_since(self, since_iso: str) -> List[ClosedTradeRecord]:
        """All closed trades with closed_at >= since_iso, oldest first -- read-only, used by the
        regime health monitor (app/regime_monitor.py) to compute rolling-window statistics over
        real live trade history. Adds no new table/column and changes no existing behavior."""
        cur = self.conn.execute(
            "SELECT * FROM trade_history WHERE closed_at >= ? ORDER BY closed_at ASC", (since_iso,)
        )
        columns = [d[0] for d in cur.description]
        records = []
        for row in cur.fetchall():
            data = dict(zip(columns, row))
            records.append(
                ClosedTradeRecord(
                    id=data["id"],
                    symbol=data["symbol"],
                    side=data["side"],
                    entry_price=data["entry_price"],
                    exit_price=data["exit_price"],
                    quantity=data["quantity"],
                    pnl=data["pnl"],
                    exit_reason=data["exit_reason"],
                    opened_at=data["opened_at"],
                    closed_at=data["closed_at"],
                    fees=data.get("fees") or 0.0,
                )
            )
        return records

    def list_recent_trade_history(self, limit: int = 5) -> List[ClosedTradeRecord]:
        cur = self.conn.execute(
            "SELECT * FROM trade_history ORDER BY id DESC LIMIT ?", (limit,)
        )
        columns = [d[0] for d in cur.description]
        records = []
        for row in cur.fetchall():
            data = dict(zip(columns, row))
            records.append(
                ClosedTradeRecord(
                    id=data["id"],
                    symbol=data["symbol"],
                    side=data["side"],
                    entry_price=data["entry_price"],
                    exit_price=data["exit_price"],
                    quantity=data["quantity"],
                    pnl=data["pnl"],
                    exit_reason=data["exit_reason"],
                    opened_at=data["opened_at"],
                    closed_at=data["closed_at"],
                    fees=data.get("fees") or 0.0,
                )
            )
        return records

    def close(self) -> None:
        self.conn.close()
