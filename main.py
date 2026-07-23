import argparse
import csv
import os
import re
from collections import Counter
from datetime import datetime

from dotenv import load_dotenv

from app.backtester import BacktestConfig, Backtester, BacktestResult, BacktestReport
from app.config import AppConfig, load_app_config
from app.data_feed import BinanceDataFeed
from app.live_runner import LiveRunner
from app.order_executor import SimulatedOrderExecutor, TestnetOrderExecutor
from app.risk_manager import RiskConfig
from app.strategy import TAEStrategyConfig
from app.telemetry import Telemetry

load_dotenv()

def parse_date(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            value_int = int(value)
            if len(value) == 13:
                value_int //= 1000
            return datetime.fromtimestamp(value_int)
        except ValueError as error:
            raise ValueError(f"Invalid date format: {value}") from error


def load_candles_from_csv(path: str, start_date: str | None = None, end_date: str | None = None) -> list[list]:
    start_dt = parse_date(start_date) if start_date else None
    end_dt = parse_date(end_date) if end_date else None
    candles: list[list] = []

    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        for row in reader:
            if not row or len(row) < 6:
                continue
            timestamp = row[0].strip()
            try:
                dt = parse_date(timestamp)
            except ValueError:
                continue
            if start_dt and dt < start_dt:
                continue
            if end_dt and dt > end_dt:
                continue

            try:
                numeric_values = [float(value) for value in row[1:6]]
            except ValueError:
                continue

            candles.append([dt.isoformat(), *numeric_values])

    return candles


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def infer_csv_path(symbol: str, timeframe: str, start_date: str, end_date: str) -> str:
    safe_symbol = sanitize_filename(symbol.replace("/", "_"))
    safe_timeframe = sanitize_filename(timeframe)
    safe_start = sanitize_filename(start_date)
    safe_end = sanitize_filename(end_date)
    return f"{safe_symbol}_{safe_timeframe}_{safe_start}_{safe_end}.csv"


def download_candles_to_csv(
    path: str,
    symbol: str,
    interval: str,
    start_date: str,
    end_date: str,
) -> bool:
    feed = BinanceDataFeed()
    start_dt = parse_date(start_date)
    end_dt = parse_date(end_date)
    start_time_ms = int(start_dt.timestamp() * 1000)
    end_time_ms = int(end_dt.timestamp() * 1000)
    klines = feed.get_historical_klines(symbol, interval, start_time_ms, end_time_ms)
    if not klines:
        return False

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for item in klines:
            writer.writerow(item)

    return True


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def render_row(cells: list[str]) -> str:
        return "| " + " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)) + " |"

    separator = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    print(separator)
    print(render_row(headers))
    print(separator)
    for row in rows:
        print(render_row(row))
    print(separator)


def print_backtest_report(
    report: BacktestReport,
    result: BacktestResult,
    start_capital: float,
    trade_log_limit: int = 20,
    exported_csv_path: str | None = None,
) -> None:
    trades = result.trades
    total_fees = sum(trade.fees for trade in trades)
    wins = [trade.pnl for trade in trades if trade.pnl > 0]
    losses = [trade.pnl for trade in trades if trade.pnl < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    best_trade = max((trade.pnl for trade in trades), default=0.0)
    worst_trade = min((trade.pnl for trade in trades), default=0.0)
    return_pct = (result.final_capital - start_capital) / start_capital if start_capital else 0.0

    print("\n=== BACKTEST SUMMARY ===\n")
    _print_table(
        ["Metric", "Value"],
        [
            ["Total trades", str(report.total_trades)],
            ["Winning trades", str(report.winning_trades)],
            ["Losing trades", str(report.losing_trades)],
            ["Win rate", _fmt_pct(report.win_rate)],
            ["Total P&L", _fmt_money(report.total_pnl)],
            ["Total fees paid", _fmt_money(total_fees)],
            ["Starting capital", _fmt_money(start_capital)],
            ["Final capital", _fmt_money(result.final_capital)],
            ["Return", _fmt_pct(return_pct)],
            ["Avg win", _fmt_money(avg_win)],
            ["Avg loss", _fmt_money(avg_loss)],
            ["Best trade", _fmt_money(best_trade)],
            ["Worst trade", _fmt_money(worst_trade)],
            ["Profit factor", "inf" if profit_factor == float("inf") else f"{profit_factor:.2f}"],
            ["Sharpe-like ratio", f"{report.sharpe_like_ratio:.3f}"],
            ["Max drawdown", _fmt_pct(report.max_drawdown)],
        ],
    )

    if trades:
        reason_counts = Counter(trade.exit_reason for trade in trades)
        print("\n=== EXIT REASON BREAKDOWN ===\n")
        _print_table(
            ["Exit reason", "Count", "% of trades"],
            [
                [reason, str(count), _fmt_pct(count / len(trades))]
                for reason, count in reason_counts.most_common()
            ],
        )

        def trade_row(i: int, trade) -> list[str]:
            return [
                str(i + 1),
                trade.timestamp,
                trade.side,
                f"{trade.entry_price:.2f}",
                f"{trade.exit_price:.2f}",
                trade.exit_reason,
                _fmt_money(trade.pnl),
                _fmt_money(trade.fees),
            ]

        headers = ["#", "Candle", "Side", "Entry", "Exit", "Reason", "PnL", "Fees"]
        if trade_log_limit <= 0 or len(trades) <= trade_log_limit:
            print("\n=== TRADE LOG ===\n")
            _print_table(headers, [trade_row(i, trade) for i, trade in enumerate(trades)])
        else:
            half = trade_log_limit // 2
            hidden = len(trades) - (half * 2)
            print(f"\n=== TRADE LOG (first {half} of {len(trades)} trades) ===\n")
            _print_table(headers, [trade_row(i, trade) for i, trade in enumerate(trades[:half])])
            print(f"\n... {hidden} trades omitted ...\n")
            print(f"=== TRADE LOG (last {half} of {len(trades)} trades) ===\n")
            _print_table(
                headers,
                [trade_row(len(trades) - half + i, trade) for i, trade in enumerate(trades[-half:])],
            )
            if exported_csv_path:
                print(f"\nFull trade log ({len(trades)} trades) written to {exported_csv_path}")
    else:
        print("\nNo trades were executed during this backtest window.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading bot CLI")
    parser.add_argument("--config", default=None, help="Path to central JSON config file")
    parser.add_argument("--mode", choices=["bot", "backtest"], default="bot")
    parser.add_argument("--csv", help="Path to a CSV file containing historical candle data")
    parser.add_argument("--symbol", help="Trading symbol to use for backtesting or live mode")
    parser.add_argument("--capital", type=float, help="Capital size for the bot and backtester")
    parser.add_argument("--timeframe", help="Candlestick timeframe for backtesting")
    parser.add_argument("--start-date", help="ISO date or timestamp to start backtest from")
    parser.add_argument("--end-date", help="ISO date or timestamp to end backtest at")
    parser.add_argument("--download-if-missing", action="store_true", help="Download candle data from Binance if the CSV file is missing")
    parser.add_argument("--verbose", action="store_true", help="Print per-signal/trade telemetry logs during the run")
    parser.add_argument("--trade-log-limit", type=int, default=20, help="Max trades to print in the CLI trade log table (0 = show all)")
    parser.add_argument("--export-trades", help="Path to write the full trade log as CSV")
    parser.add_argument(
        "--execution-mode",
        choices=["simulated", "testnet"],
        default=None,
        help="Live bot order execution: 'simulated' fills locally against live prices with no "
        "exchange account; 'testnet' places real (fake-funds) orders on Binance Spot Testnet. "
        "Defaults to config.json's live.execution_mode (simulated) if not given.",
    )
    args = parser.parse_args()

    config = load_app_config(args.config) if args.config else load_app_config()
    capital = args.capital if args.capital is not None else config.risk.capital
    strategy_config = TAEStrategyConfig(
        short_window=config.strategy.short_window,
        long_window=config.strategy.long_window,
        value_window=config.strategy.value_window,
        value_zone_pct=config.strategy.value_zone_pct,
        reversal_lookback=config.strategy.reversal_lookback,
        signal_threshold_pct=config.strategy.signal_threshold_pct,
        min_trend_distance_pct=config.strategy.min_trend_distance_pct,
        min_signal_score=config.strategy.min_signal_score,
    )

    if args.mode == "backtest":
        symbol = args.symbol or config.symbol
        timeframe = args.timeframe or config.backtest.timeframe
        if args.csv:
            csv_path = args.csv
        elif args.download_if_missing and args.start_date and args.end_date:
            csv_path = infer_csv_path(symbol, timeframe, args.start_date, args.end_date)
        else:
            csv_path = config.csv_path

        if not os.path.exists(csv_path):
            if args.download_if_missing or config.download_if_missing:
                if not args.start_date or not args.end_date:
                    print("Please provide --start-date and --end-date to download missing historical data")
                    return
                print(f"Downloading historical candles for {symbol} from Binance into {csv_path}...")
                success = download_candles_to_csv(
                    csv_path,
                    symbol=symbol,
                    interval=timeframe,
                    start_date=args.start_date,
                    end_date=args.end_date,
                )
                if not success:
                    print("Failed to download historical candle data from Binance.")
                    return
            else:
                print(f"File not found: {csv_path}")
                return

        candles = load_candles_from_csv(csv_path, start_date=args.start_date, end_date=args.end_date)
        if not candles:
            print("No candles found for the selected timeline")
            return

        backtest_config = BacktestConfig(
            start_capital=capital,
            timeframe=timeframe,
            risk_per_trade_pct=config.backtest.risk_per_trade_pct,
            max_daily_loss_pct=config.backtest.max_daily_loss_pct,
            max_trades_per_day=config.backtest.max_trades_per_day,
            max_open_positions=config.backtest.max_open_positions,
            max_position_pct=config.backtest.max_position_pct,
            max_consecutive_losses=config.backtest.max_consecutive_losses,
            cooldown_period=config.backtest.cooldown_period,
            reward_ratio=config.backtest.reward_ratio,
            stop_loss_pct=config.backtest.stop_loss_pct,
            take_profit_pct=config.backtest.take_profit_pct,
            partial_exit_profit_pct=config.backtest.partial_exit_profit_pct,
            partial_exit_qty_pct=config.backtest.partial_exit_qty_pct,
            trailing_stop_pct=config.backtest.trailing_stop_pct,
            use_atr_stop=config.backtest.use_atr_stop,
            atr_period=config.backtest.atr_period,
            atr_multiplier=config.backtest.atr_multiplier,
            trade_fee_pct=config.backtest.trade_fee_pct,
            slippage_pct=config.backtest.slippage_pct,
        )
        telemetry = Telemetry(enabled=True, logger=print if args.verbose else None)
        backtester = Backtester(config=backtest_config, strategy_config=strategy_config, telemetry=telemetry)
        result = backtester.run(candles)
        report = backtester.generate_report(result)

        exported_csv_path = None
        if result.trades and (args.export_trades or len(result.trades) > args.trade_log_limit > 0):
            exported_csv_path = args.export_trades or f"{os.path.splitext(csv_path)[0]}_trades.csv"
            backtester.export_results(result, exported_csv_path, format="csv")

        print_backtest_report(
            report,
            result,
            start_capital=capital,
            trade_log_limit=args.trade_log_limit,
            exported_csv_path=exported_csv_path,
        )
        return

    symbol = args.symbol or config.symbol
    execution_mode = args.execution_mode or config.live.execution_mode
    symbol_filters = None
    if execution_mode == "testnet":
        from app.binance_client import BinanceTestnetClient

        try:
            testnet_client = BinanceTestnetClient()
        except ValueError as error:
            print(f"Cannot start live bot: {error}")
            return
        order_executor = TestnetOrderExecutor(client=testnet_client)
        symbol_filters = testnet_client.get_symbol_filters(symbol)
    else:
        order_executor = SimulatedOrderExecutor(
            trade_fee_pct=config.backtest.trade_fee_pct,
            slippage_pct=config.backtest.slippage_pct,
            data_feed=BinanceDataFeed(),  # fills cross the live bid/ask spread, not just last-trade price
        )

    try:
        runner = LiveRunner(
            order_executor=order_executor,
            symbol_filters=symbol_filters,
            risk_config=RiskConfig(
                capital=capital,
                risk_per_trade_pct=config.risk.risk_per_trade_pct,
                reward_ratio=config.risk.reward_ratio,
                max_daily_loss_pct=config.risk.max_daily_loss_pct,
                max_drawdown_pct=config.risk.max_drawdown_pct,
                max_trades_per_day=config.risk.max_trades_per_day,
                max_open_positions=config.risk.max_open_positions,
                max_position_pct=config.risk.max_position_pct,
                max_consecutive_losses=config.risk.max_consecutive_losses,
                cooldown_period=config.risk.cooldown_period,
            ),
            strategy_config=strategy_config,
            symbol=symbol,
            timeframe=args.timeframe or config.live.timeframe,
            poll_interval_seconds=config.live.poll_interval_seconds,
            warmup_candles=config.live.warmup_candles,
            stop_loss_pct=config.live.stop_loss_pct,
            take_profit_pct=config.live.take_profit_pct,
            partial_exit_profit_pct=config.live.partial_exit_profit_pct,
            partial_exit_qty_pct=config.live.partial_exit_qty_pct,
            trailing_stop_pct=config.live.trailing_stop_pct,
            db_path=config.live.db_path,
            summary_interval_seconds=config.live.summary_interval_seconds,
            command_poll_interval_seconds=config.live.command_poll_interval_seconds,
            telemetry=Telemetry(enabled=True, logger=print if args.verbose else None),
        )
    except ValueError as error:
        print(f"Cannot start live bot: {error}")
        return
    runner.run_forever()


if __name__ == "__main__":
    main()
