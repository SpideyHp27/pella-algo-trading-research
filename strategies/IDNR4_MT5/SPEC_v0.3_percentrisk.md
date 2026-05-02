# SPECS-PATCH: IDNR4_MT5 v0.2 -> v0.3

**Date:** 2026-05-02
**Trigger:** ChannelBreakoutVIP_MT5 v0.2 percent-risk validation passed all 6 gates with Sharpe lift (USDJPY 1.35→1.45, XAUUSD 1.53→1.74) and ending-balance compounding (+44% USDJPY). Apply the same pattern to IDNR4 to capture compounding on its surviving Sharpe (XAUUSD ~0.79 IS — borderline).

## What v0.2 was missing vs Carver / Pella conventions

| Missing | Source | Impact |
|---|---|---|
| Volatility-scaled position sizing | Carver *Systematic Trading* Ch 11 (vol-targeting) | Fixed `Lots = 0.10` ignores account growth — flat returns instead of compounding |
| Risk-as-percent-of-balance input | Pella v0.2 ChannelBreakoutVIP convention (passed validation 2026-05-02) | Cannot run the same EA on different account sizes without manual lot adjustment |
| Max-lots cap | LucidFlex prop firm soft limit | Ungated percent-risk on a runaway-balance compounding chain could place implausibly large orders |

## v0.3 changes (additive only — v0.2 logic preserved)

### Three new inputs (mirror ChannelBreakoutVIP_MT5 v0.2 names exactly)

- `UseRiskPercent` (bool, default **false**) — backward compatible. When false, behavior is identical to v0.2.
- `RiskPercent` (double, default 1.0) — percent of `AccountInfoDouble(ACCOUNT_BALANCE)` to risk per trade.
- `MaxLotsCap` (double, default 10.0) — hard cap regardless of computation.

### One new helper

```mql5
double ComputeLotSize(double entryPrice, double slPrice)
```

- Uses `OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, 1.0, slPrice, entryPrice, profit)` to derive USD-loss-per-lot at the actual SL distance (broker-agnostic — works for any contract size).
- Returns legacy `Lots` if `UseRiskPercent=false`, `slPrice<=0`, `RiskPercent<=0`, the OrderCalcProfit call fails, or the computed loss-per-lot is non-positive (defensive).
- Normalizes against `SYMBOL_VOLUME_MIN`, `SYMBOL_VOLUME_MAX`, `SYMBOL_VOLUME_STEP`, then enforces `MaxLotsCap`.

### Wiring

`PlaceBracket()` computes `dynamicLots` ONCE per setup using `(buyPrice, sellPrice)` as the (entry, SL) pair (symmetric — the bracket's other side has the same width). All four trade calls in the v0.2 strategy receive `dynamicLots`:

1. `trade.BuyStop(dynamicLots, buyPrice, _Symbol, sellPrice, ...)` — long entry with bracket SL
2. `trade.SellStop(dynamicLots, sellPrice, _Symbol, buyPrice, ...)` — short entry with bracket SL
3. `trade.SellStop(dynamicLots, revPrice, ...)` — stop-reverse on long fill
4. `trade.BuyStop(dynamicLots, revPrice, ...)` — stop-reverse on short fill

Reverse orders inherit the lot size that was alive on the entry side — this is correct because the reverse IS the symmetric flip of the original setup.

## What's NOT changing

- ID/NR4 detection logic
- Bracket geometry (`setupHigh = iHigh(D1,1)`, `setupLow = iLow(D1,1)`)
- Stop-reverse logic
- MOC exit at MaxHoldDays
- v0.2 risk controls (FixedStopLossUSD, DailyMaxLossUSD, MinTradeValueUSD)
- Trailing stop (UseTrailingStop, TrailingActivationR, TrailingStepR)
- Magic number, EntryOffsetPoints, NRPeriod

## Backward compatibility

`UseRiskPercent = false` (default) reproduces v0.2 behavior bit-identically. Existing optimization runs / live deployments keep working without input changes. To turn on percent-risk, set `UseRiskPercent = true` in the EA inputs.

## Acceptance criteria for v0.3

1. Compiles 0 errors in MetaEditor.
2. Re-run XAUUSD H1 2020-2026 with `UseRiskPercent=false`: trade count, PF, Sharpe must match v0.2 within rounding (sanity — change must be backward-compatible).
3. Re-run XAUUSD H1 2020-2026 with `UseRiskPercent=true, RiskPercent=1.0`: trade count must stay within ±2% of v0.2 (signal layer untouched). Sharpe should improve via compounding. PF should not collapse.
4. MC bootstrap p95 max DD with percent-risk enabled must remain < 25%.
5. No Lot value computed should exceed MaxLotsCap.

## Why mirror ChannelBreakoutVIP exactly

Identical input names + helper signature means the validation orchestrator (`tools/run_validation.py`) can swap experts in TestSpec without input-schema rework. The pattern becomes a portable Pella convention: every EA that grows a percent-risk variant gets the same `UseRiskPercent / RiskPercent / MaxLotsCap` triplet, computed via `OrderCalcProfit` on the actual SL distance.
