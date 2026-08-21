from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional


CONFIG_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "config.json")


@dataclass
class RiskConfigData:
    capital: float
    risk_per_trade_pct: float = 0.005
    reward_ratio: float = 2.0
    max_daily_loss_pct: float = 0.01
    max_drawdown_pct: float = 0.05
    max_trades_per_day: int = 3
    max_open_positions: int = 1
    max_position_pct: float = 0.1
    max_consecutive_losses: int = 0
    cooldown_period: int = 0
    min_entry_spacing_ticks: int = 0
    compounding_enabled: bool = False
    leverage: float = 1.0
    max_leverage: float = 5.0
    margin_type: str = "ISOLATED"
    liquidation_buffer_pct: float = 0.08
    max_position_notional: float = float("inf")
    maintenance_margin_rate: float = 0.004
    margin_ratio_warn_pct: float = 0.40
    margin_ratio_force_close_pct: float = 0.65


@dataclass
class BacktestConfigData:
    start_capital: float = 50000.0
    timeframe: str = "1m"
    risk_per_trade_pct: float = 0.003
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.05
    max_trades_per_day: int = 8
    max_open_positions: int = 3
    max_position_pct: float = 0.1
    max_consecutive_losses: int = 0
    cooldown_period: int = 0
    min_entry_spacing_ticks: int = 0
    compounding_enabled: bool = False
    reward_ratio: float = 2.0
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.02
    partial_exit_profit_pct: float = 0.01
    partial_exit_qty_pct: float = 0.5
    trailing_stop_pct: float = 0.005
    use_atr_stop: bool = False
    atr_period: int = 14
    atr_multiplier: float = 2.0
    trade_fee_pct: float = 0.0005
    slippage_pct: float = 0.0005
    htf_filter_enabled: bool = False
    htf_filter_mode: str = "both"
    htf_short_period: int = 9
    htf_long_period: int = 21
    long_only: bool = False  # skip sell/short signals -- required for real spot execution, which can't short
    leverage: float = 1.0
    max_leverage: float = 5.0
    margin_type: str = "ISOLATED"
    liquidation_buffer_pct: float = 0.08
    max_position_notional: float = float("inf")
    maintenance_margin_rate: float = 0.004
    funding_enabled: bool = False


@dataclass
class StrategyConfigData:
    short_window: int = 5
    long_window: int = 20
    value_window: int = 50
    value_zone_pct: float = 0.02
    reversal_lookback: int = 3
    signal_threshold_pct: float = 0.001
    min_trend_distance_pct: float = 0.002
    min_signal_score: float = 0.45


@dataclass
class LiveConfigData:
    timeframe: str = "5m"
    poll_interval_seconds: int = 300
    warmup_candles: int = 100
    stop_loss_pct: float = 0.01
    take_profit_pct: float = 0.03
    partial_exit_profit_pct: float = 0.01
    partial_exit_qty_pct: float = 0.5
    trailing_stop_pct: float = 0.005
    db_path: str = "live_state.db"
    execution_mode: str = "simulated"
    market_type: str = "spot"  # "spot" or "futures" (USDT-M, isolated margin, one-way mode)
    long_only: bool = False  # skip sell/short signals -- required for real spot execution, which can't short
    summary_interval_seconds: int = 3600
    command_poll_interval_seconds: int = 5


@dataclass
class AppConfig:
    risk: RiskConfigData
    backtest: BacktestConfigData
    strategy: StrategyConfigData
    live: LiveConfigData
    symbol: str
    csv_path: str
    download_if_missing: bool
    download_limit: int


def load_app_config(path: Optional[str] = None) -> AppConfig:
    config_path = path or os.path.abspath(CONFIG_PATH)
    with open(config_path, encoding="utf-8") as handle:
        data = json.load(handle)

    return AppConfig(
        risk=RiskConfigData(**data.get("risk", {})),
        backtest=BacktestConfigData(**data.get("backtest", {})),
        strategy=StrategyConfigData(**data.get("strategy", {})),
        live=LiveConfigData(**data.get("live", {})),
        symbol=data.get("symbol", "BTC/USDT"),
        csv_path=data.get("csv_path", "historical.csv"),
        download_if_missing=data.get("download_if_missing", False),
        download_limit=data.get("download_limit", 1000),
    )
