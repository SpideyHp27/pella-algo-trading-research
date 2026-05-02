//+------------------------------------------------------------------+
//|                                                     BridgeEA.mq5 |
//|  Loads MT5Bridge.dll into the MT5 process and starts an HTTP     |
//|  server on localhost:<Port>. Pushes live state (account, positions,|
//|  orders, terminal) to the bridge on a timer.                      |
//|                                                                    |
//|  Attach to ANY chart (one instance per MT5 terminal). DLL must be |
//|  in MQL5\Libraries\. "Allow DLL imports" must be enabled.         |
//+------------------------------------------------------------------+
#property copyright "Zeno"
#property version   "0.47"
#property strict
#property description "MT5Bridge v0.40 - class-name fix + readable error JSON + /version endpoint"

#import "MT5Bridge.dll"
   int  BridgeStart(int port);
   void BridgeStop();
   void BridgePushAccount(double balance, double equity, double margin, double freeMargin, double profit);
   void BridgePushPositions(string json);
   void BridgePushOrders(string json);
   void BridgePushTerminal(string json);
#import

input int    Port            = 8889;   // HTTP server port
input int    PushIntervalMs  = 1000;   // State refresh interval

//+------------------------------------------------------------------+
int OnInit()
{
   int ok = BridgeStart(Port);
   if(ok != 1)
   {
      Print("BridgeEA: FAILED to start MT5Bridge on port ", Port,
            " — check that MT5Bridge.dll is in MQL5\\Libraries and DLL imports are allowed.");
      return INIT_FAILED;
   }

   // Push terminal info once at startup (rarely changes).
   BridgePushTerminal(BuildTerminalJson());

   EventSetMillisecondTimer(PushIntervalMs);
   PrintFormat("BridgeEA: MT5Bridge listening on http://localhost:%d  | push every %dms", Port, PushIntervalMs);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   BridgeStop();
   Print("BridgeEA: stopped (reason=", reason, ")");
}

//+------------------------------------------------------------------+
void OnTimer()
{
   // Account snapshot — pushed every PushIntervalMs.
   BridgePushAccount(
      AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_MARGIN),
      AccountInfoDouble(ACCOUNT_MARGIN_FREE),
      AccountInfoDouble(ACCOUNT_PROFIT)
   );

   BridgePushPositions(BuildPositionsJson());
   BridgePushOrders(BuildOrdersJson());
}

//+------------------------------------------------------------------+
void OnTick() { /* no-op — bridge is timer-driven */ }

//+------------------------------------------------------------------+
//|  JSON builders (no MQL5 stdlib for JSON, so build by string)      |
//+------------------------------------------------------------------+

string BuildTerminalJson()
{
   string s = "{";
   s += "\"company\":\"" + EscapeStr(AccountInfoString(ACCOUNT_COMPANY)) + "\",";
   s += "\"server\":\"" + EscapeStr(AccountInfoString(ACCOUNT_SERVER)) + "\",";
   s += "\"name\":\"" + EscapeStr(AccountInfoString(ACCOUNT_NAME)) + "\",";
   s += "\"login\":" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   s += "\"currency\":\"" + EscapeStr(AccountInfoString(ACCOUNT_CURRENCY)) + "\",";
   s += "\"leverage\":" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
   s += "\"trade_allowed\":" + (AccountInfoInteger(ACCOUNT_TRADE_ALLOWED) ? "true" : "false") + ",";
   s += "\"terminal_path\":\"" + EscapeStr(TerminalInfoString(TERMINAL_PATH)) + "\",";
   s += "\"terminal_data_path\":\"" + EscapeStr(TerminalInfoString(TERMINAL_DATA_PATH)) + "\",";
   s += "\"build\":" + IntegerToString((int)TerminalInfoInteger(TERMINAL_BUILD));
   s += "}";
   return s;
}

string BuildPositionsJson()
{
   string s = "[";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(i > 0) s += ",";
      s += "{";
      s += "\"ticket\":" + IntegerToString((int)ticket) + ",";
      s += "\"symbol\":\"" + EscapeStr(PositionGetString(POSITION_SYMBOL)) + "\",";
      s += "\"type\":\"" + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL") + "\",";
      s += "\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) + ",";
      s += "\"price_open\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 5) + ",";
      s += "\"price_current\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT), 5) + ",";
      s += "\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), 5) + ",";
      s += "\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), 5) + ",";
      s += "\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) + ",";
      s += "\"swap\":" + DoubleToString(PositionGetDouble(POSITION_SWAP), 2) + ",";
      s += "\"magic\":" + IntegerToString((int)PositionGetInteger(POSITION_MAGIC)) + ",";
      s += "\"comment\":\"" + EscapeStr(PositionGetString(POSITION_COMMENT)) + "\"";
      s += "}";
   }
   s += "]";
   return s;
}

string BuildOrdersJson()
{
   string s = "[";
   int total = OrdersTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0) continue;
      if(i > 0) s += ",";
      s += "{";
      s += "\"ticket\":" + IntegerToString((int)ticket) + ",";
      s += "\"symbol\":\"" + EscapeStr(OrderGetString(ORDER_SYMBOL)) + "\",";
      s += "\"type\":" + IntegerToString((int)OrderGetInteger(ORDER_TYPE)) + ",";
      s += "\"volume\":" + DoubleToString(OrderGetDouble(ORDER_VOLUME_INITIAL), 2) + ",";
      s += "\"price_open\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), 5) + ",";
      s += "\"sl\":" + DoubleToString(OrderGetDouble(ORDER_SL), 5) + ",";
      s += "\"tp\":" + DoubleToString(OrderGetDouble(ORDER_TP), 5) + ",";
      s += "\"magic\":" + IntegerToString((int)OrderGetInteger(ORDER_MAGIC)) + ",";
      s += "\"comment\":\"" + EscapeStr(OrderGetString(ORDER_COMMENT)) + "\"";
      s += "}";
   }
   s += "]";
   return s;
}

string EscapeStr(string s)
{
   string out_str = "";
   int len = StringLen(s);
   for(int i = 0; i < len; i++)
   {
      ushort c = StringGetCharacter(s, i);
      if(c == '"' || c == '\\') out_str += "\\";
      out_str += ShortToString(c);
   }
   return out_str;
}
