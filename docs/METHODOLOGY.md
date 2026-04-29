# Methodology

The non-negotiable rules I run every strategy through.

---

## Quality gates

A strategy must pass **all** of these before it advances to cross-validation:

| Gate | Threshold | Why |
|---|---|---|
| Profit factor | > 1.3 | < 1.0 loses money. 1.0–1.3 doesn't have enough margin to survive realistic costs. |
| Sharpe ratio | > 1.0 | Returns must be meaningful relative to volatility. |
| Max drawdown | < 25% | Real-money psychology. Anything deeper and most traders close the system mid-drawdown. |
| Recovery factor | > 3 | Net profit ≥ 3× max drawdown. Pays you to live through the bad weeks. |
| Total trades | > 100 | Statistical significance. < 100 trades is a sample, not a strategy. |
| Avg trade hold | > 5s | Prop firms classify anything mostly held under 5s as microscalping and ban it. |

If a strategy fails any gate, it doesn't move forward. The fix is to investigate why it failed (overfitting, regime mismatch, broker quirk) and either iterate or shelve. There's no "close enough."

---

## Cross-validation

A backtest on a single platform with a single broker's data is preliminary, not final. Brokers have data quirks, fill model assumptions, and quote cleanliness issues that vary widely. A strategy that looks good on Broker A may fall apart on Broker B for reasons that have nothing to do with the strategy itself.

**The rule:** every strategy that passes gates gets re-implemented on the other platform with that platform's data feed, then compared side-by-side. If the results diverge meaningfully (different sign on profit, different drawdown profile, different trade frequency), the strategy isn't real — it's a data artifact.

The reverse holds: a strategy that survives both platforms is much more likely to survive live.

---

## Backtest configuration defaults

Every initial run uses these unless there's a specific reason to deviate:

| Parameter | Value |
|---|---|
| Modelling | Every tick based on real ticks (MT5) / Standard fastest fill (NT8) |
| Initial capital | $10,000 (MT5 demo) / $100,000 (NT8 prop sim) |
| Currency | USD |
| Leverage | 1:100 |
| Slippage | 2 points |
| Commission | Broker-realistic (set per platform) |

Initial test windows are typically 5 years where data is available. Multi-year history is mandatory for any strategy claiming robustness across regimes.

---

## Anti-patterns I won't accept

These are listed because I've watched myself want to do every one of them and have to talk myself out of it.

**Curve-fitting.** Tuning parameters until the equity curve looks pretty. The fix is to lock parameters early and only refine after out-of-sample tests succeed.

**Survivorship bias.** Testing on instruments that exist today rather than what existed during the test window. Continuous-contract handling matters here.

**Look-ahead bias.** Using bar values that wouldn't have been available at the decision point. Common with naive `iClose(0)` access — the current bar isn't closed yet.

**Inflating the trade count.** Strategies with thousands of trades on a five-year window are almost always microscalping or churn. Both get banned by prop firms and both lose to costs.

**Skipping cross-validation because the gates passed.** The whole point of the cross-validation step is to catch the cases where the gates lied. Skipping it because "it already looks good" defeats the entire pipeline.

---

## Strategic priorities

These are the project-level rules that override tactical decisions.

1. **Strategy is the asset, prop firm is the venue.** Don't optimize for a specific firm. Optimize for a working model. The right firm follows.
2. **Cross-validation is mandatory before deployment.** No live trading on single-platform backtest, ever.
3. **Every strategy is logged in the index, even the dead ends.** Dead ends are results too. Future me will need to know I already tried this.
4. **Pipeline first, strategy second.** Investing in pipeline quality compounds across every strategy I'll ever test. Strategy hunting without a pipeline is wasted time.
