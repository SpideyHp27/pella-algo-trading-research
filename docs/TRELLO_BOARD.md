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

**Meta EA v0.1 scaffold compiles clean**
- `strategies/PellaMetaEA/PellaMetaEA.mq5`
- TT NDX subsystem fully implemented
- Subsystems 1, 2, 4 stubbed for v0.2 port
- Shared safety layer fully implemented (DD circuit, weekend flat, news blackout stub, max-concurrent cap, magic isolation, cross-symbol polling)

---

## IN PROGRESS list — 0 cards

(Walk-forward batch completed — 20 backtests across 5 specs × 4 windows in 15.5 min. See `CLI_VALIDATION_2026-05-02_WALKFORWARD.md` for results. Findings moved to "DONE" + new follow-up cards added below.)

---

## TO-DO list — 13 cards

### Immediate next session (high priority)

**INVESTIGATE ChBVIP USDJPY 2026 Q1 collapse**
- Walk-forward revealed Sharpe -2.47 in Q1 2026
- Was thriving in 2024 (Sharpe 2.83)
- Diagnose: regime shift? specific bad month? broker change? data issue?
- Decision: retire from USDJPY, or just delay deployment + keep watching

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
