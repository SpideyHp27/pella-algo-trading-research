# SPECS-PATCH: PellaMetaEA v0.1 — greenfield

**Date:** 2026-05-02
**Trigger:** Three independent surviving sub-strategies (after correlation collapsing of redundant pairs) need to be deployable from a single chart-attached EA. Running 5 separate EAs on 5 separate charts is operational overhead — and they need shared news-blackout / gap-filter / anti-overlap rules anyway.

## Goal

A single Expert Advisor file `PellaMetaEA.mq5` that:
1. Wraps the three independent strategy buckets (USDJPY-FX, NDX-equity, Gold-metal) as **subsystems** (Carver's terminology, *Systematic Trading* Ch 7).
2. Activates only the relevant subsystem(s) based on the chart symbol where attached.
3. Enforces shared safety rules: news blackouts, weekend flat, max-portfolio-DD circuit, max concurrent positions cap.
4. Reports per-subsystem P&L in the journal so we can decommission individual subsystems if any goes stale.

## Architecture

### One EA, multi-symbol, single chart attachment

The Pella deployment model: attach `PellaMetaEA.mq5` to ONE chart (e.g. NDX H1) but the EA monitors and trades all configured symbols via cross-symbol ticks. Reason: prop firms typically count "EAs running" not "symbols traded" — fewer attachments = simpler ops.

### Subsystem registry

Each subsystem is a discrete code block with:
- **Trigger:** which symbol + timeframe + entry conditions
- **Sizer:** which lot calculation (fixed / percent-risk)
- **Exits:** SL/TP/trailing/MOC rules
- **Magic number:** unique per subsystem (so positions can be tracked back to the right logic)

Subsystem table for v0.1:

| ID | Magic | Symbol | TF | Logic source | Entry | Sizing |
|---|---|---|---|---|---|---|
| 1 | 7011001 | USDJPY | H1 | ChannelBreakoutVIP_MT5 v0.2 | Donchian channel breakout | PCT 1% |
| 2 | 7011002 | XAUUSD | H1 | ChannelBreakoutVIP_MT5 v0.2 | Donchian channel breakout | PCT 1% |
| 3 | 7011003 | NDX    | H1 | TuesdayTurnaroundNDX (port of `CalculateLotSize`) | Mon-after-red-Friday → exit Wed | PCT 1% |
| 4 | 7011004 | XAUUSD | H4 | IDNR4_MT5 v0.3 | Inside Day + NR4 OCO breakout | PCT 1% |

Note: TT NDX FIXED variant (Sharpe 1.53) is intentionally omitted from v0.1 — it's 96% correlated with TT NDX PCT and doesn't add diversification. The PCT variant is preferred for compounding.

Note: ChBVIP XAUUSD and IDNR4 XAUUSD are 0.63 correlated. Both included in v0.1 BUT each runs at HALF the risk (RiskPercent = 0.5) to keep total gold concentration risk constant. User can change later.

### Shared safety layer

These apply to ALL subsystems uniformly:

- **News blackout** (input `BlackoutBeforeNewsMin = 30`, `BlackoutAfterNewsMin = 15`): if NFP / FOMC / CPI is within that window, no NEW entries (existing positions unaffected). Pre-loaded news calendar — manual maintenance.
- **Weekend flat** (input `FlatBeforeWeekendHours = 2`): close all positions 2 hours before market close on Friday. Avoids weekend gap risk.
- **Max portfolio DD circuit** (input `MaxPortfolioDDPercent = 8.0`): if account equity drops 8% from running peak, close all positions and disable for the rest of the calendar week. Below the 25% MC gate, well below LucidFlex 12% EOD limit.
- **Max concurrent positions cap** (input `MaxConcurrentPositions = 3`): never more than 3 open positions across all subsystems. Prevents a multi-subsystem-simultaneous-fire from concentrating risk.
- **Magic number isolation:** each subsystem only sees and manages positions tagged with its own magic. Hand-placed manual trades (other magic) are invisible.

## Inputs (one input group per subsystem + one shared)

```
input group "==== Subsystems ===="
input bool   EnableSubsystem1_ChBVIP_USDJPY = true;
input bool   EnableSubsystem2_ChBVIP_XAUUSD = true;
input bool   EnableSubsystem3_TT_NDX        = true;
input bool   EnableSubsystem4_IDNR4_XAUUSD  = true;

input group "==== Subsystem 1 — ChBVIP USDJPY H1 ===="
input double S1_RiskPercent = 1.0;
input double S1_MaxLotsCap  = 10.0;
... (mirror of v0.2 inputs)

input group "==== Subsystem 2 — ChBVIP XAUUSD H1 ===="
input double S2_RiskPercent = 0.5;   // halved due to gold collinearity with subsystem 4
... etc

input group "==== Subsystem 3 — TT NDX H1/H4 ===="
input double S3_RiskPercent = 1.0;
input int    S3_EntryHour   = 16;
input int    S3_EntryMinute = 30;
... etc

input group "==== Subsystem 4 — IDNR4 XAUUSD H4 ===="
input double S4_RiskPercent = 0.5;   // halved due to gold collinearity with subsystem 2
... etc

input group "==== Shared safety layer ===="
input double MaxPortfolioDDPercent  = 8.0;
input int    MaxConcurrentPositions = 3;
input int    FlatBeforeWeekendHours = 2;
input int    BlackoutBeforeNewsMin  = 30;
input int    BlackoutAfterNewsMin   = 15;
input bool   DebugMode              = false;
```

## What's IN scope for v0.1

- All 4 subsystems wrapped, each behind its `EnableSubsystem*` toggle.
- Per-symbol cross-tick logic so the EA can run on any symbol's chart and still trade USDJPY/XAUUSD/NDX as needed (uses `CopyRates` + `OnTimer` polling for non-chart symbols).
- Shared safety layer.
- Per-subsystem magic numbers + position tracking.

## What's OUT of scope for v0.1 (deferred to v0.2)

- News calendar auto-fetching (manual JSON file for v0.1)
- Per-subsystem realised-P&L journaling beyond MT5's own
- Subsystem allocation rebalancing (v0.1 risk %s are fixed at compile time)
- Adding more subsystems (only the 4 surviving v0.1 specs)

## Acceptance criteria for v0.1

1. Compiles 0 errors in MetaEditor CLI.
2. Smoke test: launch on one chart (e.g. NDX H1) with all subsystems enabled. Backtest 2024-01-01 → 2024-06-30. Should produce trades from all enabled subsystems' source symbols (verify via per-magic-number breakdown of deals).
3. With only one subsystem enabled, the trade list should match the standalone EA's output for that subsystem within ±3% trade count (the safety layer may cull a few).
4. Disabling a subsystem must produce ZERO trades from its symbol (verifies the toggle works).
5. With all subsystems enabled, total trade count should approximate the sum of the standalones, minus collisions caught by the safety layer.

## Why mirror existing strategy code

Each subsystem is a near-direct port of the standalone EA's PlaceBracket / entry logic, with two changes:
- Magic number replaced with the subsystem-specific magic
- Lot sizing helper renamed to `S<N>_ComputeLotSize` and uses `S<N>_RiskPercent`

This is a **mechanical refactor**, not a redesign. The strategies have already passed all 6 Pella gates standalone; the Meta EA is a deployment wrapper, not a strategy change.

## Open question for v0.2

Should the safety layer's "max portfolio DD" track DD per subsystem separately (so a misbehaving subsystem can be auto-disabled without killing the working ones) or only at the portfolio level? v0.1 ships with portfolio-level only for simplicity. Revisit after first paper-trade window.
