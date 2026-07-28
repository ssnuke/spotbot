from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import requests


@dataclass
class SymbolFilters:
    step_size: float
    tick_size: float
    min_notional: float


def round_step_size(quantity: float, step_size: float) -> float:
    """Round quantity down to the exchange's allowed lot-size precision."""
    if step_size <= 0:
        return quantity
    precision = max(0, -_exponent(step_size))
    steps = int(quantity / step_size)
    return round(steps * step_size, precision)


def _exponent(value: float) -> int:
    text = f"{value:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return -len(text.split(".")[1])


class BinanceSpotClient:
    """Signed REST client for Binance's Spot API surface. Binance Spot Testnet mirrors
    the production API exactly, so this same client works against either -- only the
    base URL and credentials differ, which is what BinanceTestnetClient/BinanceLiveClient
    below configure."""

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _signed_request(self, method: str, path: str, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        signature = self._sign(params)
        url = f"{self.base_url}{path}"
        headers = {"X-MBX-APIKEY": self.api_key}
        response = requests.request(
            method, url, headers=headers, params={**params, "signature": signature}, timeout=10
        )
        response.raise_for_status()
        return response.json()

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        url = f"{self.base_url}/api/v3/exchangeInfo"
        response = requests.get(url, params={"symbol": symbol.upper().replace("/", "")}, timeout=10)
        response.raise_for_status()
        payload = response.json()
        symbol_info = payload["symbols"][0]

        step_size = 1.0
        tick_size = 0.01
        min_notional = 0.0
        for f in symbol_info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
            elif f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = float(f.get("minNotional", f.get("notional", 0.0)))
        return SymbolFilters(step_size=step_size, tick_size=tick_size, min_notional=min_notional)

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict:
        params = {
            "symbol": symbol.upper().replace("/", ""),
            "side": side.upper(),
            "type": "MARKET",
            "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
        }
        return self._signed_request("POST", "/api/v3/order", params)

    def get_account(self) -> dict:
        return self._signed_request("GET", "/api/v3/account", {})

    def get_api_key_permissions(self) -> dict:
        """Returns what THIS SPECIFIC API KEY is scoped to do (enableWithdrawals,
        enableSpotAndMarginTrading, etc.) -- distinct from get_account()'s canTrade/canWithdraw,
        which reflect the ACCOUNT's overall status, not any particular key's permissions."""
        return self._signed_request("GET", "/sapi/v1/account/apiRestrictions", {})


class BinanceTestnetClient(BinanceSpotClient):
    """Signed REST client for Binance's public Spot Testnet (fake funds, real exchange matching)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        api_key = api_key or os.getenv("BINANCE_API_KEY")
        api_secret = api_secret or os.getenv("BINANCE_API_SECRET")
        base_url = base_url or os.getenv("BINANCE_TESTNET_BASE_URL", "https://testnet.binance.vision")
        if not api_key or not api_secret:
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET must be set (generate a free key at "
                "https://testnet.binance.vision/)"
            )
        super().__init__(api_key, api_secret, base_url)


class BinanceLiveClient(BinanceSpotClient):
    """Signed REST client for Binance's production Spot exchange -- REAL funds, REAL orders.
    Deliberately reads separate BINANCE_LIVE_* env vars (not the BINANCE_API_KEY/SECRET used
    for testnet) so a .env mistake can't point real money at the wrong credentials."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        api_key = api_key or os.getenv("BINANCE_LIVE_API_KEY")
        api_secret = api_secret or os.getenv("BINANCE_LIVE_API_SECRET")
        base_url = base_url or os.getenv("BINANCE_LIVE_BASE_URL", "https://api.binance.com")
        if not api_key or not api_secret:
            raise ValueError(
                "BINANCE_LIVE_API_KEY and BINANCE_LIVE_API_SECRET must be set to trade with "
                "real funds on Binance. Create a trading-only API key (withdrawals disabled) "
                "in your Binance account settings."
            )
        super().__init__(api_key, api_secret, base_url)

    def assert_safe_to_trade(self) -> None:
        """Refuses to proceed unless the account can trade and, critically, this specific API
        key cannot withdraw. A real-money bot should never hold a withdrawal-capable key -- if
        this key is scoped too broadly, fail loudly at startup rather than trade with it anyway.

        Two separate Binance endpoints are needed: /api/v3/account's canTrade reflects the
        ACCOUNT's overall status (e.g. compliance holds), while /sapi/v1/account/apiRestrictions'
        enableWithdrawals/enableSpotAndMarginTrading reflect what THIS KEY is actually scoped to
        do -- account-level canWithdraw being true does not mean this key can withdraw."""
        account = self.get_account()
        if not account.get("canTrade", False):
            raise ValueError("This Binance account cannot trade right now (restricted or suspended).")

        permissions = self.get_api_key_permissions()
        if not permissions.get("enableSpotAndMarginTrading", False):
            raise ValueError("This Binance API key does not have Spot & Margin trading permission enabled.")
        if permissions.get("enableWithdrawals", False):
            raise ValueError(
                "This Binance API key has WITHDRAWAL permission enabled -- refusing to start. "
                "Create a key with trading permission only, withdrawals disabled, for live trading."
            )
