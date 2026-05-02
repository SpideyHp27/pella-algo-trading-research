#!/usr/bin/env python3
"""Robust MT5 compile wrapper.

MetaEditor's CLI /compile silently fails when the source path contains spaces.
This wrapper:
  1. If the source filename has spaces, copies it to a no-space alias first
  2. Compiles the alias via metaeditor64.exe /compile
  3. Copies the resulting .ex5 BACK to the original space-name so iCustom()
     and other lookups by name still work
  4. Reads the compile log and surfaces errors/warnings/result

USAGE:
    python tools/mt5_compile.py "path/to/My Strategy.mq5"
    python tools/mt5_compile.py --indicator "path/to/Daily VWAP.mq5"

Returns exit 0 if compile succeeded with 0 errors, exit 1 otherwise.
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

METAEDITOR = os.environ.get(
    "METAEDITOR64",
    r"C:\Program Files\MetaTrader 5\metaeditor64.exe"
)


def compile_mq5(source_path: Path, log_path: Path | None = None,
                verbose: bool = True) -> tuple[int, int, int]:
    """Compile an .mq5 file via MetaEditor CLI. Returns (errors, warnings, exit_code).

    Handles the space-in-filename bug by transparently aliasing.
    """
    if not source_path.is_file():
        raise FileNotFoundError(f"Source not found: {source_path}")

    has_space = " " in source_path.name

    # Default log path next to source, fallback to a temp location
    if log_path is None:
        log_path = source_path.parent / f"{source_path.stem}.compile.log"

    if has_space:
        # Compile under a no-space alias to dodge MetaEditor's bug
        alias = source_path.parent / source_path.name.replace(" ", "_")
        if verbose:
            print(f"  Source has spaces -- compiling under alias: {alias.name}")
        shutil.copy2(source_path, alias)
        compile_target = alias
    else:
        compile_target = source_path

    cmd = [METAEDITOR,
           f"/compile:{compile_target}",
           f"/log:{log_path}"]
    if verbose:
        print(f"  Running: metaeditor64 /compile:{compile_target.name}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        if verbose: print("  Compile timed out after 120s")
        return (1, 0, -1)

    # Read log (UTF-16 LE)
    errors = 0
    warnings = 0
    log_text = ""
    if log_path.is_file():
        try:
            raw = log_path.read_bytes()
            log_text = raw.decode("utf-16-le", errors="replace")
            # Parse the "Result: X errors, Y warnings" line
            import re
            m = re.search(r"Result:\s*(\d+)\s*errors?,\s*(\d+)\s*warnings?", log_text)
            if m:
                errors = int(m.group(1))
                warnings = int(m.group(2))
        except Exception as e:
            if verbose: print(f"  (could not parse log: {e})")

    if has_space:
        # Move the alias .ex5 back to the original name so iCustom() finds it
        alias_ex5 = compile_target.with_suffix(".ex5")
        original_ex5 = source_path.with_suffix(".ex5")
        if alias_ex5.is_file():
            shutil.copy2(alias_ex5, original_ex5)
            if verbose:
                print(f"  Copied {alias_ex5.name} -> {original_ex5.name}")
        # Clean up alias .mq5 (keep alias .ex5 too for now — harmless)
        try:
            alias.unlink()
        except Exception:
            pass

    if verbose:
        print(f"  Result: {errors} errors, {warnings} warnings (exit={rc})")
        if errors > 0:
            # Surface the error lines from the log
            for line in log_text.splitlines():
                if "error" in line.lower() and "0 errors" not in line:
                    print(f"    {line.strip()}")

    return (errors, warnings, rc)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="Path to .mq5 file (Expert or Indicator)")
    ap.add_argument("--log", default=None, help="Custom log path (default: next to source)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    source = Path(args.source)
    log_path = Path(args.log) if args.log else None

    errors, warnings, rc = compile_mq5(source, log_path, verbose=not args.quiet)

    if errors > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
