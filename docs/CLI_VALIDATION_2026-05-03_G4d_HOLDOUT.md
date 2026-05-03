# G4d Holdout Validation Report

Methodology: each strategy's full window split into IS (everything before holdout cutoff) and OOS holdout (most recent year). The OOS year is data the strategy was NEVER tested on during research — the most direct curve-fit detector.

Verdicts:
- PASS — holdout Sharpe within 50% of IS AND holdout PF ≥ 1.0
- WARN — holdout Sharpe drop > 50% (edge degraded but PF still positive)
- FAIL — holdout PF < 1.0 (strategy didn't generalize)
- COLLAPSE — holdout Sharpe < 0 (actively losing OOS)

| Strategy | IS Sharpe | OOS Sharpe | Δ Sharpe % | IS PF | OOS PF | IS Trades | OOS Trades | Verdict |
|---|---|---|---|---|---|---|---|---|
| ChBVIP_USDJPY_H1_v04 | 1.061 | 2.445 | 130.4% | 1.689 | 2.95 | 263 | 44 | **PASS** |
| ChBVIP_XAUUSD_H1_v04 | 1.42 | 2.774 | 95.4% | 1.714 | 2.263 | 462 | 106 | **PASS** |
| TT_NDX_H1_PCT | 1.248 | 1.387 | 11.1% | 1.964 | 2.067 | 135 | 27 | **PASS** |
| IDNR4_XAUUSD_H4_v03 | 1.057 | 1.451 | 37.3% | 1.905 | 2.294 | 136 | 33 | **PASS** |
| PellaMarubozu_XAUUSD_M5_NY | 0.818 | 3.601 | 340.2% | 1.186 | 1.775 | 686 | 234 | **PASS** |
| BLHCLC_NDX_D1H4 | 1.867 | -0.068 | -103.6% | 1.523 | 0.984 | 697 | 106 | **COLLAPSE** |

## Decision rule
- Anything PASS stays at its current Tier.
- Anything WARN gets demoted from T1 → T2 (real edge but not as robust as we thought).
- Anything FAIL or COLLAPSE gets shelved regardless of prior gates.