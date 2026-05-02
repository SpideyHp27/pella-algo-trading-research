# SPECS-PATCH: ChannelBreakoutVIP_MT5 v0.3 -> v0.4

**Date:** 2026-05-02
**Trigger:** Multi-angle USDJPY 2026 Q1 investigation revealed:

| Spec | Sharpe |
|---|---|
| H1 PCT 1% (default) | -2.47 |
| H1 PCT 1% **with `UseFixedStopLoss=true` (no ATR trail)** | **+1.39** |

The ATR trailing stop (`close - 2*ATR`) sits too close to current price during low-volatility regimes. Every minor wiggle whipsaws out a position before the breakout can develop. In high-volatility regimes (2024 USDJPY) the trail does useful work. The fix is to make the trail volatility-aware.

## Hypothesis

The ATR trail is correctly designed for its trigger condition (give back 2 ATR of profit) but its *activation* is unconditional. In a regime where price barely moves more than 0.1% per hour, a 2-ATR trail effectively means "exit on the slightest pullback" — which is hostile to a breakout strategy.

**Fix design:** add `MinAtrPctForTrail` input. The ATR trail only adjusts the SL upward when `ATR / close >= MinAtrPctForTrail`. Below that threshold, the trail goes idle and only the channel SL (or fixed dollar SL) exits the position. Default 0.0 = backward-compatible (always trail, identical to v0.3 behavior).

## v0.4 changes (additive only — v0.3 logic preserved when default)

### One new input

```mql5
input group "Trailing-stop volatility gate (v0.4)"
input double MinAtrPctForTrail = 0.0;   // 0=always trail (v0.3 behavior); 0.05 recommended for FX
```

### One new check inside the existing trailing block

In `OnTick()`, the existing block is:
```mql5
if (!UseFixedStopLoss && atr0 > 0)
{
   double candidate = close0 - atr0 * AtrMult;
   if (candidate > newTrailingSL) newTrailingSL = candidate;
   trailingStop = newTrailingSL;
   ...
}
```

Becomes:
```mql5
bool atrTrailActive = !UseFixedStopLoss && atr0 > 0;
if (atrTrailActive && MinAtrPctForTrail > 0 && close0 > 0)
{
   double atrPct = atr0 / close0 * 100.0;
   if (atrPct < MinAtrPctForTrail) atrTrailActive = false;  // vol gate
}
if (atrTrailActive)
{
   double candidate = close0 - atr0 * AtrMult;
   if (candidate > newTrailingSL) newTrailingSL = candidate;
   trailingStop = newTrailingSL;
   ...
}
```

That's it. Three lines.

## What's NOT changing

- Channel breakout entry logic
- Channel-low SL anchoring (`UseChannelExit`)
- Fixed dollar SL fallback (`UseFixedStopLoss`)
- Percent-risk sizing (`UseRiskPercent`)
- Volatility-regime entry filter (`MinChannelWidthPct` from v0.3 — still defaults to 0)
- Daily target gates
- Magic number, Lots, Length, AtrPeriod, AtrMult

## Acceptance criteria for v0.4

1. Compiles 0 errors in MetaEditor CLI.
2. With `MinAtrPctForTrail=0.0` (default), backtest must produce **bit-identical** trade list and final balance to v0.3 on USDJPY H1 PCT 1% 2024 window. (Backward compat verification.)
3. With `MinAtrPctForTrail=0.05` (5 bps): on 2024 USDJPY, Sharpe should remain ≥ 80% of v0.3 baseline (2.83 → ≥ 2.27). On 2026 Q1 USDJPY, Sharpe should improve substantially (-2.47 → positive).
4. Walk-forward across all 4 windows (2023, 2024, 2025, 2026 Q1) with vol-gate on: at least 3/4 windows pass 1.0 Sharpe gate, AND no window has Sharpe < 0.

## Why a single new input vs the existing UseFixedStopLoss toggle

`UseFixedStopLoss=true` is binary — turns off trail entirely for the whole backtest. That's too crude: 2024 actually benefits from the trail (locks in the monster up-moves). What we want is a CONDITIONAL: trail when there's volatility to trail in, don't trail when there isn't. `MinAtrPctForTrail` is the cleanest expression of that.

## Why this also helps XAUUSD / IDNR4 conceptually

The same volatility-mismatch failure mode applies to any breakout strategy with an ATR trail. If ChBVIP XAUUSD or IDNR4 ever hit a low-vol gold regime, they'd suffer the same way. The vol-gated trail is a portable improvement, not a USDJPY-specific patch.

## Open question

Is 0.05% (5 basis points) the right default threshold? Need to characterize the ATR/close distribution across our test windows to pick a non-arbitrary value. v0.4 ships with the input but defaults to 0 (disabled, backward compat). Tomorrow's calibration session will set the recommended value per asset.
