import hashlib
import hmac

import pytest

from app.binance_client import BinanceTestnetClient, round_step_size


def test_round_step_size_truncates_to_lot_precision():
    assert round_step_size(0.159894, 0.0001) == 0.1598
    assert round_step_size(1.0, 1.0) == 1.0
    assert round_step_size(0.00012345, 0.00001) == 0.00012


def test_round_step_size_never_rounds_up():
    # Rounding up could push an order over available capital/notional.
    assert round_step_size(0.19999, 0.1) == 0.1


def test_client_requires_api_credentials(monkeypatch):
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(ValueError):
        BinanceTestnetClient()


def test_signature_matches_expected_hmac():
    client = BinanceTestnetClient(api_key="key", api_secret="secret")
    params = {"symbol": "BTCUSDT", "timestamp": 123456}
    signature = client._sign(params)

    expected = hmac.new(b"secret", b"symbol=BTCUSDT&timestamp=123456", hashlib.sha256).hexdigest()
    assert signature == expected
