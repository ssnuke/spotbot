"""Read-only web dashboard for the live bot: equity curve, open positions, trade history,
and headline stats. Runs as its own process, completely separate from the trading bot --
it only ever opens live_state.db in SQLite read-only mode (mode=ro), so a bug here can
never corrupt or block the bot's own state writes. Meant to sit behind a reverse proxy
(Caddy) that terminates HTTPS; this process itself only binds to localhost.
"""
from __future__ import annotations

import csv
import io
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

load_dotenv()

DB_PATH = os.getenv("DASHBOARD_DB_PATH", "live_state.db")
SYMBOL = os.getenv("DASHBOARD_SYMBOL", "SOL/USDT")
CHART_INTERVAL = os.getenv("DASHBOARD_CHART_INTERVAL", "5m")
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
STATIC_DIR = Path(__file__).parent / "dashboard_static"

app = Flask(__name__, static_folder=None)


def _connect_readonly() -> sqlite3.Connection:
    """A dashboard bug must never be able to write to the bot's live state -- opened
    strictly read-only at the SQLite level, not just by convention in this code."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not DASHBOARD_USERNAME or not DASHBOARD_PASSWORD:
            return Response(
                "Dashboard auth is not configured -- set DASHBOARD_USERNAME and "
                "DASHBOARD_PASSWORD in .env before starting this service.",
                status=500,
            )
        auth = request.authorization
        valid = (
            auth
            and secrets.compare_digest(auth.username, DASHBOARD_USERNAME)
            and secrets.compare_digest(auth.password, DASHBOARD_PASSWORD)
        )
        if not valid:
            return Response(
                "Authentication required", status=401,
                headers={"WWW-Authenticate": 'Basic realm="Trading Dashboard"'},
            )
        return view(*args, **kwargs)

    return wrapped


@app.route("/")
@require_auth
def index():
    return send_from_directory(STATIC_DIR, "index.html")


IST = timezone(timedelta(hours=5, minutes=30))


def _trading_pnl_windows(conn, now_utc: datetime | None = None) -> dict:
    """Real trading PnL for today/this week/this month/all time, summed directly from
    trade_history rows -- NOT from risk_state.cumulative_pnl, which can include deposits or
    other balance corrections picked up by the bot's equity-reconciliation logic (it can't tell
    a deposit apart from trading profit, so a manual top-up shows up there as if it were a huge
    winning trade). Individual trade_history rows are never touched by that reconciliation, so
    summing them directly is unaffected by deposits/withdrawals -- a clean trading-only figure.

    "Today"/"this week"/"this month" boundaries are IST (UTC+5:30, no DST) even though
    closed_at is stored in UTC -- each boundary is computed at IST midnight, then converted to
    its equivalent UTC instant before comparing against the stored UTC strings, since SQLite
    compares TEXT lexicographically and a bare IST-offset string would not sort correctly
    against UTC-offset ones."""
    now_ist = (now_utc or datetime.now(timezone.utc)).astimezone(IST)
    today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start_ist = today_start_ist - timedelta(days=today_start_ist.weekday())  # Monday 00:00 IST
    month_start_ist = today_start_ist.replace(day=1)

    def sum_since(since_ist: datetime) -> float:
        since_utc = since_ist.astimezone(timezone.utc)
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trade_history WHERE closed_at >= ?",
            (since_utc.isoformat(),),
        ).fetchone()
        return row["total"] or 0.0

    all_time_row = conn.execute("SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trade_history").fetchone()
    return {
        "trading_pnl_today": sum_since(today_start_ist),
        "trading_pnl_week": sum_since(week_start_ist),
        "trading_pnl_month": sum_since(month_start_ist),
        "trading_pnl_all_time": all_time_row["total"] or 0.0,
    }


# Matches heartbeat_watchdog.py's own default HEARTBEAT_MAX_AGE_SECONDS -- past this, the
# bot is presumed to have stopped polling rather than just being between candles.
STALE_POLL_THRESHOLD_SECONDS = 900


def _compute_bot_activity(risk: dict, open_positions: int, now_utc: datetime | None = None) -> dict:
    """Derives what the bot is actually doing right now from risk_state's last_poll_at/
    trading_paused columns (added alongside this feature -- rows written before this deploy,
    or read before trading-bot.service has restarted to add the columns, have last_poll_at
    missing/NULL, which reads as maximally stale until the next poll writes one; `.get()`
    throughout so an un-migrated row degrades to "offline" instead of a 500). "Offline" always
    wins over position/pause state: a stale bot isn't reliably doing anything, whatever the
    last-known state said."""
    now = now_utc or datetime.now(timezone.utc)
    last_poll_at = risk.get("last_poll_at")
    poll_age_seconds = None
    if last_poll_at:
        try:
            poll_age_seconds = (now - datetime.fromisoformat(last_poll_at)).total_seconds()
        except ValueError:
            poll_age_seconds = None

    stale = poll_age_seconds is None or poll_age_seconds > STALE_POLL_THRESHOLD_SECONDS
    if stale:
        status = "offline"
    elif risk.get("trading_paused"):
        status = "paused"
    elif open_positions > 0:
        status = "in_position"
    else:
        status = "scanning"

    return {
        "bot_status": status,
        "trading_paused": bool(risk.get("trading_paused")),
        "last_poll_at": last_poll_at,
        "poll_age_seconds": poll_age_seconds,
    }


@app.route("/api/summary")
@require_auth
def summary():
    conn = _connect_readonly()
    try:
        risk = conn.execute("SELECT * FROM risk_state WHERE id = 1").fetchone()
        open_positions = conn.execute("SELECT COUNT(*) AS n FROM open_trades").fetchone()["n"]
        pnl_windows = _trading_pnl_windows(conn)
    finally:
        conn.close()

    if risk is None:
        return jsonify({"error": "No risk_state row yet -- bot hasn't run a poll cycle."}), 503

    activity = _compute_bot_activity(dict(risk), open_positions)

    equity = risk["equity"] or 0.0
    peak_equity = risk["peak_equity"] or equity
    closed_trades = risk["closed_trades"] or 0
    winning_trades = risk["winning_trades"] or 0
    drawdown_pct = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0.0
    win_rate_pct = (winning_trades / closed_trades * 100) if closed_trades > 0 else 0.0

    return jsonify({
        "symbol": SYMBOL,
        "equity": equity,
        "peak_equity": peak_equity,
        "drawdown_pct": drawdown_pct,
        # cumulative_pnl is kept for backward compatibility ONLY -- it can include deposits/
        # corrections picked up by equity reconciliation (see _trading_pnl_windows' docstring).
        # Use trading_pnl_* for an honest trading-only figure.
        "cumulative_pnl": risk["cumulative_pnl"] or 0.0,
        **pnl_windows,
        **activity,
        "closed_trades": closed_trades,
        "winning_trades": winning_trades,
        "losing_trades": risk["losing_trades"] or 0,
        "win_rate_pct": win_rate_pct,
        "open_positions": open_positions,
        "daily_loss": risk["daily_loss"] or 0.0,
        "consecutive_losses": risk["consecutive_losses"] or 0,
        "allocated_margin": risk["allocated_margin"] or 0.0,
        "notional_exposure": risk["notional_exposure"] or 0.0,
        "cumulative_funding_paid": risk["cumulative_funding_paid"] or 0.0,
    })


def _fetch_mark_price(symbol: str) -> float | None:
    """Public Binance endpoint, no API key needed. Best-effort -- if it fails, positions
    still render with their static entry/stop/target, just without a live unrealized PnL."""
    try:
        binance_symbol = symbol.replace("/", "")
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/ticker/price",
            params={"symbol": binance_symbol},
            timeout=3,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        return None


@app.route("/api/candles")
@require_auth
def candles():
    """Recent OHLC candles for the live market chart -- Binance's public futures klines
    endpoint, no API key needed. Same read-only-to-the-outside-world posture as the mark-price
    fetch below: this process never places orders or touches account data, only market data."""
    limit = min(500, max(20, request.args.get("limit", 150, type=int)))
    try:
        resp = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": SYMBOL.replace("/", ""), "interval": CHART_INTERVAL, "limit": limit},
            timeout=5,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch candles: {exc}"}), 502

    return jsonify({
        "symbol": SYMBOL,
        "interval": CHART_INTERVAL,
        "candles": [
            {"time": c[0], "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4])}
            for c in raw
        ],
    })


@app.route("/api/positions")
@require_auth
def positions():
    conn = _connect_readonly()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM open_trades ORDER BY opened_at DESC")]
    finally:
        conn.close()

    mark_price = _fetch_mark_price(SYMBOL) if rows else None
    for row in rows:
        row["mark_price"] = mark_price
        if mark_price is not None:
            qty = row["remaining_quantity"]
            entry = row["entry_execution_price"] or row["entry_price"]
            if row["side"] == "buy":
                unrealized = qty * (mark_price - entry)
            else:
                unrealized = qty * (entry - mark_price)
            row["unrealized_pnl"] = unrealized
        else:
            row["unrealized_pnl"] = None

    return jsonify(rows)


@app.route("/api/trades")
@require_auth
def trades():
    page = max(1, request.args.get("page", 1, type=int))
    page_size = min(100, max(1, request.args.get("page_size", 20, type=int)))
    offset = (page - 1) * page_size

    conn = _connect_readonly()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM trade_history").fetchone()["n"]
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM trade_history ORDER BY closed_at DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )
        ]
    finally:
        conn.close()

    return jsonify({
        "trades": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    })


@app.route("/api/trades/export.csv")
@require_auth
def export_trades_csv():
    """Every closed trade, unpaginated -- opens directly in Excel/Sheets/Numbers.
    Read-only (same mode=ro connection as every other route here)."""
    conn = _connect_readonly()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM trade_history ORDER BY closed_at DESC")]
    finally:
        conn.close()

    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_history.csv"},
    )


@app.route("/api/equity-curve")
@require_auth
def equity_curve():
    conn = _connect_readonly()
    try:
        risk = conn.execute("SELECT equity, cumulative_pnl FROM risk_state WHERE id = 1").fetchone()
        rows = list(conn.execute("SELECT closed_at, pnl FROM trade_history ORDER BY closed_at ASC"))
    finally:
        conn.close()

    if risk is None:
        return jsonify([])

    current_equity = risk["equity"] or 0.0
    # Anchor on the sum of real trade_history rows, NOT risk_state.cumulative_pnl -- that
    # counter gets deposits/withdrawals folded into it by equity reconciliation (it can't tell
    # a manual top-up apart from trading profit), which would shift this entire curve up or
    # down by the deposit amount and make its endpoint disagree with the current balance shown
    # above it. Trade rows are never touched by that reconciliation, so this stays clean.
    total_trade_pnl = sum(row["pnl"] or 0.0 for row in rows)
    # Walk forward from the implied starting point so the curve ends exactly at current
    # equity -- an approximation (funding payments outside of trade PnL aren't broken out
    # separately here), close enough for a glance-at-it dashboard.
    running = current_equity - total_trade_pnl
    points = [{"date": None, "equity": running}]
    for row in rows:
        running += row["pnl"]
        points.append({"date": row["closed_at"], "equity": running})

    return jsonify(points)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("DASHBOARD_PORT", "8080")))
