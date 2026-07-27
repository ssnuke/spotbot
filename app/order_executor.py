from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

from app.binance_client import BinanceLiveClient, BinanceTestnetClient
from app.data_feed import BinanceDataFeed


class OrderExecutor(Protocol):
    def place_order(self, symbol: str, side: str, quantity: float, reference_price: float) -> dict: ...


@dataclass
class SimulatedOrderExecutor:
    """Fills orders locally with no exchange account and no real money. When a data_feed
    is supplied, fills cross the live bid/ask spread (buys fill at the ask, sells at the
    bid) like a real market order would, with slippage_pct layered on top for market
    impact beyond the quoted spread. Without a data_feed (e.g. backtesting-style use),
    falls back to applying slippage_pct directly to the reference price."""

    trade_fee_pct: float = 0.0005
    slippage_pct: float = 0.0005
    data_feed: Optional[BinanceDataFeed] = None

    def place_order(self, symbol: str, side: str, quantity: float, reference_price: float) -> dict:
        is_buy = side.upper() == "BUY"
        book = self.data_feed.get_book_ticker(symbol) if self.data_feed else None

        if book:
            base_price = book["ask_price"] if is_buy else book["bid_price"]
        else:
            base_price = reference_price

        fill_price = base_price * (1 + self.slippage_pct) if is_buy else base_price * (1 - self.slippage_pct)
        commission = quantity * fill_price * self.trade_fee_pct
        return {
            "orderId": "SIM",
            "fills": [{"qty": f"{quantity}", "price": f"{fill_price}", "commission": f"{commission}"}],
        }


@dataclass
class TestnetOrderExecutor:
    """Places real (fake-funds) orders against Binance's Spot Testnet matching engine."""

    client: BinanceTestnetClient

    def place_order(self, symbol: str, side: str, quantity: float, reference_price: float) -> dict:
        return self.client.place_market_order(symbol, side, quantity)


@dataclass
class LiveOrderExecutor:
    """Places REAL orders with REAL funds on Binance's production spot exchange."""

    client: BinanceLiveClient

    def place_order(self, symbol: str, side: str, quantity: float, reference_price: float) -> dict:
        return self.client.place_market_order(symbol, side, quantity)
