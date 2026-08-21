# Crypto Spot Trading Bot

A safety-first crypto spot trading bot scaffold with a modular architecture for paper trading and future live trading.

## Goals
- Define a strict risk model before any live execution.
- Support paper trading with live market data.
- Keep the design modular and easy to extend.

## Project structure
- app/: core bot modules
- tests/: unit tests
- requirements.txt: Python dependencies

## Quick start
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Order type
The initial implementation uses market orders for execution. This is the simplest and most practical choice for a first version, especially for a bot that needs to react quickly to signals.

## Backtesting approach
The backtester simulates a time-based walk-forward replay over historical candles. It processes candles in chronological order and issues trades at the close of each candle, which is a realistic approximation for a first version of a 24/7 bot.


Backtesting command -

```bash
python3 main.py --mode backtest --download-if-missing --start-date 2025-01-01 --end-date 2025-04-28
```

## How the Bot Works (overview)

- **Modes:** The project supports two primary modes:
	- **Backtest** — replay historical candles from a CSV and simulate trade execution, P&L, trailing stops and partial exits.
	- **Bot (live/paper)** — generate signals from the live data feed, validate against the `RiskManager`, submit market orders via the `ExecutionEngine`, and notify via `TelegramNotifier`.

- **Central configuration:** `config.json` controls risk, backtest and strategy parameters (capital, `risk_per_trade_pct`, `stop_loss_pct`, timeframes, windows for the TAE strategy, etc.). `main.py` loads this into the `AppConfig` value objects.

- **Strategy layer:** `app/strategy.py` currently implements the TAE strategy (`TAEStrategy`) and a simple fallback `MomentumStrategy`. A strategy's responsibility is to inspect recent price history and return a `Signal(symbol, side, confidence)` or `None`.

- **Risk management:** `app/risk_manager.py` enforces capital constraints, per-trade risk sizing, maximum daily loss, maximum trades per day, and maximum concurrent open positions. The risk manager exposes `validate_trade()` and `create_trade_plan()` which compute `position_size` and `take_profit` from stop-loss distance and configured reward ratio.

- **Backtester:** `app/backtester.py` drives the replay. For each candle it:
	1. Asks the strategy for a signal.
	2. If a signal exists, it computes SL/TP using configured percentages, asks `RiskManager` to validate, and creates a `BacktestTrade`.
	3. It then walks forward in time to find partial exits, take-profits or trailing-stop triggers and records realized P&L and trade metadata.

- **Live bot flow (`app/bot.py`):** On each new price tick it:
	1. Calls `strategy.generate_signal()`.
	2. Computes a stop-loss (using `stop_loss_pct`) and calls `risk_manager.validate_trade()`.
	3. If allowed, calls `execution_engine.submit_order()` and increments `risk_manager.open_positions`.
	4. Sends notifications via `TelegramNotifier`.

## Where things currently cause poor backtest results

- **Very tight fixed stops:** default `stop_loss_pct` is 1% (and trailing stop 0.5%). When markets are volatile this often hits stops before trends develop.
- **Risk sizing tied to absolute SL distance:** position size = risk_amount / (entry - stop). With tiny SL distance this can lead to oversized positions and strange P&L artifacts if price granularity or rounding is different.
- **TAE strategy sensitivity:** the `TAEStrategy` generates many signals but the backtester previously rejected many due to mis-wired risk limits (now fixed). Still, entry confirmation and value-area logic are conservative and may not match assumed stop widths.
- **Slippage & fees not modeled:** the backtester assumes fills at SL/TP/prices exactly, no trading fees or slippage — this inflates apparent performance and hides execution drag.

## Suggested prioritized improvements (short roadmap)

- **1) Volatility-aware stops (high priority):** implement ATR-based stop sizing and position sizing (use ATR to compute a reasonable stop distance, then size position to risk a fixed % of capital).

- **2) Fees and slippage model (high):** add configurable commission and per-trade slippage to the backtester to make simulated P&L realistic.

- **3) Strategy tuning & unit tests (medium):** improve `TAEStrategy` entry logic (stronger confirmation, multi-timeframe checks) and add focused unit tests that assert expected signals on small synthetic series.

- **4) Exit improvements (medium):** use ATR-based trailing stops or time-based exits and test partial exit behaviour under different volatility regimes.

- **5) Telemetry & metrics (low/medium):** add per-trade metadata, log to CSV/JSON, compute annualized-like metrics, and plot equity curves for visual inspection.

- **6) Simulation features (low):** support commission tiers, per-exchange tick sizes, and order types (limit + IOC).

## Useful files

- `main.py` — CLI entrypoint and download helper
- `app/config.py` — central config loader (maps `config.json` into objects)
- `app/strategy.py` — strategy implementations (`TAEStrategy`, `MomentumStrategy`)
- `app/risk_manager.py` — risk rules and trade plan creation
- `app/backtester.py` — backtest driver and report generator
- `app/bot.py` — live/paper bot runner


python3 main.py --mode backtest --download-if-missing --start-date 2025-01-01 --end-date 2025-06-30



python3 main.py --mode backtest --download-if-missing --start-date 2024-01-01 --end-date 2024-12-30

python3 main.py --mode backtest --download-if-missing --start-date 2026-04-01 --end-date 2026-04-30

python3 main.py --mode backtest --download-if-missing --start-date 2026-05-01 --end-date 2026-06-30

python3 main.py --mode backtest --download-if-missing --start-date 2022-01-01 --end-date 2022-12-30




python3 main.py --mode backtest --start-date 2025-01-01 --end-date 2025-12-01 --download-if-missing --capital 200

python3 main.py --mode backtest --start-date 2024-01-01 --end-date 2024-12-01 --download-if-missing --capital 200

python3 main.py --mode backtest --start-date 2023-01-01 --end-date 2023-12-01 --download-if-missing --capital 200

python3 main.py --mode backtest --start-date 2021-01-01 --end-date 2021-12-01 --download-if-missing --capital 200


Futures Mode 
% python3 main.py --mode backtest --download-if-missing --start-date 2025-01-01 --end-date 2025-06-30 --capital 1000

=== BACKTEST SUMMARY ===

python3 main.py --mode backtest --download-if-missing --start-date 2025-01-01 --end-date 2025-12-30 --capital 1000

python3 main.py --mode backtest --download-if-missing --start-date 2024-01-01 --end-date 2024-12-30 --capital 1000