# Regime Health Monitor — Calibration Layer

Research/observability package only. **Nothing here touches production trading logic** —
`app/strategy.py`, `app/live_runner.py`, `app/risk_manager.py`, and `config.json` are untouched,
and nothing in this directory is imported by the live bot.

Read `REGIME_THRESHOLD_REPORT.md` first — it's the actual deliverable. This README just orients
you around the files and how to reproduce them.

## How to reproduce, in order

```bash
python3 research/regime_monitor/build_dataset.py       # runs the REV-2C backtest once, builds the two window datasets
python3 research/regime_monitor/sample_size.py          # Step 5
python3 research/regime_monitor/percentiles.py          # Step 6/7
python3 research/regime_monitor/correlations.py         # Step 9
python3 research/regime_monitor/stress_score.py         # Step 8/10/11
python3 research/regime_monitor/episodes.py             # Step 12/13 (re-runs the backtest ~16 times on small date-sliced windows)
python3 research/regime_monitor/out_of_sample.py        # Step 14
python3 research/regime_monitor/stability.py            # Step 15
python3 research/regime_monitor/trend_and_warning.py    # Step 16/17/18
python3 research/regime_monitor/reversal_density_check.py  # Step 19
python3 research/regime_monitor/band_statistics.py       # Step 22 + recovery_analysis.csv
python3 research/regime_monitor/charts.py                # Step 25 (needs matplotlib)
```

Total runtime: roughly 2 minutes. `build_dataset.py` and `episodes.py` are the only steps that
re-run the actual backtest engine; everything else is pure CSV/JSON post-processing of their
output, which is why the later steps run in well under a second each.

## Canonical data sources (Step 1 findings)

- **Trade outcomes, PnL, fees, funding, exit reasons, timestamps, cooldown flags**:
  `research/reversal_experiments/engine.py`'s `TradeRecord`, produced by `run_backtest()`. This is
  the SAME engine used for every research artifact produced earlier in this project (the July/
  August report, the historical-analogue scan, the regime-survival study) — it mirrors
  `app/live_runner.py` + `app/risk_manager.py`'s real exit/risk logic and `config.json`'s
  `"risk"` + `"live"` + `"strategy"` sections exactly (verified field-by-field earlier in this
  project). Reusing it here means this calibration is built on the actual production math, not a
  reimplementation of it.
- **Account equity**: reconstructed by accumulating `TradeRecord.pnl` from a fixed starting
  capital, same convention as every prior report in this repo.
- `app/state_store.py` (the LIVE sqlite schema) is **not** used — the live account has only a few
  days of REV-2C history, nowhere near enough for calibration. This is a real, stated limitation:
  thresholds are calibrated on backtest behavior, not the live account's own (currently tiny)
  trade history.
- `app/regime_filter.py` exists in the codebase but is an unrelated HTF-EMA trend filter — grep
  confirms it is not imported by `app/strategy.py`, `app/live_runner.py`, or the research engine.
  Naming collision with "regime" only; not part of REV-2C's actual signal path.
- `app/backtester.py` was inspected for its day-boundary convention (`_get_candle_date` /
  `_get_candle_date_local`), which the research engine deliberately replicates for reproduction
  fidelity with earlier validated reports — not otherwise used directly here.

## Reproduction check (Step 2)

The full-history run here produced **16,190 trades**, identical to the trade count from the two
independent REV-2C full-history studies run earlier in this project
(`find_similar_periods.py`, `regime_survival_study.py`) using the same engine and config. No
discrepancy to diagnose — the reproduction matches exactly.

## File map

| File | Produced by | Step |
|---|---|---|
| `rolling_windows_daily.csv` | `build_dataset.py` | 3 |
| `calibration_windows.csv` | `build_dataset.py` | 3/4 |
| `sample_size_analysis.csv` | `sample_size.py` | 5 |
| `percentiles.json` | `percentiles.py` | 6/7 |
| `correlations.csv` | `correlations.py` | 9 |
| `stress_score_history.csv` | `stress_score.py` | 8/10/11 |
| `historical_episodes.csv` | `episodes.py` | 12 |
| `threshold_candidates.csv` | `episodes.py` | 13 |
| `out_of_sample_results.csv` | `out_of_sample.py` | 14 |
| `threshold_stability.csv` | `stability.py` | 15 |
| `regime_band_statistics.csv` | `band_statistics.py` | 22 |
| `recovery_analysis.csv` | `band_statistics.py` | 22/25 |
| `charts/*.png` (7 figures) | `charts.py` | 25 |
| `data_cache/*.json` | various | intermediate values consumed by later steps / the report |

## Data limitation, stated plainly (Step 1 + Step 20)

`max_drawdown_pct`, `drawdown_duration_days`, `recovery_time_days` in `rolling_windows_daily.csv`
/ `calibration_windows.csv` are computed **peak-to-trough on the one continuous 2020–2026
compounding backtest's own equity curve, local to each window**. This is appropriate for a
health-monitor context (drawdown is used as descriptive context, never as a score input — see
`REGIME_THRESHOLD_REPORT.md`'s Stress Score section) but is **not** the same basis as the
isolated fresh-capital dollar figures in the three earlier one-off reports
(`recent_months.html`, `similar_periods.html`, `regime_survival.html`). Episode-level 30/60/90-day
dollar outcomes in `historical_episodes.csv` and `threshold_candidates.csv` use the isolated-
replay method instead (`_isolated_replay.py`), specifically because that comparison needs to be
apples-to-apples across eras of very different account size.
