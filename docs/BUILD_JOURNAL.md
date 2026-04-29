# Pella Build Journal

A first-person record of building this trading research pipeline. What I tried, what failed, what I learned. Written week-by-week as it happens.

---

## Week 1 — getting started

**Naming.** I almost called this "Renaissance Technologies Medallion Fund" because that fund is the gold standard of systematic trading and I wanted to anchor on something inspiring. Decided that was too long and too presumptuous for a personal project. Settled on **Pella** — Alexander the Great's birthplace. Short, memorable, and a reminder that Alexander started in one specific place and built outward. I want to do the same: pick a venue, make it work, then expand.

**The stack I committed to.** Two platforms:
- **MetaTrader 5** for CFDs, forex, indices. Tick-data backtesting on the right broker is genuinely good.
- **NinjaTrader 8** for futures. Required for any prop firm in the futures space (Lucid, Apex, Tradeify, etc.).

**Project layout.**

```
research/INDEX.md              ← every strategy hypothesis, alive or dead
strategies/<name>/             ← source + notes per strategy
results/<name>/<run>/          ← captured backtest results
docs/BUILD_JOURNAL.md          ← this file
docs/METHODOLOGY.md            ← the rules I'm not allowed to break
```

The `research/INDEX.md` is the single source of truth for what I'm working on. Active, shelved, deployed, dead end — every row stays forever, even the dead ends, because the dead end is itself a result.

---

## Week 1 — the data wall

Trying to backtest futures strategies in NinjaTrader 8 turned into a multi-day data-sourcing problem. NT8 needs historical futures data and there's no obvious free source.

What I tried, in order:

| Source | Result |
|---|---|
| Quant Data Manager | Doesn't export futures. Only crypto, forex, indices. The export buttons exist but are paywalled. Wasted ~3 hours figuring this out. |
| Kinetick free feed | Provides only end-of-day daily bars. No M1/M5/M15. Useless for any strategy with intraday entries. |
| NinjaTrader Brokerage 14-day trial | Works, but only for 14 days. Long enough to test the pipeline mechanics, not long enough to do real research. |
| Tradovate via funded prop account | ~2 years of intraday futures, free side-effect of being funded. Chicken and egg: I need data to pass eval, I get data after passing eval. |
| Databento | Best data available, deepest history. ~$99+/month. Defer until something is working. |

After hitting wall after wall I realized the question I was asking was wrong. I'd been asking *"how do I get cheap futures data?"* The right question turned out to be *"do I need futures data at all to make this work?"*

The answer was no. I could build strategies on **MT5 with a Darwinex demo account** — free tick-level forex/CFD data, ten-plus years deep — and worry about the futures-specific port to NT8 only after a strategy was actually working. This pivoted the whole project.

The mental shift, recorded as I wrote it:

> *"It's not about which prop, it's about coming up with a working model."*

The strategy is the asset. The prop firm is just a venue. If I have a model that works, I can pick the cheapest venue that suits it. If I don't have a model, no venue saves me.

---

## Week 1 — pipeline automation

With the strategic pivot done, I stopped fighting data and started building the actual pipeline. Two halves:

**MT5 side.** A small HTTP bridge runs inside MT5 (loaded as an Expert Advisor) and exposes endpoints to configure and run the Strategy Tester from outside MT5. So instead of clicking through Strategy Tester dialogs, I send a JSON POST and get back a result. The bridge is a NativeAOT C# DLL, which means it runs in-process inside MT5 — no UIAutomation, no focus stealing, no subprocess weirdness. End-to-end backtest in 30 seconds to a few minutes depending on whether tick data is cached.

**NT8 side.** A Python pipeline drives NinjaTrader 8's Strategy Analyzer via pywinauto. Less elegant than the MT5 in-process bridge, but it works. The pipeline deploys the strategy file, compiles it, configures the Strategy Analyzer, presses Run, and waits for a result JSON file written by the strategy itself in `State.Terminated`.

Both pipelines deliberately mirror each other in terms of inputs (instrument, timeframe, date range, modelling) and outputs (a JSON result). That's by design — same strategy, same parameters, runnable on either platform, results comparable side by side. That's the cross-validation layer.

---

## Week 1 — first real result

Pulled a Keltner-channel breakout strategy from a community source and ran it through the MT5 pipeline. Five years of USDJPY tick data, every-tick-based-on-real-ticks modelling.

| Metric | Result |
|---|---|
| Total trades | 1,429 |
| Profit factor | 1.16 |
| Sharpe ratio | 1.64 |
| Max equity drawdown | 6.64% |
| Recovery factor | 5.16 |
| Win rate | 52.27% |
| Avg trade hold | 8h 25m |
| Bridge runtime | 72 seconds |

The strategy passes 6 of my 7 quality gates. PF 1.16 misses my 1.3 threshold, so it's not a deployment candidate as-is — but it's a strong baseline.

**The most important number wasn't the PF or the Sharpe.** It was the trade count: 1,429 against the original author's stated 1,423 — a 0.4% match. That was the moment I knew the pipeline worked. The strategy logic, the tick data feed, and the bridge automation all agreed bit-for-bit with somebody else's environment. That meant any future strategy result coming through this pipeline is trustworthy in the same way.

---

## Week 1 — engineering hygiene

Adopted a four-document governance contract midway through the week. The motivation was specific: AI tools love to "help" — when you ask for a small fix, they often rename variables, add safety guards, or reorganize logic at the same time. On code that handles real money, those silent changes are dangerous.

The contract is three roles:

- **SPECS** — write what should change, with explicit baseline and acceptance criteria
- **CODEGEN** — implement only what was specified, nothing else
- **AUDIT** — read-only verification, ✅ PASS or ❌ FAIL, no fixes proposed

The "silence rule" is the most important one in practice: anything not explicitly in the spec is forbidden. No "while I'm here" cleanup, no extra guards, no renames, no optimization. AI gets turned from a creative collaborator into a disciplined tool that does exactly what's specified.

---

## Week 1 — pipeline lessons

Things I learned the hard way, in case I (or someone else) build something similar:

**On strategy sourcing.** Strategies floating around community forums vary in quality. Some are written for the wrong platform version (MT4 functions in a file labeled MT5). Some have hardcoded broker-specific paths. Compile before you trust the description. Run before you trust the equity curve.

**On tick data.** First run on a new instrument/range downloads the tick history (~10 minutes for 5 years on a major forex pair). Subsequent runs on the same data are nearly instant. Plan the order of work to amortize the download cost.

**On Strategy Tester / Analyzer state.** Both platforms have UI state that the bridge depends on. The MT5 Strategy Tester needs to be visible (Ctrl+R) and in Expert Advisor mode (not Indicator mode) before configure calls. The NT8 NinjaScript Editor and Strategy Analyzer windows need to be open before pywinauto drives them. These are one-time clicks but they're prerequisites the docs don't make obvious.

**On capturing results.** Both bridges return a thin status (`completed`, `elapsed_seconds`) and the actual metrics live in the platform's own report tabs. Capture results between runs — they get overwritten. The NT8 pattern (strategy writes JSON in `State.Terminated`) is cleaner than the MT5 pattern (read from UI report tabs). I'm migrating the MT5 side toward something similar.

**On data coverage.** Specific futures contracts (e.g. the March 2026 Yen contract) only have data from when that contract started trading. Multi-year backtests need the **continuous contract** (e.g. `6J 00-00`) with rollover policy set to "Merge back adjusted." Without that, you get artificial gaps that destroy any strategy that depends on session-by-session continuity.

---

## What I'm working on next

- Re-run a batch of MT5 strategies with proper between-run metric capture.
- Get the NT8 path producing a first clean result. The blocker right now is identifying whether the futures session template includes overnight bars (the strategy I'm testing depends on the Asian session, 8PM-11PM Eastern time).
- Once any single strategy passes all 7 gates on one platform, take it into cross-validation on the other.

The goal for this month is not a winning strategy. The goal is a working pipeline I can throw any new idea at. Once the pipeline is clean, the strategy hunt becomes the easy part.
