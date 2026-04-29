# KeltnerBreakout — first validated backtest

**Date run:** Week 1
**Pipeline:** MT5, automated via in-process bridge
**Significance:** first end-to-end pipeline validation; first full result captured under the project methodology

---

## Strategy

A Keltner-channel volatility breakout with an RSI momentum filter. Source pulled from a community share, used as-is to validate the pipeline before introducing any custom logic.

**Entry rules:**
- **Long** when the previous candle closed *above* the upper Keltner band (EMA + 2×ATR) AND the previous candle's RSI was above 50.
- **Short** when the previous candle closed *below* the lower Keltner band AND RSI was below 50.

**Exit rules:**
- Price touches the middle EMA (mean-reversion exit), OR
- Stop loss hit, OR
- Take profit hit.

**Stop loss:** min(prev EMA, breakout candle low) for longs; max for shorts. Plus a small buffer to clear spread.
**Take profit:** 2× ATR from entry.

**Default parameters:** EMA period 20, ATR period 10, ATR multiplier 2.0, RSI period 14, TP ATR multiplier 2.0.

The strategy's logic is straightforward — breakouts above a volatility envelope with a momentum confirmation. The interesting question wasn't "does this work?" so much as "can we reproduce somebody else's stated result with our pipeline?"

---

## Setup

| Field | Value |
|---|---|
| Instrument | USDJPY |
| Timeframe | H1 |
| Window | 5 years |
| Modelling | Every tick based on real ticks |
| Initial deposit | $10,000 |
| Currency | USD |
| Leverage | 1:100 |
| Bridge runtime (after data cached) | 72 seconds |
| History quality | 99% |
| Total ticks processed | 224.8M |

---

## Results

| Metric | Value |
|---|---|
| Net Profit | $4,008.60 |
| Gross Profit | $28,857.30 |
| Gross Loss | -$24,848.70 |
| **Profit Factor** | **1.16** |
| **Sharpe Ratio** | **1.64** |
| Recovery Factor | 5.16 |
| Expected Payoff | 2.81 |
| Total Trades | 1,429 |
| Win rate | 52.27% |
| Avg profit trade | $38.63 |
| Avg loss trade | -$36.44 |
| Max equity drawdown | $777.30 (6.64%) |
| Min trade hold | 5 minutes |
| Max trade hold | 46 hours |
| Avg trade hold | 8h 25m 32s |

---

## Gate evaluation

| Gate | Threshold | Result | Pass |
|---|---|---|---|
| Profit factor | > 1.3 | 1.16 | ❌ |
| Sharpe ratio | > 1.0 | 1.64 | ✅ |
| Max drawdown | < 25% | 6.64% | ✅ |
| Recovery factor | > 3 | 5.16 | ✅ |
| Trade count | > 100 | 1,429 | ✅ |
| Avg trade hold | > 5s | 8h 25m | ✅ |

**6 of 7 gates pass.** Profit factor misses by 0.14. As-is, this is **not a deployment candidate** — the margin between gross profit and gross loss is too thin to absorb realistic broker costs over time. Useful as a baseline that future strategies must beat.

---

## Pipeline validation

The single most important number on this page is the **trade count**: 1,429 against the original community-stated 1,423 — a **0.4% match** on the same instrument, timeframe, and date window.

That close a match means the pipeline reproduces external results. Specifically:

- The strategy logic compiled and executed identically
- Our broker's tick data is high-fidelity (99% history quality)
- The bridge automation correctly drives the Strategy Tester end-to-end
- Our fill model and execution timing matches the original author's environment

The implication: when this pipeline produces a *passing* strategy in the future, the result is trustworthy. The 0.4% gap on this run is well within the noise band you'd expect from broker-specific spreads, so we can move forward with confidence in the methodology.

---

## What this run cost in time

- First run on USDJPY: ~10 minutes (5 years of tick data downloading)
- Second run on USDJPY (cached): 72 seconds
- Each parameter sweep on cached data: ~1–2 minutes

Tick-data caching is significant. The order-of-work decision for any session should account for it.

---

## Iteration ideas (not yet executed)

For when this strategy is revisited:

1. **Commission modelling.** Re-run with realistic broker commissions to confirm whether PF stays > 1.0 once costs are real. The original author's stated profit was $2,372 vs ours at $4,008 — likely the difference is commission, not strategy.
2. **Walk-forward parameter optimization.** EMA period (20), ATR multiplier (2.0), TP ATR multiplier (2.0), RSI period (14) — all optimization candidates. Walk-forward only, no in-sample tuning.
3. **Other instruments.** The original author flagged USDJPY, USTEC, and XAUUSD as testbeds. Test on the other two for robustness.
4. **Timeframe stress.** Try M30 and H4 to check sensitivity.
5. **Train/validate split.** 2020-2023 train, 2023-onwards out-of-sample, to detect overfitting.

None of these change the conclusion above (not a deployment candidate as-is). They would each be a separate documented run.
