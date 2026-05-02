# MT5 Bridge — Handoff Brief for Bridge Dev Collaboration

**Audience:** another developer (and their AI assistant) picking up MT5 bridge work, specifically the **sticky Expert dropdown bug** + related issues.

**Prerequisite:** read this end-to-end before touching code. Most context is in here.

---

## 1. What this bridge is

An HTTP-driven automation layer that lets external programs (Claude Code, Python scripts, etc.) drive **MetaTrader 5's GUI** without human clicks. Specifically the Strategy Tester for backtesting.

**Architecture:**

```
External program (Claude Code / Python / curl)
        │
        │ HTTP (port 8891)
        ▼
MT5Bridge.dll (NativeAOT C# shared library)
        │
        │ Win32 SendMessage + UIAutomation
        ▼
MetaTrader 5 (terminal64.exe — user-mode GUI app)
```

**Loading sequence:**
1. `BridgeEA.mq5` (MQL5 expert advisor) is dragged onto a chart inside MT5.
2. BridgeEA loads `MT5Bridge.dll` via the MQL5 `#import` directive.
3. The DLL spawns an `HttpListener` on `http://localhost:8891/` and a dedicated **STA thread** for UIAutomation calls.
4. External programs POST JSON to bridge endpoints, e.g.:

```bash
curl -X POST -d '{"expert":"MyEA.ex5","symbol":"XAUUSD","timeframe":"H1"}' \
     http://localhost:8891/tester/configure

curl -X POST -d '{}' http://localhost:8891/tester/run
```

**Key technical constraints:**
- Strategy Tester window must be open (Ctrl+R inside MT5)
- AutoTrading toggle must be ON
- DLL imports must be allowed (Tools → Options → Expert Advisors → Allow DLL imports)
- **Optimization dropdown must be set to "Disabled"** — see "Other Issues" below for why this matters

---

## 2. The sticky-dropdown bug (the main thing we're fixing)

### Symptom

External program calls `POST /tester/configure` with `"expert":"NewEA.ex5"`. Bridge reports `set_ok=true` for the expert field. **But MT5 silently keeps the previously selected EA.** Subsequent `POST /tester/run` then runs the wrong EA.

In our case it caused us to run `AsiaRangeBreakout.ex5` three times in a row when we asked for `ChannelBreakoutVIP_MT5.ex5`. Confirmed by checking the Tester log — the cache filename clearly showed the wrong expert name.

### Root cause (Win32 internals)

The bridge's combo-setter function uses standard Win32 messages:

```csharp
SendMessage(combo_hwnd, CB_FINDSTRING, -1, value);    // find item
SendMessage(combo_hwnd, CB_SETCURSEL, idx, 0);        // select it
```

This **DOES** change the combobox's visual selection. The dropdown's display text updates to the new EA name. So on screen and in `WM_GETTEXT` queries, everything looks correct.

**But Win32 treats programmatic `CB_SETCURSEL` as "synthetic"** — i.e., not user-initiated. Because of this, the combobox does NOT fire the `CBN_SELCHANGE` notification to its parent dialog.

The MT5 Strategy Tester only loads/refreshes the new EA when it receives `WM_COMMAND` with `CBN_SELCHANGE` in the high word. Without that notification, the visible dropdown text changes but the underlying expert stays the same. Hence the sticky behavior.

This is documented Win32 behavior, not an MT5 quirk:
- Microsoft docs on `CB_SETCURSEL`: *"This message does not generate the CBN_SELCHANGE notification."*

### The fix (v0.44 patch)

After `CB_SETCURSEL` succeeds, manually send `WM_COMMAND` with `CBN_SELCHANGE` to the combo's parent window:

```csharp
private static bool SetComboValueWin32(IntPtr root, int id, string value)
{
    var h = FindDescendantById(root, id);
    if (h == IntPtr.Zero) return false;

    // Try exact match first (avoids picking the wrong item when names share
    // a prefix, e.g. "Donchian" vs "DonchianChannelEA"), then prefix match.
    var idx = SendMessageStr(h, CB_FINDSTRINGEXACT, new IntPtr(-1), value).ToInt64();
    if (idx == CB_ERR)
        idx = SendMessageStr(h, CB_FINDSTRING, new IntPtr(-1), value).ToInt64();

    if (idx != CB_ERR)
    {
        SendMessageInt(h, CB_SETCURSEL, new IntPtr((int)idx), IntPtr.Zero);

        // CRITICAL: CB_SETCURSEL alone does NOT fire CBN_SELCHANGE.
        // The parent dialog needs the WM_COMMAND notification to actually
        // act on the selection (load new EA, refresh dependent fields, etc).
        //
        // wParam encoding: HIWORD = notification code, LOWORD = control id
        // lParam: handle to the control sending the notification
        var parent = GetParent(h);
        if (parent != IntPtr.Zero)
        {
            int ctrlId = GetDlgCtrlID(h);
            long wParam = ((long)CBN_SELCHANGE << 16) | ((uint)ctrlId & 0xFFFFu);
            SendMessageInt(parent, WM_COMMAND, new IntPtr(wParam), h);
        }
        return true;
    }
    // Editable: set text directly (fallback for non-list combos)
    SendMessageStr(h, WM_SETTEXT, IntPtr.Zero, value);
    return true;
}
```

**Required new constants:**

```csharp
private const uint CB_FINDSTRINGEXACT  = 0x0158;
private const uint WM_COMMAND          = 0x0111;
private const int  CBN_SELCHANGE       = 1;
```

### Note on `CB_FINDSTRING` vs `CB_FINDSTRINGEXACT`

`CB_FINDSTRING` does prefix matching, which can match the wrong item when EAs have similar prefixes (e.g. `Donchian` matches `DonchianChannelEA`). `CB_FINDSTRINGEXACT` does exact matching. We try EXACT first, fall back to prefix if exact fails. This was a secondary issue surfaced during the fix.

### Same pattern applies to other combos

This bug affects **every combobox the bridge sets** — Symbol, Timeframe, Modelling, Currency, Leverage, etc. The fix is universal because the helper is shared.

### Verification

After applying the fix:

```bash
# Build the DLL
cd path/to/bridge
dotnet publish -c Release  # NativeAOT publish

# Replace the DLL in MT5 Libraries folder
# (MT5 must be CLOSED first — DLL stays loaded in terminal64.exe
# until process exit, even after BridgeEA is removed from chart)
cp bin/Release/net8.0-windows/win-x64/publish/MT5Bridge.dll \
   ~/AppData/Roaming/MetaQuotes/Terminal/<TERMINAL_ID>/MQL5/Libraries/

# Reopen MT5, reattach BridgeEA, verify version bump
curl http://localhost:8891/version
# Should report new version + "combo-cbn-selchange-fix" in features
```

Functional test: open Strategy Tester with EA "A" selected, then via bridge configure to EA "B". Confirm that `/tester/run` actually executes EA "B" by checking the Tester log's `expert file added: ...` line.

---

## 3. Other related issues we hit (worth fixing while you're in there)

### 3a. Optimization-mode disk bomb

**Symptom:** Strategy Tester defaults to `Optimization = "Slow complete algorithm"` after certain operations. If the user (or bridge) clicks Start without verifying, the tester fans out across **every symbol in Market Watch**, downloading fresh tick data per symbol. We had two near-disk-fills (down to 399 MB) inside 24 hours from this.

**Bridge fix proposal:** add an `optimization` parameter to `/tester/configure`:

```csharp
// Pseudocode addition
case "optimization":
    SetComboValueWin32(testerRoot, ID_OPTIMIZATION, "Disabled");
    break;
```

The combo control ID for the Optimization dropdown needs to be discovered (probably similar discovery process to how `ID_EXPERT` was found — see `WPF_TREE.md` if you have it, or use Inspect.exe / WinSpy).

**Workaround until fixed:** never call `/tester/run` without first having a human verify in the MT5 UI that Optimization is set to Disabled. Or set it programmatically in a separate `/tester/set_optimization_disabled` endpoint.

### 3b. `mt5_compile` MCP tool broken

The MCP tool `mt5_compile` errors with: `metaeditor invocation failed: name 'DARWINEX_ME_PATH' is not defined`.

This is an undefined Python variable in the bridge's MCP server (or its Python wrapper, depending on architecture). Root cause unknown without reading that code, but the workaround is direct CLI:

```bash
"C:/Program Files/MetaTrader 5/metaeditor64.exe" \
  /compile:"path/to/MyEA.mq5" /log:"path/to/MyEA.compile.log"
# Logs are UTF-16LE encoded
```

If you fix `mt5_compile` directly in the bridge, make it auto-detect the metaeditor path (fall back to `C:\Program Files\MetaTrader 5\metaeditor64.exe` if env var unset).

### 3c. `dlls_allowed: false` reported even when DLLs work

The `mt5_terminal` health endpoint reports `dlls_allowed: false` because that field reads the **global** Tools → Options → Expert Advisors → Allow DLL imports setting. The bridge actually loads through the **per-EA** "Allow DLL imports" checkbox (the popup when dragging EA to chart). These are different settings.

In recent MT5 builds (5800+), the per-EA setting can override the global one, so the bridge can work with `dlls_allowed: false` reported. **This isn't a bug**, but it's confusing in the health dashboard. Worth adding a comment or renaming the field to `dlls_allowed_global` for clarity.

---

## 4. Source code

The full v0.44 source is in `tooling/mt5_bridge/MT5Bridge_v0.44.cs` (and the `.csproj` next to it) in this repo. Drop into your IDE and the patched function is `SetComboValueWin32` around line 542.

Build:
```bash
dotnet publish -c Release
# Output: bin/Release/net8.0-windows/win-x64/publish/MT5Bridge.dll
# Note: NativeAOT requires Visual Studio Build Tools or vswhere.exe on PATH
```

If `vswhere.exe` is missing (typical first-time NativeAOT setup), add to PATH:
```bash
export PATH="/c/Program Files (x86)/Microsoft Visual Studio/Installer:$PATH"
```

---

## 5. What's already working in the bridge (don't regress)

These work as-is in v0.43.2 and don't need changes:

- HTTP listener on port 8891
- STA worker thread for UIAutomation calls
- All read endpoints: `/version`, `/account`, `/symbols`, `/symbol`, `/rates`, `/tick`, `/positions`, `/orders`, `/deals`, `/history`, `/list_strategies`, `/read_strategy`, `/write_strategy`, `/portfolio_exposure`
- Tester endpoints: `/tester/configure`, `/tester/run`, `/tester/go`, `/tester/show_tab`
- The version-bump-on-build mechanism via `BUILD_TIMESTAMP` (regenerates per build)
- The Win32 tab-switch fix (`ShowTesterTab` already does the WM_NOTIFY/TCN_SELCHANGE pattern correctly — that's the model the dropdown fix follows)

Don't break these. The combobox fix is purely additive to `SetComboValueWin32`.

---

## 6. Test plan after applying the fix

1. **Rebuild the DLL** as documented above.
2. **Close MT5 fully** (File → Exit; verify no `terminal64.exe` in Task Manager).
3. **Replace the DLL** in `<TERMINAL>/MQL5/Libraries/MT5Bridge.dll`.
4. **Reopen MT5**, reattach BridgeEA, AutoTrading on, verify smiley-face top-right.
5. **`curl http://localhost:8891/version`** → should report v0.44 + features include `combo-cbn-selchange-fix`.
6. **Functional test:**
   - In MT5: open Strategy Tester with EA "FooEA" selected
   - From terminal: `curl -X POST -d '{"expert":"BarEA.ex5"}' http://localhost:8891/tester/configure`
   - In MT5: visually verify the Expert dropdown now shows BarEA
   - Run `/tester/run` and check Tester log → cache file should reference BarEA, NOT FooEA
7. **Regression test:** all the other endpoints still work (run a full backtest end-to-end).

---

## 7. Quick reference

| File | Purpose |
|---|---|
| `MT5Bridge.cs` | The whole bridge in one file (~890 lines). Patched function at `SetComboValueWin32` |
| `MT5Bridge.csproj` | NativeAOT project config |
| `BridgeEA.mq5` | MQL5 EA that loads the DLL, runs inside MT5 (not in this handoff package, lives in MQL5\Experts\) |

---

## 8. If you hit problems

- **Build fails with `vswhere.exe not found`** — add VS Build Tools installer to PATH (see Section 4).
- **Build fails with `link.exe error`** — VS Build Tools either not installed or missing C++ workload. Install "Desktop development with C++" workload.
- **DLL won't replace ("file in use")** — MT5 still running. Close fully, kill `terminal64.exe` if needed.
- **Bridge starts but `/version` returns connection refused** — port 8891 reserved by Windows HTTP.sys. Run `net stop http /y && net start http` in admin shell.
- **Bridge starts but combo selection still sticky after fix** — verify the build actually deployed by checking the version string. If it's still v0.43.2, the swap didn't happen.

---

## Final note

The bridge architecture is solid. v0.43.2 already had the right pattern (sending `WM_NOTIFY/TCN_SELCHANGE` to the parent for tab controls — see `ShowTesterTab`). The combobox fix just applies the same notification pattern. So the patch should feel natural, not invasive.

Good luck. Ping back if you hit something not covered here.
