import sqlite3

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
