# MQL5 Scripts / EAs / Services — Handoff Brief for Collaborator

**Audience:** another developer (and their AI assistant) building **MQL5 native tooling** to replace or complement Python bridge calls.

**Read this first.** It explains the architecture, where MQL5 native code fits, what's worth building, and what's a dead end.

---

## 1. Project context (TL;DR)

We're building an algorithmic trading research pipeline. Strategies are MetaTrader 5 (MT5) Expert Advisors, run through MT5's Strategy Tester for backtesting, with results extracted and pushed through a Monte Carlo + correlation analysis pipeline.

The current architecture has two automation layers:

```
┌────────────────────────────────────────────────────────────────┐
│  External world: Claude Code, Python scripts, curl              │
└──────────────────────┬─────────────────────────────────────────┘
                       │ HTTP (port 8891)
                       ▼
┌────────────────────────────────────────────────────────────────┐
│  MT5Bridge.dll (NativeAOT C# shared library)                   │
│  Loaded into MT5 by BridgeEA.mq5 expert advisor                │
│  Drives MT5 GUI via Win32 SendMessage + UIAutomation           │
└──────────────────────┬─────────────────────────────────────────┘
                       │ Win32 / UIAutomation
                       ▼
┌────────────────────────────────────────────────────────────────┐
│  MetaTrader 5 terminal (terminal64.exe)                        │
│  ┌────────────────────────────────────────────┐                │
│  │  MQL5 runtime (executes EAs, Scripts,      │                │
│  │  Services as compiled .ex5 files)          │                │
│  └────────────────────────────────────────────┘                │
└────────────────────────────────────────────────────────────────┘
```

- Bridge layer drives MT5's **GUI** (Strategy Tester dropdowns, click Start, etc.)
- MQL5 layer is what runs **inside** MT5 (strategies that produce trades, scripts that read account state, etc.)

**This brief is about the MQL5 layer.** The bridge layer is documented separately in `BRIDGE_HANDOFF.md`.

---

## 2. The MQL5 runtime — three program types

MetaEditor (the MQL5 IDE) compiles three different runtime types:

| Type | Lifecycle | Use case |
|---|---|---|
| **Expert Advisor** (EA) | Attached to chart OR loaded by Strategy Tester. Receives ticks via `OnTick()`, lifecycle hooks `OnInit`/`OnDeinit`/`OnTester` | Strategies (this is what we mostly write) |
| **Script** | One-shot. Attach to chart → runs `OnStart()` → unloads. No tick events. | Ad-hoc operations: bulk symbol management, pre-flight checks, one-time data dumps |
| **Service** | Background daemon. Runs continuously without needing a chart. `OnStart()` runs once, can spawn long-running work. | Continuous monitors: bridge health watchdog, weekly review reports |

All three compile to `.ex5` files and run inside the MT5 process.

Source files live in `<MT5_DATA_PATH>/MQL5/`:
- `Experts/` — EAs
- `Scripts/` — one-shot scripts
- `Services/` — background services
- `Indicators/` — chart indicators (not relevant here)
- `Include/` — shared headers
- `Files/` — runtime file I/O target (writes from EAs/scripts land here by default)
- `Files/Common/` — shared file location across all installed terminals

---

## 3. What MQL5 scripts CAN do (capabilities)

Scripts have full access to:

- **Market data:** `SymbolInfoDouble`, `SymbolInfoInteger`, `iHigh/iLow/iClose/iVolume`, `CopyRates`, `CopyTicks`
- **Symbol management:** `SymbolSelect(name, true/false)` to add/remove from Market Watch
- **History:** `HistorySelect`, `HistoryDealsTotal`, `HistoryDealGetDouble/Integer/String` — full deal history of the account
- **Account info:** `AccountInfoDouble(ACCOUNT_BALANCE/EQUITY/MARGIN/...)`
- **Trading:** `OrderSend`, `CTrade` class — place market/pending orders, modify, close
- **File I/O:** `FileOpen/FileWrite/FileRead/FileClose` — write CSV, JSON, anything
- **Timing:** `TimeCurrent`, `Sleep` (in services/scripts only — never in EAs)
- **Telegram-style notifications:** `SendNotification`, `SendMail`
- **Reading terminal info:** `TerminalInfoInteger(TERMINAL_DLLS_ALLOWED)`, etc. (read-only)

Scripts can call DLLs (just like EAs) if the global Allow DLL imports flag is on.

---

## 4. What MQL5 scripts CAN'T do (hard limits)

This is what stops scripts from fully replacing the bridge:

| Capability | MQL5 access |
|---|---|
| **Drive Strategy Tester GUI** (select EA, click Start) | ❌ no API, GUI-only |
| **Toggle terminal-level settings** (Allow DLL imports global, Optimization dropdown) | ❌ read-only, write requires GUI |
| **Configure Tools → Options** | ❌ GUI-only |
| **Add chart objects to other charts programmatically** | Limited (only on the chart it's attached to via `ChartFirst`/`ChartNext` plus per-chart calls) |
| **Operate on MT5's Tester window** | ❌ scripts don't run from the Tester window, only from charts |
| **Sleep inside an EA's OnTick** | ❌ blocks the tick loop |
| **Network sockets** (raw) | ❌ only `WebRequest` (HTTP/HTTPS) is available, and only if the URL is in the WebRequest allowed list |

**For bridge issues specifically:**
- Sticky Expert dropdown: ❌ scripts can't fix this. The bridge's Win32 patch is the only fix.
- Optimization-mode disk bomb: ❌ scripts can't disable Optimization mode. Workaround is the bridge param + human verification.
- `dlls_allowed` flag: read-only, scripts can warn but not fix.

---

## 5. Three concrete things worth building in MQL5

These would meaningfully improve our pipeline. Each one can be done as either a script (one-shot) or wired into existing EAs.

### Proposal A — `OnTester()` trade dumper (preferred)

**Replaces:** the Python `mt5_tester_report.py` log-parser approach (which parses UTF-16 Tester logs to extract trades).

**How it works:** MT5 calls `OnTester()` automatically at the end of every Strategy Tester run. We write to a CSV from there.

**Implementation pattern (drop into any EA):**

```mql5
double OnTester()
{
   // Build filename: <expert>_<symbol>_<period>.csv
   string fname = StringFormat("test_%s_%s_%s.csv",
                               MQLInfoString(MQL_PROGRAM_NAME),
                               _Symbol,
                               EnumToString(_Period));

   int h = FileOpen(fname, FILE_WRITE | FILE_CSV | FILE_COMMON, ',');
   if (h == INVALID_HANDLE) return 0.0;

   FileWrite(h, "ticket","time","type","entry","price","volume","profit","symbol");

   HistorySelect(0, TimeCurrent());
   uint count = HistoryDealsTotal();
   for (uint i = 0; i < count; i++)
   {
      ulong t = HistoryDealGetTicket(i);
      if (t == 0) continue;
      FileWrite(h,
         (long)t,
         (datetime)HistoryDealGetInteger(t, DEAL_TIME),
         (long)HistoryDealGetInteger(t, DEAL_TYPE),
         (long)HistoryDealGetInteger(t, DEAL_ENTRY),
         HistoryDealGetDouble(t, DEAL_PRICE),
         HistoryDealGetDouble(t, DEAL_VOLUME),
         HistoryDealGetDouble(t, DEAL_PROFIT),
         HistoryDealGetString(t, DEAL_SYMBOL)
      );
   }
   FileClose(h);

   // Return the strategy's "score" for optimization runs (use any metric)
   return TesterStatistics(STAT_PROFIT_FACTOR);
}
```

The CSV lands in `<MT5_DATA_PATH>/MQL5/Files/Common/` (the `FILE_COMMON` flag) — accessible from outside MT5 without per-terminal pathing.

**Where to put this:** add it to a shared `BridgeReporter.mqh` header, then `#include` in every EA. One-line opt-in per strategy.

**Advantages over log parsing:**
- No UTF-16 nightmare
- No format-version dependency (MT5 logs change between builds)
- Direct USD profit (not estimated from price-diff × contract-size like our parser does)
- Per-deal data is clean and complete (commission, swap, all included)

### Proposal B — `SymbolPreflight` script (one-shot)

**Replaces:** "manually check the symbol is right before kicking off a 14-minute backtest." Catches the **NDAQ vs NDX** type mistakes before they waste time.

**Implementation:**

```mql5
// Save as: <MT5>/MQL5/Scripts/SymbolPreflight.mq5
#property strict

input string CheckSymbol = "XAUUSD";

void OnStart()
{
   string s = CheckSymbol;
   Print("=== Pre-flight for ", s, " ===");

   if (!SymbolSelect(s, true))
   {
      PrintFormat("ERROR: %s not visible / not subscribable", s);
      return;
   }

   double bid = SymbolInfoDouble(s, SYMBOL_BID);
   double ask = SymbolInfoDouble(s, SYMBOL_ASK);
   double point = SymbolInfoDouble(s, SYMBOL_POINT);
   double contractSize = SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE);
   double tickValue = SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE);
   string description = SymbolInfoString(s, SYMBOL_DESCRIPTION);
   string path = SymbolInfoString(s, SYMBOL_PATH);

   PrintFormat("Description: %s", description);
   PrintFormat("Path: %s", path);
   PrintFormat("Bid/Ask: %.5f / %.5f", bid, ask);
   PrintFormat("Spread: %d points", (int)SymbolInfoInteger(s, SYMBOL_SPREAD));
   PrintFormat("Point: %.10f, Contract size: %.2f, Tick value: %.5f",
               point, contractSize, tickValue);

   // Sanity check: prevent NDAQ vs NDX mistakes by flagging stocks vs indices
   if (StringFind(path, "Stocks") >= 0)
      Print("WARN: this symbol is in Stocks\\ path. Not an index. Did you mean a different symbol?");

   datetime firstM1 = (datetime)SeriesInfoInteger(s, PERIOD_M1, SERIES_FIRSTDATE);
   PrintFormat("M1 history begins: %s", TimeToString(firstM1));
}
```

Drag onto any chart, change `CheckSymbol` input, run. Reads everything to the Experts log.

### Proposal C — `BatchSymbolPrep` script (one-shot)

**Replaces:** manually adding symbols to Market Watch one by one before a sweep.

```mql5
// Save as: <MT5>/MQL5/Scripts/BatchSymbolPrep.mq5
#property strict

input string Symbols = "XAUUSD,USDJPY,NDX,SP500";  // comma-separated

void OnStart()
{
   string symList[];
   StringSplit(Symbols, ',', symList);
   for (int i = 0; i < ArraySize(symList); i++)
   {
      string s = symList[i];
      if (SymbolSelect(s, true))
         PrintFormat("OK: %s added to Market Watch", s);
      else
         PrintFormat("FAIL: %s not subscribable on this broker", s);
   }
}
```

Useful before a multi-asset sweep so all needed symbols are subscribed and start downloading tick data.

---

## 6. Build + deploy procedure for MQL5 scripts

### From inside MetaEditor (GUI)

1. File → New → Script → next → fill name → finish (creates `<name>.mq5` in `MQL5/Scripts/`)
2. Paste code, F7 to compile
3. In MT5: Navigator panel → Scripts → drag `<name>` onto any chart → script runs once

### Headless from CLI (preferred for automation)

```bash
"C:/Program Files/MetaTrader 5/metaeditor64.exe" \
  /compile:"path/to/<MT5>/MQL5/Scripts/MyScript.mq5" \
  /log:"path/to/MyScript.compile.log"
# Logs are UTF-16LE encoded
```

This is what we use to compile EAs in the project — same command works for scripts and services.

### Where files end up

| Source location | Compiled output |
|---|---|
| `MQL5/Experts/MyEA.mq5` | `MQL5/Experts/MyEA.ex5` |
| `MQL5/Scripts/MyScript.mq5` | `MQL5/Scripts/MyScript.ex5` |
| `MQL5/Services/MySvc.mq5` | `MQL5/Services/MySvc.ex5` |

The `.ex5` is what MT5 actually loads. After compile, the new `.ex5` is picked up automatically — no MT5 restart needed.

---

## 7. Where MQL5 helps vs where the bridge helps

| Task | Use bridge | Use MQL5 |
|---|---|---|
| Configure Strategy Tester (select EA, set dates, modelling) | ✅ | ❌ no API |
| Click Start on Strategy Tester | ✅ | ❌ no API |
| Read terminal/account state | Either works | ✅ cleaner |
| Place orders / manage positions | Either works | ✅ runs locally, no HTTP overhead |
| Bulk symbol management | Either works | ✅ simpler |
| Pre-flight symbol check before a test | Either works | ✅ runs in MT5's context |
| Extract trade history after a backtest | Bridge HTTP + log parser | ✅ `OnTester()` is the right hook |
| Long-running monitors (e.g. weekly review) | Cron + bridge HTTP | ✅ Service type, runs in MT5 |
| Cross-process operations (e.g. alert from bridge to phone) | Bridge `WebRequest` | Either works |

**Rule of thumb:** if the operation needs to drive MT5's GUI, use the bridge. Otherwise prefer MQL5 — fewer moving parts, no HTTP, no UTF-16 log parsing.

---

## 8. Concrete next steps for the collaborator

If their goal is replacing log-parsing with native MQL5 tooling:

1. **Build a `BridgeReporter.mqh`** include file with the `OnTester()` CSV dump from Proposal A, plus optional helpers (e.g. `WriteResultsJson()` for JSON output).
2. **Wire it into 1-2 existing EAs** as proof-of-concept. The output CSV should match the format `mt5_tester_report.py` produces today, so downstream tools (Monte Carlo script, correlation analysis) keep working unchanged.
3. **Verify by running a backtest** and comparing the new MQL5-emitted CSV against the Python-parsed one. Identical content = success.
4. **Extend to all 8-12 active EAs** by adding `#include <BridgeReporter.mqh>` + a one-line `OnTester()` call.
5. **Delete `mt5_tester_report.py`** when all EAs use the new path.

If their goal is something else, the relevant pattern is in Proposal B or C.

---

## 9. Common gotchas

- **`OnTester()` only fires in Strategy Tester runs**, not on live charts. Don't put live-trading file writes there.
- **`FileOpen` with `FILE_COMMON`** writes to `<MT5_DATA_PATH>/MQL5/Files/Common/` — accessible from outside MT5 by any process. Without `FILE_COMMON`, files land in per-terminal `MQL5/Files/` which Strategy Tester sandboxes (the file written during a test is in a different folder than charts use, can be confusing).
- **Strings in `FileWrite()`** are CSV-escaped automatically when `FILE_CSV` flag is on. Don't pre-escape.
- **`HistorySelect(start, end)` is required** before `HistoryDealsTotal()` returns anything. Without it, you get 0 deals even when there are thousands.
- **Compiling a script while it's loaded** doesn't break anything — MT5 picks up new `.ex5` automatically the next time the script is run.
- **`SendNotification()` requires** MT5 mobile app paired (Tools → Options → Notifications, MetaQuotes ID set). Without that pairing, calls silently no-op.
- **`WebRequest()` requires** the URL to be in Tools → Options → Expert Advisors → "Allow WebRequest for listed URL" allowlist, GUI-only setting.
- **Services don't have a chart context** — `_Symbol`, `_Period`, `iClose()` etc don't work the same way. Use explicit symbol names.

---

## 10. Existing project assets your AI should know about

These exist in the parent project (this repo):

| File | What it does |
|---|---|
| `tooling/mt5_bridge/MT5Bridge_v0.44.cs` | The current bridge source (NativeAOT C#, ~890 lines) |
| `tooling/mt5_bridge/BridgeEA.mq5` | The MQL5 EA that loads the bridge DLL |
| `tooling/mt5_bridge/MT5Bridge.csproj` | NativeAOT project config |
| `docs/BRIDGE_HANDOFF.md` | Bridge fix brief (sticky Expert dropdown, v0.44 patch) |
| `docs/MQL5_SCRIPTS_HANDOFF.md` | This document |
| `docs/StreetSmarts_catalog.md` | Strategy catalog from the Raschke/Connors book |
| `docs/paper_trade_plan.md` | Deferred paper-trade deployment design |
| `docs/BUILD_JOURNAL.md` | Project history / decisions |
| `docs/TODAY_2026-05-02.md` | Latest status (what's running, what's queued) |

The strategy `.mq5` files themselves are in the parent (private) repo at `C:/Lab/strategies/<name>/<name>.mq5`. If the collaborator needs to see one as a reference, ask the user to share that one EA's source — it's not blocked by anything but isn't pre-staged in this public mirror.

---

## 11. Final note

MQL5 native code is the right tool for **anything that runs inside MT5's process** — strategies, post-test analysis, account monitoring, symbol management. The bridge is the right tool for **anything that drives MT5's GUI** — configure tester, click buttons, change dropdowns.

Don't overlap them — pick the right layer for each task. If you start writing an MQL5 service that uses `WebRequest` to call the bridge to do something MQL5 could do directly, you're routing through 4 layers when you should use 1. Conversely, don't try to do GUI automation from MQL5 — the API isn't there, you'd be reinventing the bridge.

Good luck. Ping back if a use case isn't covered here.
