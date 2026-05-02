# Correlation Matrix — Surviving Pella Portfolio

**Date:** 2026-05-02
**Window:** 2020-01-01 → 2026-04-30 (intersection days only)
**Method:** Pearson correlation on per-day aggregated P&L
**Tool:** `tools/correlation_survivors.py` (wrapper over `correlation_matrix.py`)

## Headline

**Average pairwise correlation = +0.189 — PASSES the Carver/Clenow 0.35 portfolio gate.**

Two redundant pairs flagged. After collapsing them, the **5 specs reduce to 3 truly independent allocations**.

## Pair-by-pair

| Pair | r | Overlap (days) | Verdict |
|---|---|---|---|
| TT_NDX_FIXED × TT_NDX_PCT | **+0.96** | 163 | REDUNDANT — same strategy, same asset, only sizing differs |
| ChBVIP_XAUUSD × IDNR4_XAUUSD | **+0.63** | 69 | REDUNDANT — both trade gold, collinear by asset |
| ChBVIP_XAUUSD × TT_NDX_PCT | +0.24 | 66 | INDEPENDENT (good) |
| ChBVIP_XAUUSD × TT_NDX_FIXED | +0.22 | 66 | INDEPENDENT (good) |
| ChBVIP_USDJPY × IDNR4_XAUUSD | +0.14 | 61 | INDEPENDENT (good) |
| TT_NDX_FIXED × IDNR4_XAUUSD | +0.10 | 18 | INDEPENDENT (good) |
| TT_NDX_PCT × IDNR4_XAUUSD | +0.05 | 18 | INDEPENDENT (good) |
| ChBVIP_USDJPY × ChBVIP_XAUUSD | -0.11 | 167 | INDEPENDENT (good) |
| ChBVIP_USDJPY × TT_NDX_PCT | -0.16 | 67 | INDEPENDENT (good) |
| ChBVIP_USDJPY × TT_NDX_FIXED | -0.17 | 67 | INDEPENDENT (good) |

## Three independent allocations after collapsing redundancies

| Bucket | Strategy candidates | Best Sharpe | Notes |
|---|---|---|---|
| **FX Major** | ChBVIP USDJPY H1 PCT 1% | 1.45 | only candidate in this bucket |
| **US Equity Index** | TT NDX H1/H4 (FIXED or PCT — pick one) | 1.53 (FIXED) / 1.27 (PCT) | choose by deployment objective |
| **Gold** | ChBVIP XAUUSD H1 PCT *or* IDNR4 XAUUSD H4 PCT | 1.74 (ChBVIP) / 1.10 (IDNR4) | treat as one allocation; ChBVIP wins on Sharpe |

## What this means for deployment

- **Don't run all 5 specs in parallel as if they're independent risks.** TT FIXED and TT PCT are 96% the same return stream; running both doubles position sizing on every NDX trade with no real diversification benefit.
- **If keeping both gold strategies, halve their position sizes** — they trade the same underlying. Otherwise gold concentration risk doubles.
- **Genuine diversification:** USDJPY-currency / NDX-equity / gold-metal triad. Three independent return streams. This is the actual deployable portfolio shape.

## Methodology note

Correlation computed only on dates where both strategies had at least one trade (intersection method, not zero-padded union). For low-frequency strategies like IDNR4 H4 vs TT NDX, this means the overlap can be small (18-69 days) — the +/-0.14 correlations should be read as "probably independent but small-sample." The TT FIXED × PCT (+0.96, 163 days) and ChBVIP USDJPY × XAUUSD (-0.11, 167 days) results are the high-confidence ones.

## Pipeline status

This satisfies Pella Pipeline v1.3 Gate 7 (correlation audit). Remaining gates:
- Gate 5: Walk-forward analysis (next)
- Gate 8: Live demo / paper trade
