# Paper-Trade Deployment Plan (deferred — design doc only)

**Status:** Design / blueprint. **Not active yet.** Per `feedback_priority_paper_trading.md` memory note (2026-05-01), paper trading is deprioritized until the backtest pipeline + IS/OOS + cost stress + correlation gates are clean. This document is the spec for when we *do* deploy.

**Trigger to activate:** All four conditions met:
1. We have ≥4 candidates that have cleared Pella gates **AND** Monte Carlo (`monte_carlo.py` p95 DD < 25%, prob_ruin < 1%) **AND** IS/OOS split (OOS edge sign survives).
2. Cost-stress re-runs at 2× spread keep PF > 1.10.
3. Pairwise daily P/L correlation matrix between candidates is computed.
4. User signals "ready for paper trading."

---

## Why paper-trade at all

Backtests omit several real-world frictions that only show up live:
- **Variable spread / re-quotes** — historical tick data has historical spreads; today's broker spread distribution may differ.
- **Slippage at stop fills** — backtests assume execution at the exact stop price.
- **News-event gaps** — may stop you out at a much worse price than the stop level.
- **Partial fills** — large lots can't always fill at one price.
- **Broker quirks** — different brokers handle hedging, swaps, and position queries differently.
- **Server latency** — the EA's signal might fire seconds before broker accepts the order.

Paper trading on a demo with realistic broker conditions surfaces all of these without risking capital. If the live demo equity stays inside the Monte Carlo prediction band (95% CI), the strategy is safe to scale to a small real account. If it drifts persistently outside, something is broken — investigate before live capital.

## The candidate pool (current best 4-5)

These are the strategies that **clear Pella gates AND look likely to pass MC**:

| Strategy | Asset | TF | Net % | PF | Asymmetry | Trade ct | Archetype |
|---|---|---|---|---|---|---|---|
| Gen_Breakout | USDJPY | H1 | +369% | 1.20 | — | 1,271 | Range-breakout (both dir) |
| Gen_Breakout | XAUUSD | H1 | +140% | ~1.15 | 1.83× | 1,590 | Range-breakout (both dir) |
| ChannelBreakoutVIP | XAUUSD | H1 | +44.6% | 1.63 | 2.34× | 686 | Donchian-breakout (long) |
| GoldTrendBreakout | XAUUSD | H1 | +48.5% | 1.64 | 2.44× | 368 | Donchian/Keltner (long) |
| TuesdayTurnaroundNDX | XAUUSD | H1 | +14.4% | 1.48 | 1.06× | 141 | Pattern/seasonality |

**Diversification gap:** 3 of 5 are long-only Donchian-family on XAUUSD — they will correlate hard. After the new ports (IDNR4, TurtleSoup, HolyGrail, EightyTwenty) are tested, the candidate pool diversifies meaningfully.

## Broker accounts to use

Three demo accounts, one per portfolio bucket, to test broker-quirk diversity:

| Account | Broker | Account size | Bucket purpose |
|---|---|---|---|
| **Vantage Demo #1** | Vantage | 100,000 USD | Trend-follow bucket: Gen_Breakout × 2 + ChannelBVIP + GoldTrend |
| **FundedNext Demo #1** | FundedNext (existing <FUNDEDNEXT_ACCOUNT>) | 100,000 USD | Pattern bucket: TuesdayTurnaround + future pattern strategies |
| **Vantage Demo #2** | Vantage | 100,000 USD | Vol-expansion bucket: IDNR4 + TurtleSoup + HolyGrail (after ports validated) |

Reasoning per Puntos pattern: **one MT5 terminal per portfolio bucket**, accounts decoupled, EAs in each terminal don't interfere.

## Run window + review cadence

- **Minimum window:** 6 weeks
- **Soft target:** 8 weeks
- **Reason:** strategies trade 5-30 times/month per asset. 6 weeks ≈ 30-180 trades — enough sample to compare to backtest expected value with confidence.

**Weekly review (every Saturday):**
1. Pull weekly P/L per strategy from MT5 `mt5_history` MCP tool.
2. Append to a running CSV: `C:/Lab/results/_paper/weekly_pnl.csv`
3. Compute: weekly return, weekly max DD, trade count.
4. Compare to backtest expectation: is live within ±50% of expected weekly E/t?
5. Flag deviations.

## Kill / keep gates (apply at end of week 6)

Per Pipeline v1.3 Gate 8:

| Live signal | Action |
|---|---|
| Live equity inside MC 95% prediction band | **KEEP** — trading as expected |
| Live equity persistently below MC band (≥3 weeks below 5th-pct) | **CUT SIZE 50%** — investigate but don't kill |
| Live equity catastrophically below band (>2× MC worst-case DD) | **KILL** — structural issue |
| Live equity above band but P/L erratic | **MONITOR** — could be regime-favorable |
| Trade count <50% of expected for the period | **INVESTIGATE** — bridge / data / signal issue |
| Live E/t < 50% of historical with no obvious cause | **KILL** — edge has decayed |

## What live monitoring requires (infrastructure not yet built)

- [ ] **Telegram bot** for EA notifications + weekly summary (Puntos pattern, deferred)
- [ ] **VPS** to run terminals 24/7 (each terminal needs ~500 MB RAM, ~5 GB disk)
- [ ] **`paper_review.py` script** to pull MT5 weekly P/L → CSV → run MC predict-and-verify check
- [ ] **Cron / scheduled task** to run the weekly review automatically

These are deferred until activation trigger. Building them now is premature.

## Post-paper-trade decision tree

After 6-8 weeks:

```
                ┌── 5+ strategies pass live gates
                │   → Build live portfolio with kelly-fractioned sizing
                │   → Move to LucidFlex eval (futures path) or
                │     FundedNext / FTMO eval (CFD path)
                │
─── Review ─────┤── 2-4 strategies pass
                │   → Live trade what passed at small size
                │   → Continue researching to fill out portfolio
                │
                └── 0-1 strategies pass
                    → Iterate: re-test cost-stress, add filters,
                      try different assets
                    → Don't deploy real money
```

## Roles and ownership

- **User:** runs the strategies in MT5 terminals, monitors notifications, makes go/no-go decisions
- **Claude (this assistant):** writes the EAs, runs backtests, builds the analysis tools, writes weekly review reports

## Source

- Inspired by Puntos workflow (Discord 2026-04-28): "15-20 EAs paper trading, after 6-8 weeks kill some and look for best mix/correlation."
- Pella Pipeline v1.3 Gate 8 (Demo / Live deployment).
- LucidFlex eval rules (`lucid_rules.md`).
