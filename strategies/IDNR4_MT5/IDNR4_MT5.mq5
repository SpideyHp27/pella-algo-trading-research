//+------------------------------------------------------------------+
//|                                              IDNR4_MT5.mq5 v0.3 |
//|     Inside Day + NR4 volatility-expansion OCO breakout port     |
//|     Source: Street Smarts (Raschke / Connors, 1995), Chapter 19 |
//+------------------------------------------------------------------+
//
// v0.3 changes vs v0.2:
//   + UseRiskPercent / RiskPercent / MaxLotsCap — Carver vol-targeting
//     pattern, mirror ChannelBreakoutVIP_MT5 v0.2 convention
//   + ComputeLotSize() helper using OrderCalcProfit for broker-agnostic
//     USD-per-lot derivation
//   Default UseRiskPercent=false -> bit-identical to v0.2
//
// v0.2 changes vs v0.1:
//   + Trailing stop on profitable positions (book: "trail a stop to
//     lock in accrued profits") — main fix for v0.1 win-rate issue
//   + FixedStopLossUSD — Pella/LucidFlex compliance
//   + DailyMaxLossUSD — daily loss circuit breaker
//   + MinTradeValueUSD — skip too-thin brackets
//
// CORE LOGIC (unchanged from v0.1):
//   New day -> check yesterday's D1 bar for ID+NR4
//   If yes: place OCO bracket (BuyStop + SellStop) for today
//   On fill: cancel OCO partner, optionally arm stop-reverse
//   Exits: trailing stop (v0.2), MOC at 2 days unprofitable, fixed $ SL
//+------------------------------------------------------------------+

#property strict
#property copyright "Pella project - Street Smarts IDNR4 port"
#property version   "0.3"

#include <Trade\Trade.mqh>

input group "ID/NR setup"
input int    NRPeriod          = 4;
input bool   UseStopReverse    = true;
input int    MaxHoldDays       = 2;

input group "Risk management (v0.2)"
input double FixedStopLossUSD  = 500.0;    // hard worst-case per-trade $ stop (0 = disable)
input double DailyMaxLossUSD   = 1000.0;   // daily $ loss circuit (0 = disable)
input double MinTradeValueUSD  = 40.0;     // skip setups smaller than this $ bracket (0 = disable)

input group "Trailing stop (v0.2)"
input bool   UseTrailingStop      = true;
input double TrailingActivationR  = 1.0;   // activate trail at this many R of unrealized profit (R = bracket width)
input double TrailingStepR        = 1.0;   // keep SL at posMaxFavorable - TrailingStepR * R

input group "Trade settings"
input double Lots              = 0.10;
input int    EntryOffsetPoints = 1;
input ulong  MagicNumber       = 7010707;

input group "Position sizing (v0.3)"
input bool   UseRiskPercent    = false;   // false -> use Lots (v0.2 behavior)
input double RiskPercent       = 1.0;     // % of balance to risk per trade
input double MaxLotsCap        = 10.0;    // hard cap on computed lot size

CTrade trade;

// State
datetime lastBarTime    = 0;
datetime setupDate      = 0;
datetime entryDate      = 0;
double   setupHigh      = 0;
double   setupLow       = 0;
ulong    pendingBuyTicket  = 0;
ulong    pendingSellTicket = 0;
ulong    reverseTicket     = 0;

// v0.2 trailing-stop state (per current open position)
double   posBracketWidth  = 0;     // R in price terms
double   posMaxFavorable  = 0;     // highest Bid (long) or lowest Ask (short) seen since entry
bool     dailyLossTripped = false; // disable trading rest of today after circuit breaker
double   posDynamicLots   = 0;     // v0.3: lot size computed at PlaceBracket, reused for stop-reverse

datetime DateOnly(datetime t)
{
   MqlDateTime dt;
   TimeToStruct(t, dt);
   dt.hour = 0; dt.min = 0; dt.sec = 0;
   return StructToTime(dt);
}

int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {}

bool IsInsideDay()
{
   double h1 = iHigh(_Symbol, PERIOD_D1, 1);
   double l1 = iLow (_Symbol, PERIOD_D1, 1);
   double h2 = iHigh(_Symbol, PERIOD_D1, 2);
   double l2 = iLow (_Symbol, PERIOD_D1, 2);
   if (h1 == 0.0 || h2 == 0.0) return false;
   return (h1 < h2) && (l1 > l2);
}

bool IsNRPeriod()
{
   double r1 = iHigh(_Symbol, PERIOD_D1, 1) - iLow(_Symbol, PERIOD_D1, 1);
   if (r1 <= 0) return false;
   for (int i = 2; i <= NRPeriod; i++)
   {
      double rN = iHigh(_Symbol, PERIOD_D1, i) - iLow(_Symbol, PERIOD_D1, i);
      if (rN <= 0) return false;
      if (r1 >= rN) return false;
   }
   return true;
}

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

void CancelAllOurPending()
{
   for (int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if (OrderSelect(ticket))
      {
         if (OrderGetString(ORDER_SYMBOL) == _Symbol &&
             (ulong)OrderGetInteger(ORDER_MAGIC) == MagicNumber)
            trade.OrderDelete(ticket);
      }
   }
   pendingBuyTicket  = 0;
   pendingSellTicket = 0;
   reverseTicket     = 0;
}

void CloseAllOurPositions()
{
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            trade.PositionClose(ticket);
      }
   }
}

double ComputeLotSize(double entryPrice, double slPrice)
{
   if (!UseRiskPercent)        return Lots;
   if (slPrice <= 0)           return Lots;
   if (entryPrice <= 0)        return Lots;
   if (RiskPercent <= 0)       return Lots;

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskUSD = balance * (RiskPercent / 100.0);
   if (riskUSD <= 0) return Lots;

   double profit = 0;
   // OrderCalcProfit(BUY, open=entry, close=SL): if SL < entry, profit is negative -> magnitude IS our per-lot loss
   if (!OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, 1.0, entryPrice, slPrice, profit))
      return Lots;
   double lossPerLot = -profit;
   if (lossPerLot <= 0) return Lots;

   double lots = riskUSD / lossPerLot;

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

double EstimateBracketValueUSD()
{
   double bracketWidth = setupHigh - setupLow;
   if (bracketWidth <= 0) return 0;
   double profit = 0;
   if (!OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, Lots, setupLow, setupHigh, profit))
      return 0;
   return profit;
}

void PlaceBracket()
{
   double h1 = iHigh(_Symbol, PERIOD_D1, 1);
   double l1 = iLow (_Symbol, PERIOD_D1, 1);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double offset = EntryOffsetPoints * point;

   setupHigh = h1;
   setupLow  = l1;

   if (MinTradeValueUSD > 0)
   {
      double bracketUSD = EstimateBracketValueUSD();
      if (bracketUSD < MinTradeValueUSD) return;  // skip thin setup
   }

   double buyPrice  = h1 + offset;
   double sellPrice = l1 - offset;

   // v0.3: compute lot ONCE per setup using bracket width as the SL distance.
   // Symmetric: both BuyStop and SellStop have the same distance to their SL.
   double dynamicLots = ComputeLotSize(buyPrice, sellPrice);
   posDynamicLots = dynamicLots;  // remembered for stop-reverse in HandleEntryFill

   if (trade.BuyStop(dynamicLots, buyPrice, _Symbol, sellPrice, 0.0,
                     ORDER_TIME_DAY, 0, "IDNR4_BuyBracket"))
      pendingBuyTicket = trade.ResultOrder();

   if (trade.SellStop(dynamicLots, sellPrice, _Symbol, buyPrice, 0.0,
                      ORDER_TIME_DAY, 0, "IDNR4_SellBracket"))
      pendingSellTicket = trade.ResultOrder();
}

void HandleEntryFill(long dealType, double dealPrice)
{
   entryDate = DateOnly(TimeCurrent());
   posBracketWidth = setupHigh - setupLow;
   posMaxFavorable = dealPrice;  // initialize trailing reference

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double offset = EntryOffsetPoints * point;

   if (dealType == DEAL_TYPE_BUY)
   {
      if (pendingSellTicket > 0)
      {
         trade.OrderDelete(pendingSellTicket);
         pendingSellTicket = 0;
      }
      if (UseStopReverse)
      {
         double revPrice = setupLow - offset;
         double revLots = (posDynamicLots > 0) ? posDynamicLots : Lots;
         if (trade.SellStop(revLots, revPrice, _Symbol, 0.0, 0.0,
                            ORDER_TIME_DAY, 0, "IDNR4_StopReverse"))
            reverseTicket = trade.ResultOrder();
      }
   }
   else if (dealType == DEAL_TYPE_SELL)
   {
      if (pendingBuyTicket > 0)
      {
         trade.OrderDelete(pendingBuyTicket);
         pendingBuyTicket = 0;
      }
      if (UseStopReverse)
      {
         double revPrice = setupHigh + offset;
         double revLots = (posDynamicLots > 0) ? posDynamicLots : Lots;
         if (trade.BuyStop(revLots, revPrice, _Symbol, 0.0, 0.0,
                           ORDER_TIME_DAY, 0, "IDNR4_StopReverse"))
            reverseTicket = trade.ResultOrder();
      }
   }
}

void OnTradeTransaction(const MqlTradeTransaction& trans,
                        const MqlTradeRequest& request,
                        const MqlTradeResult& result)
{
   if (trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if (trans.symbol != _Symbol) return;

   if (HistoryDealSelect(trans.deal))
   {
      long magic = HistoryDealGetInteger(trans.deal, DEAL_MAGIC);
      if (magic != (long)MagicNumber) return;

      long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
      long type  = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
      double price = HistoryDealGetDouble(trans.deal, DEAL_PRICE);

      if (entry == DEAL_ENTRY_IN)
         HandleEntryFill(type, price);
   }
}

void CheckMOCExit()
{
   if (!HasOpenPosition()) return;
   if (entryDate == 0) return;

   datetime today = DateOnly(TimeCurrent());
   int daysHeld = (int)((today - entryDate) / 86400);
   if (daysHeld < MaxHoldDays) return;

   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         {
            double profit = PositionGetDouble(POSITION_PROFIT);
            if (profit <= 0.0)
               trade.PositionClose(ticket);
         }
      }
   }
}

void CheckFixedStopLoss()
{
   if (FixedStopLossUSD <= 0) return;
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         {
            double profit = PositionGetDouble(POSITION_PROFIT);
            if (profit <= -FixedStopLossUSD)
               trade.PositionClose(ticket);
         }
      }
   }
}

double DailyRealizedPnL()
{
   datetime startOfDay = DateOnly(TimeCurrent());
   if (!HistorySelect(startOfDay, TimeCurrent() + 86400)) return 0;
   double total = 0;
   uint count = HistoryDealsTotal();
   for (uint i = 0; i < count; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if (ticket == 0) continue;
      if (HistoryDealGetInteger(ticket, DEAL_MAGIC) != (long)MagicNumber) continue;
      if (HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol) continue;
      total += HistoryDealGetDouble(ticket, DEAL_PROFIT);
      total += HistoryDealGetDouble(ticket, DEAL_SWAP);
      total += HistoryDealGetDouble(ticket, DEAL_COMMISSION);
   }
   return total;
}

double UnrealizedPnL()
{
   double total = 0;
   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (PositionSelectByTicket(ticket))
      {
         if (PositionGetString(POSITION_SYMBOL) == _Symbol &&
             (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
            total += PositionGetDouble(POSITION_PROFIT);
      }
   }
   return total;
}

void CheckDailyLossCircuit()
{
   if (DailyMaxLossUSD <= 0) return;
   double total = DailyRealizedPnL() + UnrealizedPnL();
   if (total <= -DailyMaxLossUSD)
   {
      CloseAllOurPositions();
      CancelAllOurPending();
      dailyLossTripped = true;
   }
}

void UpdateTrailingStop()
{
   if (!UseTrailingStop) return;
   if (posBracketWidth <= 0) return;

   for (int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if (!PositionSelectByTicket(ticket)) continue;
      if (PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if ((ulong)PositionGetInteger(POSITION_MAGIC) != MagicNumber) continue;

      long type = PositionGetInteger(POSITION_TYPE);
      double openPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double currentSL = PositionGetDouble(POSITION_SL);
      double currentTP = PositionGetDouble(POSITION_TP);

      if (type == POSITION_TYPE_BUY)
      {
         double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
         if (bid > posMaxFavorable) posMaxFavorable = bid;

         double profitR = (posMaxFavorable - openPrice) / posBracketWidth;
         if (profitR < TrailingActivationR) continue;

         double newSL = posMaxFavorable - TrailingStepR * posBracketWidth;
         if (newSL > currentSL && newSL < bid)
            trade.PositionModify(ticket, newSL, currentTP);
      }
      else if (type == POSITION_TYPE_SELL)
      {
         double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         if (posMaxFavorable == 0 || ask < posMaxFavorable) posMaxFavorable = ask;

         double profitR = (openPrice - posMaxFavorable) / posBracketWidth;
         if (profitR < TrailingActivationR) continue;

         double newSL = posMaxFavorable + TrailingStepR * posBracketWidth;
         if ((currentSL == 0 || newSL < currentSL) && newSL > ask)
            trade.PositionModify(ticket, newSL, currentTP);
      }
   }
}

void OnTick()
{
   datetime curBar = iTime(_Symbol, _Period, 0);
   if (curBar == lastBarTime) return;
   lastBarTime = curBar;

   // v0.2 risk checks (run every new bar regardless of position state)
   CheckFixedStopLoss();
   CheckDailyLossCircuit();
   if (HasOpenPosition()) UpdateTrailingStop();

   CheckMOCExit();

   datetime today = DateOnly(TimeCurrent());
   if (setupDate == today) return;
   setupDate = today;

   // Reset daily loss flag at start of new day
   dailyLossTripped = false;

   if (HasOpenPosition()) return;
   if (dailyLossTripped) return;

   CancelAllOurPending();
   entryDate = 0;
   posBracketWidth = 0;
   posMaxFavorable = 0;

   if (Bars(_Symbol, PERIOD_D1) < NRPeriod + 3) return;

   if (IsInsideDay() && IsNRPeriod())
      PlaceBracket();
}
//+------------------------------------------------------------------+
