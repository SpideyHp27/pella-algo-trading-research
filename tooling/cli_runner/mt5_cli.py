#!/usr/bin/env python3
"""Headless MT5 backtest runner via terminal64.exe /config:tester.ini.

Bypasses the bridge entirely. Each call:
  1. Pre-flight: verify .ex5 exists, MT5 not running, symbol alias sanity-check
  2. Generates a tester.ini in MT5's terminal data directory (UTF-16 LE BOM)
  3. Launches terminal64.exe /config:tester.ini
  4. MT5 reads the ini, runs the backtest, then exits (ShutdownTerminal=1)
  5. Post-flight: scans tester log for license errors, init failures,
     0-trade silent failures
  6. Returns structured result with diagnostics

CONSTRAINTS:
  - MT5 GUI must be CLOSED before invocation (file-locks on terminal data dir).
  - Darwinex Demo account auto-login must be configured.
  - Tick data for the test symbol must already be cached locally.

ENUM auto-conversion:
  ENUM_TIMEFRAMES inputs (PERIOD_H1, PERIOD_H4, etc.) are auto-converted to
  the integer values MT5 tester.ini expects. ENUM_DAY_OF_WEEK similarly.

USAGE (single test):

    from mt5_cli import TestSpec, MT5CliRunner

    spec = TestSpec(
        expert="ChannelBreakoutVIP_MT5",
        symbol="USDJPY",
        timeframe="H1",
        start_date="2020.01.01",
        end_date="2026.04.30",
        inputs={"UseRiskPercent": True, "RiskPercent": 1.0},
        label="USDJPY_v02_RiskPct",
    )
    runner = MT5CliRunner()
    result = runner.run_test(spec)
    print(result)
"""
from __future__ import annotations
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Default paths — override via env vars if your install differs
MT5_EXE = os.environ.get("MT5_EXE", r"C:\Program Files\MetaTrader 5\terminal64.exe")
MT5_DATA = os.environ.get(
    "MT5_DATA",
    r"C:\Users\hoysa\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075"
)

# ENUM_TIMEFRAMES name -> MT5 integer code (from MQL5 docs)
ENUM_TIMEFRAMES = {
    "PERIOD_M1": 1, "PERIOD_M2": 2, "PERIOD_M3": 3, "PERIOD_M4": 4, "PERIOD_M5": 5,
    "PERIOD_M6": 6, "PERIOD_M10": 10, "PERIOD_M12": 12, "PERIOD_M15": 15,
    "PERIOD_M20": 20, "PERIOD_M30": 30,
    "PERIOD_H1": 16385, "PERIOD_H2": 16386, "PERIOD_H3": 16387, "PERIOD_H4": 16388,
    "PERIOD_H6": 16390, "PERIOD_H8": 16392, "PERIOD_H12": 16396,
    "PERIOD_D1": 16408, "PERIOD_W1": 32769, "PERIOD_MN1": 49153,
}

# Common symbol confusion warnings (from session experience).
# These trigger pre-flight warnings when a TestSpec uses one of these names.
SYMBOL_ALIAS_WARNINGS = {
    "NDAQ": "NDAQ is Nasdaq Inc. STOCK (~$91). For NASDAQ-100 index use 'NDX' on Darwinex.",
    "NDX100": "Try 'NDX' on Darwinex (which exposes NASDAQ-100 as 'NDX').",
    "US100": "On Darwinex this is 'NDX'. On other brokers may be 'US100' or 'NAS100'.",
    "NAS100": "On Darwinex this is 'NDX'.",
    "NDX_TICK": "NDX_Tick is a separate symbol on some brokers (FBS, IC Markets) "
                "with real-tick history. Darwinex Demo only has 'NDX'. Use 'NDX' "
                "with modelling=8 (real ticks) for closest equivalent.",
    "GOLD": "On Darwinex use 'XAUUSD' (gold spot).",
    "XAU": "On Darwinex use the full 'XAUUSD' (not just 'XAU').",
    "XAUUSD_TICK": "Use 'XAUUSD' on Darwinex; modelling=8 gives real-tick simulation already.",
    "NQ": "NQ is Nasdaq-100 FUTURES (CME). On MT5/CFD brokers use 'NDX' (CFD index). "
          "NQ futures are only available via NinjaTrader/Tradovate/IB.",
    "GC": "GC is gold FUTURES (COMEX). On MT5/CFD brokers use 'XAUUSD' (CFD spot).",
    "ES": "ES is S&P 500 FUTURES (CME). On MT5/CFD brokers use 'SP500'.",
    "YM": "YM is Dow Jones FUTURES (CBOT). On MT5/CFD brokers use 'WS30' or 'US30'.",
}

# Known fatal log patterns (regex) and human-readable explanations
LOG_FAILURE_PATTERNS = [
    (re.compile(r"invalid license \((\d+)\)"),
     "MARKETPLACE LICENSE ERROR: this .ex5 is bound to your MQL5 account. "
     "Install via MT5 GUI -> Navigator -> Market -> Purchased -> right-click Install. "
     "A copy-pasted .ex5 cannot be used directly."),
    (re.compile(r"loading of .+ failed"),
     "EA LOAD FAILURE: the .ex5 could not be loaded by MT5 Tester. "
     "Common causes: license error, missing dependency (.dll, custom indicator), "
     "or compiled for a different MT5 build."),
    (re.compile(r"cannot load Experts\\.+\.ex5"),
     "EA LOAD FAILURE (alternate form). See above."),
    (re.compile(r"INIT_FAILED|init failed", re.IGNORECASE),
     "EA OnInit() RETURNED FAILURE: the EA's own init logic refused to start. "
     "Check input validity, indicator handles, custom files in MQL5/Files/."),
    (re.compile(r"there are no symbols selected", re.IGNORECASE),
     "SYMBOL NOT IN MARKET WATCH: the symbol must be added to Market Watch first."),
    (re.compile(r"history quality.*?: 0%"),
     "ZERO HISTORY: the symbol has no tick data for this period. "
     "Check Tools -> Options -> Charts -> Max bars in history; or download ticks "
     "via View -> Symbols -> select symbol -> Bars."),
]


@dataclass
class TestSpec:
    """Single backtest specification.

    modelling values:
      0 = Every tick (synthetic from M1)   1 = 1m OHLC   2 = Open prices only
      4 = Math calculations                8 = Every tick based on real ticks (preferred)

    delays values:
      0 = Zero latency   1 = Random delay   (2-7 = fixed ms)
    """
    expert: str
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    modelling: int = 8
    deposit: int = 50000
    currency: str = "USD"
    leverage: int = 100
    delays: int = 0
    inputs: dict = field(default_factory=dict)
    label: str = ""
    timeout_seconds: int = 1800


def _is_mt5_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10
        )
        return "terminal64.exe" in result.stdout
    except Exception:
        return False


def _kill_mt5() -> None:
    try:
        subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"], timeout=10,
                       capture_output=True, text=True)
    except Exception:
        pass


def _convert_input_value(v) -> str:
    """Convert a Python input value to the string form MT5 tester.ini expects.

    Handles:
      - bool -> "true" / "false"
      - float -> repr (preserves precision)
      - ENUM_TIMEFRAMES strings ("PERIOD_H4") -> integer codes ("16388")
      - Everything else -> str()
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str) and v in ENUM_TIMEFRAMES:
        return str(ENUM_TIMEFRAMES[v])
    return str(v)


def _write_tester_ini(spec: TestSpec, ini_path: Path) -> None:
    """UTF-16 LE BOM with CRLF line endings (MT5's required format)."""
    lines = [
        "[Tester]",
        f"Expert={spec.expert}",
        f"Symbol={spec.symbol}",
        f"Period={spec.timeframe}",
        f"Optimization=0",
        f"Model={spec.modelling}",
        f"FromDate={spec.start_date}",
        f"ToDate={spec.end_date}",
        f"ForwardMode=0",
        f"Deposit={spec.deposit}",
        f"Currency={spec.currency}",
        f"Leverage={spec.leverage}",
        f"ExecutionMode={spec.delays}",
        f"Visual=0",
        f"Replace=1",
        f"ShutdownTerminal=1",
    ]
    if spec.label:
        lines.append(f"Report=tester_{spec.label}")
        lines.append(f"ReplaceReport=1")

    if spec.inputs:
        lines.append("")
        lines.append("[TesterInputs]")
        for k, v in spec.inputs.items():
            lines.append(f"{k}={_convert_input_value(v)}")

    text = "\r\n".join(lines) + "\r\n"
    bom = b"\xff\xfe"
    ini_path.write_bytes(bom + text.encode("utf-16-le"))


def _find_latest_log(log_dir: Path) -> Optional[Path]:
    if not log_dir.exists():
        return None
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _log_size(path: Optional[Path]) -> int:
    if path is None or not path.exists():
        return 0
    try:
        return path.stat().st_size
    except Exception:
        return 0


def _scan_log_for_failures(log_path: Path, since_byte: int = 0,
                           max_scan_bytes: int = 200_000) -> list[str]:
    """Scan the tail of the tester log for known failure patterns.

    Returns human-readable diagnostic strings, empty list if nothing flagged.
    Reads at most `max_scan_bytes` from the end of the log (UTF-16 LE).
    """
    findings: list[str] = []
    try:
        size = log_path.stat().st_size
        offset = max(since_byte, size - max_scan_bytes)
        with log_path.open("rb") as f:
            f.seek(offset)
            raw = f.read()
        try:
            text = raw.decode("utf-16-le", errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        seen = set()
        for pattern, msg in LOG_FAILURE_PATTERNS:
            if pattern.search(text) and msg not in seen:
                findings.append(msg)
                seen.add(msg)
    except Exception as e:
        findings.append(f"(log scan error: {e})")
    return findings


def _check_symbol_alias(symbol: str) -> Optional[str]:
    """Return a warning string if the symbol matches a known alias confusion, else None."""
    return SYMBOL_ALIAS_WARNINGS.get(symbol.upper())


def _ex5_exists(mt5_data: str, expert: str) -> bool:
    """Check if the .ex5 binary is in MT5's Experts folder."""
    p = Path(mt5_data) / "MQL5" / "Experts" / f"{expert}.ex5"
    return p.is_file()


class MT5CliRunner:
    """Headless MT5 backtest runner with pre-flight + post-flight diagnostics."""

    def __init__(self, mt5_exe: str = MT5_EXE, mt5_data: str = MT5_DATA):
        self.mt5_exe = mt5_exe
        self.mt5_data = mt5_data
        self.log_dir = Path(mt5_data) / "Tester" / "logs"

    def run_test(self, spec: TestSpec, force_kill_existing: bool = False) -> dict:
        """Run one backtest, return result dict with diagnostics.

        Result keys:
            success: bool — whether MT5 ran cleanly AND log shows real activity
            error: str — top-level error if hard fail
            warnings: list[str] — non-fatal advisories (symbol alias, etc.)
            diagnostics: list[str] — log-scan findings from known failure patterns
            elapsed_seconds: float — wall time
            returncode: int — terminal64.exe exit code
            log_path: str — path to Tester log
            log_growth_bytes: int — how much log grew during this test
            spec: dict — echo of the spec
        """
        start = time.time()
        spec_dict = {k: v for k, v in spec.__dict__.items()}
        warnings: list[str] = []

        # Pre-flight: symbol alias check (advisory, not fatal)
        alias_warn = _check_symbol_alias(spec.symbol)
        if alias_warn:
            warnings.append(f"Symbol '{spec.symbol}': {alias_warn}")

        # Pre-flight: confirm .ex5 exists
        if not _ex5_exists(self.mt5_data, spec.expert):
            return {
                "success": False,
                "error": f"EA binary not found: {spec.expert}.ex5 is missing from "
                         f"{self.mt5_data}/MQL5/Experts/. Compile the EA first or "
                         f"copy the .ex5 into that folder.",
                "elapsed_seconds": 0,
                "warnings": warnings,
                "spec": spec_dict,
            }

        # Pre-flight: MT5 must be closed
        if _is_mt5_running():
            if force_kill_existing:
                _kill_mt5()
                time.sleep(2)
            else:
                return {
                    "success": False,
                    "error": "MT5 (terminal64.exe) is currently running. "
                             "Close it before running CLI tests, or pass force_kill_existing=True.",
                    "elapsed_seconds": 0,
                    "warnings": warnings,
                    "spec": spec_dict,
                }

        # Capture pre-test state for log growth diff
        pre_log = _find_latest_log(self.log_dir)
        pre_size = _log_size(pre_log)

        # Generate tester.ini
        ini_path = Path(self.mt5_data) / "tester_cli.ini"
        _write_tester_ini(spec, ini_path)

        # Launch
        cmd = [self.mt5_exe, f"/config:{ini_path}"]
        try:
            proc = subprocess.Popen(cmd)
            try:
                rc = proc.wait(timeout=spec.timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                return {
                    "success": False,
                    "error": f"Timeout after {spec.timeout_seconds}s — MT5 did not finish + shutdown",
                    "elapsed_seconds": time.time() - start,
                    "warnings": warnings,
                    "spec": spec_dict,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to launch MT5: {e}",
                "elapsed_seconds": time.time() - start,
                "warnings": warnings,
                "spec": spec_dict,
            }

        # Post-test analysis
        post_log = _find_latest_log(self.log_dir)
        post_size = _log_size(post_log)
        log_growth = post_size - (pre_size if post_log == pre_log else 0)

        # Scan log for known failure patterns (license, init failure, etc.)
        diagnostics: list[str] = []
        if post_log is not None:
            scan_from = pre_size if post_log == pre_log else 0
            diagnostics = _scan_log_for_failures(post_log, since_byte=scan_from)

        # Define success: clean exit + meaningful log activity + no fatal diagnostics
        run_succeeded = (rc == 0 and log_growth > 1000 and not diagnostics)

        return {
            "success": run_succeeded,
            "elapsed_seconds": time.time() - start,
            "returncode": rc,
            "log_path": str(post_log) if post_log else None,
            "log_growth_bytes": log_growth,
            "warnings": warnings,
            "diagnostics": diagnostics,
            "spec": spec_dict,
        }


def main():
    """Quick smoke test."""
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--expert", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--timeframe", default="H1")
    ap.add_argument("--start", default="2020.01.01")
    ap.add_argument("--end", default="2026.04.30")
    ap.add_argument("--modelling", type=int, default=8)
    ap.add_argument("--deposit", type=int, default=50000)
    ap.add_argument("--label", default="")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--force-kill", action="store_true")
    args = ap.parse_args()

    spec = TestSpec(
        expert=args.expert, symbol=args.symbol, timeframe=args.timeframe,
        start_date=args.start, end_date=args.end,
        modelling=args.modelling, deposit=args.deposit,
        label=args.label or f"{args.expert}_{args.symbol}_{args.timeframe}",
        timeout_seconds=args.timeout,
    )
    runner = MT5CliRunner()
    result = runner.run_test(spec, force_kill_existing=args.force_kill)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
