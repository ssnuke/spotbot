from __future__ import annotations

import os
from typing import Optional

import requests


class BinanceDataFeed:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or os.getenv("BINANCE_BASE_URL", "https://api.binance.com")

    def get_latest_price(self, symbol: str) -> Optional[float]:
        url = f"{self.base_url}/api/v3/ticker/price"
        params = {"symbol": symbol.upper().replace("/", "")}
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            return float(payload.get("price"))
        except requests.RequestException:
            return None

    def get_book_ticker(self, symbol: str) -> Optional[dict]:
        url = f"{self.base_url}/api/v3/ticker/bookTicker"
        params = {"symbol": symbol.upper().replace("/", "")}
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            return {"bid_price": float(payload["bidPrice"]), "ask_price": float(payload["askPrice"])}
        except (requests.RequestException, KeyError, ValueError, TypeError):
            return None

    def get_recent_candles(self, symbol: str, interval: str = "1m", limit: int = 50) -> list[list[float]]:
        url = f"{self.base_url}/api/v3/klines"
        params = {"symbol": symbol.upper().replace("/", ""), "interval": interval, "limit": limit}
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            return [item[:6] for item in payload]
        except requests.RequestException:
            return []

    def get_historical_klines(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 1000,
    ) -> list[list[float]]:
        url = f"{self.base_url}/api/v3/klines"
        symbol_key = symbol.upper().replace("/", "")
        all_klines: list[list[float]] = []
        current_start = start_time_ms

        while current_start < end_time_ms:
            params = {
                "symbol": symbol_key,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_time_ms,
                "limit": limit,
            }
            try:
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                payload = response.json()
            except requests.RequestException:
                break

            if not payload:
                break

            all_klines.extend(item[:6] for item in payload)
            last_open_time = int(payload[-1][0])
            if last_open_time <= current_start:
                break
            current_start = last_open_time + 1

        return all_klines
