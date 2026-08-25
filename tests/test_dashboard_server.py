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
