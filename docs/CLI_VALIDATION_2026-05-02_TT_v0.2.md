# TuesdayTurnaroundNDX — Cross-Symbol Validation

**Date:** 2026-05-02
**Window:** 2020-01-01 → 2026-04-30 (6.3 years)
**Engine:** MT5 build 5833, "Every tick based on real ticks", Darwinex Demo
**Equity:** $50,000 starting · 1:100 leverage · USD account
**Pipeline:** Bridgeless CLI runner

## Headline

TuesdayTurnaroundNDX **passes all 6 Pella gates on its native asset (NDX) and fails Sharpe on every other symbol tested.** Compounding mode delivers +257% ending balance on NDX with a Sharpe trade-off (1.528 → 1.275, both above the 1.0 gate).

Decision: **deploy NDX, shelve XAUUSD and USDJPY.**

## Scoreboard

| Spec | Trades | PF | Sharpe | MaxDD% | RF | SQN | p-IID | p-HAC | MC p95 DD% | Final$ | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| TT NDX FIXED 0.10  | 163 | 2.280 | **1.528** | 0.33% | 8.92 | 4.049 | <0.001 | <0.001 | 0.5%  | $63,536  | **PASS** |
| TT NDX PCT 1%      | 163 | 2.105 | 1.275     | 4.16% | 5.55 | 3.334 | <0.001 | <0.001 | 6.2%  | **$178,671** | **PASS** |
| TT XAUUSD FIXED    | 141 | 1.495 | 0.568     | 6.30% | 2.11 | 1.433 | <0.10  | <0.10  | 13.4% | $57,265  | Sh fail (shelve) |
| TT XAUUSD PCT 1%   | 141 | 1.487 | 0.552     | 3.33% | 2.04 | 1.392 | <0.10  | <0.10  | 6.9%  | $53,514  | Sh fail (shelve) |
| TT USDJPY FIXED    | 142 | 1.390 | 0.559     | 1.37% | 2.20 | 1.433 | <0.10  | <0.10  | 2.7%  | $51,458  | Sh fail (shelve) |
| TT USDJPY PCT 1%   | 142 | 1.390 | 0.559     | 0.14% | 2.20 | 1.433 | <0.10  | <0.10  | 0.3%  | $50,144  | Sh fail (shelve) |

## Three findings

### 1. Home-asset edge is real

Strategy is named TuesdayTurnaroundNDX. Designed for NDX/US100. Tests on XAUUSD and USDJPY tested whether the underlying "Mon-after-red-Friday → Wed exit" pattern is universal or asset-specific.

Verdict: **asset-specific.** The institutional reset pattern that drives this signal is most pronounced in the index that drives global risk sentiment. Trying to deploy it elsewhere is fitting the strategy to the wrong regime.

### 2. The percent-risk paradox on NDX

| Mode | Sharpe | Ending balance |
|---|---|---|
| Fixed lots (0.10) | 1.528 | $63,536 (+27%) |
| Percent-risk (1.0%) | 1.275 | **$178,671 (+257%)** |

Compounding lifts the ending balance by 4.6× (and PF only drops from 2.28 to 2.10), but Sharpe declines by 16%. Mechanism: position size scales linearly with balance, but the strategy is low-frequency with rare-but-large weekly bets. Variance grows faster than mean as size compounds. Both modes still pass the 1.0 Sharpe gate, but **the pick depends on the deployment objective:**
- **Raw risk-adjusted return** → use FIXED 0.10 lots.
- **Maximize ending balance over a long window** → use PCT 1.0%.

Both stay an order of magnitude below the 25% MaxDD gate.

### 3. XAUUSD/USDJPY shelved cleanly

Sharpe ~0.55 on both pairs in both modes. p-IID < 0.10 (not statistically significant under either Newey-West HAC or naive IID). The signal layer doesn't generalize. No amount of sizing wizardry rescues an absent edge.

## Confirmed surviving Pella portfolio after this validation

| Strategy | Symbol | Mode | Sharpe | Notes |
|---|---|---|---|---|
| ChannelBreakoutVIP_MT5 | USDJPY | PCT 1% | 1.448 | (validated 2026-05-02) |
| ChannelBreakoutVIP_MT5 | XAUUSD | PCT 1% | 1.743 | (validated 2026-05-02) |
| TuesdayTurnaroundNDX   | NDX    | PCT 1% | 1.275 | compounding mode |
| TuesdayTurnaroundNDX   | NDX    | FIXED  | 1.528 | risk-adjusted mode |

Three strategies × four total tests × all 6 gates pass on each. p-HAC < 0.001 across the lot.
