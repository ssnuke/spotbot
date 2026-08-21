from __future__ import annotations

from dataclasses import dataclass
from typing import List, Literal, Optional

# "up"/"down" identify the trend/pullback direction being evaluated; each maps to a
# long ("buy") or short ("sell") Signal.side once a direction clears the score threshold.
Direction = Literal["up", "down"]

_SIDE_BY_DIRECTION: dict[Direction, str] = {"up": "buy", "down": "sell"}


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
    long_only: bool = False


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
    """Trend / Area / Entry pullback strategy.

    Fully symmetric between longs and shorts: every directional check (trend, pullback
    zone, reversal candle, momentum, short-EMA alignment) is implemented once and takes
    a `direction` ("up" or "down") argument, rather than duplicating logic per side. An
    uptrend looks for pullbacks into support and mirrors a downtrend's pullbacks into
    resistance with identical scoring weights and threshold.
    """

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
        self.long_only = cfg.long_only

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

    def _trend_direction(self, prices: List[float]) -> Optional[Direction]:
        """Symmetric by construction: "up" and "down" require the same margin
        (min_trend_distance_pct) on either the SMA or EMA short/long comparison."""
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
        """Recent swing support/resistance, shared by both directions -- an uptrend
        measures pullback distance from `support`, a downtrend from `resistance`."""
        window = prices[-self.value_window :]
        return min(window), max(window)

    def _pullback_distance(self, current_price: float, support: float, resistance: float, direction: Direction) -> float:
        """How far price has pulled back into the value zone: distance above support
        for an uptrend, distance below resistance for a downtrend -- mirrored math."""
        if direction == "up":
            return (current_price - support) / support if support > 0 else 0.0
        return (resistance - current_price) / resistance if resistance > 0 else 0.0

    def _reversal_trigger(self, prices: List[float], direction: Direction) -> bool:
        """A reversal candle off a local extreme: ticking up from a local low for an
        uptrend, or down from a local high for a downtrend."""
        recent = prices[-(self.reversal_lookback + 1) :]
        if len(recent) < 3:
            return False
        if direction == "up":
            return recent[-1] >= recent[-2] and recent[-2] >= min(recent[:-2])
        return recent[-1] <= recent[-2] and recent[-2] <= max(recent[:-2])

    def _momentum_confirms(self, current_price: float, previous_price: float, recent_change: float, direction: Direction) -> bool:
        """Price is still moving with the trend, or any move against it is within
        signal_threshold_pct noise -- mirrored inequality per direction."""
        if direction == "up":
            return current_price > previous_price or recent_change >= -self.signal_threshold_pct
        return current_price < previous_price or recent_change <= self.signal_threshold_pct

    def _aligned_with_short_ema(self, current_price: float, short_ema: float, direction: Direction) -> bool:
        """Price must be on the trend's side of the short EMA before scoring even
        starts: at/above it for an uptrend, at/below it for a downtrend."""
        if direction == "up":
            return current_price >= short_ema
        return current_price <= short_ema

    def _score(self, prices: List[float], current_price: float, support: float, resistance: float, direction: Direction) -> float:
        """Same three weighted sub-conditions for both directions: value-zone pullback
        (0.45, the strongest contributor), reversal candle (0.35), momentum (0.20)."""
        score = 0.0
        pullback = self._pullback_distance(current_price, support, resistance, direction)
        if pullback <= self.value_zone_pct:
            score += 0.45
        if self._reversal_trigger(prices, direction):
            score += 0.35

        previous_price = prices[-2]
        recent_change = (current_price - previous_price) / previous_price if previous_price else 0.0
        if self._momentum_confirms(current_price, previous_price, recent_change, direction):
            score += 0.20

        return score

    def generate_signal(self, prices: List[float], symbol: str = "BTC/USDT") -> Signal | None:
        if len(prices) < max(self.long_window, self.value_window) + self.reversal_lookback + 1:
            return None

        direction = self._trend_direction(prices)
        if direction is None:
            return None
        if direction == "down" and self.long_only:
            return None

        current_price = prices[-1]
        short_ema = self._ema(prices[-self.short_window :], self.short_window)
        if not self._aligned_with_short_ema(current_price, short_ema, direction):
            return None

        support, resistance = self._swing_zone(prices)
        score = self._score(prices, current_price, support, resistance, direction)
        if score < self.min_signal_score:
            return None

        confidence = round(min(0.95, 0.6 + score * 0.3), 2)
        return Signal(symbol=symbol, side=_SIDE_BY_DIRECTION[direction], confidence=confidence)

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
