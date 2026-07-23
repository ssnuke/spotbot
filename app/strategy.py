from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class TAEStrategyConfig:
    short_window: int = 5
    long_window: int = 20
    value_window: int = 24
    value_zone_pct: float = 0.03
    reversal_lookback: int = 2
    signal_threshold_pct: float = 0.001
    min_trend_distance_pct: float = 0.001
    min_signal_score: float = 0.45


@dataclass
class Signal:
    symbol: str
    side: str
    confidence: float


class BaseStrategy:
    @property
    def warmup_period(self) -> int:
        return 0

    def generate_signal(self, prices: List[float], symbol: str = "BTC/USDT") -> Signal | None:
        raise NotImplementedError


class TAEStrategy:
    def __init__(self, config: TAEStrategyConfig | None = None):
        cfg = config or TAEStrategyConfig()
        self.short_window = cfg.short_window
        self.long_window = cfg.long_window
        self.value_window = cfg.value_window
        self.value_zone_pct = cfg.value_zone_pct
        self.reversal_lookback = cfg.reversal_lookback
        self.signal_threshold_pct = cfg.signal_threshold_pct
        self.min_trend_distance_pct = cfg.min_trend_distance_pct
        self.min_signal_score = cfg.min_signal_score

    def _moving_average(self, prices: List[float]) -> float:
        return sum(prices) / len(prices)

    def _ema(self, prices: List[float], period: int) -> float:
        if not prices:
            return 0.0
        if period <= 1:
            return float(prices[-1])
        multiplier = 2.0 / (period + 1)
        ema = float(prices[0])
        for price in prices[1:]:
            ema = (price * multiplier) + (ema * (1 - multiplier))
        return ema

    def _trend_direction(self, prices: List[float]) -> str | None:
        if len(prices) < self.long_window + 1:
            return None

        short_avg = self._moving_average(prices[-self.short_window :])
        long_avg = self._moving_average(prices[-self.long_window - 1 : -1])
        short_ema = self._ema(prices[-self.short_window :], self.short_window)
        long_ema = self._ema(prices[-self.long_window - 1 : -1], self.long_window)
        if short_avg >= long_avg * (1 + self.min_trend_distance_pct) or short_ema >= long_ema * (1 + self.min_trend_distance_pct):
            return "up"
        if short_avg <= long_avg * (1 - self.min_trend_distance_pct) or short_ema <= long_ema * (1 - self.min_trend_distance_pct):
            return "down"
        return None

    def _swing_zone(self, prices: List[float]) -> tuple[float, float]:
        window = prices[-self.value_window :]
        return min(window), max(window)

    def _pullback_distance(self, current_price: float, support: float, resistance: float, side: str) -> float:
        if side == "buy":
            return (current_price - support) / support if support > 0 else 0.0
        return (resistance - current_price) / resistance if resistance > 0 else 0.0

    def _reversal_trigger(self, prices: List[float], side: str) -> bool:
        recent = prices[-(self.reversal_lookback + 1) :]
        if len(recent) < 3:
            return False
        if side == "buy":
            return recent[-1] >= recent[-2] and recent[-2] >= min(recent[:-2])
        return recent[-1] <= recent[-2] and recent[-2] <= max(recent[:-2])

    def generate_signal(self, prices: List[float], symbol: str = "BTC/USDT") -> Signal | None:
        if len(prices) < max(self.long_window, self.value_window) + self.reversal_lookback + 1:
            return None

        trend = self._trend_direction(prices)
        if trend is None:
            return None

        support, resistance = self._swing_zone(prices)
        current_price = prices[-1]
        recent_change = (current_price - prices[-2]) / prices[-2] if prices[-2] else 0.0

        if trend == "up":
            pullback = self._pullback_distance(current_price, support, resistance, "buy")
            if current_price >= self._ema(prices[-self.short_window :], self.short_window):
                score = 0.0
                if pullback <= self.value_zone_pct:
                    score += 0.45
                if self._reversal_trigger(prices, "buy"):
                    score += 0.35
                if current_price > prices[-2] or recent_change >= -self.signal_threshold_pct:
                    score += 0.20
                if score >= self.min_signal_score:
                    return Signal(symbol=symbol, side="buy", confidence=round(min(0.95, 0.6 + score * 0.3), 2))

        if trend == "down":
            pullback = self._pullback_distance(current_price, support, resistance, "sell")
            if current_price <= self._ema(prices[-self.short_window :], self.short_window):
                score = 0.0
                if pullback <= self.value_zone_pct:
                    score += 0.45
                if self._reversal_trigger(prices, "sell"):
                    score += 0.35
                if current_price < prices[-2] or recent_change <= self.signal_threshold_pct:
                    score += 0.20
                if score >= self.min_signal_score:
                    return Signal(symbol=symbol, side="sell", confidence=round(min(0.95, 0.6 + score * 0.3), 2))

        return None

    @property
    def warmup_period(self) -> int:
        return max(self.long_window, self.value_window) + self.reversal_lookback + 1


class MomentumStrategy(BaseStrategy):
    def __init__(self, lookback: int = 3, momentum_pct: float = 0.001):
        self.lookback = lookback
        self.momentum_pct = momentum_pct

    @property
    def warmup_period(self) -> int:
        return self.lookback + 1

    def generate_signal(self, prices: List[float], symbol: str = "BTC/USDT") -> Signal | None:
        if len(prices) < self.warmup_period:
            return None

        current_price = prices[-1]
        previous_price = prices[-self.warmup_period]
        price_change = current_price - previous_price
        threshold = previous_price * self.momentum_pct

        if price_change >= threshold:
            return Signal(symbol=symbol, side="buy", confidence=0.6)
        if price_change <= -threshold:
            return Signal(symbol=symbol, side="sell", confidence=0.6)
        return None
