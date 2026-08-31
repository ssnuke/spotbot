# Regime Health Monitor — Threshold Calibration Report

**Scope: research and calibration only.** No production trading logic, `config.json`, or live
behavior was touched to produce this report. Nothing here is deployed. This report exists to
answer one question: *what should the Regime Health Monitor's thresholds be, based on REV-2C's own
historical behavior, and how often would they have triggered?*

---

## Executive Summary

- REV-2C's own 6-year history was scanned in 30-day windows (2,160 overlapping daily windows for
  live-monitor simulation, 72 non-overlapping windows for calibration) to derive percentile-based
  thresholds — not hand-picked numbers.
- A continuous **0–100 stress score** was built from two largely-independent pillars (profit
  factor and reversal-exit share, weighted 55/45), chosen *after* a correlation analysis showed
  win rate, expectancy, cooldown rate, and reversal density each mostly duplicate one of those two
  signals.
- Band cutoffs (HEALTHY / NORMAL / CAUTION / STRESS / EXTREME_STRESS) fall out of the score's own
  historical quartiles: **<33.0 / 33.0–50.1 / 50.1–64.6 / 64.6–76.8 / ≥76.8**.
- A separate hard "July-like failure" AND-rule (win rate, PF, and reversal share simultaneously in
  the bad tail) was tested at three percentile pairs (Q20/80, Q25/75, Q30/70). All three found
  7–10 historical episodes, and **every episode with a full 90-day horizon (6 of 7, or 9 of 10)
  fully recovered to breakeven within 90 days** — 100% recovery rate at 30/60/90 days across all
  three candidate thresholds.
- **Out-of-sample validation is the strongest result in this report**: thresholds frozen using
  only data through Dec 2025 correctly flagged the actual July 2026 episode (STRESS by Jul 24,
  EXTREME_STRESS by Jul 25) and correctly tracked its de-escalation back to NORMAL by Aug 20 —
  entirely on data the calibration never saw.
- PF and win-rate percentiles are **stable** across calibration cutoffs from 2022 through today
  (moves of <0.05 PF, <2pp win rate). Reversal-share percentiles are **not** — the median
  reversal-exit share has drifted from ~40% (2022 calibration) to ~60% (full-history calibration).
  This is flagged as a real limitation, not glossed over.
- The monitor is a **confirmatory / real-time descriptor more than a true leading indicator** for
  sudden drawdowns: median lag from first STRESS flag to that episode's peak drawdown is only 1.5
  days (mean 5.3, pulled up by a handful of slow-grinding episodes with 30–77 day leads). It works
  well as "tell me what regime I'm in right now," less well as "warn me weeks before it gets bad."
- Confidence: **Medium**. See the Limitations section — one asset, one strategy, a genuinely small
  number of extreme-regime samples, and the reversal-share instability above are the reasons this
  isn't High.

---

## 1. Historical Data

| | |
|---|---|
| Dataset | SOL/USDT, 5m candles, 2020-09-01 → 2026-08-30 |
| Engine | `research/reversal_experiments/engine.py::run_backtest`, REV-2C policy (`SHORTLIST[2]`) |
| Total trades | 16,190 (matches two independent earlier full-history runs in this project exactly — Step 2 reproduction check passed) |
| Daily rolling windows (30d, 1d step) | 2,160 |
| Calibration windows (30d, non-overlapping) | 72 |
| Minimum trade count applied | 100 trades/window (see §2 — did not actually exclude anything at this dataset's trade pace) |

**Calibration spacing choice (Step 4):** non-overlapping 30-day windows, not weekly-spaced. A
7-day step still shares 23 of 30 days (77%) between adjacent windows — barely more independent
than daily. A 30-day step shares nothing between adjacent windows, so each calibration point is a
genuinely distinct slice of history. The cost is fewer points (72 vs. 2,160+), which is exactly
why §2 and §7 both exist — to check whether 72 is still enough.

---

## 2. Minimum Sample Size (Step 5)

Trade-count distribution across the 72 calibration windows: **min 141, P25 209, median 226, P75
238, max 273**. Every single 30-day window in this history — even the quietest — cleared 141
trades.

Sweeping candidate minimums (0/30/50/75/100 trades) against P25/P50/P75 of PF and win rate: **zero
windows were excluded at any candidate threshold**, and the percentile estimates were identical
across all five candidates (see `sample_size_analysis.csv`). REV-2C simply trades often enough
(≈7–8/day even in quiet regimes) that a 30-day window is never thin.

**Recommendation: 100 trades minimum**, adopted as a forward-looking safety margin for the live
daily monitor (in case some future regime trades far less often), not because it changed a single
number in this calibration.

---

## 3. Percentile Table (Steps 6/7)

Calibration windows (n=72, min_trades=100 — see `percentiles.json` for the full P05–P95 grid):

| Metric | Direction | P10 | P25 | P50 | P75 | P90 |
|---|---|---:|---:|---:|---:|---:|
| Profit Factor | lower = worse | 1.021 | 1.299 | 1.613 | 1.899 | 2.629 |
| Win Rate % | lower = worse | 39.92 | 42.74 | 47.87 | 51.34 | 56.84 |
| Reversal-Exit Share % | higher = worse | 31.50 | 40.96 | 60.04 | 72.09 | 81.46 |
| Expectancy $/trade | lower = worse | 0.05 | 0.63 | 1.23 | 2.15 | 3.34 |
| Reversal Density (mean/4h bucket) | higher = worse | 1.49 | 2.33 | 3.33 | 4.15 | 4.73 |
| Cooldown Rate | higher = worse | 0.121 | 0.134 | 0.154 | 0.170 | 0.179 |
| Max Drawdown % (window-local, context only) | higher = worse | 0.47 | 0.65 | 0.93 | 1.71 | 2.71 |
| Trade Count | context only | 186 | 209 | 226 | 237 | 249 |

Chart: `charts/01_distributions_grid.png`.

---

## 4. Correlation Analysis (Step 9)

Full matrix in `correlations.csv`. Pairs with |r| ≥ 0.6:

| Pair | r |
|---|---:|
| reversal_exit_share ↔ reversal_density | 0.955 |
| win_rate ↔ cooldown_rate | −0.949 |
| profit_factor ↔ expectancy | 0.911 |
| profit_factor ↔ win_rate | 0.835 |
| profit_factor ↔ cooldown_rate | −0.811 |
| expectancy ↔ cooldown_rate | −0.703 |
| win_rate ↔ expectancy | 0.681 |

**Reading this**: profit_factor, win_rate, expectancy, and cooldown_rate are four measurements of
essentially one underlying "trade quality" phenomenon. reversal_exit_share and reversal_density
are two measurements of one "whipsaw character" phenomenon (confirmed again, more rigorously, in
§9/Step 19 below). max_drawdown_pct is the most independent of the seven, but even it correlates
moderately with reversal_exit_share (−0.550) — deep reversal-driven whipsaw periods tend to
produce deeper drawdowns, which is intuitive and not a scoring problem since drawdown isn't a
score input anyway.

---

## 5. Stress Score (Step 8/10/11)

Two pillars, chosen directly from §4's correlation structure to avoid counting the same
phenomenon multiple times:

```
pf_badness       = 100 - percentile_rank(profit_factor)      [lower PF = worse]
reversal_badness = percentile_rank(reversal_exit_share)       [higher share = worse]

stress_score = 0.55 * pf_badness + 0.45 * reversal_badness
```

Percentile ranks are computed against the 72-window calibration distribution — a score of 80
means "this window's PF/reversal mix is worse than ~80% of REV-2C's own 6-year history," nothing
external.

win_rate, expectancy, and cooldown_rate were **excluded** from the formula (each shares ≥68%
variance with profit_factor — including them would weight "trade quality" roughly 4x over
"whipsaw character," an arbitrary and unjustified imbalance). reversal_density was excluded for
the same reason relative to reversal_exit_share. max_drawdown_pct was excluded per Step 20 —
it's reported as context, never as a score driver.

**Weighting (55/45, not 50/50):** a slight tilt toward profit factor, since it's the more
integrative bottom-line measure (it already reflects the outcome of win rate AND average win/loss
size), while reversal share captures a real but narrower structural feature. This is a judgment
call, not something the data forces to a specific decimal — §7's threshold-stability check is the
real test of whether this choice is trustworthy.

**Score distribution** (calibration, n=72): P05 16.6 · P10 19.4 · P25 33.0 · P50 50.1 · P75 64.6 ·
P90 76.8 · P95 88.7. Chart: `charts/02_stress_score_distribution.png`.

---

## 6. Regime Bands

Cutoffs are the score's own P25/P50/P75/P90 — by construction, not tuned to hit a target
distribution:

| Band | Score range | Share of calibration windows | Share of daily-rolling history |
|---|---|---:|---:|
| HEALTHY | 0 – 33.0 | 25.0% | 19.7% |
| NORMAL | 33.0 – 50.1 | 25.0% | 31.6% |
| CAUTION | 50.1 – 64.6 | 25.0% | 24.3% |
| STRESS | 64.6 – 76.8 | 13.9% | 13.4% |
| EXTREME_STRESS | ≥ 76.8 | 11.1% | 11.0% |

The daily-rolling share differs slightly from the calibration share (expected — the cutoffs were
set on the 72 non-overlapping windows, then applied to the denser 2,160-window daily series) but
lines up closely, which is itself a mild consistency check that the two datasets describe the
same underlying process.

**Historical meaning of each band** (median stats, from `regime_band_statistics.csv`):

| Band | Median PF | Median win rate | Median reversal share | Median drawdown (context) |
|---|---:|---:|---:|---:|
| HEALTHY | 2.08 | 50.8% | 34.8% | 1.4% |
| NORMAL | 1.82 | 49.6% | 60.3% | 0.8% |
| CAUTION | 1.47 | 47.6% | 59.6% | 0.9% |
| STRESS | 1.35 | 45.8% | 69.0% | 0.9% |
| EXTREME_STRESS | 1.06 | 42.1% | 81.7% | 0.7% |

PF and win rate decline monotonically band-to-band; reversal share rises. This is the sanity check
that the score is measuring something real, not noise.

---

## 7. Threshold Stability (Step 15)

Recalibrating from cutoffs at end-2022 / end-2023 / end-2024 / end-2025 / full history
(`threshold_stability.csv`, chart `06_threshold_stability.png`):

| Metric | Movement (2022-cutoff → full history) |
|---|---:|
| PF P25/P50/P75 | 0.04 / 0.03 / 0.05 (absolute) |
| Win rate P25/P50/P75 | 1.4 / 1.8 / 0.4 percentage points |
| Reversal share P25/P50/P75 | **12.2 / 20.3 / 22.5 percentage points** |

**PF and win-rate thresholds are stable** — a monitor calibrated in 2022 would look almost
identical to one calibrated today. **Reversal-share thresholds are not stable** — the historical
"normal" reversal-exit share has drifted up substantially (median ~40% in 2022-era calibration vs.
~60% today), most likely reflecting REV-2C's own confirmation-streak mechanic interacting with a
genuinely choppier multi-year period, or possibly a structural change in SOL's own price action
over 6 years. **This is flagged, not hidden**: reversal-share-based thresholds should be
revisited periodically (e.g., annually), not treated as permanent.

---

## 8. Historical Episodes (Step 12)

Merging adjacent/nearby (≤14-day gap) STRESS-or-worse daily windows produced **22 episodes** over
six years (`historical_episodes.csv`) — roughly one every 3–4 months, ranging from short 30-day
spikes to long 100+ day grinds (e.g. Apr–Sep 2023). Chart: `charts/03_stress_score_timeline.png`
shows all 22 shaded against the full score history — visually, the score oscillates through this
band range regularly; it is not a rare state by this (broader, "elevated") definition — that rarer
definition is §9's hard rule instead.

Recovery time by peak severity band (`recovery_analysis.csv`):

| Peak band | n episodes | Median recovery time |
|---|---:|---:|
| STRESS | 10 | 3.4 days |
| EXTREME_STRESS | 12 | 8.85 days |

---

## 9. The "July-Like Failure" Hard Rule (Step 13)

Separate from the continuous score: an AND-rule requiring win rate, PF, *and* reversal share to
simultaneously breach the bad-tail percentile, tested at three candidate splits
(`threshold_candidates.csv`):

| Q(lo/hi) | WR ≤ | PF ≤ | Rev ≥ | Episodes | Full-horizon episodes | Avg max DD | Median recovery | Recovery rate 30/60/90d |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Q20/80 | 41.5% | 1.199 | 75.6% | 7 | 6 | 15.9% | 16.6d | 100% / 100% / 100% |
| Q25/75 | 42.7% | 1.299 | 72.1% | 7 | 6 | 16.0% | 16.6d | 100% / 100% / 100% |
| Q30/70 | 43.6% | 1.341 | 68.6% | 10 | 9 | 15.3% | 16.0d | 100% / 100% / 100% |

All three candidate splits agree closely (7–10 episodes, ~16% average drawdown, ~16-day median
recovery, 100% recovery rate on every complete episode). **This robustness across three distinct
percentile choices is the main reason to trust the finding at all** — it isn't an artifact of
picking Q25/75 specifically. The "mild false alarm" check (an episode whose drawdown never
exceeded the calibration's own median drawdown) found **zero** false alarms at any threshold —
every flagged episode produced real, above-median damage.

**Recommended hard-rule name and thresholds**: `JULY_LIKE_FAILURE`, using the **Q25/75** split
(the middle of the three tested, and matches the original ad hoc definition from the earlier
one-off study almost exactly) — win rate ≤ 42.7%, PF ≤ 1.30, reversal share ≥ 72.1%, all
simultaneously, sustained ≥14 days.

---

## 10. Out-of-Sample Validation (Step 14)

Thresholds frozen using **only** calibration windows through 2025-12-31 (64 windows), then applied
unmodified to daily windows from 2026-01-01 onward (242 windows) — the validation period never
touched the calibration.

| | |
|---|---|
| Frozen band cutoffs | P25=34.3, P50=49.1, P75=64.6, P90=77.6 |
| Validation period | 2026-01-01 → 2026-08-30 |
| Band shares in validation | STRESS 24.0%, EXTREME 19.0%, CAUTION 26.9%, NORMAL 28.1%, HEALTHY 2.1% |

**The result that matters most**: applying only pre-2026 knowledge, the monitor:
- Flagged **STRESS on 2026-07-24**, **EXTREME_STRESS on 2026-07-25** — correctly catching the real
  July episode, entirely out-of-sample.
- Tracked a clean de-escalation through August: STRESS → CAUTION (Aug 17) → **NORMAL by 2026-08-20**,
  and stayed NORMAL through month-end.

Two things worth flagging honestly: (1) STRESS/EXTREME combined made up **43% of validation-period
days** — more than double the ~25% base rate the calibration period would predict, meaning 2026
has been a genuinely rougher year for REV-2C than 2020–2025 overall, not just in July; (2) early
July itself (Jul 1–10) read as NORMAL/HEALTHY, not STRESS — the 30-day lookback window means the
monitor lags real-time deterioration by design (see §11), so it did not catch the regime shift
the moment it began, only after ~3 weeks of accumulating bad trades.

---

## 11. Early-Warning Quality (Step 16)

For all 22 score-based episodes (`early_warning_lags.json`, chart `05_early_warning_lag.png`):
lag from first STRESS flag to that episode's own peak-drawdown day.

| | |
|---|---|
| Median lag | **1.5 days** |
| Mean lag | 5.3 days |
| Episodes with positive lag (real warning) | 12 of 22 |
| Episodes with zero/negative lag (coincident or late) | 10 of 22 |
| Longest observed leads | 77d (Apr–Sep 2023), 35d (Aug–Dec 2024), 32d (Mar–Jun 2024), 29d (Aug–Oct 2023) |

**Honest conclusion**: this monitor is better described as a **real-time regime descriptor /
confirmatory signal** than a true leading indicator for sudden drawdowns. For long, grinding
regimes it can lead by 4+ weeks. For sharp, fast episodes (several 2026 episodes show 0-day lag)
it arrives right alongside the damage, not before it. This matches the design intent from Step 20
— PF/win-rate/reversal-share deteriorate as bad trades accumulate, which happens roughly in step
with, not meaningfully ahead of, the drawdown those same trades cause.

---

## 12. Trend & Hysteresis (Steps 17/18)

**Trend** (`trend_history.csv`): week-over-week score deltas have an empirical middle-50% "noise
band" of **−7.6 to +7.8 points**. A move outside that band is classified DETERIORATING (>+7.8) or
IMPROVING (<−7.6); inside it, STABLE. Applied across history: 538 DETERIORATING days, 1,070 STABLE
days, 538 IMPROVING days (by construction, since the band is itself the middle 50%).

**Hysteresis** (`hysteresis_sweep.json`): the raw daily band series flips state 110 times over 6
years. Requiring N consecutive days to both enter and exit an elevated state:

| Rule | Transitions | % of raw | Avg detection delay |
|---|---:|---:|---:|
| Raw (1/1) | 110 | 100% | 0 days |
| 2 enter / 2 exit | 80 | 72.7% | 1 day |
| 3 enter / 3 exit | 52 | 47.3% | 2 days |
| 2 enter / 5 exit | 62 | 56.4% | 1 day |
| 3 enter / 7 exit | 40 | 36.4% | 2 days |

**Recommendation: 2 consecutive days to enter, 5 to exit.** This cuts flapping by ~44% for only a
1-day entry delay, and the asymmetric exit (5 days) matches the practical need from the July/August
episode itself — Step 17 called out that "very bad → rapidly improving" should read differently
from "moderately bad → getting steadily worse"; a slower exit requirement avoids the monitor
prematurely declaring "all clear" on a single good day inside a still-fragile recovery.

---

## 13. Reversal Density — Incremental Value (Step 19)

Contemporaneous correlation with reversal_exit_share: **r = 0.955** (near-total overlap, §4).
Forward test (does this window's density predict *next* window's expectancy any better than
share alone?): forward r for share = −0.069, for density = −0.076; partial correlation of density
with next-window expectancy after controlling for share = **−0.034**.

**Conclusion: negligible incremental value.** Reversal density is kept on the dashboard as a
descriptive/contextual stat (it's intuitive and was flagged in earlier research) but is **not** a
separate scored input — including it alongside reversal_exit_share would essentially double-count
the same signal for no predictive benefit.

---

## 14. Recommended Dashboard Fields (Step 23)

Layout only — no values invented, every field maps to something this report actually computed:

```
━━━━━━━━━━━━━━━━━━━━━━
       REGIME HEALTH
━━━━━━━━━━━━━━━━━━━━━━

State:            [HEALTHY / NORMAL / CAUTION / STRESS / EXTREME_STRESS]
Stress score:      [0-100, this window]
Trend:             [IMPROVING / STABLE / DETERIORATING] (7d-change vs ±7.6/7.8pt noise band)
Days in current state: [count, using 2-day-enter / 5-day-exit hysteresis]

30D Profit Factor:        [value]   Historical percentile: [Nth]
30D Win Rate:              [value]   Historical percentile: [Nth]
30D Reversal-Exit Share:   [value]   Historical percentile: [Nth]
30D Expectancy:            [$/trade]
30D Trades:                [count]
4H Reversal Density:       [context only -- not scored]
Current Drawdown (context):[%, window-local]

JULY_LIKE_FAILURE flag:    [not active / ACTIVE since <date>]

Historical similar episodes: [N in this band]
Historical recovery:         [X of Y complete episodes recovered within 90d]
━━━━━━━━━━━━━━━━━━━━━━
```

---

## 15. Final Recommendation Summary (Step 28)

**1. Percentile thresholds** — see §3's full table; core scored inputs are profit_factor and
reversal_exit_share (§5 explains why win_rate/expectancy/cooldown_rate/reversal_density are
excluded from the formula despite being reported).

**2. Regime bands** — HEALTHY <33.0, NORMAL 33.0–50.1, CAUTION 50.1–64.6, STRESS 64.6–76.8,
EXTREME_STRESS ≥76.8 (§6).

**3. Hard failure-regime rule** — `JULY_LIKE_FAILURE`: win rate ≤42.7%, PF ≤1.30, reversal share
≥72.1%, simultaneously, sustained ≥14 days (§9, Q25/75 split, corroborated by Q20/80 and Q30/70).

**4. Historical trigger frequency** — STRESS+EXTREME_STRESS combined: ~24% of daily-rolling
history (§6); 22 discrete elevated episodes over 6 years (§8); 7–10 hard-rule failure episodes
depending on percentile choice (§9).

**5. Out-of-sample validation** — frozen pre-2026 thresholds correctly flagged the real July 2026
episode (STRESS Jul 24, EXTREME Jul 25) and its recovery to NORMAL by Aug 20, with zero
adjustment (§10). 2026 overall ran hotter (43% STRESS+EXTREME days) than the calibration period's
~25% base rate.

**6. Threshold stability** — PF and win-rate thresholds are stable back to 2022 (§7). Reversal-
share thresholds are not (12–22 percentage-point drift) — flagged as a limitation requiring
periodic recalibration.

**7. Early-warning quality** — median 1.5-day lag to peak drawdown; real (2–11 week) leads exist
for slow-grinding regimes but not for sharp ones (§11). This is a confirmatory monitor first, an
early-warning system second.

**8. Recovery quality** — 100% recovery rate at 30/60/90 days across every hard-rule threshold
candidate with a complete observation window (§9); median recovery 3.4 days from a STRESS peak,
8.85 days from an EXTREME_STRESS peak (§8). Recommended hysteresis: 2 days to enter an elevated
state, 5 days to exit (§12).

**9. Recommended dashboard fields** — §14.

**10. Confidence level: MEDIUM.** High confidence in the PF/win-rate calibration (stable across 5
different cutoff years) and in the out-of-sample validation (genuinely predicted July before
seeing it). Reduced by: reversal-share threshold instability (§7), a small absolute number of
extreme-regime episodes to learn from (22 elevated, 7–10 hard-failure — enough to see a pattern,
not enough for tight statistical confidence), and the single-asset/single-strategy scope (§16).

---

## 16. Limitations

- **One asset.** Everything here is SOL/USDT's specific 2020–2026 price path, not an independent
  sample of market regimes.
- **One strategy.** Thresholds are specific to REV-2C's exact confirmation-streak behavior; they
  would not transfer to a different reversal policy or a different strategy family without
  re-calibration.
- **Overlapping windows / path dependence.** The daily-rolling dataset used for the live-monitor
  simulation and for episode detection has ~97% overlap between adjacent days by construction —
  this is fine for describing "what would the monitor have shown," but every episode-count and
  band-share statistic in this report ultimately traces back to a small number of genuinely
  distinct underlying market episodes, not hundreds of independent observations.
- **Limited extreme-regime samples.** 22 elevated episodes and 7–10 hard-failure episodes over 6
  years is enough to establish a real, repeated pattern — it is not enough to make "100% recovery
  rate" a statistical guarantee. A first failure to recover has not happened in this history; that
  does not mean it cannot happen.
- **Reversal-share threshold instability** (§7) means this specific input's calibration should be
  revisited periodically, not treated as fixed.
- **Historical only.** Every number in this report describes what REV-2C's own past looked like.
  None of it is a forecast, and market structure for SOL specifically or crypto perpetuals
  generally could shift in ways this history never saw.
- **This is calibration, not deployment.** No live `/regime` command exists yet. These are the
  frozen candidate thresholds for that future implementation, pending review.
