import sqlite3

import pytest

import app.dashboard_server as dashboard_module


class FakeKlinesResponse:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self._rows


def _client(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_USERNAME", "user")
    monkeypatch.setattr(dashboard_module, "DASHBOARD_PASSWORD", "pass")
    dashboard_module.app.config["TESTING"] = True
    return dashboard_module.app.test_client()


def _auth_headers():
    import base64

    token = base64.b64encode(b"user:pass").decode()
    return {"Authorization": f"Basic {token}"}


def test_candles_endpoint_returns_parsed_ohlc(monkeypatch):
    client = _client(monkeypatch)
    raw_klines = [
        [1700000000000, "100.0", "105.0", "99.0", "103.0", "1000", 1700000299999, "0", 0, "0", "0", "0"],
        [1700000300000, "103.0", "108.0", "102.0", "107.0", "1200", 1700000599999, "0", 0, "0", "0", "0"],
    ]
    monkeypatch.setattr(dashboard_module.requests, "get", lambda *a, **k: FakeKlinesResponse(raw_klines))

    response = client.get("/api/candles", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["symbol"] == dashboard_module.SYMBOL
    assert len(payload["candles"]) == 2
    assert payload["candles"][0] == {"time": 1700000000000, "open": 100.0, "high": 105.0, "low": 99.0, "close": 103.0}


def test_candles_endpoint_returns_502_on_upstream_failure(monkeypatch):
    client = _client(monkeypatch)

    def raise_error(*a, **k):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(dashboard_module.requests, "get", raise_error)

    response = client.get("/api/candles", headers=_auth_headers())

    assert response.status_code == 502


def test_candles_endpoint_requires_auth(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/api/candles")
    assert response.status_code == 401


def _seed_trade_history_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_price REAL, exit_price REAL,
            quantity REAL, pnl REAL, exit_reason TEXT, opened_at TEXT, closed_at TEXT, fees REAL
        )"""
    )
    conn.execute(
        "INSERT INTO trade_history (symbol, side, entry_price, exit_price, quantity, pnl, "
        "exit_reason, opened_at, closed_at, fees) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("SOL/USDT", "buy", 100.0, 101.0, 1.0, 1.0, "take_profit", "2026-01-01T00:00:00", "2026-01-01T01:00:00", 0.05),
    )
    conn.commit()
    conn.close()


def test_export_trades_csv_requires_auth(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/api/trades/export.csv")
    assert response.status_code == 401


def test_export_trades_csv_returns_downloadable_csv(monkeypatch, tmp_path):
    db_path = tmp_path / "live_state.db"
    _seed_trade_history_db(db_path)
    monkeypatch.setattr(dashboard_module, "DB_PATH", str(db_path))
    client = _client(monkeypatch)

    response = client.get("/api/trades/export.csv", headers=_auth_headers())

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment; filename=trade_history.csv" in response.headers["Content-Disposition"]
    body = response.get_data(as_text=True)
    assert "SOL/USDT" in body
    assert "take_profit" in body


def _seed_full_db(db_path, trades):
    """trades: list of (pnl, closed_at_iso) tuples."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_price REAL, exit_price REAL,
            quantity REAL, pnl REAL, exit_reason TEXT, opened_at TEXT, closed_at TEXT, fees REAL
        )"""
    )
    for pnl, closed_at in trades:
        conn.execute(
            "INSERT INTO trade_history (symbol, side, entry_price, exit_price, quantity, pnl, "
            "exit_reason, opened_at, closed_at, fees) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("SOL/USDT", "buy", 100.0, 101.0, 1.0, pnl, "take_profit", closed_at, closed_at, 0.05),
        )
    conn.execute(
        """CREATE TABLE risk_state (
            id INTEGER PRIMARY KEY, daily_loss REAL, consecutive_losses INTEGER,
            cumulative_pnl REAL, closed_trades INTEGER, winning_trades INTEGER, losing_trades INTEGER,
            equity REAL, peak_equity REAL, allocated_margin REAL, notional_exposure REAL,
            cumulative_funding_paid REAL
        )"""
    )
    conn.execute(
        "INSERT INTO risk_state (id, daily_loss, consecutive_losses, cumulative_pnl, closed_trades, "
        "winning_trades, losing_trades, equity, peak_equity, allocated_margin, notional_exposure, "
        "cumulative_funding_paid) VALUES (1, 0, 0, ?, ?, ?, ?, ?, ?, 0, 0, 0)",
        (
            # cumulative_pnl deliberately set far higher than the sum of real trades below --
            # simulates a deposit having been folded into it by equity reconciliation.
            675.09 + sum(p for p, _ in trades),
            len(trades), sum(1 for p, _ in trades if p > 0), sum(1 for p, _ in trades if p <= 0),
            793.37, 793.37,
        ),
    )
    conn.execute("CREATE TABLE open_trades (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()


def test_summary_trading_pnl_excludes_deposit_polluted_cumulative(monkeypatch, tmp_path):
    db_path = tmp_path / "live_state.db"
    now = dashboard_module.datetime.now(dashboard_module.timezone.utc)
    trades = [
        (10.0, now.isoformat()),                                    # today
        (-3.0, (now - dashboard_module.timedelta(days=1)).isoformat()),   # earlier this week (probably)
        (5.0, (now - dashboard_module.timedelta(days=40)).isoformat()),   # well before this month
    ]
    _seed_full_db(db_path, trades)
    monkeypatch.setattr(dashboard_module, "DB_PATH", str(db_path))
    client = _client(monkeypatch)

    response = client.get("/api/summary", headers=_auth_headers())

    assert response.status_code == 200
    body = response.get_json()
    # the legacy field still carries the deposit-inflated number, for backward compatibility
    assert body["cumulative_pnl"] == pytest.approx(675.09 + 12.0)
    # but the new trading-only figures must never include that deposit
    assert body["trading_pnl_today"] == pytest.approx(10.0)
    assert body["trading_pnl_all_time"] == pytest.approx(12.0)
    assert body["trading_pnl_all_time"] < 100  # sanity: nowhere near the deposit-polluted figure


def test_trading_pnl_today_boundary_uses_ist_not_utc(tmp_path):
    # "Now" is 2026-09-03 01:00 IST, which is still 2026-09-02 in UTC. IST's "today" started
    # at 2026-09-03 00:00 IST = 2026-09-02 18:30 UTC. A trade closed at 2026-09-02 19:00 UTC
    # (after that IST boundary, even though its UTC calendar date is still "Sep 2") must count
    # as today; one closed at 2026-09-02 17:00 UTC (before the IST boundary) must not.
    db_path = tmp_path / "live_state.db"
    _seed_full_db(db_path, [
        (7.0, "2026-09-02T19:00:00+00:00"),   # after IST midnight -> counts as today (IST)
        (-2.0, "2026-09-02T17:00:00+00:00"),  # before IST midnight -> still "yesterday" in IST
    ])
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    now_utc = dashboard_module.datetime(2026, 9, 2, 19, 30, tzinfo=dashboard_module.timezone.utc)
    windows = dashboard_module._trading_pnl_windows(conn, now_utc=now_utc)
    conn.close()

    assert windows["trading_pnl_today"] == pytest.approx(7.0)
    assert windows["trading_pnl_all_time"] == pytest.approx(5.0)


def test_equity_curve_ignores_deposit_polluted_cumulative_pnl(monkeypatch, tmp_path):
    db_path = tmp_path / "live_state.db"
    trades = [
        (10.0, "2026-01-01T01:00:00+00:00"),
        (-3.0, "2026-01-02T01:00:00+00:00"),
        (5.0, "2026-01-03T01:00:00+00:00"),
    ]
    _seed_full_db(db_path, trades)  # cumulative_pnl seeded as 675.09 + sum(pnls) -- deposit-polluted
    monkeypatch.setattr(dashboard_module, "DB_PATH", str(db_path))
    client = _client(monkeypatch)

    response = client.get("/api/equity-curve", headers=_auth_headers())

    assert response.status_code == 200
    points = response.get_json()
    assert len(points) == 4  # implied start + one per trade
    # the curve must end exactly at current equity (793.37, per _seed_full_db) --
    # not shifted down by the 675.09 "deposit" folded into cumulative_pnl
    assert points[-1]["equity"] == pytest.approx(793.37)
    assert points[0]["equity"] == pytest.approx(793.37 - sum(p for p, _ in trades))


def test_export_trades_csv_handles_empty_history(monkeypatch, tmp_path):
    db_path = tmp_path / "live_state.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE trade_history (
            id INTEGER PRIMARY KEY, symbol TEXT, side TEXT, entry_price REAL, exit_price REAL,
            quantity REAL, pnl REAL, exit_reason TEXT, opened_at TEXT, closed_at TEXT, fees REAL
        )"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(dashboard_module, "DB_PATH", str(db_path))
    client = _client(monkeypatch)

    response = client.get("/api/trades/export.csv", headers=_auth_headers())

    assert response.status_code == 200
    assert response.get_data(as_text=True) == ""
