from unittest.mock import MagicMock, patch

import pytest

from app.binance_futures_client import BinanceFuturesClient


def _make_client():
    return BinanceFuturesClient(api_key="key", api_secret="secret", base_url="https://fapi.binance.com")


class _FakeExchangeInfoResponse:
    def __init__(self, symbols):
        self._symbols = symbols

    def raise_for_status(self):
        pass

    def json(self):
        return {"symbols": self._symbols}


def _symbol_entry(symbol, step_size, tick_size, min_notional):
    return {
        "symbol": symbol,
        "filters": [
            {"filterType": "LOT_SIZE", "stepSize": str(step_size)},
            {"filterType": "PRICE_FILTER", "tickSize": str(tick_size)},
            {"filterType": "MIN_NOTIONAL", "notional": str(min_notional)},
        ],
    }


def test_get_symbol_filters_matches_requested_symbol_not_first_in_list():
    # Regression test: Binance's futures exchangeInfo endpoint ignores the `symbol` query
    # param and always returns every symbol -- blindly taking the first entry silently returns
    # a DIFFERENT symbol's filters (this caused every real SOLUSDT order to be rounded to
    # BTCUSDT's precision and rejected). Deliberately puts the requested symbol NOT first.
    symbols = [
        _symbol_entry("BTCUSDT", step_size=0.001, tick_size=0.1, min_notional=50.0),
        _symbol_entry("ETHUSDT", step_size=0.001, tick_size=0.01, min_notional=20.0),
        _symbol_entry("SOLUSDT", step_size=0.01, tick_size=0.01, min_notional=5.0),
    ]
    client = _make_client()
    with patch("app.binance_futures_client.requests.get", return_value=_FakeExchangeInfoResponse(symbols)):
        filters = client.get_symbol_filters("SOL/USDT")

    assert filters.step_size == 0.01
    assert filters.tick_size == 0.01
    assert filters.min_notional == 5.0


def test_get_symbol_filters_raises_clearly_for_unknown_symbol():
    symbols = [_symbol_entry("BTCUSDT", step_size=0.001, tick_size=0.1, min_notional=50.0)]
    client = _make_client()
    with patch("app.binance_futures_client.requests.get", return_value=_FakeExchangeInfoResponse(symbols)):
        with pytest.raises(ValueError, match="SOLUSDT"):
            client.get_symbol_filters("SOL/USDT")


def test_get_symbol_filters_does_not_rely_on_server_side_symbol_filtering():
    # The request must not depend on the `symbol` query param actually working server-side
    # (it doesn't, on the real endpoint) -- confirms the fix fetches the full list and filters
    # client-side instead of trusting the server to have narrowed it down.
    symbols = [_symbol_entry("SOLUSDT", step_size=0.01, tick_size=0.01, min_notional=5.0)]
    client = _make_client()
    mock_get = MagicMock(return_value=_FakeExchangeInfoResponse(symbols))
    with patch("app.binance_futures_client.requests.get", mock_get):
        client.get_symbol_filters("SOL/USDT")
    mock_get.assert_called_once()
    assert mock_get.call_args.kwargs.get("params") is None


def test_get_wallet_balance_reads_total_wallet_balance():
    client = _make_client()
    account_info = {"totalWalletBalance": "127.6543", "totalUnrealizedProfit": "9.99"}
    with patch.object(client, "get_account_info", return_value=account_info):
        balance = client.get_wallet_balance()
    assert balance == pytest.approx(127.6543)
