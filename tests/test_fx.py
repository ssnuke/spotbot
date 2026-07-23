import app.fx as fx


def test_get_usd_inr_rate_caches_and_avoids_repeat_calls(monkeypatch):
    fx._cached_rate = None
    fx._cached_at = 0.0

    call_count = 0

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"rates": {"INR": 83.25}}

    def fake_get(url, params=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return FakeResponse()

    monkeypatch.setattr(fx.requests, "get", fake_get)

    rate1 = fx.get_usd_inr_rate()
    rate2 = fx.get_usd_inr_rate()

    assert rate1 == 83.25
    assert rate2 == 83.25
    assert call_count == 1  # second call served from cache, no new request


def test_get_usd_inr_rate_falls_back_to_stale_cache_on_error(monkeypatch):
    fx._cached_rate = 82.0
    fx._cached_at = 0.0  # force expiry so a fresh fetch is attempted

    def fake_get(url, params=None, timeout=None):
        raise fx.requests.RequestException("network down")

    monkeypatch.setattr(fx.requests, "get", fake_get)

    assert fx.get_usd_inr_rate() == 82.0


def test_get_usd_inr_rate_returns_none_with_no_cache_and_error(monkeypatch):
    fx._cached_rate = None
    fx._cached_at = 0.0

    def fake_get(url, params=None, timeout=None):
        raise fx.requests.RequestException("network down")

    monkeypatch.setattr(fx.requests, "get", fake_get)

    assert fx.get_usd_inr_rate() is None
