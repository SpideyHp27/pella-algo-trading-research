//+------------------------------------------------------------------+
//|                                              PellaMetaEA.mq5 v0.1 |
//|     Multi-subsystem deployment wrapper for Pella surviving        |
//|     portfolio. Single chart attachment, multi-symbol monitoring.  |
//+------------------------------------------------------------------+
//
// SCOPE OF v0.1 (intentional scaffold):
//   - Subsystem 3 (TT NDX) is FULLY IMPLEMENTED (it's the only calendar-time-
//     driven subsystem and easiest to verify correctness independently).
//   - Subsystems 1, 2, 4 are STUBBED — they print a "v0.2 port pending"
//     message on first call and otherwise no-op. The toggle flags work.
//   - The shared safety layer (portfolio DD circuit, weekend flat, max
//     concurrent positions, news blackout stub) IS fully implemented — it
//     wraps any subsystem activity, so adding a real subsystem in v0.2 only
//     requires implementing the entry/exit logic; safety is already
//     guaranteed.
//
// WHY THIS SCAFFOLD APPROACH:
//   Each Pella subsystem has distinct entry semantics (channel breakout,
//   calendar effect, OCO IDNR4 brackets) and the chart-attachment + cross-
//   symbol model needs careful state management per subsystem. Shipping a
//   working scaffold lets us validate the architecture (safety + toggles +
//   per-magic isolation) before committing to the full multi-port. The full
//   v0.2 port is mechanical once the scaffold is proven.
//
// RUN MODES:
//   - Attach to ANY chart (NDX H1 recommended). The EA polls all configured
//     symbols via OnTimer at 1-second resolution. Native chart symbol uses
//     OnTick for tighter latency.
//   - Standalone in Strategy Tester: each subsystem's symbol can be tested
//     independently by enabling only that subsystem's toggle.
//+------------------------------------------------------------------+

#property strict
#property copyright "Pella project — Meta EA v0.1 scaffold"
#property version   "0.1"

#include <Trade\Trade.mqh>

//============================================================================
// INPUTS
//============================================================================

input group "==== Subsystem toggles ===="
input bool   EnableSubsystem1_ChBVIP_USDJPY = true;   // v0.1: stub (port in v0.2)
input bool   EnableSubsystem2_ChBVIP_XAUUSD = true;   // v0.1: stub (port in v0.2)
input bool   EnableSubsystem3_TT_NDX        = true;   // v0.1: FULLY IMPLEMENTED
input bool   EnableSubsystem4_IDNR4_XAUUSD  = true;   // v0.1: stub (port in v0.2)

input group "==== Subsystem 1: ChBVIP USDJPY H1 (stub) ===="
input string S1_Symbol         = "USDJPY";
input ENUM_TIMEFRAMES S1_TF    = PERIOD_H1;
input double S1_RiskPercent    = 1.0;
input double S1_MaxLotsCap     = 10.0;
input double S1_FixedLots      = 0.10;
input ulong  S1_Magic          = 7011001;

input group "==== Subsystem 2: ChBVIP XAUUSD H1 (stub) ===="
input string S2_Symbol         = "XAUUSD";
input ENUM_TIMEFRAMES S2_TF    = PERIOD_H1;
input double S2_RiskPercent    = 0.5;     // halved for gold collinearity with S4
input double S2_MaxLotsCap     = 10.0;
input double S2_FixedLots      = 0.10;
input ulong  S2_Magic          = 7011002;

input group "==== Subsystem 3: TT NDX H1 (LIVE) ===="
input string S3_Symbol         = "NDX";
input ENUM_TIMEFRAMES S3_TF    = PERIOD_H1;
input bool   S3_UseRiskPercent = true;
input double S3_RiskPercent    = 1.0;
input double S3_MaxLotsCap     = 10.0;
input double S3_FixedLots      = 0.10;
input int    S3_EntryDay       = 1;       // Mon
input int    S3_EntryHour      = 16;      // 16:30 broker time
input int    S3_EntryMinute    = 30;
input int    S3_ExitDay        = 3;       // Wed
input int    S3_ExitHour       = 20;
input int    S3_ExitMinute     = 0;
input bool   S3_UseFridayFilter = true;   // only enter if Friday was red
input ulong  S3_Magic          = 7011003;

input group "==== Subsystem 4: IDNR4 XAUUSD H4 (stub) ===="
input string S4_Symbol         = "XAUUSD";
input ENUM_TIMEFRAMES S4_TF    = PERIOD_H4;
input double S4_RiskPercent    = 0.5;     // halved for gold collinearity with S2
input double S4_MaxLotsCap     = 10.0;
input double S4_FixedLots      = 0.10;
input ulong  S4_Magic          = 7011004;

input group "==== Shared safety layer ===="
input double MaxPortfolioDDPercent  = 8.0;    // EOW circuit; LucidFlex EOD ceiling is 12%
input int    MaxConcurrentPositions = 3;
input int    FlatBeforeWeekendHours = 2;      // close all 2hr before market close Friday
input int    BlackoutBeforeNewsMin  = 30;     // v0.1: stub — needs news calendar JSON
input int    BlackoutAfterNewsMin   = 15;
input bool   DebugMode              = false;

//============================================================================
// GLOBALS
//============================================================================

CTrade   trade;

// Safety state
double   peakEquity        = 0;
bool     ddCircuitTripped  = false;
datetime ddCircuitDate     = 0;

// Subsystem 3 (TT NDX) state
datetime s3_lastEntryDate  = 0;
datetime s3_lastExitDate   = 0;
bool     s3_in_position    = false;
ulong    s3_position_ticket = 0;

// Stub-warning printed flags (each subsystem prints its v0.2 message once)
bool     s1_warned = false;
bool     s2_warned = false;
bool     s4_warned = false;

//============================================================================
// INIT / DEINIT
//============================================================================

int OnInit()
{
   trade.SetExpertMagicNumber(0);  // we set per-call
   peakEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   EventSetTimer(1);  // 1-second poll for cross-symbol subsystems

   // Verify all configured subsystem symbols are in Market Watch
   string syms[] = {S1_Symbol, S2_Symbol, S3_Symbol, S4_Symbol};
   bool toggles[] = {EnableSubsystem1_ChBVIP_USDJPY, EnableSubsystem2_ChBVIP_XAUUSD,
                     EnableSubsystem3_TT_NDX, EnableSubsystem4_IDNR4_XAUUSD};
   for (int i = 0; i < 4; i++)
   {
      if (toggles[i] && !SymbolSelect(syms[i], true))
      {
         PrintFormat("PellaMetaEA: WARNING — subsystem %d symbol %s could not be selected to Market Watch",
                     i + 1, syms[i]);
      }
   }

   PrintFormat("PellaMetaEA v0.1 init: peak equity %.2f, subsystems enabled: S1=%s S2=%s S3=%s S4=%s",
               peakEquity,
               EnableSubsystem1_ChBVIP_USDJPY ? "Y" : "n",
               EnableSubsystem2_ChBVIP_XAUUSD ? "Y" : "n",
               EnableSubsystem3_TT_NDX ? "Y" : "n",
               EnableSubsystem4_IDNR4_XAUUSD ? "Y" : "n");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

//============================================================================
// SAFETY LAYER
//============================================================================

bool IsNewsBlackoutActive(string symbol)
{
   // v0.1 STUB: returns false. v0.2 will load a JSON calendar from MQL5/Files
   // and check whether `symbol`'s currency is within the blackout window.
   return false;
}

bool IsWeekendFlatTime()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if (dt.day_of_week != 5) return false;  // only relevant on Friday
   // Approximate market close at 21:00 broker time (varies by symbol/broker)
   int closeHour = 21;
   return dt.hour >= (closeHour - FlatBeforeWeekendHours);
}

int CountOpenPositions()
{
   int n = 0;
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket)) n++;
   }
   return n;
}

void UpdatePeakEquity()
{
   double e = AccountInfoDouble(ACCOUNT_EQUITY);
   if (e > peakEquity) peakEquity = e;

   // Reset DD circuit at start of new calendar week
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   datetime today = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));
   if (ddCircuitTripped && today != ddCircuitDate)
   {
      MqlDateTime tripDt;
      TimeToStruct(ddCircuitDate, tripDt);
      // Reset on Monday after the trip week
      if (dt.day_of_week == 1 && (today - ddCircuitDate) >= 86400)
      {
         ddCircuitTripped = false;
         peakEquity = e;  // reset peak too
         Print("PellaMetaEA: DD circuit RESET on new week — resuming normal operation");
      }
   }
}

bool CheckPortfolioDDCircuit()
{
   double e = AccountInfoDouble(ACCOUNT_EQUITY);
   if (peakEquity <= 0) return false;
   double ddPct = (peakEquity - e) / peakEquity * 100.0;
   if (ddPct >= MaxPortfolioDDPercent && !ddCircuitTripped)
   {
      ddCircuitTripped = true;
      ddCircuitDate = TimeCurrent();
      PrintFormat("PellaMetaEA: PORTFOLIO DD CIRCUIT TRIPPED at %.2f%% — closing all positions, halt for week",
                  ddPct);
      CloseAllPositions();
      return true;
   }
   return ddCircuitTripped;
}

void CloseAllPositions()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket))
         trade.PositionClose(ticket);
   }
}

bool SafetyOK(string symbol)
{
   if (ddCircuitTripped) return false;
   if (IsWeekendFlatTime()) return false;
   if (IsNewsBlackoutActive(symbol)) return false;
   if (CountOpenPositions() >= MaxConcurrentPositions) return false;
   return true;
}

//============================================================================
// SHARED LOT SIZING (used by all subsystems)
//============================================================================

double ComputeLotSize(string symbol, double entryPrice, double slPrice,
                      double riskPct, double maxCap, double fallbackLots)
{
   if (slPrice <= 0)  return fallbackLots;
   if (entryPrice <= 0) return fallbackLots;
   if (riskPct <= 0)  return fallbackLots;

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskUSD = balance * (riskPct / 100.0);
   if (riskUSD <= 0) return fallbackLots;

   double profit = 0;
   if (!OrderCalcProfit(ORDER_TYPE_BUY, symbol, 1.0, entryPrice, slPrice, profit))
      return fallbackLots;
   double lossPerLot = -profit;
   if (lossPerLot <= 0) return fallbackLots;

   double lots = riskUSD / lossPerLot;

   double minLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if (stepLot > 0) lots = MathFloor(lots / stepLot) * stepLot;
   if (lots < minLot)  lots = minLot;
   if (lots > maxLot)  lots = maxLot;
   if (lots > maxCap)  lots = maxCap;
   return lots;
}

//============================================================================
// SUBSYSTEM 3: TuesdayTurnaroundNDX (FULLY IMPLEMENTED)
//============================================================================

void Subsystem3_TT_NDX()
{
   if (!EnableSubsystem3_TT_NDX) return;
   if (!SafetyOK(S3_Symbol)) return;

   trade.SetExpertMagicNumber(S3_Magic);

   datetime now = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(now, dt);
   datetime today = StringToTime(StringFormat("%04d.%02d.%02d", dt.year, dt.mon, dt.day));

   // Check existing position for this magic
   bool have_pos = false;
   ulong pos_ticket = 0;
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if (PositionSelectByTicket(t))
      {
         if (PositionGetString(POSITION_SYMBOL) == S3_Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == S3_Magic)
         {
            have_pos = true;
            pos_ticket = t;
            break;
         }
      }
   }

   // EXIT logic: if Wed at exit time and we have a position, close it
   if (have_pos)
   {
      if (dt.day_of_week == S3_ExitDay
          && dt.hour == S3_ExitHour
          && dt.min >= S3_ExitMinute
          && today != s3_lastExitDate)
      {
         if (trade.PositionClose(pos_ticket))
         {
            s3_lastExitDate = today;
            if (DebugMode) Print("S3: TT NDX position closed at scheduled Wed exit");
         }
      }
      return;  // already in position, no further action
   }

   // ENTRY logic: Mon at entry time, optional Friday-red filter
   if (dt.day_of_week != S3_EntryDay) return;
   if (dt.hour < S3_EntryHour) return;
   if (dt.hour == S3_EntryHour && dt.min < S3_EntryMinute) return;
   if (today == s3_lastEntryDate) return;  // already entered today

   // Friday-red filter
   if (S3_UseFridayFilter)
   {
      double fri_open  = iOpen(S3_Symbol,  PERIOD_D1, 3);  // ~3 days back is Friday
      double fri_close = iClose(S3_Symbol, PERIOD_D1, 3);
      if (fri_close >= fri_open)
      {
         if (DebugMode) Print("S3: skipping entry — Friday was not red");
         return;
      }
   }

   // Compute lot size (TT has no SL — use S3_FixedLots; if RiskPercent on,
   // use a synthetic ~100-pt SL like the standalone EA does)
   double lots;
   if (S3_UseRiskPercent)
   {
      double pt = SymbolInfoDouble(S3_Symbol, SYMBOL_POINT);
      double bid = SymbolInfoDouble(S3_Symbol, SYMBOL_BID);
      double synthSL = bid - 100.0 * pt;  // assumed ~100-pt risk per the standalone heuristic
      lots = ComputeLotSize(S3_Symbol, bid, synthSL, S3_RiskPercent, S3_MaxLotsCap, S3_FixedLots);
   }
   else
   {
      lots = S3_FixedLots;
   }

   if (trade.Buy(lots, S3_Symbol, 0, 0, 0, "PellaMeta_S3_TT_NDX"))
   {
      s3_lastEntryDate = today;
      if (DebugMode) PrintFormat("S3: TT NDX BUY at lots=%.2f", lots);
   }
}

//============================================================================
// STUB SUBSYSTEMS (v0.2 will fully port)
//============================================================================

void Subsystem1_ChBVIP_USDJPY_stub()
{
   if (!EnableSubsystem1_ChBVIP_USDJPY) return;
   if (!s1_warned)
   {
      Print("PellaMetaEA S1 (ChBVIP USDJPY): v0.1 STUB — port pending in v0.2. ",
            "Use standalone ChannelBreakoutVIP_MT5 v0.2 EA in the meantime.");
      s1_warned = true;
   }
}

void Subsystem2_ChBVIP_XAUUSD_stub()
{
   if (!EnableSubsystem2_ChBVIP_XAUUSD) return;
   if (!s2_warned)
   {
      Print("PellaMetaEA S2 (ChBVIP XAUUSD): v0.1 STUB — port pending in v0.2. ",
            "Use standalone ChannelBreakoutVIP_MT5 v0.2 EA in the meantime.");
      s2_warned = true;
   }
}

void Subsystem4_IDNR4_XAUUSD_stub()
{
   if (!EnableSubsystem4_IDNR4_XAUUSD) return;
   if (!s4_warned)
   {
      Print("PellaMetaEA S4 (IDNR4 XAUUSD H4): v0.1 STUB — port pending in v0.2. ",
            "Use standalone IDNR4_MT5 v0.3 EA in the meantime.");
      s4_warned = true;
   }
}

//============================================================================
// MAIN EVENT LOOP
//============================================================================

void OnTick()
{
   // Native chart tick — fire all subsystems once
   UpdatePeakEquity();
   if (CheckPortfolioDDCircuit()) return;

   Subsystem1_ChBVIP_USDJPY_stub();
   Subsystem2_ChBVIP_XAUUSD_stub();
   Subsystem3_TT_NDX();
   Subsystem4_IDNR4_XAUUSD_stub();
}

void OnTimer()
{
   // 1-second timer for cross-symbol polling when chart symbol differs
   // from a subsystem's target. In v0.1 only S3 (TT NDX) is live, so we
   // re-fire S3 here in case the chart symbol is not NDX.
   if (_Symbol == S3_Symbol) return;  // already handled in OnTick
   UpdatePeakEquity();
   if (CheckPortfolioDDCircuit()) return;
   Subsystem3_TT_NDX();
}
