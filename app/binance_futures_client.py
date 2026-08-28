from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests

from app.binance_client import BinanceSpotClient

# Binance error codes returned when a leverage/margin-type/position-mode setting is
# requested but already matches the account's current state -- treated as success, not
# a failure, since the desired end state is already true.
_ALREADY_SET_ERROR_CODES = {-4046, -4059}


@dataclass
class FuturesSymbolFilters:
    step_size: float
    tick_size: float
    min_notional: float


class BinanceFuturesClient:
    """Signed REST client for Binance's USDT-M Futures API surface (`/fapi`). Testnet mirrors
    production closely, so this same client works against either -- only the base URL and
    credentials differ, configured by BinanceFuturesTestnetClient/BinanceFuturesLiveClient below.
    Reuses BinanceSpotClient purely for its HMAC-SHA256 signing helper (identical scheme across
    Binance's spot/futures/sapi surfaces) -- no spot-specific endpoints are called through it."""

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self._signer = BinanceSpotClient(api_key, api_secret, base_url)

    def _signed_request(self, method: str, path: str, params: Optional[dict] = None) -> dict:
        try:
            return self._signer._signed_request(method, path, params or {})
        except requests.HTTPError as exc:
            body = self._error_body(exc)
            if body and body.get("code") in _ALREADY_SET_ERROR_CODES:
                return body
            raise

    @staticmethod
    def _error_body(exc: requests.HTTPError) -> Optional[dict]:
        response = exc.response
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def set_leverage(self, symbol: str, leverage: float) -> dict:
        params = {"symbol": symbol.upper().replace("/", ""), "leverage": int(leverage)}
        return self._signed_request("POST", "/fapi/v1/leverage", params)

    def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        params = {"symbol": symbol.upper().replace("/", ""), "marginType": margin_type.upper()}
        return self._signed_request("POST", "/fapi/v1/marginType", params)

    def set_position_mode(self, dual_side: bool = False) -> dict:
        params = {"dualSidePosition": "true" if dual_side else "false"}
        return self._signed_request("POST", "/fapi/v1/positionSide/dual", params)

    def get_position_mode(self) -> dict:
        return self._signed_request("GET", "/fapi/v1/positionSide/dual", {})

    def get_mark_price(self, symbol: str) -> Optional[float]:
        url = f"{self.base_url}/fapi/v1/premiumIndex"
        try:
            response = requests.get(url, params={"symbol": symbol.upper().replace("/", "")}, timeout=10)
            response.raise_for_status()
            return float(response.json()["markPrice"])
        except (requests.RequestException, KeyError, ValueError, TypeError):
            return None

    def get_position_risk(self, symbol: str) -> Optional[dict]:
        """Returns the position-risk entry for `symbol` (mark price, entry price,
        liquidation price, position amount, notional, isolated margin), or None if the
        account has no open position on it."""
        params = {"symbol": symbol.upper().replace("/", "")}
        payload = self._signed_request("GET", "/fapi/v3/positionRisk", params)
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if float(entry.get("positionAmt", 0.0) or 0.0) != 0.0:
                return entry
        return entries[0] if entries else None

    def get_account_info(self) -> dict:
        return self._signed_request("GET", "/fapi/v3/account", {})

    def get_wallet_balance(self) -> float:
        """USDT-M futures wallet balance: cumulative realized PnL plus deposits minus
        withdrawals, fees, and funding -- excludes unrealized PnL on open positions. This is
        the real, exchange-side realized-equity figure, on the same realized-only basis as
        RiskManager.equity -- used to correct that self-tracked figure when it drifts from
        what the account actually has (see LiveRunner._reconcile_equity_with_exchange)."""
        return float(self.get_account_info()["totalWalletBalance"])

    def get_symbol_filters(self, symbol: str) -> FuturesSymbolFilters:
        # Unlike spot's /api/v3/exchangeInfo, the futures endpoint does NOT filter by the
        # `symbol` query param -- it always returns all ~900 symbols regardless. Blindly taking
        # `symbols[0]` (as this used to) silently returns whatever symbol happens to be first
        # in Binance's list (BTCUSDT), not the requested one -- a real production incident where
        # every SOLUSDT order was rounded to BTC's quantity precision and rejected. Must search
        # the returned list for the actual match.
        symbol_key = symbol.upper().replace("/", "")
        url = f"{self.base_url}/fapi/v1/exchangeInfo"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        payload = response.json()
        symbol_info = next((s for s in payload["symbols"] if s["symbol"] == symbol_key), None)
        if symbol_info is None:
            raise ValueError(f"Symbol {symbol_key} not found in futures exchangeInfo")

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
        return FuturesSymbolFilters(step_size=step_size, tick_size=tick_size, min_notional=min_notional)

    def place_market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
        params = {
            "symbol": symbol.upper().replace("/", ""),
            "side": side.upper(),
            "type": "MARKET",
            "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
            "reduceOnly": "true" if reduce_only else "false",
            # Explicit, not left to Binance's default: the lighter "ACK" response shape can
            # omit or zero out avgPrice/executedQty, which is exactly the data
            # LiveRunner._extract_futures_fill() needs for real fill price and fee -- without
            # this, a live account can silently fall back to reporting $0.00 fees on every
            # real futures trade with no visible error anywhere.
            "newOrderRespType": "RESULT",
        }
        return self._signed_request("POST", "/fapi/v1/order", params)

    def cancel_all_open_orders(self, symbol: str) -> dict:
        params = {"symbol": symbol.upper().replace("/", "")}
        return self._signed_request("DELETE", "/fapi/v1/allOpenOrders", params)

    def close_position_market(self, symbol: str, position_side: str, quantity: float) -> dict:
        """Market-closes an open position: position_side is the side of the OPEN position
        ("buy"/"sell"), and the closing order is placed on the opposite side, reduce-only."""
        close_side = "SELL" if position_side.lower() == "buy" else "BUY"
        return self.place_market_order(symbol, close_side, quantity, reduce_only=True)

    def get_funding_payments(self, symbol: str, start_time_ms: int) -> list:
        params = {
            "symbol": symbol.upper().replace("/", ""),
            "incomeType": "FUNDING_FEE",
            "startTime": start_time_ms,
        }
        payload = self._signed_request("GET", "/fapi/v1/income", params)
        return payload if isinstance(payload, list) else []

    def get_realized_pnl_income(self, symbol: str, start_time_ms: int) -> list:
        """Binance's own realized-PnL ledger, one entry per closing fill -- computed using
        the exchange's real (blended, in one-way mode) position accounting. Used as a
        defense-in-depth cross-check for this bot's self-tracked daily_loss figure."""
        params = {
            "symbol": symbol.upper().replace("/", ""),
            "incomeType": "REALIZED_PNL",
            "startTime": start_time_ms,
        }
        payload = self._signed_request("GET", "/fapi/v1/income", params)
        return payload if isinstance(payload, list) else []


class BinanceFuturesTestnetClient(BinanceFuturesClient):
    """Signed REST client for Binance's public USDT-M Futures Testnet (fake funds, real
    exchange matching). Reuses the same BINANCE_API_KEY/SECRET env vars as the spot testnet
    client -- both are free testnet-only keys, not real-money credentials."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        api_key = api_key or os.getenv("BINANCE_API_KEY")
        api_secret = api_secret or os.getenv("BINANCE_API_SECRET")
        base_url = base_url or os.getenv("BINANCE_FUTURES_TESTNET_BASE_URL", "https://testnet.binancefuture.com")
        if not api_key or not api_secret:
            raise ValueError(
                "BINANCE_API_KEY and BINANCE_API_SECRET must be set (generate a free key at "
                "https://testnet.binancefuture.com/)"
            )
        super().__init__(api_key, api_secret, base_url)


class BinanceFuturesLiveClient(BinanceFuturesClient):
    """Signed REST client for Binance's production USDT-M Futures exchange -- REAL funds,
    REAL leveraged orders. Deliberately reads separate BINANCE_FUTURES_LIVE_* env vars (not
    the spot-live or futures-testnet vars) so a .env mistake can't point real leveraged
    orders at the wrong credentials."""

    # /sapi/* endpoints (including API key permissions) live on the main account API host
    # regardless of which product (spot/futures) the key trades -- never fapi.binance.com.
    _ACCOUNT_API_BASE_URL = "https://api.binance.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        api_key = api_key or os.getenv("BINANCE_FUTURES_LIVE_API_KEY")
        api_secret = api_secret or os.getenv("BINANCE_FUTURES_LIVE_API_SECRET")
        base_url = base_url or os.getenv("BINANCE_FUTURES_LIVE_BASE_URL", "https://fapi.binance.com")
        if not api_key or not api_secret:
            raise ValueError(
                "BINANCE_FUTURES_LIVE_API_KEY and BINANCE_FUTURES_LIVE_API_SECRET must be set to "
                "trade real futures funds. Create a futures-only API key (withdrawals disabled) "
                "in your Binance account settings."
            )
        super().__init__(api_key, api_secret, base_url)
        self._account_client = BinanceSpotClient(api_key, api_secret, self._ACCOUNT_API_BASE_URL)

    def assert_safe_to_trade(self) -> list[str]:
        """Refuses to proceed unless this API key is scoped for futures-only trading with
        withdrawals disabled. Returns a list of non-blocking warnings (e.g. IP restriction
        not enabled) the caller should surface but need not refuse to start over.

        NOTE: whether this key belongs to the account's main login (vs. a clearly-labelled
        sub-account) cannot be verified via any Binance API field -- that must be confirmed
        by the operator when the key is created, not by this check."""
        permissions = self._account_client.get_api_key_permissions()

        if not permissions.get("enableFutures", False):
            raise ValueError("This Binance API key does not have Futures trading permission enabled.")
        if permissions.get("enableWithdrawals", False):
            raise ValueError(
                "This Binance API key has WITHDRAWAL permission enabled -- refusing to start. "
                "Create a key with futures trading permission only, withdrawals disabled."
            )
        if permissions.get("enableSpotAndMarginTrading", False):
            raise ValueError(
                "This Binance API key also has Spot & Margin trading permission enabled -- "
                "refusing to start. Use a futures-only key so a bug here can never touch spot "
                "holdings."
            )

        warnings = []
        if not permissions.get("ipRestrict", False):
            warnings.append(
                "This API key does not have IP restriction enabled. Strongly recommended for "
                "a key used with real futures funds -- confirm manually before trading."
            )
        warnings.append(
            "Cannot verify programmatically that this key belongs to a non-main account -- "
            "confirm manually that this is not your main account's API key."
        )
        return warnings
