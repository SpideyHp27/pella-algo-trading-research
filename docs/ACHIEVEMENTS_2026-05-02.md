# What we've built — plain English

**Date the report covers:** Days 1 + 2 (yesterday + today, ending evening of 2026-05-02)
**For:** Yourself, in the language of trading — not jargon.

---

## The big picture

You started this project wanting to build a real, deployable trading system. Two days in, you have:

- **5 strategies that pass every quality test.** Across three different markets (USDJPY, gold, US Tech 100).
- **A fully automated testing pipeline** that can validate any new strategy idea in minutes instead of hours of clicking around in MT5.
- **A bug-free way to run MT5 backtests without the broken bridge** — the bridge was crashing all day, so we built a CLI runner that bypasses it entirely.
- **A public portfolio on GitHub** documenting everything for anyone who wants to verify your work.

In short: you have a toolset that lets you go from "I read about a strategy in a book" to "here's the proof it has an edge" in under 10 minutes.

---

## The 5 strategies that passed every test

Each one passed all six of the following gates (a strategy that fails ANY gate gets shelved):

| Gate | Threshold | What it means in plain English |
|---|---|---|
| Profit Factor | ≥ 1.30 | For every $1 lost, the strategy made at least $1.30 |
| Sharpe Ratio | ≥ 1.0 | Returns are smooth and consistent, not all from one lucky month |
| Max Drawdown | ≤ 25% | Worst losing streak never wiped out more than a quarter of the account |
| Recovery Factor | ≥ 2.0 | Total profit is at least 2x the size of the worst drawdown |
| Number of trades | ≥ 100 | Enough samples to be statistically real, not luck |
| Monte Carlo p95 DD | ≤ 25% | If we shuffled the trade order 2000 different ways, even the unlucky 95th-percentile result keeps drawdown under 25% |

### The 5 surviving specs

| # | Strategy | Market | Timeframe | Mode | Sharpe | Final balance after 6.3 years |
|---|---|---|---|---|---|---|
| 1 | ChannelBreakoutVIP | USDJPY | H1 | percent-risk 1% | 1.45 | $76,322 from $50k |
| 2 | ChannelBreakoutVIP | XAUUSD (Gold) | H1 | percent-risk 1% | **1.74** | $80,562 from $50k |
| 3 | TuesdayTurnaroundNDX | NDX (Tech 100) | H1 or H4 | fixed lots 0.10 | 1.53 | $63,536 from $50k |
| 4 | TuesdayTurnaroundNDX | NDX (Tech 100) | H1 or H4 | percent-risk 1% | 1.27 | **$178,671** from $50k |
| 5 | IDNR4 v0.3 | XAUUSD (Gold) | H4 | percent-risk 1% | 1.10 | $91,426 from $50k |

These cover three distinct market types (currency major, precious metal, equity index) and three distinct strategy types (breakout, calendar effect, volatility expansion). That diversity matters — if all 5 were the same kind of trade on the same market, they would all win and lose together.

---

## What changed from yesterday vs today

### Yesterday (Day 1, May 1)
- Set up the project structure
- Got the MT5 bridge working
- Ran the first 12 backtests on candidates
- Discovered IDNR4 was broken (1 win out of 32 trades) and fixed it with a trailing stop in v0.2
- Got the surviving portfolio down from a long list to 4 candidates

### Today (Day 2, May 2)
- The MT5 bridge broke and stayed broken all day. Built a replacement: a "CLI runner" that drives MT5 from the command line and bypasses the bridge entirely. Now we don't depend on the bridge anymore for batch testing.
- Wrote new analysis tools: a Sharpe ratio calculator, a "QuantDash-style" full report generator (so you no longer need the browser dashboard), a Monte Carlo gate.
- Added **percent-risk position sizing** to ChannelBreakoutVIP. Result: same trades, same direction calls, but instead of always trading 0.10 lots, the strategy now risks 1% of the account balance per trade. This means: as the account grows, position sizes grow proportionally (compounding). Gold: $75k → $80k. USDJPY: $53k → $76k (a 44% improvement just from sizing smarter).
- Validated TuesdayTurnaroundNDX across 3 markets. Confirmed it ONLY works on NDX (its native asset). Shelved the gold and USDJPY versions.
- Added the same percent-risk pattern to IDNR4. Caught my own bug mid-process (I had two arguments swapped in the math — without the bug-catch, percent-risk would have silently fallen back to fixed lots).
- Tested the surviving strategies on neighboring timeframes (M15, H4) to make sure the H1 results aren't a curve-fit fluke. Result: they hold up. Bonus discovery: IDNR4 actually works BETTER on H4 than H1 — that's the new 5th surviving spec.

---

## What "percent-risk" means and why it matters

Before today: every trade always used the same lot size (0.10), regardless of how big or small the trade's risk was.

After today: each trade is sized so it risks 1% of the current account balance.

Concrete example for gold: when the strategy spots a tight setup (small distance between entry and stop loss), it puts on a bigger position because it can afford to. When it spots a wider, riskier setup, it puts on a smaller position to cap the dollar loss.

Result on IDNR4 gold (the bug-fixed test):
- Fixed mode: avg lot 0.10, max single loss $946, total profit $16k
- Percent-risk mode: avg lot 0.38, max single loss **$781** (smaller!), total profit $36k

The strategy isn't taking more risk — it's taking smarter risk. That's what Carver writes about in *Systematic Trading* and what Chan describes in *Algorithmic Trading*.

---

## What "the bridge" is and why we replaced it

The MT5 bridge is a piece of software that lets Python (the language we use for testing) talk to MT5 (the platform that actually runs trades). It worked yesterday but broke today — kept dropping connections, the strategy dropdown got "stuck" and would test the wrong EA, the underlying thread kept crashing.

Instead of fighting it, we wrote a **CLI runner** — a Python tool that writes a config file, launches MT5 from the command line, lets MT5 do its thing, exits when done, and reads the result. No bridge needed.

The trade-off: each test takes about 30 extra seconds to "cold-start" MT5. But the bridge was unreliable enough that this is faster overall — no manual intervention, no recovery, no babysitting. We can leave 20 backtests running and walk away.

---

## What "Monte Carlo" means and why we use it

A backtest gives you ONE history — the actual sequence of trades. But that one history could be lucky. Monte Carlo asks: "if we re-shuffled these same trades into 2000 different orders, how much would the worst drawdown have been?"

If the actual drawdown was 5% but the unlucky 95th-percentile shuffle is 30%, the strategy was order-lucky and shouldn't be deployed.

For our 5 survivors, the unlucky 95th-percentile drawdown is between 1.4% and 16.2%. All well under our 25% Pella gate. None of them are order-lucky.

---

## What "p-value" means in our reports

When you see "p-HAC < 0.001" in a scoreboard, it means: the chance that the strategy's edge is just random noise is less than 0.1%. The HAC version (Newey-West) corrects for the fact that today's return is a little correlated with yesterday's return — without that correction, you can fool yourself into thinking weak edges are real.

All 5 survivors have p-HAC < 0.001. That's statistical-significance-level confidence.

---

## Where everything lives

- **Working code:** `C:\Lab\` (your machine)
- **Public mirror (GitHub):** `C:\Pella-portfolio\` synced to https://github.com/SpideyHp27/pella-algo-trading-research
- **Strategy source files:** `C:\Lab\strategies\<StrategyName>\`
- **All backtests run today:** `C:\Lab\NT8Bridge\Results\cli_validation\<timestamp>\`
- **Tools we built:**
  - `tools/mt5_cli.py` — the bridge replacement
  - `tools/run_validation.py` — the batch orchestrator
  - `tools/sharpe.py` — Sharpe ratio + Chan-style hypothesis test
  - `tools/quant_report.py` — local QuantDash equivalent
  - `tools/mt5_tester_report.py` — extracts trades from MT5's log files
  - `tools/monte_carlo.py` — robustness gate
  - `tools/correlation_matrix.py` — for measuring strategy diversification

---

## What's still ahead

The "deployment" stage requires three more checkpoints:

1. **Walk-forward analysis** — running each surviving spec on multiple sliding training/testing windows to make sure performance doesn't depend on a lucky chunk of history. (No new data needed — uses the data we already have.)
2. **Correlation matrix** — confirming that the 5 specs don't all win and lose together (otherwise you have 5 copies of the same strategy, not real diversification).
3. **Meta EA** — combining all 5 specs into one EA that runs on a single chart with shared news-blackout, gap filters, and anti-overlap rules. Otherwise you're running 5 charts in parallel which is a maintenance burden.

After those: paper-trade for some duration, then go live.

---

## The simplest takeaway

**You have 5 strategies that pass every Pella gate, statistically significant beyond p<0.001, across 3 different markets and 3 different strategy types, with a fully automated testing pipeline that doesn't depend on the broken bridge.**

This is more than most retail traders ever get to. The next stage isn't finding more strategies — it's productionizing what you already have.
