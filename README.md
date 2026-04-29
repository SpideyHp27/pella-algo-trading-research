# Pella — Algorithmic Trading Research & Backtesting Pipeline

![status](https://img.shields.io/badge/status-active-brightgreen) ![platforms](https://img.shields.io/badge/platforms-MT5%20%7C%20NT8-blue) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

**An end-to-end pipeline for designing, backtesting, and validating systematic trading strategies across MetaTrader 5 and NinjaTrader 8, with cross-platform validation as a hard gate before deployment.**

Codename **Pella** — after Alexander the Great's birthplace. Short, memorable, and a reminder that strategy is the asset; the venue is just where you fight.

---

## What this project is

A personal research framework I'm building to take systematic trading strategies from idea → backtest → cross-validation → simulator → live. The pipeline is platform-agnostic by design: strategies validated here run on either MetaTrader 5 (CFDs, forex, indices) or NinjaTrader 8 (futures), letting me chase whichever prop firm venue makes the most economic sense at any given time.

The core thesis behind the project, arrived at after a week of fighting data-sourcing problems on the futures side:

> *"It's not about which prop firm. It's about coming up with a working model. The strategy is the asset, the prop firm is just a venue."*

That single insight pivoted the whole project from a chase-the-cheapest-prop model into a build-the-strategy-first model.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     Strategy research & code                      │
│   research/INDEX.md  ←  master log of every hypothesis            │
│   strategies/<name>/ ←  source + notes + invalidation criteria    │
└──────────────────────┬───────────────────────────────────────────┘
                       │
       ┌───────────────┴───────────────┐
       │                               │
┌──────▼─────────┐             ┌──────▼─────────┐
│  MT5 pipeline  │             │  NT8 pipeline  │
│  CFD / forex   │             │    futures      │
│  Tick-level    │             │  Bar-level      │
│  cross-check   │             │  primary tester │
└──────┬─────────┘             └──────┬─────────┘
       │                              │
       └─────────────┬────────────────┘
                     │
            ┌────────▼────────┐
            │  Result archive │
            │  results/<name>/ │
            └────────┬────────┘
                     │
            ┌────────▼─────────────────────────┐
            │  Gate evaluation                 │
            │  PF > 1.3, Sharpe > 1.0,          │
            │  Max DD < 25%, Recovery > 3,      │
            │  Trade hold > 5s (firm rules)     │
            └────────┬─────────────────────────┘
                     │
            ┌────────▼────────┐
            │  Cross-validation│
            │  Same logic on   │
            │  the other       │
            │  platform        │
            └────────┬────────┘
                     │
                Deploy decision
```

**Why both platforms:** No single broker has clean data on both CFDs and futures. Building strategies on one and porting to the other forces clean separation of strategy logic from broker quirks, and it produces a built-in cross-validation layer — a strategy that holds up under tick data on Broker A *and* on Broker B is more likely to hold up live.

---

## Methodology

Every strategy goes through this pipeline. No shortcuts, no "looks fine, deploy."

1. **Research**: write the hypothesis in plain English. What macro/structural reason should this work? What would prove it wrong? Logged as one row in `research/INDEX.md`.

2. **Build**: implement the strategy. Comment the entry / exit / invalidation rules in the source so future-me can read it without rerunning the code.

3. **Backtest** at tick resolution where possible. MT5 Strategy Tester with "Every tick based on real ticks" is the gold standard for forex/CFDs.

4. **Quality gates** (must pass all):
   - Profit factor > 1.3
   - Sharpe > 1.0
   - Max drawdown < 25%
   - Recovery factor > 3
   - At least 100 trades (statistical significance)
   - Average trade hold time > 5 seconds (avoids prop firm microscalping rules)

5. **Cross-validation**: re-run the same logic on the other platform with that platform's data feed. If the backtest result diverges meaningfully between platforms, the strategy isn't real — it's a data artifact.

6. **Simulator**: run on a paper-trading prop simulator before any live deployment.

7. **Deploy**: only after the strategy survives all of the above.

This is conservative on purpose. Most published strategies don't survive even the gates, let alone the cross-validation step.

---

## Engineering discipline

This project enforces a four-document governance contract on any AI-assisted code change:

- **SPEC** — what is being changed, with explicit baseline and acceptance criteria
- **CODEGEN** — implements only what the SPEC declared, nothing else
- **AUDIT** — read-only verification, ✅ PASS / ❌ FAIL only

The motivation is real: it's easy for AI tools to "helpfully" rename variables, add safety checks, or reorganize logic when asked to fix something narrow. On strategy code that handles real money, that kind of silent change is dangerous. The contract turns the AI from a creative collaborator into a disciplined tool that does exactly what's specified, halts when ambiguous, and can audit its own work without modifying it.

---

## What's working today

**Pipeline is end-to-end automated.** Backtests run via HTTP / CLI, no manual clicking through Strategy Tester or Strategy Analyzer dialogs. Results land as JSON, get parsed into the result archive, and compared against the gates automatically.

**First validated backtest** is in `results/KeltnerBreakout/`. A Keltner-channel breakout strategy, run against five years of forex tick data:

| Metric | Result | Gate | Pass |
|---|---|---|---|
| Profit factor | 1.16 | > 1.3 | ❌ |
| Sharpe ratio | 1.64 | > 1.0 | ✅ |
| Max equity drawdown | 6.64% | < 25% | ✅ |
| Recovery factor | 5.16 | > 3 | ✅ |
| Total trades | 1,429 | > 100 | ✅ |
| Avg trade hold | 8h 25m | > 5s | ✅ |

The strategy passes 6 of 7 gates — sharpe is strong, drawdown is excellent, but profit factor misses by 0.14. **As-is it's not a deployment candidate.** Useful as a known-good baseline against which to measure new strategies. The full result writeup, including comparison to the original author's stated baseline, is in `results/KeltnerBreakout/`.

A side-effect of running this strategy: it validated the entire pipeline. The trade count we produced (1,429) matched the original author's stated count (1,423) within 0.4% on the same instrument and date range. That close a match means our pipeline, our tick data feed, and our execution model all agree with theirs — i.e. when we get a strategy that *does* pass gates, the result is trustworthy.

---

## What's next

- Re-run a batch of additional MT5 strategies with full metric capture between each (the first batch lost intermediate metrics — pipeline-level lesson, now solved).
- Get the NT8 path producing its first complete result on Japanese Yen futures (continuous-contract rollover and 24-hour session template are the open variables).
- Once two or more strategies pass all gates on one platform, take the strongest into cross-validation.

---

## Key files

- `docs/BUILD_JOURNAL.md` — chronological build log: what was tried, what failed, why we pivoted, what we learned
- `docs/METHODOLOGY.md` — the full version of the pipeline above
- `results/KeltnerBreakout/RESULT.md` — first validated backtest with all metrics

---

## Project status

**Active**, ~1 week old at time of last update. Documenting publicly as I build. The pipeline is the product; the strategies are samples.
