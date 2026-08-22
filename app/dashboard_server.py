"""Read-only web dashboard for the live bot: equity curve, open positions, trade history,
and headline stats. Runs as its own process, completely separate from the trading bot --
it only ever opens live_state.db in SQLite read-only mode (mode=ro), so a bug here can
never corrupt or block the bot's own state writes. Meant to sit behind a reverse proxy
(Caddy) that terminates HTTPS; this process itself only binds to localhost.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
from functools import wraps
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

load_dotenv()

DB_PATH = os.getenv("DASHBOARD_DB_PATH", "live_state.db")
SYMBOL = os.getenv("DASHBOARD_SYMBOL", "SOL/USDT")
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


@app.route("/api/summary")
@require_auth
def summary():
    conn = _connect_readonly()
    try:
        risk = conn.execute("SELECT * FROM risk_state WHERE id = 1").fetchone()
        open_positions = conn.execute("SELECT COUNT(*) AS n FROM open_trades").fetchone()["n"]
    finally:
        conn.close()

    if risk is None:
        return jsonify({"error": "No risk_state row yet -- bot hasn't run a poll cycle."}), 503

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
        "cumulative_pnl": risk["cumulative_pnl"] or 0.0,
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
    cumulative_pnl = risk["cumulative_pnl"] or 0.0
    # Walk forward from the implied starting point so the curve ends exactly at current
    # equity -- an approximation (funding payments outside of trade PnL aren't broken out
    # separately here), close enough for a glance-at-it dashboard.
    running = current_equity - cumulative_pnl
    points = [{"date": None, "equity": running}]
    for row in rows:
        running += row["pnl"]
        points.append({"date": row["closed_at"], "equity": running})

    return jsonify(points)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("DASHBOARD_PORT", "8080")))
