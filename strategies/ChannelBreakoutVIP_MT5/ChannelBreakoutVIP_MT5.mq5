//+------------------------------------------------------------------+
//|                                       ChannelBreakoutVIP_MT5.mq5 |
//|                        MT5 port of Zenom MotherEA_NT8 V5.0 — S1   |
//|                                       (Channel Breakout LONG ONLY) |
//+------------------------------------------------------------------+
//
// LOGIC (faithful to MotherEA S1 — produced 2019–2026 equity curve):
//   - On each new bar, compute Donchian-style channel:
//        upBound  = max(High, Length bars including current)
//        downBound = min(Low,  Length bars including current)
//   - When flat: place pending buy-stop at upBound + point.
//     Daily-target gate: if today's realised profit >= TakeProfit (or
//     remaining headroom < MinTradeValue) → no entry that bar.
//   - On entry: stop-loss = downBound - point (channel exit).
//     ATR trailing maintained: trailingStop = Close - ATR * AtrMult,
//     monotonic up. If Close <= trailingStop → market exit.
//   - Each new bar while in position, update SL to whichever is HIGHER:
//     channel low or ATR trailing (tighter stop wins for long).
//   - LONG ONLY (per MotherEA S1).
//
// DEFAULTS: parameters mirror MotherEA S1_* defaults; symbol pivot is
// XAUUSD (MT5 gold) instead of "MGC APR26" (NT8 micro gold futures).
//+------------------------------------------------------------------+

#property strict
#property copyright "Pella project — MotherEA S1 port"
#property version   "0.2"
//
// v0.2 changes vs v0.1:
//   + Percent-risk lot sizing (Zenom Phase 5 / Carver vol-targeting equivalent).
//     Replaces fixed `Lots` with risk-percent-of-balance / (SL distance × value/lot).
//     Toggle: UseRiskPercent (default false → backwards-compatible backtest).
//     When enabled, lot size is computed at entry from the SL distance so each
//     trade risks a constant % of account balance regardless of asset volatility.
//   + Falls back to fixed `Lots` if UseRiskPercent=false OR if SL is zero
//     (i.e. UseChannelExit=false).

#include <Trade\Trade.mqh>

input group "Channel"
input int    Length             = 50;     // Donchian length

input group "ATR trailing"
input int    AtrPeriod          = 14;
input double AtrMult             = 2.0;
input bool   UseFixedStopLoss   = false;  // false = use ATR trailing instead
input double FixedStopLossUSD   = 500.0;

input group "Risk gates"
input bool   UseDailyTarget     = true;
input double DailyTakeProfitUSD = 1000.0;
input double MinTradeValueUSD   = 40.0;
input bool   UseChannelExit     = true;   // SL anchored to channel low

input group "Trade"
input double Lots               = 0.10;       // used when UseRiskPercent=false OR SL is zero
input ulong  MagicNumber        = 7010702;

input group "Position sizing (v0.2 Zenom/Carver alignment)"
input bool   UseRiskPercent     = false;      // false=fixed Lots; true=% of balance per trade
input double RiskPercent        = 1.0;        // 1.0% per trade (Zenom recommended for challenge phase)
input double MaxLotsCap         = 10.0;       // hard cap regardless of risk math

CTrade trade;

// ── Indicator handle ──────────────────────────────────────────────────
int      atrHandle = INVALID_HANDLE;

// ── State ─────────────────────────────────────────────────────────────
datetime lastBarTime;
double   trailingStop;
bool     inLong;
ulong    pendingBuyStopTicket;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   atrHandle = iATR(_Symbol, _Period, AtrPeriod);
   if (atrHandle == INVALID_HANDLE)
   {
      Print("Failed to create ATR handle");
      return(INIT_FAILED);
   }
   lastBarTime          = 0;
   trailingStop         = 0.0;
   inLong               = false;
   pendingBuyStopTicket = 0;
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if (atrHandle != INVALID_HANDLE) IndicatorRelease(atrHandle);
}

//+------------------------------------------------------------------+
// Sync state with reality: flat in MT5 but flag says inLong → reset.
//+------------------------------------------------------------------+
bool HasOpenPosition()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            return true;
      }
   }
   return false;
}

ulong CurrentPositionTicket()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            return ticket;
      }
   }
   return 0;
}

//+------------------------------------------------------------------+
void CancelPendingBuyStop()
{
   for (int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if (OrderSelect(ticket))
      {
         if (OrderGetString(ORDER_SYMBOL) == _Symbol &&
             (ulong)OrderGetInteger(ORDER_MAGIC) == MagicNumber &&
             OrderGetInteger(ORDER_TYPE) == ORDER_TYPE_BUY_STOP)
         {
            trade.OrderDelete(ticket);
         }
      }
   }
   pendingBuyStopTicket = 0;
}

//+------------------------------------------------------------------+
double GetTodaysClosedProfit()
{
   datetime now = TimeCurrent();
   MqlDateTime dt;
   TimeToStruct(now, dt);
   dt.hour = 0; dt.min = 0; dt.sec = 0;
   datetime startOfDay = StructToTime(dt);

   if (!HistorySelect(startOfDay, now)) return 0.0;

   double profit = 0.0;
   int total = HistoryDealsTotal();
   for (int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if (ticket == 0) continue;
      if ((ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber) continue;
      if (HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol)            continue;
      if (HistoryDealGetInteger(ticket, DEAL_ENTRY) != DEAL_ENTRY_OUT)     continue;
      profit += HistoryDealGetDouble(ticket, DEAL_PROFIT)
              + HistoryDealGetDouble(ticket, DEAL_SWAP)
              + HistoryDealGetDouble(ticket, DEAL_COMMISSION);
   }
   return profit;
}

//+------------------------------------------------------------------+
double GetChannelHigh(int length)
{
   double hi = -DBL_MAX;
   for (int i = 0; i < length; i++)
   {
      double h = iHigh(_Symbol, _Period, i);
      if (h > hi) hi = h;
   }
   return hi;
}

double GetChannelLow(int length)
{
   double lo = DBL_MAX;
   for (int i = 0; i < length; i++)
   {
      double l = iLow(_Symbol, _Period, i);
      if (l < lo) lo = l;
   }
   return lo;
}

double ReadATR()
{
   double buf[1];
   if (CopyBuffer(atrHandle, 0, 0, 1, buf) <= 0) return 0.0;
   return buf[0];
}

//+------------------------------------------------------------------+
// v0.2: compute lot size from RiskPercent of balance / SL distance
// Falls back to fixed Lots if UseRiskPercent=false or SL is zero.
//+------------------------------------------------------------------+
double ComputeLotSize(double entryPrice, double slPrice)
{
   if (!UseRiskPercent)        return Lots;
   if (slPrice <= 0)           return Lots;
   if (RiskPercent <= 0)       return Lots;

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskUSD = balance * (RiskPercent / 100.0);
   if (riskUSD <= 0) return Lots;

   // OrderCalcProfit gives precise per-1-lot P/L for going from entry to SL.
   // For a long with SL below entry, profit is negative → take abs.
   double profitPerLot = 0;
   if (!OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, 1.0, entryPrice, slPrice, profitPerLot))
      return Lots;

   double lossPerLot = -profitPerLot;  // SL below entry → loss is +ve here
   if (lossPerLot <= 0) return Lots;

   double lots = riskUSD / lossPerLot;

   // Normalize to broker's lot step
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if (stepLot > 0)
      lots = MathFloor(lots / stepLot) * stepLot;

   if (lots < minLot)     lots = minLot;
   if (lots > maxLot)     lots = maxLot;
   if (lots > MaxLotsCap) lots = MaxLotsCap;

   return lots;
}

//+------------------------------------------------------------------+
void OnTick()
{
   // New-bar gate (one decision per bar like NT8 OnBarClose)
   datetime curBarTime = iTime(_Symbol, _Period, 0);
   if (curBarTime == lastBarTime) return;
   lastBarTime = curBarTime;

   // Need enough bars
   int required = MathMax(Length, AtrPeriod) + 2;
   if (Bars(_Symbol, _Period) < required) return;

   // Sync flag with reality
   bool actuallyOpen = HasOpenPosition();
   if (inLong && !actuallyOpen)
   {
      inLong       = false;
      trailingStop = 0.0;
   }

   double upBound   = GetChannelHigh(Length);
   double downBound = GetChannelLow(Length);
   double point     = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double close0    = iClose(_Symbol, _Period, 0);
   double atr0      = ReadATR();

   // ── ENTRY: place / refresh pending buy stop when flat ────────────
   if (!inLong)
   {
      bool blocked = false;
      if (UseDailyTarget)
      {
         double dp = GetTodaysClosedProfit();
         if (dp >= DailyTakeProfitUSD)              blocked = true;
         else if (DailyTakeProfitUSD - dp < MinTradeValueUSD) blocked = true;
      }

      CancelPendingBuyStop();
      if (!blocked)
      {
         double entryPrice = NormalizeDouble(upBound + point, _Digits);
         double sl         = UseChannelExit
                              ? NormalizeDouble(downBound - point, _Digits)
                              : 0.0;
         double dynamicLots = ComputeLotSize(entryPrice, sl);
         if (trade.BuyStop(dynamicLots, entryPrice, _Symbol, sl, 0.0,
                           ORDER_TIME_GTC, 0, "S1_Long"))
         {
            pendingBuyStopTicket = trade.ResultOrder();
         }
      }

      // Initialise ATR trailing seed (matches NT8 behavior at entry intent)
      if (!UseFixedStopLoss && atr0 > 0)
         trailingStop = close0 - atr0 * AtrMult;
   }

   // ── MANAGEMENT: only when actually in a long position ────────────
   if (inLong || actuallyOpen)
   {
      ulong posTicket = CurrentPositionTicket();
      if (posTicket == 0) return;

      // Channel exit: SL anchored to channel low
      double newChannelSL = NormalizeDouble(downBound - point, _Digits);

      // ATR trailing
      double newTrailingSL = trailingStop;
      if (!UseFixedStopLoss && atr0 > 0)
      {
         double candidate = close0 - atr0 * AtrMult;
         if (candidate > newTrailingSL) newTrailingSL = candidate;
         trailingStop = newTrailingSL;
      }

      // Tighter (higher for long) of the two stops wins
      double newSL = newChannelSL;
      if (!UseFixedStopLoss && newTrailingSL > newSL) newSL = newTrailingSL;

      if (PositionSelectByTicket(posTicket))
      {
         double curSL = PositionGetDouble(POSITION_SL);
         double curTP = PositionGetDouble(POSITION_TP);
         double bid   = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         // Only modify if newSL > curSL (don't loosen) and below market
         if (newSL > curSL && newSL < bid)
            trade.PositionModify(posTicket, NormalizeDouble(newSL, _Digits), curTP);
      }

      // ATR market exit: if Close <= trailingStop, close at market
      if (!UseFixedStopLoss && atr0 > 0 && close0 <= trailingStop)
      {
         trade.PositionClose(posTicket);
         inLong       = false;
         trailingStop = 0.0;
      }
   }
}

//+------------------------------------------------------------------+
// Track entry/exit fills via OnTradeTransaction
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest&     req,
                        const MqlTradeResult&      res)
{
   if (trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if (HistoryDealSelect(trans.deal))
   {
      if ((ulong)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != MagicNumber) return;
      if (HistoryDealGetString(trans.deal, DEAL_SYMBOL) != _Symbol)            return;

      long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
      long type  = HistoryDealGetInteger(trans.deal, DEAL_TYPE);

      if (entry == DEAL_ENTRY_IN && type == DEAL_TYPE_BUY)
      {
         inLong               = true;
         pendingBuyStopTicket = 0;
         double atr0 = ReadATR();
         double close0 = iClose(_Symbol, _Period, 0);
         if (!UseFixedStopLoss && atr0 > 0)
            trailingStop = close0 - atr0 * AtrMult;
      }
      else if (entry == DEAL_ENTRY_OUT && type == DEAL_TYPE_SELL)
      {
         inLong       = false;
         trailingStop = 0.0;
      }
   }
}
//+------------------------------------------------------------------+
