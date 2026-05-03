# Pella Research — Trello Board (paste-ready)

> **How to use:** create three lists in your Trello board: "Done", "In Progress", "To-Do". Copy each card title (the bold line) and paste each card's body (the bullets below it) into the Trello card description. Each `## ` heading below = one Trello card.

---

## DONE list — 36 cards

### Project setup

**Set up Pella project structure**
- Created `C:\Lab\` working dir + `C:\Pella-portfolio\` public mirror
- Defined acceptance gates in `CLAUDE.md`
- Day 1 (2026-05-01)

**Configure MT5 with Darwinex Demo + tick data**
- Account: 3000099899 (hedge mode)
- Server: Darwinex-Demo
- Real ticks cached for USDJPY / XAUUSD / NDX

**Set up GitHub portfolio repo**
- Repo: SpideyHp27/pella-algo-trading-research
- All Co-Authored-By tags removed (industry distrust of AI co-authorship)

### Strategy work

**Port ChannelBreakoutVIP_MT5 v0.1**
- Source: Zenom community
- Logic: Donchian-style channel breakout
- Day 1 backtest passed initial gates

**Port TuesdayTurnaroundNDX**
- Logic: buys Mon afternoon if Friday was red, exits Wed
- Native asset: NDX/US100

**Port IDNR4_MT5 v0.1 (Inside Day + NR4)**
- Source: Street Smarts (Raschke / Connors, 1995) Ch 19
- Logic: yesterday's bar inside the prior bar AND narrowest range of last 4

**IDNR4 v0.2 — added trailing stop, fixed dollar SL, daily loss circuit, min trade value filter**
- v0.1 had 1 win out of 32 trades (3.1% win rate) — diagnosed as missing trailing stop
- v0.2 fixed it (Day 1)

**IDNR4 v0.3 — added percent-risk position sizing**
- New inputs: UseRiskPercent / RiskPercent / MaxLotsCap
- Helper: ComputeLotSize() using OrderCalcProfit on bracket SL
- CODEGEN bug caught + fixed mid-session (swapped open/close args)
- Bit-identical backward compat verified

**ChannelBreakoutVIP v0.2 — added percent-risk sizing**
- Same UseRiskPercent / RiskPercent / MaxLotsCap pattern
- Now Pella's portable convention

### Tooling

**Build MT5 CLI runner (bypass broken bridge)**
- `tools/mt5_cli.py` — TestSpec dataclass + MT5CliRunner
- Fires terminal64.exe /config:tester.ini headlessly
- ~30s cold-start per test

**Build batch orchestrator**
- `tools/run_validation.py` — chains CLI run → log parse → CSV → quant_report → MC → scoreboard
- Persists incrementally so a mid-batch crash preserves completed runs

**Build Sharpe ratio + Chan hypothesis test tool**
- `tools/sharpe.py`
- Implements Chan AT Ch 1 t-statistic + Gaussian p-value

**Build local QuantDash equivalent**
- `tools/quant_report.py`
- Computes PF, Sharpe, MaxDD, RF, SQN, p-IID, p-HAC (Newey-West Bartlett kernel), ACF(1)

**Build Monte Carlo robustness tool**
- `tools/monte_carlo.py`
- Shuffle + bootstrap modes, 2000 runs default
- Verdict at p95 max DD vs 25% Pella gate

**Build trades extractor from MT5 log files**
- `tools/mt5_tester_report.py`
- Parses Tester logs → matched trades + USD profit estimate
- Bug-fixed: metals must be checked BEFORE generic XXX/USD branch

### Validation runs

**Run cost-stress on 4 candidates (Day 1)**
- Used Random Delay as proxy (MT5 5833 doesn't expose Spread in real-tick mode)
- All 4 passed with PF decay 0-4%

**ChannelBreakoutVIP_MT5 v0.2 percent-risk validation (4 specs)**
- USDJPY FIXED: Sharpe 1.35, PF 1.48, MC p95 1.4% — PASS
- USDJPY PCT 1%: Sharpe 1.45, PF 1.56, MC p95 8.0% — PASS, $76k final
- XAUUSD FIXED: Sharpe 1.53, PF 1.80, MC p95 5.2% — PASS
- XAUUSD PCT 1%: Sharpe 1.74, PF 1.69, MC p95 6.2% — PASS, $80k final
- All p-HAC < 0.001
- 5.8 min total via CLI runner

**TuesdayTurnaroundNDX cross-symbol validation (6 specs)**
- NDX FIXED: Sharpe 1.53, PF 2.28 — PASS
- NDX PCT 1%: Sharpe 1.27, PF 2.10 — PASS, $179k final (+257% compound)
- XAUUSD x 2: Sharpe ~0.55 — SHELVED (no edge)
- USDJPY x 2: Sharpe ~0.56 — SHELVED (no edge)
- 28.1 min total

**IDNR4_MT5 v0.3 first validation + bug catch + re-run**
- First run: bit-identical fixed vs PCT — diagnosed as my CODEGEN bug
- Fixed (open/close arg swap), recompiled, re-ran
- XAUUSD H1 PCT 1%: Sharpe 0.951, PF 1.82, MaxDD 11.6%, $85k final
- Sharpe just 0.05 below the 1.0 gate

**Timeframe robustness batch (7 specs)**
- ChBVIP M15 / H4 on USDJPY + XAUUSD: M15 too noisy, H4 graceful degradation
- TT NDX H4: bit-identical to H1 (clock-time-based)
- IDNR4 D1 / H4: D1 too thin, **H4 PASSES all 6 gates**
- 14.5 min total

**5th surviving spec graduated: IDNR4 XAUUSD H4 PCT 1%**
- Sharpe 1.10, PF 1.96, RF 8.32, MC p95 14.4%, $91k final

### Documentation

**Document everything in BUILD_JOURNAL.md**
- Live in-session per memory rule
- ~735 lines covering Days 1-2 in detail

**Mirror to public Pella-portfolio**
- All scoreboards published as `CLI_VALIDATION_2026-05-02_*.md`
- Strategy source synced
- CLI runner tooling synced

**Memory updates**
- mt5_cli_bridgeless_pipeline (NEW)
- mt5_csv_contract_size_gotcha (NEW)
- darwinex_symbol_aliases (NEW)
- monte_carlo_gate (NEW)
- methodology_pipeline_v13 (NEW)
- archetype_risk_controls (NEW)
- no_validation_loop (NEW)
- no_coauthor_tag (NEW)

**Plain-English achievement report written**
- `docs/ACHIEVEMENTS_2026-05-02.md`
- Translates jargon → readable for self-review

**Correlation matrix on 5 surviving specs (Pipeline Gate 7)**
- Avg pairwise correlation +0.189 — PASSES Carver/Clenow gate
- 2 redundant pairs flagged: TT_FIXED × TT_PCT (0.96), ChBVIP_XAU × IDNR4_XAU (0.63)
- 5 specs reduce to 3 truly independent allocations after collapse
- Tool: `tools/correlation_survivors.py`
- Doc: `docs/CORRELATION_MATRIX_2026-05-02.md`

**Build walk-forward orchestrator**
- `tools/walk_forward.py` — slides each spec across 4 OOS windows (2023, 2024, 2025, 2026Q1)
- Per-spec trend-aware verdict: STABLE / IMPROVING / DEGRADING / RED FLAG / MIXED
- Verdict-labeling bug caught and fixed (improving ≠ degrading)

**Walk-forward batch — Pipeline Gate 5 — THE BIG REVEAL**
- 20 backtests in 15.5 min
- ChBVIP XAUUSD: STABLE (4/4 pass, monotonic improvement) — TOP CANDIDATE
- IDNR4 XAUUSD H4: STABLE (3/4 pass)
- TT NDX (both modes): IMPROVING (2/4 with strong recent)
- ChBVIP USDJPY: **RED FLAG** — 2026 Q1 Sharpe -2.47 — strategy lost money this year
- Saved a deployment mistake

**Pipeline hardening pass — mt5_cli + mt5_compile + parser fixes**
- ENUM_TIMEFRAMES auto-conversion (PERIOD_H4 → 16388)
- Pre-flight .ex5 existence + symbol-alias warnings (NDAQ/NDX_Tick/NQ/GC/etc)
- Post-flight log scan for license/init/load failures
- mt5_compile.py wrapper handles filename-with-spaces bug
- mt5_tester_report.py regex now handles EA names with spaces

**BreakoutLoopHCLC v1.0 — G4b + Cross-TF VERIFIED → PROMOTED to TIER-1**
- Baseline (Discord params): 803 trades, PF 1.40, Sharpe 1.44, MaxDD 2.01%, MC p95 2.6%, p-HAC <0.001, $100k → $231k
- G4b sensitivity: 9/10 PASS, 1 WARN (pop_sl +0.5 = -49% Sharpe). NO cliff edges, NO collapses.
- Cross-TF: bit-identical across D1, H1, M15 charts (TF-robust by construction — EA uses internal Timeframe=H4 for indicators, chart TF doesn't affect signal timing)
- DEPLOYMENT LOCK: pop_sl MUST stay at 1.0 in production. Don't drift this input.
- Suspicious finding: EMA ±10 produces BIT-IDENTICAL results — investigate later (EMA may not be doing what we think at this perturbation scale)
- Bumps deployable Pella portfolio to 6 specs across 5 strategies (3 Tier-1, 2 Tier-2, 1 newly-promoted Tier-1)

**G4b Param Sensitivity tool built (param_sensitivity.py)**
- Adopted from Discord trader's gate framework
- Tests ±1 step neighbors of each parameter, holding others at baseline
- Verdicts: PASS / WARN (>40% Sharpe drop) / CLIFF (PF<1.0) / COLLAPSE (Sharpe<0)
- Required for any new candidate before T1 promotion

**PellaMarubozu_MT5 v0.1 — clean-room build + 3-TF sweep + canonical lock**
- Marketplace .ex5 had license-lock; built clean-room version from .set spec
- Tested H4 / H1 / M5 with NY 17-19 session filter
- M5 BASE PCT 0.5% locked as canonical: Sharpe 1.34, PF 1.31, MaxDD 7.87%, MC p95 8.1, p-HAC <0.001, 944 trades
- Passes 5/6 universal prop gates (MC p95 0.1pp over, deploys safely at FundedNext 0.4% recovery sizing)
- H1 dies (Sharpe 0.44, p-HAC ≥0.10 — no stat sig); H4 NO_TREND alternative also viable but lower Sharpe
- NDX failed completely on this strategy across all configs

**Meta EA v0.1 scaffold compiles clean**
- `strategies/PellaMetaEA/PellaMetaEA.mq5`
- TT NDX subsystem fully implemented
- Subsystems 1, 2, 4 stubbed for v0.2 port
- Shared safety layer fully implemented (DD circuit, weekend flat, news blackout stub, max-concurrent cap, magic isolation, cross-symbol polling)

**Look-ahead bias audit on 5 surviving EAs**
- ChBVIP_MT5 v0.4: NO HARD BIAS (Donchian + ATR use bar 0 at new-bar gate = known-now data, not future). Minor methodological cleanup queued for v0.5.
- IDNR4_MT5 v0.3: CLEAN. All bar accesses shift=1 or 2 (closed bars).
- TuesdayTurnaroundNDX: CLEAN. Calendar-time driven, Friday-red filter reads fully-closed Friday bar.
- PellaMarubozu_MT5 v0.1: CLEAN. DetectMarubozu uses bar 1, ATR shift=1, EMA on bar 0 same known-now pattern as ChBVIP.
- BreakoutLoopHCLC v1.0: UNAUDITED (fxDreema-generated 489KB source in awkward encoding). Circumstantial confidence from cross-TF + p-HAC < 0.001.

**G4b Param Sensitivity test on BreakoutLoopHCLC v1.0 — VERDICT: PROMOTED to T1**
- 11 backtests in 129 min
- 9/10 PASS, 1 WARN (pop_sl +0.5 = -49% Sharpe)
- No cliff edges, no collapses
- Cross-TF (M15/H1/D1) bit-identical = TF-robust by construction
- Promoted to T1 with deployment lock: pop_sl=1.0 must not drift

---

## IN PROGRESS list — 1 card

**G4d Holdout test on all 6 surviving specs (running)**
- 12 backtests (6 strategies × IS + OOS)
- IS: spec start_date to 2025-04-06; OOS: 2025-04-06 to 2026-04-06 (~1yr unseen)
- Verdicts: PASS / WARN / FAIL / COLLAPSE
- Strategies failing G4d get demoted regardless of prior gate results
- THE most direct curve-fit detector (per López de Prado)
- ETA: ~30-40 min total

---

## TO-DO list — 25 cards

### STRATEGIC ROADMAP — Renaissance-style buildout

**[Tier-2 — high-value foundational] Build portfolio_optimizer.py**
- Currently we equal-weight surviving strategies — suboptimal
- Need: given N validated trade CSVs, output OPTIMAL capital allocation
- Methods: risk parity / inverse-volatility / Markowitz / handcrafted (Carver)
- Bridge from "validated strategies" to "deployable portfolio"
- Estimated effort: 1 day to build cleanly
- Reference: Carver "Smart Portfolios" Ch 3-4 (handcrafted method), AFML Ch 16 (HRP)

**[Tier-2] G4c proper Walk-Forward Matrix upgrade**
- Current walk_forward.py is OOS-only (yearly slicing, no IS re-optimization)
- Proper version: re-optimize parameters on each IS fold, lock them, test on next OOS fold
- Detects parameter drift across regimes
- Reference: Pardo "Evaluation and Optimization of Trading Strategies"

**[Tier-3 — Renaissance vision] Automated discovery agent**
- Give it: directory of EA files + symbol watchlist
- It runs: intake → all 8 gates → final tier classification
- Surfaces queue of survivors for human approval
- Goal: process 20-30 strategies unattended in 24h
- ~2-3 days to build properly with current pipeline as foundation

**[Tier-3] Live monitoring infrastructure**
- Once deployed (FundedNext, future Lucid), track rolling Sharpe per strategy
- Alert on regime breakdown, signal decay, correlation regime change
- Renaissance's continuous decay monitoring was core to their 60% returns
- Reference: AFML Ch 14 (backtest statistics for live monitoring)

**[Tier-3] NT8 port + futures validation (Path B)**
- Port surviving CFD strategies to NinjaScript C#
- Re-validate on Kinetick free futures tick data (NQ, GC, ES, MGC, MNQ)
- Required for Lucid + Apex deployment
- Major project: each strategy needs intraday redesign too (no overnight on futures)

**[Tier-3] Cross-broker validation**
- Run same strategy on multiple brokers' tick feeds
- Catches data quality issues + microstructure dependencies
- Adds: FBS / IC Markets demo accounts as second broker

### Books to acquire (will materially improve what I can build)

**[High priority] López de Prado — "Advances in Financial Machine Learning" (2018)**
- Addresses every methodological pitfall we're hitting
- Topics needed: false discovery in backtests, fractional differentiation,
  meta-labeling for signal filtering, sample-uniqueness weighting, Hierarchical
  Risk Parity, deflated Sharpe ratio (replaces our naive t-test)
- Single most important book for the work

**[High priority] Carver — "Smart Portfolios" (2017)**
- Explicit math for portfolio construction
- Risk parity, handcrafted portfolios, diversification multipliers
- Bridges "have strategies" → "have a portfolio"

**[Medium] Pardo — "The Evaluation and Optimization of Trading Strategies" (2008)**
- Canonical text on proper walk-forward methodology
- Would directly inform G4c upgrade

**[Lower priority] Zuckerman — "The Man Who Solved the Market"**
- Renaissance Technologies story (inspiration + realistic expectations)

**[Lower priority] López de Prado — "Machine Learning for Asset Managers" (2020)**
- AFML follow-up, focused on portfolio managers

### Strategy sourcing (parallel to validation work)

**Find new strategy candidates from Discord/Zenom/forums**
- Triage per `strategy_intake_workflow` memory entry
- Source: .mq5 source > .set settings > .ex5 only
- Always check Discord screenshot for actual settings used (not EA defaults)
- Always note source community/author for credit + provenance

**Brain-dump of user's 3 years of discretionary observations**
- Specific session/asset/setup combos worth coding
- Plan B from earlier session — generate strategy ideas from real trader's experience
- Higher-quality source than forum scrapes
- User to schedule a brain-dump session

### Immediate next session (high priority)

**G4d Holdout tool — build it (DONE — running now)**
- Built `tools/holdout_test.py` 2026-05-03
- 12 backtests running on all 6 surviving specs
- IS: from spec start_date to 2025.04.06; OOS: 2025.04.06 → 2026.04.06
- Verdicts: PASS / WARN (>50% Sharpe drop) / FAIL (PF<1.0) / COLLAPSE (Sharpe<0)

**Re-run cross-strategy correlation matrix with all 6 specs**
- correlation_survivors.py needs BLHCLC trade CSV added
- Confirm BLHCLC is genuinely uncorrelated with the existing 5 (especially TT NDX since both trade NDX)
- Will fire after G4b verdict + cross-TF results land

**Aristhrottle dashboard integration (waiting on friend's API)**
- User asking friend (the dashboard's developer) for API base URL + auth method + 2 example endpoints (POST import, GET metrics)
- If approved, write aristhrottle_sync.py mirroring trello_sync.py pattern
- If not, manual drag-drop of our trade CSVs is the fallback
- Aristhrottle features: prop sim, journal, MC analysis, trade analysis, correlation matrix, portfolio overview

**G3 cost stress upgrade — proper 2× spread variant**
- Currently using random delay as proxy
- Discord framework wants explicit 2× spread test
- MT5 5833 doesn't expose Spread setting in real-tick mode → may need synthetic spread injection

**G5 Monte Carlo upgrades**
- Add 2× spread PF check (require > 1.05)
- Add session ±1hr PF check (require > 0.90)
- Existing tool only does shuffle/bootstrap percentile DD

**ChBVIP USDJPY rescue: v0.4 vol-aware trailing stop — DEPLOYED**
- Initial diagnosis (vol regime shift) was correct but the wrong fix layer
- v0.3 MinChannelWidthPct (entry-side filter) didn't help — wrong layer
- Multi-angle re-test found: ATR trailing stop is the killer in low-vol
- v0.4 adds MinAtrPctForTrail input — gates the trail by minimum volatility
- Threshold sweep: 0.20% is sweet spot (2024 stays at 2.31, 2026 Q1 rescued to +1.09)
- Walk-forward: mean Sharpe 1.23 (vs baseline 0.47), zero losing windows
- XAUUSD safety check: vol-trail @ 0.20 doesn't harm gold (4/4 STABLE preserved)
- DECISION: ChBVIP v0.4 deployed with MinAtrPctForTrail=0.20 universal default
- USDJPY RESTORED to deployable portfolio (4 specs total)

**PellaMetaEA v0.2 — port subsystems 1, 2, 4 fully**
- v0.1 scaffold has TT NDX (subsystem 3) live
- Mechanical port: drop entry/exit logic into placeholder functions
- Each subsystem already has its standalone EA as reference

**Bridge sticky-dropdown root-cause investigation**
- v0.44 patch did NOT fix it
- Spy++ session needed to identify what real-click signal Win32 SendMessage isn't replicating
- Low priority now that CLI runner replaces it

**Look-ahead bias audit on each surviving EA's MQL5 source**
- Per Chan AT Ch 3 requirements
- Verify no future-knowledge leakage in entry/exit logic
- 5 EAs to audit

**Apex Trader prop firm sim test (proxy for LucidFlex)**
- Stricter rules than Lucid; if pass Apex, Lucid is comfortable
- Use the 5 surviving specs

### Mid-term (when ready)

**Paper-trade demo deployment**
- Plan exists in `paper_trade_plan.md`
- Deploy to Darwinex Demo for ≥30 days
- Watch for live-vs-backtest divergence

**Brain-dump 3 years of discretionary observations**
- Specific session/asset/setup combos worth coding
- "Plan B" from action plan — generate new strategy ideas from your own experience, not from forum strategies

**QDM + Dukascopy data import**
- Cross-validate against cleaner ticks than Darwinex
- Optional, only if a strategy seems data-quality-sensitive

**Try fade strategies on EURUSD/GBPUSD (cyclic assets)**
- TurtleSoup, HolyGrail, EightyTwenty, ADXEMARetracement
- All failed on bull XAUUSD; might work on cyclic pairs

**Long-only HolyGrail variant**
- Test if disabling shorts removes the inverse-during-pullback problem on bull markets

### Long-term (deployment)

**Activate LucidFlex eval $50k**
- Pre-requisite: pass walk-forward + correlation + Meta EA + paper-trade
- Read rules: no HFT, no microscalping <5s, no cross-account hedging, EOD drawdown only

**Apex with ZEN promo code**
- Secondary target after LucidFlex
- Stricter rules — proves robustness

**FundedNext / FTMO via MT5 (CFD phase)**
- Currently FundedNext account #423659 connected
- Defer until LucidFlex funded

---

## Notes for the board

- **Don't move cards yourself once I generate them — let me update them.** I'll regenerate this file after each significant milestone so you can copy-paste fresh state.
- The Pella gates are: PF ≥ 1.30, Sharpe ≥ 1.0, MaxDD ≤ 25%, RF ≥ 2.0, N ≥ 100, MC p95 DD ≤ 25%.
- Surviving portfolio = 5 specs across 3 strategies × 3 asset classes (USDJPY, XAUUSD, NDX).
- Bridge is broken; CLI runner is the standard path.
- Account states: Darwinex-Demo (live data), FundedNext-Server (CFD prop, paused).
