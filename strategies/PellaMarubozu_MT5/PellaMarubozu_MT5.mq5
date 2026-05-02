//+------------------------------------------------------------------+
//|                                              PellaMarubozu_MT5.mq5 |
//|     Clean-room implementation of a Marubozu-pattern breakout EA    |
//|     spec'd from a marketplace .set file (no marketplace code used) |
//+------------------------------------------------------------------+
//
// LOGIC:
//   - Marubozu detection: a closed candle whose body is BodyFactor times
//     larger than the average body of the last BodyLookback closed
//     candles, AND each wick is <= WickMaxPercent of the candle's total
//     range. Bullish if Close > Open, bearish otherwise.
//   - On signal: queue a stop entry order in the signal direction with
//     BufferPoints offset above (long) / below (short) the signal high/low.
//     Order valid for at most MaxBarsAfterSig new bars.
//   - On fill: SL = ATR(ATRPeriod) × ATRMultSL away from entry.
//              TP = SL distance × RR away from entry (R-multiple TP).
//   - Trend filter (optional): EMA(EMAPeriod) on current TF.
//                              Long only if Close > EMA, short only if Close < EMA.
//   - Trailing stop (optional): activate when profit >= TrailStartPts.
//                               Maintain SL at price - TrailDistPts × point (long).
//                               Move SL in TrailStepPts increments.
//   - Daily protection: stop trading rest of day if today's realised loss
//                       exceeds MaxDailyLossPct of starting day balance, OR
//                       if trade count exceeds MaxDailyTrades.
//   - Session filter (optional): only allow new entries within
//                                 [SessionStartHour, SessionEndHour) broker time.
//   - Position sizing modes (LotMode):
//        0 = FixedLot (use FixedLot input)
//        1 = RiskMoney (risk a fixed dollar amount per trade — RiskMoney input)
//        2 = PctBalance (risk PctBalance% of account balance per trade)
//        3 = PctEquity (risk PctEquity% of account equity per trade)
//+------------------------------------------------------------------+

#property strict
#property copyright "Pella project — clean-room Marubozu pattern port"
#property version   "0.1"

#include <Trade\Trade.mqh>

input group "General"
input ulong  InpMagic            = 20250101;
input string InpTradeComment     = "PellaMBZ";
input int    InpMaxSpreadPoints  = 300;
input bool   InpAllowNewTrades   = true;

input group "Lot Sizing"
input int    InpLotMode          = 2;       // 0=FixedLot, 1=RiskMoney, 2=PctBalance, 3=PctEquity
input double InpFixedLot         = 0.10;
input double InpRiskMoney        = 10.0;    // USD risk per trade (LotMode=1)
input double InpPctBalance       = 1.0;     // % of balance to risk (LotMode=2)
input double InpPctEquity        = 1.0;     // % of equity to risk (LotMode=3)
input double InpMaxLotsCap       = 10.0;

input group "Daily Protection"
input double InpMaxDailyLossPct  = 5.0;     // 0=disabled
input int    InpMaxDailyTrades   = 10;      // 0=disabled

input group "Marubozu Pattern"
input int    InpBodyLookback     = 50;
input double InpBodyFactor       = 1.5;     // body must be > BodyFactor * avg body of last N
input double InpWickMaxPercent   = 0.15;    // each wick must be <= this fraction of range

input group "Entry"
input int    InpEntryMode        = 1;       // 0=market on close, 1=stop-order breakout
input int    InpBufferPoints     = 200;
input int    InpMaxBarsAfterSig  = 3;

input group "SL / TP"
input int    InpSLMode           = 1;       // 0=disabled (channel only), 1=ATR
input int    InpATRPeriod        = 14;
input double InpATRMultSL        = 1.0;
input double InpRR               = 3.0;     // TP distance = SL distance × RR

input group "Trend Filter"
input bool   InpUseTrendFilter   = true;
input int    InpEMAPeriod        = 200;

input group "Trailing Stop"
input bool   InpUseTrailing      = true;
input int    InpTrailStartPts    = 100;
input int    InpTrailDistPts     = 25;
input int    InpTrailStepPts     = 20;

input group "Session Filter"
input bool   InpUseSessionFilter = true;
input int    InpSessionStartHour = 17;
input int    InpSessionEndHour   = 19;

CTrade trade;

// Indicator handles
int atrHandle = INVALID_HANDLE;
int emaHandle = INVALID_HANDLE;

// State
datetime lastBarTime = 0;
ulong    pendingTicket = 0;
int      pendingDirection = 0;   // +1=long, -1=short, 0=none
datetime pendingPlacedAt = 0;
int      pendingPlacedAtBars = 0;

// Daily protection state
datetime dayStartTime = 0;
double   dayStartBalance = 0;
int      dayTradesCount = 0;
bool     dayLossTripped = false;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(20);

   atrHandle = iATR(_Symbol, _Period, InpATRPeriod);
   if (InpUseTrendFilter)
      emaHandle = iMA(_Symbol, _Period, InpEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);

   if (atrHandle == INVALID_HANDLE) { Print("ATR init failed"); return INIT_FAILED; }
   if (InpUseTrendFilter && emaHandle == INVALID_HANDLE) { Print("EMA init failed"); return INIT_FAILED; }

   dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if (atrHandle != INVALID_HANDLE) IndicatorRelease(atrHandle);
   if (emaHandle != INVALID_HANDLE) IndicatorRelease(emaHandle);
}

//+------------------------------------------------------------------+
double ReadATR()
{
   double buf[1];
   if (CopyBuffer(atrHandle, 0, 1, 1, buf) <= 0) return 0;
   return buf[0];
}

double ReadEMA()
{
   double buf[1];
   if (CopyBuffer(emaHandle, 0, 0, 1, buf) <= 0) return 0;
   return buf[0];
}

//+------------------------------------------------------------------+
// Marubozu detection on the most recently closed bar (shift = 1)
//   Returns +1 = bullish marubozu, -1 = bearish, 0 = no signal.
//+------------------------------------------------------------------+
int DetectMarubozu()
{
   double o1 = iOpen (_Symbol, _Period, 1);
   double c1 = iClose(_Symbol, _Period, 1);
   double h1 = iHigh (_Symbol, _Period, 1);
   double l1 = iLow  (_Symbol, _Period, 1);

   double range = h1 - l1;
   if (range <= 0) return 0;

   double body = MathAbs(c1 - o1);
   if (body <= 0) return 0;

   double upperWick = h1 - MathMax(o1, c1);
   double lowerWick = MathMin(o1, c1) - l1;
   double maxWick   = MathMax(upperWick, lowerWick);
   if (maxWick / range > InpWickMaxPercent) return 0;

   // Average body over last BodyLookback closed candles (shift 1..N)
   double sumBody = 0;
   int counted = 0;
   for (int i = 1; i <= InpBodyLookback; i++)
   {
      double oi = iOpen (_Symbol, _Period, i);
      double ci = iClose(_Symbol, _Period, i);
      sumBody += MathAbs(ci - oi);
      counted++;
   }
   if (counted == 0) return 0;
   double avgBody = sumBody / counted;
   if (body < InpBodyFactor * avgBody) return 0;

   return (c1 > o1) ? +1 : -1;
}

//+------------------------------------------------------------------+
bool TrendFilterPass(int direction)
{
   if (!InpUseTrendFilter) return true;
   double ema = ReadEMA();
   if (ema <= 0) return false;
   double close0 = iClose(_Symbol, _Period, 0);
   if (direction > 0) return close0 > ema;
   if (direction < 0) return close0 < ema;
   return true;
}

//+------------------------------------------------------------------+
bool IsWithinSession()
{
   if (!InpUseSessionFilter) return true;
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if (InpSessionStartHour <= InpSessionEndHour)
      return (dt.hour >= InpSessionStartHour && dt.hour < InpSessionEndHour);
   return (dt.hour >= InpSessionStartHour || dt.hour < InpSessionEndHour);
}

bool IsSpreadOK()
{
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if (point <= 0) return false;
   double spreadPts = (ask - bid) / point;
   return spreadPts <= InpMaxSpreadPoints;
}

//+------------------------------------------------------------------+
// Track new day, reset daily counters
//+------------------------------------------------------------------+
bool IsNewDay()
{
   MqlDateTime now, last;
   TimeToStruct(TimeCurrent(), now);
   TimeToStruct(dayStartTime, last);
   return (now.year != last.year || now.mon != last.mon || now.day != last.day);
}

void OnNewDay()
{
   dayStartTime    = TimeCurrent();
   dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   dayTradesCount  = 0;
   dayLossTripped  = false;
}

bool DailyProtectionOK()
{
   if (dayLossTripped) return false;
   if (InpMaxDailyTrades > 0 && dayTradesCount >= InpMaxDailyTrades) return false;
   if (InpMaxDailyLossPct > 0)
   {
      double curBalance = AccountInfoDouble(ACCOUNT_BALANCE);
      double dayLossPct = (dayStartBalance - curBalance) / dayStartBalance * 100.0;
      if (dayLossPct >= InpMaxDailyLossPct)
      {
         dayLossTripped = true;
         return false;
      }
   }
   return true;
}

//+------------------------------------------------------------------+
// Lot sizing — switch over LotMode
//+------------------------------------------------------------------+
double NormalizeLots(double lots)
{
   double minLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxLot  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if (stepLot > 0) lots = MathFloor(lots / stepLot) * stepLot;
   if (lots < minLot)        lots = minLot;
   if (lots > maxLot)        lots = maxLot;
   if (lots > InpMaxLotsCap) lots = InpMaxLotsCap;
   return lots;
}

double ComputeLot(double entryPrice, double slPrice)
{
   if (InpLotMode == 0) return NormalizeLots(InpFixedLot);

   double riskUSD;
   if (InpLotMode == 1)
      riskUSD = InpRiskMoney;
   else if (InpLotMode == 2)
      riskUSD = AccountInfoDouble(ACCOUNT_BALANCE) * (InpPctBalance / 100.0);
   else
      riskUSD = AccountInfoDouble(ACCOUNT_EQUITY)  * (InpPctEquity  / 100.0);

   if (riskUSD <= 0 || slPrice <= 0 || entryPrice <= 0) return NormalizeLots(InpFixedLot);

   double profit = 0;
   ENUM_ORDER_TYPE side = (slPrice < entryPrice) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if (!OrderCalcProfit(side, _Symbol, 1.0, entryPrice, slPrice, profit))
      return NormalizeLots(InpFixedLot);

   double lossPerLot = -profit;
   if (lossPerLot <= 0) return NormalizeLots(InpFixedLot);

   double lots = riskUSD / lossPerLot;
   return NormalizeLots(lots);
}

//+------------------------------------------------------------------+
bool HasOpenPosition()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if (PositionSelectByTicket(t))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
            return true;
      }
   }
   return false;
}

ulong CurrentPositionTicket()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if (PositionSelectByTicket(t))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
            return t;
      }
   }
   return 0;
}

void CancelPending()
{
   if (pendingTicket == 0) return;
   for (int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong t = OrderGetTicket(i);
      if (t == pendingTicket)
      {
         trade.OrderDelete(t);
         break;
      }
   }
   pendingTicket = 0;
   pendingDirection = 0;
   pendingPlacedAtBars = 0;
}

//+------------------------------------------------------------------+
void PlaceBreakoutOrder(int direction, double signalHigh, double signalLow, double atr)
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double bufferDist = InpBufferPoints * point;
   double slDist     = atr * InpATRMultSL;
   double tpDist     = slDist * InpRR;

   double entryPrice, slPrice, tpPrice;
   if (direction > 0)
   {
      entryPrice = NormalizeDouble(signalHigh + bufferDist, _Digits);
      slPrice    = NormalizeDouble(entryPrice - slDist, _Digits);
      tpPrice    = NormalizeDouble(entryPrice + tpDist, _Digits);
   }
   else
   {
      entryPrice = NormalizeDouble(signalLow - bufferDist, _Digits);
      slPrice    = NormalizeDouble(entryPrice + slDist, _Digits);
      tpPrice    = NormalizeDouble(entryPrice - tpDist, _Digits);
   }

   double lots = ComputeLot(entryPrice, slPrice);
   if (lots <= 0) return;

   bool ok;
   if (direction > 0)
      ok = trade.BuyStop(lots, entryPrice, _Symbol, slPrice, tpPrice,
                         ORDER_TIME_GTC, 0, InpTradeComment);
   else
      ok = trade.SellStop(lots, entryPrice, _Symbol, slPrice, tpPrice,
                          ORDER_TIME_GTC, 0, InpTradeComment);

   if (ok)
   {
      pendingTicket    = trade.ResultOrder();
      pendingDirection = direction;
      pendingPlacedAt  = TimeCurrent();
      pendingPlacedAtBars = 0;
   }
}

//+------------------------------------------------------------------+
void ManageTrailingStop()
{
   if (!InpUseTrailing) return;
   ulong t = CurrentPositionTicket();
   if (t == 0) return;
   if (!PositionSelectByTicket(t)) return;

   long posType = PositionGetInteger(POSITION_TYPE);
   double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double curSL     = PositionGetDouble(POSITION_SL);
   double curTP     = PositionGetDouble(POSITION_TP);
   double point     = SymbolInfoDouble(_Symbol, SYMBOL_POINT);

   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);

   double profitPts = 0;
   double newSL = curSL;

   if (posType == POSITION_TYPE_BUY)
   {
      profitPts = (bid - openPrice) / point;
      if (profitPts < InpTrailStartPts) return;
      double candidateSL = bid - InpTrailDistPts * point;
      // Step in TrailStepPts increments
      if (curSL > 0 && (candidateSL - curSL) < InpTrailStepPts * point) return;
      if (candidateSL > curSL && candidateSL < bid)
         newSL = candidateSL;
   }
   else if (posType == POSITION_TYPE_SELL)
   {
      profitPts = (openPrice - ask) / point;
      if (profitPts < InpTrailStartPts) return;
      double candidateSL = ask + InpTrailDistPts * point;
      if (curSL > 0 && (curSL - candidateSL) < InpTrailStepPts * point) return;
      if ((candidateSL < curSL || curSL == 0) && candidateSL > ask)
         newSL = candidateSL;
   }

   if (newSL != curSL)
      trade.PositionModify(t, NormalizeDouble(newSL, _Digits), curTP);
}

//+------------------------------------------------------------------+
void OnTick()
{
   // Daily reset
   if (IsNewDay()) OnNewDay();

   // Manage open position trailing on every tick
   if (HasOpenPosition()) ManageTrailingStop();

   // New-bar gate for signal evaluation
   datetime curBarTime = iTime(_Symbol, _Period, 0);
   bool newBar = (curBarTime != lastBarTime);
   if (newBar)
   {
      lastBarTime = curBarTime;
      if (pendingTicket != 0)
      {
         pendingPlacedAtBars++;
         if (pendingPlacedAtBars > InpMaxBarsAfterSig)
            CancelPending();
      }
   }
   if (!newBar) return;

   // Pre-checks
   if (!InpAllowNewTrades) return;
   if (HasOpenPosition()) return;
   if (!DailyProtectionOK()) return;
   if (!IsWithinSession()) return;
   if (!IsSpreadOK()) return;

   // Need data
   if (Bars(_Symbol, _Period) < MathMax(InpBodyLookback + 2, InpEMAPeriod + 2)) return;

   // Detect Marubozu
   int direction = DetectMarubozu();
   if (direction == 0) return;

   // Trend filter
   if (!TrendFilterPass(direction)) return;

   // Cancel stale pending if direction differs
   if (pendingTicket != 0 && pendingDirection != direction)
      CancelPending();

   if (pendingTicket != 0) return;

   double atr = ReadATR();
   if (atr <= 0) return;

   double signalHigh = iHigh(_Symbol, _Period, 1);
   double signalLow  = iLow (_Symbol, _Period, 1);
   PlaceBreakoutOrder(direction, signalHigh, signalLow, atr);
}

//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest&     req,
                        const MqlTradeResult&      res)
{
   if (trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if (HistoryDealSelect(trans.deal))
   {
      if ((ulong)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != InpMagic) return;
      if (HistoryDealGetString(trans.deal, DEAL_SYMBOL) != _Symbol)         return;

      long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
      if (entry == DEAL_ENTRY_IN)
      {
         dayTradesCount++;
         pendingTicket = 0;
         pendingDirection = 0;
         pendingPlacedAtBars = 0;
      }
   }
}
