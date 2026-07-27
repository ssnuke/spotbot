import hashlib
import hmac

import pytest

from app.binance_client import BinanceLiveClient, BinanceTestnetClient, round_step_size


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


def test_live_client_requires_separate_live_credentials(monkeypatch):
    # Live credentials are deliberately separate env vars from testnet's, so setting only
    # the testnet ones must NOT be enough to construct a live (real-money) client.
    monkeypatch.delenv("BINANCE_LIVE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_LIVE_API_SECRET", raising=False)
    monkeypatch.setenv("BINANCE_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "testnet-secret")
    with pytest.raises(ValueError):
        BinanceLiveClient()


def test_live_client_defaults_to_production_base_url(monkeypatch):
    monkeypatch.delenv("BINANCE_LIVE_BASE_URL", raising=False)
    client = BinanceLiveClient(api_key="key", api_secret="secret")
    assert client.base_url == "https://api.binance.com"


def test_assert_safe_to_trade_passes_for_trade_only_key(monkeypatch):
    client = BinanceLiveClient(api_key="key", api_secret="secret")
    monkeypatch.setattr(client, "get_account", lambda: {"canTrade": True, "canWithdraw": False})
    client.assert_safe_to_trade()  # should not raise


def test_assert_safe_to_trade_rejects_withdrawal_enabled_key(monkeypatch):
    client = BinanceLiveClient(api_key="key", api_secret="secret")
    monkeypatch.setattr(client, "get_account", lambda: {"canTrade": True, "canWithdraw": True})
    with pytest.raises(ValueError, match="WITHDRAWAL"):
        client.assert_safe_to_trade()


def test_assert_safe_to_trade_rejects_key_without_trade_permission(monkeypatch):
    client = BinanceLiveClient(api_key="key", api_secret="secret")
    monkeypatch.setattr(client, "get_account", lambda: {"canTrade": False, "canWithdraw": False})
    with pytest.raises(ValueError, match="trading permission"):
        client.assert_safe_to_trade()
