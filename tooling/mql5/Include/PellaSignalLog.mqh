//+------------------------------------------------------------------+
//|                                              PellaSignalLog.mqh  |
//|                                                                  |
//|  Lightweight signal-log writer used by every Pella EA so the     |
//|  paper-replay agent can reconstruct WHAT THE EA INTENDED         |
//|  independent of how the broker actually filled it.               |
//|                                                                  |
//|  Each call to PellaLogSignal() appends one JSONL line to a       |
//|  daily-rotated file in the MT5 Common Files directory:           |
//|                                                                  |
//|    Common/Files/Pella/signals/<EA>_<symbol>_<YYYY-MM-DD>.jsonl   |
//|                                                                  |
//|  Common Files is shared across MT5 terminal instances on a host, |
//|  which the Python paper_replay_agent reads.                      |
//|                                                                  |
//|  USAGE in any EA:                                                |
//|      #include <PellaSignalLog.mqh>                               |
//|      ...                                                         |
//|      // when EA decides to enter:                                |
//|      PellaLogSignal("entry", "BUY", 2350.12, 0.50,               |
//|                     2340.0, 2360.0, MagicNumber, "breakout");    |
//|      // when EA decides to exit:                                 |
//|      PellaLogSignal("exit",  "BUY", 2358.45, 0.50,               |
//|                     0, 0, MagicNumber, "tp_hit");                |
//|                                                                  |
//|  Fail-quiet: write errors are reported via Print(); the EA's     |
//|  trading logic never blocks on the log.                          |
//+------------------------------------------------------------------+
#property strict

#ifndef PELLA_SIGNAL_LOG_MQH
#define PELLA_SIGNAL_LOG_MQH

#define PELLA_SIGNAL_LOG_VERSION 1
#define PELLA_SIGNAL_DIR         "Pella\\signals"

//+------------------------------------------------------------------+
//| Render an ENUM_TIMEFRAMES value as the short string the rest of  |
//| the Pella stack uses ("H2" not 16386).                           |
//+------------------------------------------------------------------+
string PellaSignalLog_TfToString(const ENUM_TIMEFRAMES tf)
{
   switch(tf)
   {
      case PERIOD_M1:  return "M1";
      case PERIOD_M2:  return "M2";
      case PERIOD_M3:  return "M3";
      case PERIOD_M4:  return "M4";
      case PERIOD_M5:  return "M5";
      case PERIOD_M6:  return "M6";
      case PERIOD_M10: return "M10";
      case PERIOD_M12: return "M12";
      case PERIOD_M15: return "M15";
      case PERIOD_M20: return "M20";
      case PERIOD_M30: return "M30";
      case PERIOD_H1:  return "H1";
      case PERIOD_H2:  return "H2";
      case PERIOD_H3:  return "H3";
      case PERIOD_H4:  return "H4";
      case PERIOD_H6:  return "H6";
      case PERIOD_H8:  return "H8";
      case PERIOD_H12: return "H12";
      case PERIOD_D1:  return "D1";
      case PERIOD_W1:  return "W1";
      case PERIOD_MN1: return "MN1";
      default:         return IntegerToString((int)tf);
   }
}

//+------------------------------------------------------------------+
//| Backslash-escape \ and " inside a string before embedding it     |
//| into a JSON value. Newlines and tabs are stripped (signals are   |
//| structured; free-form notes shouldn't carry control chars).      |
//+------------------------------------------------------------------+
string PellaSignalLog_JsonEscape(const string s)
{
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(s, i);
      if(c == '\\')      out += "\\\\";
      else if(c == '"')  out += "\\\"";
      else if(c == '\n' || c == '\r' || c == '\t') out += " ";
      else               out += ShortToString(c);
   }
   return out;
}

//+------------------------------------------------------------------+
//| Format `dt` as ISO 8601 UTC: 2026-05-04T13:30:15Z                |
//+------------------------------------------------------------------+
string PellaSignalLog_IsoUtc(const datetime dt)
{
   MqlDateTime t;
   TimeToStruct(dt, t);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02dZ",
                       t.year, t.mon, t.day, t.hour, t.min, t.sec);
}

string PellaSignalLog_DateOnly(const datetime dt)
{
   MqlDateTime t;
   TimeToStruct(dt, t);
   return StringFormat("%04d-%02d-%02d", t.year, t.mon, t.day);
}

//+------------------------------------------------------------------+
//| Build the daily-rotated filename. `ea` is sanitised to filename- |
//| safe chars (alnum + underscore). `symbol` likewise.              |
//+------------------------------------------------------------------+
string PellaSignalLog_Sanitise(const string s)
{
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(s, i);
      bool ok = (c >= '0' && c <= '9') ||
                (c >= 'A' && c <= 'Z') ||
                (c >= 'a' && c <= 'z') ||
                c == '_' || c == '-' || c == '.';
      out += ok ? ShortToString(c) : "_";
   }
   return out;
}

string PellaSignalLog_Path(const string ea, const string symbol,
                           const datetime now)
{
   return PELLA_SIGNAL_DIR + "\\" +
          PellaSignalLog_Sanitise(ea)     + "_" +
          PellaSignalLog_Sanitise(symbol) + "_" +
          PellaSignalLog_DateOnly(now)    + ".jsonl";
}

//+------------------------------------------------------------------+
//| Append one JSONL record. Returns true on success.                |
//|                                                                  |
//| Required:  signal_type ("entry"/"exit"/"modify"/"cancel"),       |
//|            side ("BUY"/"SELL"), price, volume                    |
//| Optional:  sl, tp, magic, comment, ea_override, symbol_override, |
//|            tf_override (defaults: __PROGRAMNAME, Symbol(),       |
//|            current chart period).                                |
//+------------------------------------------------------------------+
bool PellaLogSignal(
   const string signal_type,
   const string side,
   const double price,
   const double volume,
   const double sl                       = 0.0,
   const double tp                       = 0.0,
   const long   magic                    = 0,
   const string comment                  = "",
   const string ea_override              = "",
   const string symbol_override          = "",
   const ENUM_TIMEFRAMES tf_override     = PERIOD_CURRENT
)
{
   const datetime now    = TimeGMT();
   const string   ea     = (StringLen(ea_override)     > 0) ? ea_override
                                                            : MQLInfoString(MQL_PROGRAM_NAME);
   const string   sym    = (StringLen(symbol_override) > 0) ? symbol_override
                                                            : Symbol();
   const ENUM_TIMEFRAMES tf = (tf_override == PERIOD_CURRENT) ? Period() : tf_override;
   const int      digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   const string path = PellaSignalLog_Path(ea, sym, now);

   const int handle = FileOpen(
      path,
      FILE_WRITE | FILE_READ | FILE_TXT | FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_COMMON | FILE_ANSI
   );
   if(handle == INVALID_HANDLE)
   {
      PrintFormat("[PellaSignalLog] FileOpen failed for %s, err=%d", path, GetLastError());
      return false;
   }

   FileSeek(handle, 0, SEEK_END);

   const string line = StringFormat(
      "{\"v\":%d,\"ts\":\"%s\",\"ea\":\"%s\",\"symbol\":\"%s\",\"tf\":\"%s\","
      "\"magic\":%I64d,\"signal_type\":\"%s\",\"side\":\"%s\","
      "\"price\":%s,\"volume\":%s,\"sl\":%s,\"tp\":%s,\"comment\":\"%s\"}",
      PELLA_SIGNAL_LOG_VERSION,
      PellaSignalLog_IsoUtc(now),
      PellaSignalLog_JsonEscape(ea),
      PellaSignalLog_JsonEscape(sym),
      PellaSignalLog_TfToString(tf),
      (long)magic,
      PellaSignalLog_JsonEscape(signal_type),
      PellaSignalLog_JsonEscape(side),
      DoubleToString(price,  digits),
      DoubleToString(volume, 4),
      DoubleToString(sl,     digits),
      DoubleToString(tp,     digits),
      PellaSignalLog_JsonEscape(comment)
   );

   FileWriteString(handle, line + "\n");
   FileFlush(handle);
   FileClose(handle);
   return true;
}

#endif // PELLA_SIGNAL_LOG_MQH
