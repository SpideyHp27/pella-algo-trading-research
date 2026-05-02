#!/usr/bin/env python3
"""Walk-forward analysis — Pipeline v1.3 Gate 5.

For each TestSpec, generates a sequence of OOS (out-of-sample) windows
that slide forward in time. Each window is a separate backtest. The
purpose is to confirm the strategy edge is stable across DIFFERENT chunks
of history, not just on the full window where it was first measured.

Default window plan (over 2020-01 .. 2026-04):
  Window 1 OOS: 2023-01-01 to 2024-01-01
  Window 2 OOS: 2024-01-01 to 2025-01-01
  Window 3 OOS: 2025-01-01 to 2026-01-01
  Window 4 OOS: 2026-01-01 to 2026-04-30

(IS phase is implicit: the strategy was already validated on the full
2020-2026 window. Walk-forward here is OOS-only sliding to detect regime
breakdown over time. For full IS/OOS optimisation we'd need a parameter
optimiser — out of scope for this bridgeless pipeline.)

USAGE:
    uv run python tools/walk_forward.py --custom NT8Bridge/tools/specs_<...>.json

Each input spec gets expanded into N window-specific specs. The orchestrator
runs them all via mt5_cli, parses, computes per-window metrics, and emits a
walk-forward stability report.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mt5_cli import TestSpec, MT5CliRunner
import mt5_tester_report as mtr
from quant_report import compute_report


RESULTS_DIR = Path(__file__).parent.parent / "Results" / "walk_forward"


# Default OOS-sliding windows (year-aligned, MT5 date format YYYY.MM.DD)
DEFAULT_WINDOWS = [
    ("2023.01.01", "2024.01.01", "OOS_2023"),
    ("2024.01.01", "2025.01.01", "OOS_2024"),
    ("2025.01.01", "2026.01.01", "OOS_2025"),
    ("2026.01.01", "2026.04.30", "OOS_2026Q1"),
]


def expand_spec(base: TestSpec, windows) -> list[TestSpec]:
    """Generate one TestSpec per window from a base spec."""
    out = []
    for start, end, label_suffix in windows:
        d = asdict(base)
        d["start_date"] = start
        d["end_date"] = end
        d["label"] = f"{base.label}_{label_suffix}"
        out.append(TestSpec(**d))
    return out


def parse_log_to_csv(log_path: Path, expert_filter: str, csv_out: Path) -> dict:
    text = mtr.read_utf16(str(log_path))
    segments = list(mtr.parse_test_segments(text))
    if not segments:
        return {"error": f"No tests in {log_path}"}
    parsed = [mtr.parse_segment(s, e, m, lines) for s, e, m, lines in segments]
    matching = [p for p in parsed if p["expert"].startswith(expert_filter)]
    if not matching:
        return {"error": f"No tests of '{expert_filter}'"}
    p = matching[-1]
    trades = mtr.pair_trades(p["deals"])
    import csv
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    with csv_out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "side", "profit", "volume",
                    "open_price", "close_price", "price_diff", "open_time"])
        for t in trades:
            w.writerow([t["close_time"], t["symbol"], t["side"],
                        t["profit"], t["volume"],
                        t["open_price"], t["close_price"],
                        t["price_diff"], t["open_time"]])
    return {"trade_count": len(trades), "final_balance": p["final_balance"]}


def run_one(spec: TestSpec, runner: MT5CliRunner, out_dir: Path) -> dict:
    print(f"  -> {spec.label} {spec.symbol} {spec.timeframe} "
          f"{spec.start_date}->{spec.end_date}", flush=True)
    cli = runner.run_test(spec)
    if not cli["success"]:
        return {"spec": asdict(spec), "error": cli.get("error")}
    log_path = Path(cli["log_path"])
    csv_path = out_dir / f"trades_{spec.label}.csv"
    parse = parse_log_to_csv(log_path, spec.expert, csv_path)
    if "error" in parse:
        return {"spec": asdict(spec), "error": parse["error"]}
    report = compute_report(csv_path, starting_equity=spec.deposit)
    return {"spec": asdict(spec),
            "trades": parse["trade_count"],
            "final_balance": parse["final_balance"],
            "metrics": report}


def stability_report(results_by_strategy: dict) -> str:
    """Markdown summary: per-strategy OOS Sharpe / PF / DD across windows."""
    lines = ["## Walk-Forward Stability — per-strategy view\n"]
    for strat_label, window_results in results_by_strategy.items():
        lines.append(f"### {strat_label}\n")
        lines.append("| Window | Trades | PF | Sharpe | MaxDD% | Final$ |")
        lines.append("|---|---|---|---|---|---|")
        sharpes = []
        for w in window_results:
            if "error" in w:
                lines.append(f"| {w['spec']['label'].split('_')[-1]} | ERROR | — | — | — | — |")
                continue
            m = w["metrics"]
            if "error" in m:
                lines.append(f"| {w['spec']['label'].split('_')[-1]} | 0 | — | — | — | — |")
                continue
            label_short = w["spec"]["label"].split("_")[-1]
            sharpes.append(m.get("sharpe_annual", 0) or 0)
            lines.append(
                f"| {label_short} | {m.get('trades')} | {m.get('profit_factor')} | "
                f"{m.get('sharpe_annual')} | {m.get('max_dd_pct')} | "
                f"${w['final_balance']} |"
            )
        if sharpes:
            n = len(sharpes)
            mean_s = sum(sharpes) / n
            std_s = (sum((s - mean_s)**2 for s in sharpes) / max(1, n-1))**0.5
            sub_gate = sum(1 for s in sharpes if s < 1.0)
            lines.append("")
            lines.append(f"**Stability:** mean Sharpe {mean_s:.2f} | std {std_s:.2f} "
                         f"| {n - sub_gate}/{n} windows pass 1.0 Sharpe gate")
            # Verdict considers TREND not just pass-rate. A strategy with bad early
            # years and strong recent ones is IMPROVING, not degrading.
            recent_half = sharpes[len(sharpes)//2:]
            early_half = sharpes[:len(sharpes)//2]
            recent_mean = sum(recent_half)/len(recent_half) if recent_half else 0
            early_mean = sum(early_half)/len(early_half) if early_half else 0
            most_recent = sharpes[-1]
            if most_recent < -1.0:
                verdict = "RED FLAG — most recent window has Sharpe < -1 (regime collapse?)"
            elif sub_gate == 0:
                verdict = "STABLE — every window passes 1.0 Sharpe gate"
            elif sub_gate <= n // 4:
                verdict = "STABLE — most windows pass"
            elif recent_mean > early_mean and most_recent >= 1.0:
                verdict = "IMPROVING — early misses, recent windows pass"
            elif recent_mean < early_mean and most_recent < 1.0:
                verdict = "DEGRADING — recent windows miss the gate"
            else:
                verdict = "MIXED — no clear trend, manual review needed"
            lines.append(f"**Verdict:** {verdict}")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--custom", required=True,
                    help="JSON file with list of base TestSpec dicts")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    args = ap.parse_args()

    out_dir = Path(args.out) / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    base_specs = [TestSpec(**d) for d in json.loads(Path(args.custom).read_text(encoding="utf-8"))]

    print(f"Output dir: {out_dir}")
    print(f"Base specs: {len(base_specs)}, windows per spec: {len(DEFAULT_WINDOWS)}")
    print(f"Total backtests: {len(base_specs) * len(DEFAULT_WINDOWS)}")
    print()

    runner = MT5CliRunner()
    t0 = time.time()
    results_by_strategy = {}

    for base in base_specs:
        print(f"=== {base.label} ===")
        windowed = expand_spec(base, DEFAULT_WINDOWS)
        results_by_strategy[base.label] = []
        for spec in windowed:
            r = run_one(spec, runner, out_dir)
            results_by_strategy[base.label].append(r)
            # Persist after every run
            (out_dir / "results.json").write_text(
                json.dumps(results_by_strategy, indent=2, default=str),
                encoding="utf-8",
            )
            (out_dir / "stability.md").write_text(
                f"# Walk-Forward Stability Report — {time.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"Elapsed: {(time.time()-t0)/60:.1f} min\n\n"
                + stability_report(results_by_strategy),
                encoding="utf-8",
            )

    print(f"\nDONE in {(time.time()-t0)/60:.1f} min")
    print(f"Stability report: {out_dir / 'stability.md'}")
    print()
    print(stability_report(results_by_strategy))


if __name__ == "__main__":
    main()
