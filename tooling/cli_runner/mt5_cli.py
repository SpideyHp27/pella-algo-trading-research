#!/usr/bin/env python3
"""Headless MT5 backtest runner via terminal64.exe /config:tester.ini.

Bypasses the bridge entirely. Each call:
  1. Generates a tester.ini in MT5's terminal data directory (UTF-16 LE BOM)
  2. Launches terminal64.exe /config:tester.ini
  3. MT5 reads the ini, runs the backtest, then exits (ShutdownTerminal=1)
  4. Parses the resulting Tester log via existing mt5_tester_report.py
  5. Returns structured metrics

CONSTRAINTS:
  - MT5 GUI must be CLOSED before invocation (file-locks on terminal data dir).
    Each CLI call cold-launches MT5; the GUI cannot run simultaneously.
  - Darwinex Demo account auto-login must be configured (no credential prompts).
  - Tick data for the test symbol must already be cached locally.

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

USAGE (batch — see run_validation.py for full orchestrator).
"""
from __future__ import annotations
import os
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


@dataclass
class TestSpec:
    """Single backtest specification.

    modelling values (per MT5 docs):
      0 = Every tick (synthetic ticks from M1)
      1 = 1 minute OHLC
      2 = Open prices only
      4 = Math calculations (no price data)
      8 = Every tick based on real ticks (preferred for our work)

    delays values (Tester GUI label → enum):
      0 = Zero latency, ideal execution
      1 = Random delay
      (others: 2-7 are fixed-ms delays)
    """
    expert: str                          # EA name without .ex5 (e.g. "ChannelBreakoutVIP_MT5")
    symbol: str                          # "USDJPY", "XAUUSD"
    timeframe: str                       # "M1", "M5", "M15", "M30", "H1", "H4", "D1"
    start_date: str                      # "2020.01.01" (YYYY.MM.DD)
    end_date: str                        # "2026.04.30"
    modelling: int = 8                   # 8 = every tick real
    deposit: int = 50000
    currency: str = "USD"
    leverage: int = 100
    delays: int = 0                      # 0 = zero latency
    inputs: dict = field(default_factory=dict)  # EA input overrides
    label: str = ""                      # used for report filename
    timeout_seconds: int = 1800          # 30 min default per test


def _is_mt5_running() -> bool:
    """Check if terminal64.exe is currently running on Windows."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq terminal64.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10
        )
        return "terminal64.exe" in result.stdout
    except Exception:
        return False


def _kill_mt5() -> None:
    """Force-terminate any running terminal64.exe (use carefully)."""
    try:
        subprocess.run(["taskkill", "/F", "/IM", "terminal64.exe"], timeout=10,
                       capture_output=True, text=True)
    except Exception:
        pass


def _write_tester_ini(spec: TestSpec, ini_path: Path) -> None:
    """Generate tester.ini in UTF-16 LE BOM with CRLF line endings (MT5's required format)."""
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
            if isinstance(v, bool):
                v_str = "true" if v else "false"
            elif isinstance(v, float):
                v_str = repr(v)
            else:
                v_str = str(v)
            lines.append(f"{k}={v_str}")

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


class MT5CliRunner:
    """Headless MT5 backtest runner."""

    def __init__(self, mt5_exe: str = MT5_EXE, mt5_data: str = MT5_DATA):
        self.mt5_exe = mt5_exe
        self.mt5_data = mt5_data
        self.log_dir = Path(mt5_data) / "Tester" / "logs"

    def run_test(self, spec: TestSpec, force_kill_existing: bool = False) -> dict:
        """Run one backtest, return result dict.

        Result keys:
            success: bool — whether MT5 exited cleanly within timeout
            error: str — error message if success=False
            elapsed_seconds: float — wall time
            log_path: str — path to Tester log (use mt5_tester_report.py to parse)
            log_growth_bytes: int — how much log grew during this test (sanity check)
            spec: dict — echo of the spec
        """
        start = time.time()
        spec_dict = {k: v for k, v in spec.__dict__.items()}

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
                    "spec": spec_dict,
                }

        # Pre-test log size for growth check
        pre_log = _find_latest_log(self.log_dir)
        pre_size = _log_size(pre_log)

        ini_path = Path(self.mt5_data) / "tester_cli.ini"
        _write_tester_ini(spec, ini_path)

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
                    "spec": spec_dict,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to launch MT5: {e}",
                "elapsed_seconds": time.time() - start,
                "spec": spec_dict,
            }

        # Post-test log analysis
        post_log = _find_latest_log(self.log_dir)
        post_size = _log_size(post_log)
        log_growth = post_size - (pre_size if post_log == pre_log else 0)

        return {
            "success": rc == 0 and log_growth > 0,
            "elapsed_seconds": time.time() - start,
            "returncode": rc,
            "log_path": str(post_log) if post_log else None,
            "log_growth_bytes": log_growth,
            "spec": spec_dict,
        }


def main():
    """Quick smoke test — runs one backtest and prints the result.

    Usage:  uv run python tools/mt5_cli.py --expert ChannelBreakoutVIP_MT5 --symbol USDJPY \
                 --start 2020.01.01 --end 2026.04.30
    """
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
    ap.add_argument("--force-kill", action="store_true",
                    help="Kill any running MT5 before launching")
    args = ap.parse_args()

    spec = TestSpec(
        expert=args.expert,
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_date=args.start,
        end_date=args.end,
        modelling=args.modelling,
        deposit=args.deposit,
        label=args.label or f"{args.expert}_{args.symbol}_{args.timeframe}",
        timeout_seconds=args.timeout,
    )

    runner = MT5CliRunner()
    result = runner.run_test(spec, force_kill_existing=args.force_kill)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
