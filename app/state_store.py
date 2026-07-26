from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS open_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, entry_price REAL, entry_execution_price REAL,
                stop_loss REAL, take_profit REAL, quantity REAL, remaining_quantity REAL,
                trailing_stop REAL, partial_exit_done INTEGER, entry_order_id TEXT, opened_at TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, side TEXT, entry_price REAL, exit_price REAL, quantity REAL,
                pnl REAL, exit_reason TEXT, opened_at TEXT, closed_at TEXT
            )"""
        )
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

    def save_risk_state(
        self,
        risk_manager,
        current_day: str,
        cumulative_pnl: float = 0.0,
        closed_trades: int = 0,
        winning_trades: int = 0,
        losing_trades: int = 0,
    ) -> None:
        self.conn.execute(
            """INSERT INTO risk_state (id, daily_loss, daily_trades, open_positions, allocated_capital,
                consecutive_losses, cooldown_remaining, current_day, cumulative_pnl, closed_trades,
                winning_trades, losing_trades, equity, peak_equity)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                daily_loss=excluded.daily_loss, daily_trades=excluded.daily_trades,
                open_positions=excluded.open_positions, allocated_capital=excluded.allocated_capital,
                consecutive_losses=excluded.consecutive_losses, cooldown_remaining=excluded.cooldown_remaining,
                current_day=excluded.current_day, cumulative_pnl=excluded.cumulative_pnl,
                closed_trades=excluded.closed_trades, winning_trades=excluded.winning_trades,
                losing_trades=excluded.losing_trades, equity=excluded.equity,
                peak_equity=excluded.peak_equity""",
            (
                risk_manager.daily_loss,
                risk_manager.daily_trades,
                risk_manager.open_positions,
                risk_manager.allocated_capital,
                risk_manager.consecutive_losses,
                risk_manager.cooldown_remaining,
                current_day,
                cumulative_pnl,
                closed_trades,
                winning_trades,
                losing_trades,
                risk_manager.equity,
                risk_manager.peak_equity,
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
                entry_order_id, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                )
            )
        return trades

    def add_trade_history(self, record: ClosedTradeRecord) -> int:
        cur = self.conn.execute(
            """INSERT INTO trade_history (symbol, side, entry_price, exit_price, quantity,
                pnl, exit_reason, opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        self.conn.commit()
        return cur.lastrowid

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
                )
            )
        return records

    def close(self) -> None:
        self.conn.close()
