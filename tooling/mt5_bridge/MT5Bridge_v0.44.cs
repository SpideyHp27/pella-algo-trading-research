// MT5Bridge.cs
// In-process bridge for MetaTrader 5.
// Same architectural class as NT8 ClaudeBridge / BacktestServer:
//   - Runs INSIDE MT5 process (loaded via MQL5 #import from BridgeEA.mq5)
//   - HTTP server on localhost:<port>
//   - Live state cached from MQL5 EA push calls
//   - Strategy Tester driven via .NET UIAutomation (in-process, no IPC overhead)
//
// Build:  dotnet publish -c Release -r win-x64
// Output: bin/Release/net8.0-windows/win-x64/publish/MT5Bridge.dll

using System;
using System.Collections.Concurrent;
using System.IO;
using System.Linq;
using System.Net;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;

namespace MT5Bridge;

public static class Bridge
{
    private static HttpListener? _listener;
    private static CancellationTokenSource? _cts;
    private static Task? _serverTask;
    private static int _port;

    private static readonly ConcurrentDictionary<string, string> _state = new();
    private static readonly ConcurrentDictionary<string, BacktestJob> _jobs = new();
    private static readonly JsonSerializerOptions _jsonOpts = new() { WriteIndented = false };

    // Build identity — baked in at compile time so /version proves which DLL is loaded.
    // Update VERSION manually, BUILD_TIMESTAMP regenerates per build.
    private const string VERSION = "0.44.0";
    private static readonly string BUILD_TIMESTAMP = DateTime.UtcNow.ToString("O");
    private static readonly string BUILD_FEATURES = "sta-threading,marshaling-utf16-fix,manual-json-account,version-endpoint,combo-cbn-selchange-fix,combo-findstring-exact";

    // ── STA dispatcher for UIAutomation calls ─────────────────────────────
    // .NET UIAutomation requires STA threading for many WPF/Win32 control
    // operations. Calling from an MTA Task thread (which is the default for
    // Task.Run) raises COM AccessViolations that crash the host process (MT5).
    // ALL UIAutomation work runs through this dedicated STA worker thread.
    private static readonly System.Collections.Concurrent.BlockingCollection<Action> _staQueue
        = new System.Collections.Concurrent.BlockingCollection<Action>();
    private static Thread? _staThread;
    private const int STA_TIMEOUT_MS = 30_000;

    private class BacktestJob
    {
        public string Id { get; set; } = "";
        public string Status { get; set; } = "queued"; // queued | running | completed | failed
        public string? Results { get; set; }
        public string? Error { get; set; }
        public DateTime Created { get; set; }
        public DateTime? Completed { get; set; }
    }

    // ── DLL exports (called from MQL5 EA via #import) ─────────────────────

    [UnmanagedCallersOnly(EntryPoint = "BridgeStart", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static int BridgeStart(int port)
    {
        try
        {
            _port = port > 0 ? port : 8889;

            // Spin up dedicated STA thread for UIAutomation BEFORE starting the HTTP listener.
            // This thread owns ALL UIAutomation calls — protects MT5 host process from COM
            // threading violations.
            _staThread = new Thread(StaWorkerLoop)
            {
                IsBackground = true,
                Name = "MT5Bridge.STA"
            };
            _staThread.SetApartmentState(ApartmentState.STA);
            _staThread.Start();

            _listener = new HttpListener();
            _listener.Prefixes.Add($"http://localhost:{_port}/");
            _listener.Start();
            _cts = new CancellationTokenSource();
            _serverTask = Task.Run(() => HandleLoop(_cts.Token));
            return 1;
        }
        catch
        {
            return 0;
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgeStop", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgeStop()
    {
        try
        {
            _cts?.Cancel();
            if (_listener?.IsListening == true)
            {
                _listener.Stop();
                _listener.Close();
            }
            _staQueue.CompleteAdding();
            _staThread?.Join(2000);
        }
        catch { }
    }

    // ── STA worker — runs all UIAutomation calls ──────────────────────────

    private static void StaWorkerLoop()
    {
        try
        {
            foreach (var work in _staQueue.GetConsumingEnumerable())
            {
                try { work(); }
                catch { /* swallow — caller already received exception via TaskCompletionSource */ }
            }
        }
        catch (InvalidOperationException) { /* CompleteAdding called — normal shutdown */ }
        catch { /* never let STA worker propagate — would crash host process */ }
    }

    /// <summary>
    /// Run a function on a fresh STA thread and wait for the result.
    /// Per-call STA thread (not shared worker) — simpler, no queue lifecycle issues,
    /// reliable under NativeAOT. ~1-5ms thread creation overhead per call.
    /// All UIAutomation calls MUST go through this — calling them on an MTA
    /// thread causes COM AccessViolations that crash MT5.
    /// </summary>
    private static T RunOnSta<T>(Func<T> func, int timeoutMs = STA_TIMEOUT_MS)
    {
        T? result = default;
        Exception? error = null;
        var thread = new Thread(() =>
        {
            try { result = func(); }
            catch (Exception ex) { error = ex; }
        })
        {
            IsBackground = true,
            Name = "MT5Bridge.STA.OneShot"
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!thread.Join(timeoutMs))
            throw new TimeoutException($"STA work timed out after {timeoutMs}ms");
        if (error != null) throw error;
        return result!;
    }

    /// <summary>
    /// RunOnSta wrapper that NEVER throws — converts any exception to a JSON error
    /// response. Use this in HTTP handler entry points so a UIAutomation crash
    /// becomes a 500 response, not a host process crash.
    /// </summary>
    private static string RunOnStaSafe(Func<string> func, int timeoutMs = STA_TIMEOUT_MS)
    {
        try
        {
            return RunOnSta(func, timeoutMs);
        }
        catch (TimeoutException tex)
        {
            return Err(tex.Message, "TimeoutException");
        }
        catch (AggregateException aex) when (aex.InnerException != null)
        {
            return Err(aex.InnerException.Message, aex.InnerException.GetType().Name);
        }
        catch (Exception ex)
        {
            return Err(ex.Message, ex.GetType().Name);
        }
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgePushAccount", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushAccount(double balance, double equity, double margin, double freeMargin, double profit)
    {
        // Manual JSON to avoid NativeAOT anonymous-type serialization issues.
        // Use InvariantCulture so decimals are "." not "," (ES/RO locale guard).
        var inv = System.Globalization.CultureInfo.InvariantCulture;
        _state["account"] = "{"
            + "\"balance\":"      + balance.ToString(inv)     + ","
            + "\"equity\":"       + equity.ToString(inv)      + ","
            + "\"margin\":"       + margin.ToString(inv)      + ","
            + "\"free_margin\":"  + freeMargin.ToString(inv)  + ","
            + "\"profit\":"       + profit.ToString(inv)      + ","
            + "\"ts\":\""         + DateTime.UtcNow.ToString("O") + "\""
            + "}";
    }

    // EA pushes positions/orders as pre-built JSON arrays (assembled on the MQL5 side from PositionsTotal())
    [UnmanagedCallersOnly(EntryPoint = "BridgePushPositions", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushPositions(IntPtr jsonPtr)
    {
        var json = Marshal.PtrToStringUni(jsonPtr);
        if (json != null) _state["positions"] = json;
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgePushOrders", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushOrders(IntPtr jsonPtr)
    {
        var json = Marshal.PtrToStringUni(jsonPtr);
        if (json != null) _state["orders"] = json;
    }

    [UnmanagedCallersOnly(EntryPoint = "BridgePushTerminal", CallConvs = new[] { typeof(CallConvCdecl) })]
    public static void BridgePushTerminal(IntPtr jsonPtr)
    {
        var json = Marshal.PtrToStringUni(jsonPtr);
        if (json != null) _state["terminal"] = json;
    }

    // ── HTTP listener loop ────────────────────────────────────────────────

    private static async Task HandleLoop(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _listener?.IsListening == true)
        {
            try
            {
                var ctx = await _listener.GetContextAsync();
                _ = Task.Run(() => Dispatch(ctx));
            }
            catch (HttpListenerException) { break; }
            catch (ObjectDisposedException) { break; }
            catch { /* keep listening */ }
        }
    }

    private static void Dispatch(HttpListenerContext ctx)
    {
        string body = "{}";
        int status = 200;
        try
        {
            string path = (ctx.Request.Url?.AbsolutePath ?? "/").TrimEnd('/').ToLowerInvariant();
            if (path == "") path = "/";
            string method = ctx.Request.HttpMethod;

            // Path-parametrized: GET /backtest/{id}
            if (method == "GET" && path.StartsWith("/backtest/"))
            {
                var id = path["/backtest/".Length..];
                body = GetJob(id);
            }
            else
            {
                body = (method, path) switch
                {
                    ("GET", "/")           => HealthJson(),
                    ("GET", "/health")     => HealthJson(),
                    ("GET", "/version")    => VersionJson(),
                    ("GET", "/account")    => _state.TryGetValue("account",   out var a) ? a : """{"error":"no account state — is BridgeEA running?"}""",
                    ("GET", "/positions")  => _state.TryGetValue("positions", out var p) ? p : "[]",
                    ("GET", "/orders")     => _state.TryGetValue("orders",    out var o) ? o : "[]",
                    ("GET", "/terminal")   => _state.TryGetValue("terminal",  out var tt) ? tt : "{}",
                    ("POST", "/tester/configure") => TesterConfigure(ctx.Request),
                    ("POST", "/tester/run")       => TesterRunSync(ctx.Request),
                    ("POST", "/tester/show_tab")  => TesterShowTab(ctx.Request),
                    ("POST", "/backtest")         => EnqueueBacktest(ctx.Request),
                    ("GET",  "/backtests")        => ListJobs(),
                    ("GET",  "/tester/diag")      => TesterDiag(),
                    _ => Fail(ref status, 404, $"unknown endpoint: {method} {path}")
                };
            }
        }
        catch (Exception ex)
        {
            status = 500;
            body = Err(ex.Message, ex.GetType().Name);
        }
        finally
        {
            try
            {
                var bytes = Encoding.UTF8.GetBytes(body);
                ctx.Response.StatusCode = status;
                ctx.Response.ContentType = "application/json";
                ctx.Response.ContentLength64 = bytes.Length;
                ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
                ctx.Response.OutputStream.Close();
            }
            catch { }
        }
    }

    private static string Fail(ref int status, int code, string msg)
    {
        status = code;
        return Err(msg);
    }

    private static string HealthJson() =>
        "{\"status\":\"ok\",\"service\":\"MT5Bridge\",\"version\":\""
        + VERSION + "\",\"build_timestamp\":\"" + BUILD_TIMESTAMP + "\"}";

    private static string VersionJson() =>
        "{\"version\":\"" + VERSION + "\","
        + "\"build_timestamp\":\"" + BUILD_TIMESTAMP + "\","
        + "\"features\":\"" + BUILD_FEATURES + "\","
        + "\"runtime\":\".NET 8 NativeAOT\","
        + "\"thread_model\":\"STA worker for UIAutomation, MTA for state reads\","
        + "\"sta_thread_alive\":" + (_staThread?.IsAlive == true ? "true" : "false") + ","
        + "\"jobs_count\":" + _jobs.Count + "}";

    // ── Strategy Tester driving via .NET UIAutomation (in-process) ────────
    //
    // The Strategy Tester panel is a docked WPF/WinForms hybrid inside MT5's
    // main window. Once we discover its AutomationIds (via Inspect.exe walk
    // on the live tree), we use AutomationElement.FindFirst with property
    // conditions to locate controls and ValuePattern/InvokePattern/etc. to
    // drive them.
    //
    // V0.1: stubs return "todo" with diagnostic info from the live tree.
    // V0.2: fill in actual ST control paths after one inspection pass.

    // Find MT5 window — try (1) terminal64.exe process MainWindowHandle (most robust),
    // (2) class name "MetaQuotes::MetaTrader::5.00" (verified live April 19, 2026),
    // (3) name contains "MetaTrader". The class name format may vary by build.
    private static AutomationElement? FindMt5Window()
    {
        // Path 1: process-based (most robust — survives MT5 build version changes)
        try
        {
            var proc = System.Diagnostics.Process.GetProcessesByName("terminal64")
                .FirstOrDefault(p => p.MainWindowHandle != IntPtr.Zero);
            if (proc != null && proc.MainWindowHandle != IntPtr.Zero)
            {
                var el = AutomationElement.FromHandle(proc.MainWindowHandle);
                if (el != null) return el;
            }
        }
        catch { }

        // Path 2: exact class name (MT5 build 5800)
        var root = AutomationElement.RootElement;
        return root.FindFirst(TreeScope.Children,
            new PropertyCondition(AutomationElement.ClassNameProperty, "MetaQuotes::MetaTrader::5.00"))
            ?? root.FindFirst(TreeScope.Children,
                new PropertyCondition(AutomationElement.ClassNameProperty, "MetaQuotes::MetaTrader::5::Wnd"));
    }

    // ── Error JSON helper — builds string manually so NativeAOT doesn't swallow it ──
    private static string Err(string msg, string? type = null) =>
        "{\"error\":\"" + Escape(msg ?? "") + "\""
        + (type != null ? ",\"type\":\"" + Escape(type) + "\"" : "")
        + "}";

    private static string TesterDiag() => RunOnStaSafe(() => TesterDiagImpl());

    private static string TesterDiagImpl()
    {
        try
        {
            var mt5 = FindMt5Window();
            if (mt5 == null) return """{"error":"MT5 main window not found"}""";

            var sb = new StringBuilder();
            sb.Append("{\"mt5\":{");
            sb.Append($"\"name\":\"{Escape(mt5.Current.Name)}\",");
            sb.Append($"\"class\":\"{Escape(mt5.Current.ClassName)}\",");
            sb.Append($"\"automation_id\":\"{Escape(mt5.Current.AutomationId)}\"");
            sb.Append("},\"children\":[");

            var walker = TreeWalker.ControlViewWalker;
            var child = walker.GetFirstChild(mt5);
            bool first = true;
            int count = 0;
            while (child != null && count < 50)
            {
                if (!first) sb.Append(',');
                sb.Append('{')
                  .Append($"\"name\":\"{Escape(child.Current.Name)}\",")
                  .Append($"\"class\":\"{Escape(child.Current.ClassName)}\",")
                  .Append($"\"type\":\"{child.Current.ControlType.ProgrammaticName}\",")
                  .Append($"\"automation_id\":\"{Escape(child.Current.AutomationId)}\"")
                  .Append('}');
                first = false;
                child = walker.GetNextSibling(child);
                count++;
            }
            sb.Append("]}");
            return sb.ToString();
        }
        catch (Exception ex)
        {
            return Err(ex.Message);
        }
    }

    // ── Pure Win32 driving — NO UIAutomation, NO COM, NO STA ────────────────
    // All ST control manipulation goes through Win32 messages on the validated
    // numeric Win32 control IDs. Thread-safe, can't crash MT5, NativeAOT-clean.

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetWindow(IntPtr hWnd, uint uCmd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern int GetDlgCtrlID(IntPtr hwndCtl);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetDlgItem(IntPtr hDlg, int nIDDlgItem);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessageStr(IntPtr hWnd, uint Msg, IntPtr wParam, string lParam);

    [DllImport("user32.dll", EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessageInt(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowTextW(IntPtr hWnd, [Out] System.Text.StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassNameW(IntPtr hWnd, [Out] System.Text.StringBuilder lpClassName, int nMaxCount);

    [DllImport("user32.dll", EntryPoint = "SendMessageW")]
    private static extern int SendMessageSysTime(IntPtr hWnd, uint Msg, IntPtr wParam, ref SYSTEMTIME lParam);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessageFindStr(IntPtr hWnd, uint Msg, IntPtr wParam, string lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct SYSTEMTIME
    {
        public ushort wYear, wMonth, wDayOfWeek, wDay, wHour, wMinute, wSecond, wMilliseconds;
    }

    private const uint GW_CHILD          = 5;
    private const uint GW_HWNDNEXT       = 2;
    private const uint WM_SETTEXT        = 0x000C;
    private const uint WM_GETTEXT        = 0x000D;
    private const uint BM_CLICK          = 0x00F5;
    private const uint CB_FINDSTRING       = 0x014C;
    private const uint CB_SELECTSTRING     = 0x014D;
    private const uint CB_SETCURSEL        = 0x014E;
    private const uint CB_GETCOUNT         = 0x0146;
    private const uint CB_FINDSTRINGEXACT  = 0x0158;
    private const uint DTM_SETSYSTEMTIME   = 0x1002;
    private const uint WM_COMMAND          = 0x0111;
    private const int  CBN_SELCHANGE       = 1;
    private const int  CB_ERR              = -1;

    // Tab control messages (SysTabControl32)
    private const uint TCM_FIRST         = 0x1300;
    private const uint TCM_SETCURSEL     = TCM_FIRST + 12;  // 0x130C
    private const uint TCM_GETITEMCOUNT  = TCM_FIRST + 4;   // 0x1304
    private const uint TCM_GETCURSEL     = TCM_FIRST + 11;  // 0x130B
    private const uint TCM_GETITEMW      = TCM_FIRST + 60;  // 0x133C
    private const uint TCIF_TEXT         = 0x0001;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct TCITEMW
    {
        public uint mask;
        public uint dwState;
        public uint dwStateMask;
        public IntPtr pszText;
        public int cchTextMax;
        public int iImage;
        public IntPtr lParam;
    }

    [DllImport("user32.dll", EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessageTcItem(IntPtr hWnd, uint Msg, IntPtr wParam, ref TCITEMW lParam);

    [DllImport("user32.dll")]
    private static extern IntPtr GetParent(IntPtr hWnd);

    // NMHDR for WM_NOTIFY — tells the parent to redraw the tab's content pane
    [StructLayout(LayoutKind.Sequential)]
    private struct NMHDR
    {
        public IntPtr hwndFrom;
        public IntPtr idFrom;
        public int code;
    }

    [DllImport("user32.dll", EntryPoint = "SendMessageW")]
    private static extern IntPtr SendMessageNMHDR(IntPtr hWnd, uint Msg, IntPtr wParam, ref NMHDR lParam);

    private const uint WM_NOTIFY      = 0x004E;
    private const int  TCN_FIRST      = -550;
    private const int  TCN_SELCHANGE  = TCN_FIRST - 1;  // -551

    // ── Strategy Tester field-control IDs (validated April 19, 2026) ──
    // See WPF_TREE.md for the full map.
    private const string ST_INNER_ID    = "10476";
    private const string ID_EXPERT      = "10485";
    private const string ID_SYMBOL      = "10486";
    private const string ID_TIMEFRAME   = "10487";
    private const string ID_DATE_TYPE   = "10123";
    private const string ID_START_DATE  = "10550";
    private const string ID_END_DATE    = "10551";
    private const string ID_FORWARD     = "10492";
    private const string ID_DELAYS      = "10488";
    private const string ID_MODELLING   = "10515";
    private const string ID_DEPOSIT     = "10489";
    private const string ID_CURRENCY    = "10559";
    private const string ID_LEVERAGE    = "10473";
    private const string ID_OPTIMIZE    = "10491";
    private const string ID_START_BTN   = "16790";

    // Find MT5 main HWND via running process (no UIAutomation, no COM)
    private static IntPtr FindMt5Hwnd()
    {
        try
        {
            var proc = System.Diagnostics.Process.GetProcessesByName("terminal64")
                .FirstOrDefault(p => p.MainWindowHandle != IntPtr.Zero);
            return proc?.MainWindowHandle ?? IntPtr.Zero;
        }
        catch { return IntPtr.Zero; }
    }

    // Iterative DFS through all descendant windows, find by dialog control ID.
    // Uses GetWindow/GetDlgCtrlID — no callbacks, NativeAOT-clean.
    private static IntPtr FindDescendantById(IntPtr root, int id)
    {
        if (root == IntPtr.Zero) return IntPtr.Zero;
        var stack = new System.Collections.Generic.Stack<IntPtr>();
        stack.Push(root);
        while (stack.Count > 0)
        {
            var current = stack.Pop();
            var child = GetWindow(current, GW_CHILD);
            while (child != IntPtr.Zero)
            {
                if (GetDlgCtrlID(child) == id) return child;
                stack.Push(child);
                child = GetWindow(child, GW_HWNDNEXT);
            }
        }
        return IntPtr.Zero;
    }

    private static bool SetComboValueWin32(IntPtr root, int id, string value)
    {
        var h = FindDescendantById(root, id);
        if (h == IntPtr.Zero) return false;
        // Strategy: try exact-match first (avoids picking the wrong item when
        // names share a prefix, e.g. "Donchian" vs "DonchianChannelEA"), then
        // fall back to prefix match.
        var idx = SendMessageStr(h, CB_FINDSTRINGEXACT, new IntPtr(-1), value).ToInt64();
        if (idx == CB_ERR)
            idx = SendMessageStr(h, CB_FINDSTRING, new IntPtr(-1), value).ToInt64();

        if (idx != CB_ERR)
        {
            SendMessageInt(h, CB_SETCURSEL, new IntPtr((int)idx), IntPtr.Zero);

            // CRITICAL: CB_SETCURSEL changes the visual selection but does NOT
            // fire CBN_SELCHANGE — programmatic selection is treated as
            // synthetic. MT5's Strategy Tester only loads the new EA / refreshes
            // dependent fields when it receives WM_COMMAND with CBN_SELCHANGE
            // from the combo's parent. Without this, the dropdown text appears
            // to change but the underlying selection is not committed (the
            // notorious "set_ok but MT5 keeps the previous EA loaded" bug).
            //
            // wParam encoding: HIWORD(notification code) | LOWORD(control id)
            // lParam: handle to the combobox sending the notification.
            var parent = GetParent(h);
            if (parent != IntPtr.Zero)
            {
                int ctrlId = GetDlgCtrlID(h);
                long wParam = ((long)CBN_SELCHANGE << 16) | ((uint)ctrlId & 0xFFFFu);
                SendMessageInt(parent, WM_COMMAND, new IntPtr(wParam), h);
            }
            return true;
        }
        // Editable: set text directly
        SendMessageStr(h, WM_SETTEXT, IntPtr.Zero, value);
        return true;
    }

    private static bool SetDateFieldWin32(IntPtr root, int id, DateTime date)
    {
        var h = FindDescendantById(root, id);
        if (h == IntPtr.Zero) return false;
        var st = new SYSTEMTIME
        {
            wYear = (ushort)date.Year,
            wMonth = (ushort)date.Month,
            wDay = (ushort)date.Day
        };
        SendMessageSysTime(h, DTM_SETSYSTEMTIME, IntPtr.Zero, ref st);
        return true;
    }

    private static bool ClickButtonWin32(IntPtr root, int id)
    {
        var h = FindDescendantById(root, id);
        if (h == IntPtr.Zero) return false;
        SendMessageInt(h, BM_CLICK, IntPtr.Zero, IntPtr.Zero);
        return true;
    }

    private static string GetWindowTextWin32(IntPtr hwnd)
    {
        var sb = new StringBuilder(256);
        GetWindowTextW(hwnd, sb, 256);
        return sb.ToString();
    }

    // Find first descendant window matching className (DFS, GetWindow/GW_CHILD + GetClassNameW)
    private static IntPtr FindDescendantByClass(IntPtr root, string className)
    {
        if (root == IntPtr.Zero) return IntPtr.Zero;
        var stack = new System.Collections.Generic.Stack<IntPtr>();
        stack.Push(root);
        var sb = new System.Text.StringBuilder(64);
        while (stack.Count > 0)
        {
            var current = stack.Pop();
            var child = GetWindow(current, GW_CHILD);
            while (child != IntPtr.Zero)
            {
                sb.Clear();
                GetClassNameW(child, sb, 64);
                if (sb.ToString() == className) return child;
                stack.Push(child);
                child = GetWindow(child, GW_HWNDNEXT);
            }
        }
        return IntPtr.Zero;
    }

    // Enumerate ALL descendant windows matching className (not just first)
    private static System.Collections.Generic.List<IntPtr> FindAllByClass(IntPtr root, string className)
    {
        var result = new System.Collections.Generic.List<IntPtr>();
        if (root == IntPtr.Zero) return result;
        var stack = new System.Collections.Generic.Stack<IntPtr>();
        stack.Push(root);
        var sb = new System.Text.StringBuilder(64);
        while (stack.Count > 0)
        {
            var current = stack.Pop();
            var child = GetWindow(current, GW_CHILD);
            while (child != IntPtr.Zero)
            {
                sb.Clear();
                GetClassNameW(child, sb, 64);
                if (sb.ToString() == className) result.Add(child);
                stack.Push(child);
                child = GetWindow(child, GW_HWNDNEXT);
            }
        }
        return result;
    }

    // Find the tab index whose caption matches tabName (case-insensitive)
    // Returns -1 if not found.
    private static int GetTabIndexByName(IntPtr tabHwnd, string tabName)
    {
        var count = (int)SendMessageInt(tabHwnd, TCM_GETITEMCOUNT, IntPtr.Zero, IntPtr.Zero);
        if (count <= 0) return -1;
        var buf = Marshal.AllocHGlobal(256 * 2);
        try
        {
            for (int i = 0; i < count; i++)
            {
                var item = new TCITEMW { mask = TCIF_TEXT, pszText = buf, cchTextMax = 256 };
                SendMessageTcItem(tabHwnd, TCM_GETITEMW, new IntPtr(i), ref item);
                var text = Marshal.PtrToStringUni(buf) ?? "";
                if (string.Equals(text, tabName, StringComparison.OrdinalIgnoreCase))
                    return i;
            }
        }
        finally { Marshal.FreeHGlobal(buf); }
        return -1;
    }

    // Find the Tester tab control (the one containing "Graph" tab) and switch to tabName.
    // Iterates all SysTabControl32 descendants since MT5 has several tab controls.
    // CRITICAL: after TCM_SETCURSEL we must send WM_NOTIFY/TCN_SELCHANGE to the parent
    // so MT5 redraws the content pane. Without this, only the tab header visually updates.
    private static bool ShowTesterTab(IntPtr mt5, string tabName)
    {
        var tabs = FindAllByClass(mt5, "SysTabControl32");
        foreach (var tabHwnd in tabs)
        {
            int idx = GetTabIndexByName(tabHwnd, tabName);
            if (idx >= 0)
            {
                SendMessageInt(tabHwnd, TCM_SETCURSEL, new IntPtr(idx), IntPtr.Zero);

                var parent = GetParent(tabHwnd);
                if (parent != IntPtr.Zero)
                {
                    var ctrlId = GetDlgCtrlID(tabHwnd);
                    var nm = new NMHDR
                    {
                        hwndFrom = tabHwnd,
                        idFrom   = new IntPtr(ctrlId),
                        code     = TCN_SELCHANGE,
                    };
                    SendMessageNMHDR(parent, WM_NOTIFY, new IntPtr(ctrlId), ref nm);
                }
                return true;
            }
        }
        return false;
    }

    // ── /tester/show_tab?name=Graph — switch Strategy Tester tab by name ──
    private static string TesterShowTab(HttpListenerRequest req)
    {
        try
        {
            var name = req.QueryString["name"];
            if (string.IsNullOrEmpty(name)) return Err("name query param required (e.g. ?name=Graph)");
            var mt5 = FindMt5Hwnd();
            if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");
            bool ok = ShowTesterTab(mt5, name);
            return "{\"status\":\"" + (ok ? "switched" : "not_found") + "\",\"tab\":\"" + Escape(name) + "\"}";
        }
        catch (Exception ex) { return Err(ex.Message, ex.GetType().Name); }
    }

    // ── /tester/configure — pure Win32 SendMessage, no UIAutomation ──

    private static string TesterConfigure(HttpListenerRequest req)
    {
        try
        {
            string bodyStr;
            using (var reader = new StreamReader(req.InputStream))
                bodyStr = reader.ReadToEnd();

            var mt5 = FindMt5Hwnd();
            if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");

            var body = JsonNode.Parse(bodyStr) as JsonObject ?? new JsonObject();
            var ok = new System.Collections.Generic.List<string>();
            var failed = new System.Collections.Generic.List<string>();

            void TryCombo(string field, int id)
            {
                var v = body[field]?.ToString();
                if (string.IsNullOrEmpty(v)) return;
                (SetComboValueWin32(mt5, id, v) ? ok : failed).Add(field);
            }

            TryCombo("expert",       int.Parse(ID_EXPERT));
            TryCombo("symbol",       int.Parse(ID_SYMBOL));
            TryCombo("timeframe",    int.Parse(ID_TIMEFRAME));
            TryCombo("delays",       int.Parse(ID_DELAYS));
            TryCombo("modelling",    int.Parse(ID_MODELLING));
            TryCombo("deposit",      int.Parse(ID_DEPOSIT));
            TryCombo("currency",     int.Parse(ID_CURRENCY));
            TryCombo("leverage",     int.Parse(ID_LEVERAGE));
            TryCombo("optimization", int.Parse(ID_OPTIMIZE));
            TryCombo("forward_type", int.Parse(ID_FORWARD));

            if (body["start_date"] != null || body["end_date"] != null)
                SetComboValueWin32(mt5, int.Parse(ID_DATE_TYPE), "Custom period");

            var sdStr = body["start_date"]?.ToString();
            if (!string.IsNullOrEmpty(sdStr) && DateTime.TryParse(sdStr, out var sd))
                (SetDateFieldWin32(mt5, int.Parse(ID_START_DATE), sd) ? ok : failed).Add("start_date");

            var edStr = body["end_date"]?.ToString();
            if (!string.IsNullOrEmpty(edStr) && DateTime.TryParse(edStr, out var ed))
                (SetDateFieldWin32(mt5, int.Parse(ID_END_DATE), ed) ? ok : failed).Add("end_date");

            // Manual JSON — no anonymous types (NativeAOT safe)
            var sb = new StringBuilder();
            sb.Append("{\"status\":\"").Append(failed.Count == 0 ? "configured" : "partial").Append("\",");
            sb.Append("\"set_ok\":[");
            for (int i = 0; i < ok.Count; i++) { if (i > 0) sb.Append(','); sb.Append('"').Append(Escape(ok[i])).Append('"'); }
            sb.Append("],\"set_failed\":[");
            for (int i = 0; i < failed.Count; i++) { if (i > 0) sb.Append(','); sb.Append('"').Append(Escape(failed[i])).Append('"'); }
            sb.Append("]}");
            return sb.ToString();
        }
        catch (Exception ex)
        {
            return Err(ex.Message, ex.GetType().Name);
        }
    }

    // ── /tester/run — click Start via Win32, poll button text for completion ──

    private static string TesterRunSync(HttpListenerRequest req)
    {
        try
        {
            var ts = req.QueryString["timeout"];
            int timeoutSec = (ts != null && int.TryParse(ts, out var t)) ? t : 1800;

            var mt5 = FindMt5Hwnd();
            if (mt5 == IntPtr.Zero) return Err("MT5 main window not found");

            var startBtn = FindDescendantById(mt5, int.Parse(ID_START_BTN));
            if (startBtn == IntPtr.Zero) return Err("Start button (id=16790) not found");

            var t0 = DateTime.UtcNow;
            SendMessageInt(startBtn, BM_CLICK, IntPtr.Zero, IntPtr.Zero);

            // Auto-switch to tab (default "Graph") after Start click so the video shows equity curve.
            // Wait briefly for new tabs (Backtest/Graph) to appear after backtest kicks off.
            var focusTab = req.QueryString["focus_tab"] ?? "Graph";
            if (!string.IsNullOrEmpty(focusTab) && focusTab != "none")
            {
                for (int i = 0; i < 10; i++)
                {
                    Thread.Sleep(300);
                    if (ShowTesterTab(mt5, focusTab)) break;
                }
            }

            var deadline = DateTime.UtcNow.AddSeconds(timeoutSec);
            bool sawRunning = false;
            string lastSeen = "";

            while (DateTime.UtcNow < deadline)
            {
                Thread.Sleep(500);
                // Re-find in case the button HWND regenerated
                var btn = FindDescendantById(mt5, int.Parse(ID_START_BTN));
                if (btn == IntPtr.Zero) continue;
                var name = GetWindowTextWin32(btn);
                lastSeen = name;
                if (name == "Stop") sawRunning = true;
                else if (name == "Start" && sawRunning)
                {
                    var el = (DateTime.UtcNow - t0).TotalSeconds;
                    return "{\"status\":\"completed\",\"elapsed_seconds\":"
                         + el.ToString(System.Globalization.CultureInfo.InvariantCulture)
                         + "}";
                }
            }

            var elT = (DateTime.UtcNow - t0).TotalSeconds;
            return "{\"status\":\"timeout\",\"elapsed_seconds\":"
                 + elT.ToString(System.Globalization.CultureInfo.InvariantCulture)
                 + ",\"last_button_state\":\"" + Escape(lastSeen)
                 + "\",\"saw_running\":" + (sawRunning ? "true" : "false") + "}";
        }
        catch (Exception ex)
        {
            return Err(ex.Message, ex.GetType().Name);
        }
    }

    private static string EnqueueBacktest(HttpListenerRequest req)
    {
        var id = Guid.NewGuid().ToString("N")[..8];
        var job = new BacktestJob { Id = id, Created = DateTime.UtcNow };
        _jobs[id] = job;
        // TODO V0.2: kick off a Task that drives ST end-to-end and updates the job.
        return JsonSerializer.Serialize(new { job_id = id, status = "queued" });
    }

    private static string GetJob(string id)
    {
        if (!_jobs.TryGetValue(id, out var job))
            return JsonSerializer.Serialize(new { error = "job not found", id });
        return JsonSerializer.Serialize(new
        {
            id = job.Id,
            status = job.Status,
            results = job.Results,
            error = job.Error,
            created = job.Created.ToString("O"),
            completed = job.Completed?.ToString("O")
        });
    }

    private static string ListJobs()
    {
        var jobs = _jobs.Values.OrderByDescending(j => j.Created).Take(50).Select(j => new
        {
            id = j.Id,
            status = j.Status,
            created = j.Created.ToString("O"),
            completed = j.Completed?.ToString("O")
        });
        return JsonSerializer.Serialize(jobs);
    }

    private static string Escape(string s) => (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"");
}
