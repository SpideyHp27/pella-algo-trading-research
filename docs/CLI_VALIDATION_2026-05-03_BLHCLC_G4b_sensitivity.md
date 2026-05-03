# G4b Param Sensitivity Report

## Baseline

- Label: `BLHCLC_NDX_baseline`
- Trades: 803
- PF: 1.396
- Sharpe: 1.444
- MaxDD%: 2.01
- p-HAC: <0.001

## Per-param sensitivity

| Param variant | Trades | PF | Sharpe | Δ Sharpe % | Verdict |
|---|---|---|---|---|---|
| BLHCLC_NDX_baseline_TP_- | 803 | 1.44 | 1.565 | 8.4% | PASS |
| BLHCLC_NDX_baseline_TP_+ | 799 | 1.337 | 1.274 | -11.8% | PASS |
| BLHCLC_NDX_baseline_lookback_- | 873 | 1.302 | 1.202 | -16.8% | PASS |
| BLHCLC_NDX_baseline_lookback_+ | 755 | 1.391 | 1.389 | -3.8% | PASS |
| BLHCLC_NDX_baseline_EMA_- | 803 | 1.396 | 1.444 | 0.0% | PASS |
| BLHCLC_NDX_baseline_EMA_+ | 803 | 1.396 | 1.444 | 0.0% | PASS |
| BLHCLC_NDX_baseline_pop_sl_- | 806 | 1.204 | 0.902 | -37.5% | PASS |
| BLHCLC_NDX_baseline_pop_sl_+ | 764 | 1.188 | 0.731 | -49.4% | WARN |
| BLHCLC_NDX_baseline_spread_- | 796 | 1.39 | 1.426 | -1.2% | PASS |
| BLHCLC_NDX_baseline_spread_+ | 803 | 1.396 | 1.443 | -0.1% | PASS |

## Overall

**WARN — at least one steep gradient (>40% Sharpe drop)**