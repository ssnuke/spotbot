from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional


def _parse_datetime(candle) -> datetime:
    timestamp = candle[0]
    if isinstance(timestamp, str):
        return datetime.fromisoformat(timestamp)
    if isinstance(timestamp, (int, float)):
        value = int(timestamp)
        if len(str(value)) == 13:
            value //= 1000
        return datetime.fromtimestamp(value)
    raise ValueError("Unsupported candle timestamp format")


class HTFTrendSeries:
    """For every base-timeframe (e.g. 5m) candle, tracks the higher-timeframe (e.g.
    1H/4H) EMA trend direction, using only fully COMPLETED higher-timeframe bars —
    the currently-forming higher-timeframe bar is never used, so there is no lookahead."""

    def __init__(self, candles: list, bucket_hours: int, short_period: int = 9, long_period: int = 21):
        self.bucket_hours = bucket_hours
        self.short_period = short_period
        self.long_period = long_period
        self.trend_by_index: List[Optional[str]] = []
        self._compute(candles)

    def _bucket_key(self, dt: datetime) -> datetime:
        bucket_start_hour = (dt.hour // self.bucket_hours) * self.bucket_hours
        return dt.replace(hour=bucket_start_hour, minute=0, second=0, microsecond=0)

    def _compute(self, candles: list) -> None:
        short_mult = 2.0 / (self.short_period + 1)
        long_mult = 2.0 / (self.long_period + 1)
        ema_short: Optional[float] = None
        ema_long: Optional[float] = None
        completed_bars = 0
        current_bucket_key: Optional[datetime] = None
        current_bucket_close: Optional[float] = None
        current_trend: Optional[str] = None

        for candle in candles:
            dt = _parse_datetime(candle)
            close = float(candle[4])
            bucket_key = self._bucket_key(dt)

            if current_bucket_key is None:
                current_bucket_key = bucket_key
            elif bucket_key != current_bucket_key:
                completed_close = current_bucket_close
                completed_bars += 1
                if ema_short is None:
                    ema_short = completed_close
                    ema_long = completed_close
                else:
                    ema_short = completed_close * short_mult + ema_short * (1 - short_mult)
                    ema_long = completed_close * long_mult + ema_long * (1 - long_mult)

                if completed_bars >= self.long_period:
                    if ema_short > ema_long:
                        current_trend = "up"
                    elif ema_short < ema_long:
                        current_trend = "down"
                    else:
                        current_trend = None

                current_bucket_key = bucket_key

            current_bucket_close = close
            self.trend_by_index.append(current_trend)

    def trend_at(self, index: int) -> Optional[str]:
        return self.trend_by_index[index]


@dataclass
class RegimeFilterConfig:
    enabled: bool = False
    mode: str = "both"  # "both", "1h_only", "4h_only", "either"
    short_period: int = 9
    long_period: int = 21


class RegimeFilter:
    """Gates a signal on whether the higher-timeframe (1H/4H) trend agrees with the
    signal's direction, so entries taken against a strong opposing higher-timeframe
    trend get rejected before ever reaching the risk manager."""

    def __init__(self, candles: list, config: RegimeFilterConfig):
        self.config = config
        if config.enabled:
            self.htf_1h = HTFTrendSeries(candles, bucket_hours=1, short_period=config.short_period, long_period=config.long_period)
            self.htf_4h = HTFTrendSeries(candles, bucket_hours=4, short_period=config.short_period, long_period=config.long_period)
        else:
            self.htf_1h = None
            self.htf_4h = None

    def allows(self, index: int, side: str) -> bool:
        if not self.config.enabled:
            return True

        wanted = "up" if side == "buy" else "down"
        trend_1h = self.htf_1h.trend_at(index)
        trend_4h = self.htf_4h.trend_at(index)
        mode = self.config.mode

        if mode == "1h_only":
            return trend_1h == wanted
        if mode == "4h_only":
            return trend_4h == wanted
        if mode == "either":
            return trend_1h == wanted or trend_4h == wanted
        return trend_1h == wanted and trend_4h == wanted  # "both" (default)
