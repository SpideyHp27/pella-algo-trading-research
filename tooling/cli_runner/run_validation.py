#!/usr/bin/env python3
"""Headless backtest orchestrator — bridge-free.

Pipeline per TestSpec:
  1. MT5CliRunner.run_test()        — fires terminal64.exe /config:tester.ini
  2. mt5_tester_report.parse_*      — parses the freshly-written Tester log
  3. write trades CSV               — same shape sharpe / monte_carlo / quant_report consume
  4. quant_report.compute_report    — full Aristhrottle-style metric block
  5. aggregate                      — JSON scoreboard + markdown table

Why this exists:
  The MT5 bridge has been unreliable today (sticky dropdown, STA crashes, port
  drops). Running tests via terminal64.exe in /config:headless mode avoids the
  bridge entirely. Trade-off: ~30s cold-start per test vs the bridge's ~1s.

CONSTRAINT: MT5 GUI must be CLOSED for the entire run (each test cold-launches
a fresh MT5 instance — file lock conflicts otherwise).

USAGE:
    uv run python tools/run_validation.py             # uses default validation set
    uv run python tools/run_validation.py --quick     # one-spec smoke test
    uv run python tools/run_validation.py --custom my_specs.json
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Local imports — same dir
sys.path.insert(0, str(Path(__file__).parent))
from mt5_cli import TestSpec, MT5CliRunner
import mt5_tester_report as mtr
from quant_report import compute_report
import monte_carlo as mc


RESULTS_DIR = Path(__file__).parent.parent / "Results" / "cli_validation"


def parse_log_to_csv(log_path: Path, expert_filter: str, csv_out: Path) -> dict:
    """Parse latest test of given expert from log, write trades CSV. Return summary dict."""
    text = mtr.read_utf16(str(log_path))
    segments = list(mtr.parse_test_segments(text))
    if not segments:
        return {"error": f"No tests found in {log_path}"}

    parsed = [mtr.parse_segment(s, e, m, lines) for s, e, m, lines in segments]
    matching = [p for p in parsed if p["expert"].startswith(expert_filter)]
    if not matching:
        return {"error": f"No tests of '{expert_filter}' in {log_path}"}

    p = matching[-1]  # most recent
    trades = mtr.pair_trades(p["deals"])

    # Write trades CSV — schema matches what sharpe/monte_carlo/quant_report expect
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

    return {
        "expert": p["expert"],
        "period": f"{p['start']} -> {p['end']}",
        "deal_count": len(p["deals"]),
        "trade_count": len(trades),
        "final_balance": p["final_balance"],
        "csv_path": str(csv_out),
        "inputs_echo": {k: v for k, v in list(p["inputs"].items())[:8]},  # first 8 for sanity
    }


def run_one(spec: TestSpec, runner: MT5CliRunner, out_dir: Path) -> dict:
    """Full pipeline for one spec. Returns combined dict."""
    print(f"\n{'='*70}")
    print(f"[{spec.label or spec.expert}] {spec.symbol} {spec.timeframe} "
          f"{spec.start_date}->{spec.end_date}")
    print(f"{'='*70}")

    cli_result = runner.run_test(spec)
    print(f"  CLI result: success={cli_result['success']} "
          f"elapsed={cli_result['elapsed_seconds']:.1f}s "
          f"log_growth={cli_result.get('log_growth_bytes', 0)}B")

    if not cli_result["success"]:
        return {"spec": asdict(spec), "cli": cli_result, "error": cli_result.get("error")}

    log_path = Path(cli_result["log_path"])
    csv_path = out_dir / f"trades_{spec.label or spec.expert}.csv"
    parse_summary = parse_log_to_csv(log_path, spec.expert, csv_path)
    if "error" in parse_summary:
        return {"spec": asdict(spec), "cli": cli_result, "parse": parse_summary}

    print(f"  Parsed: {parse_summary['trade_count']} trades, "
          f"final balance ${parse_summary['final_balance']}")

    report = compute_report(csv_path, starting_equity=spec.deposit)
    print(f"  Sharpe(annual)={report.get('sharpe_annual')}  "
          f"PF={report.get('profit_factor')}  "
          f"MaxDD%={report.get('max_dd_pct')}  "
          f"Trades={report.get('trades')}")

    # Monte Carlo — Gate 6. 2000 runs is plenty for a screening verdict.
    mc_result = None
    try:
        profits = mc.read_trades(csv_path)
        if profits:
            shuffle_sim = mc.run_simulation(profits, runs=2000,
                                            starting_equity=spec.deposit,
                                            mode="shuffle", seed=42)
            bootstrap_sim = mc.run_simulation(profits, runs=2000,
                                              starting_equity=spec.deposit,
                                              mode="bootstrap", seed=43)
            mc_result = {
                "shuffle_p95_dd": shuffle_sim["max_dd_pct"]["p95"],
                "shuffle_p99_dd": shuffle_sim["max_dd_pct"]["p99"],
                "shuffle_prob_profitable": shuffle_sim["prob_profitable"],
                "bootstrap_p95_dd": bootstrap_sim["max_dd_pct"]["p95"],
                "bootstrap_p99_dd": bootstrap_sim["max_dd_pct"]["p99"],
                "bootstrap_prob_profitable": bootstrap_sim["prob_profitable"],
                "shuffle_verdict": mc.gate_verdict(shuffle_sim),
                "bootstrap_verdict": mc.gate_verdict(bootstrap_sim),
            }
            print(f"  MC shuffle p95 DD={mc_result['shuffle_p95_dd']:.1f}%  "
                  f"bootstrap p95 DD={mc_result['bootstrap_p95_dd']:.1f}%")
    except Exception as e:
        mc_result = {"error": str(e)}
        print(f"  MC failed: {e}")

    return {
        "spec": asdict(spec),
        "cli": {k: v for k, v in cli_result.items() if k != "spec"},
        "parse": parse_summary,
        "metrics": report,
        "monte_carlo": mc_result,
    }


def scoreboard_md(results: list[dict]) -> str:
    """Compact markdown scoreboard.

    Gates checked: PF >= 1.3, Sharpe >= 1.0, MaxDD <= 25%, RF >= 2.0, N >= 100,
    MC bootstrap p95 DD <= 25% (Pella Gate 6).
    """
    rows = ["| Label | Trades | PF | Sharpe | MaxDD% | RF | SQN | p-IID | p-HAC | MC p95 DD% | Verdict |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "metrics" not in r:
            label = r["spec"].get("label") or r["spec"].get("expert", "?")
            rows.append(f"| {label} | ERROR | — | — | — | — | — | — | — | — | {r.get('error', 'failed')} |")
            continue
        m = r["metrics"]
        if "error" in m:
            label = r["spec"].get("label") or r["spec"].get("expert", "?")
            rows.append(f"| {label} | 0 | — | — | — | — | — | — | — | — | {m['error']} |")
            continue
        # Pella gates
        sharpe = m.get("sharpe_annual", 0) or 0
        pf = m.get("profit_factor", 0) or 0
        dd = m.get("max_dd_pct", 100) or 100
        rf = m.get("recovery_factor") or 0
        n = m.get("trades", 0) or 0
        mc_block = r.get("monte_carlo") or {}
        mc_p95 = mc_block.get("bootstrap_p95_dd")
        gates = []
        if pf >= 1.3: gates.append("PF")
        if sharpe >= 1.0: gates.append("Sh")
        if dd <= 25: gates.append("DD")
        if rf and rf >= 2.0: gates.append("RF")
        if n >= 100: gates.append("N")
        if mc_p95 is not None and mc_p95 <= 25: gates.append("MC")
        gate_total = 6
        verdict = "PASS" if len(gates) == gate_total else f"PARTIAL({'/'.join(gates) or 'none'})"
        mc_cell = f"{mc_p95:.1f}" if mc_p95 is not None else "—"
        rows.append(
            f"| {m['label']} | {n} | {pf} | {sharpe} | {dd} | {rf} | {m.get('sqn')} | "
            f"{m.get('p_value_iid')} | {m.get('p_value_hac')} | {mc_cell} | {verdict} |"
        )
    return "\n".join(rows)


def default_specs() -> list[TestSpec]:
    """Default validation set: ChannelBreakoutVIP_MT5 v0.2 percent-risk validation."""
    return [
        # Sanity baseline — fixed lots, should reproduce prior known-good numbers
        TestSpec(
            expert="ChannelBreakoutVIP_MT5",
            symbol="USDJPY", timeframe="H1",
            start_date="2020.01.01", end_date="2026.04.30",
            inputs={"UseRiskPercent": False, "Lots": 0.10},
            label="ChBVIP_USDJPY_FIXED",
        ),
        # Percent-risk on — same window, validate edge survives sizing change
        TestSpec(
            expert="ChannelBreakoutVIP_MT5",
            symbol="USDJPY", timeframe="H1",
            start_date="2020.01.01", end_date="2026.04.30",
            inputs={"UseRiskPercent": True, "RiskPercent": 1.0},
            label="ChBVIP_USDJPY_PCT1",
        ),
        # XAUUSD baseline
        TestSpec(
            expert="ChannelBreakoutVIP_MT5",
            symbol="XAUUSD", timeframe="H1",
            start_date="2020.01.01", end_date="2026.04.30",
            inputs={"UseRiskPercent": False, "Lots": 0.10},
            label="ChBVIP_XAUUSD_FIXED",
        ),
        # XAUUSD percent-risk
        TestSpec(
            expert="ChannelBreakoutVIP_MT5",
            symbol="XAUUSD", timeframe="H1",
            start_date="2020.01.01", end_date="2026.04.30",
            inputs={"UseRiskPercent": True, "RiskPercent": 1.0},
            label="ChBVIP_XAUUSD_PCT1",
        ),
    ]


def quick_smoke_spec() -> list[TestSpec]:
    """Single short test for sanity-checking the CLI runner end-to-end."""
    return [
        TestSpec(
            expert="ChannelBreakoutVIP_MT5",
            symbol="USDJPY", timeframe="H1",
            start_date="2024.01.01", end_date="2024.06.30",  # 6mo for fast feedback
            inputs={"UseRiskPercent": False, "Lots": 0.10},
            label="SMOKE_ChBVIP_USDJPY_H1_2024H1",
            timeout_seconds=600,
        ),
    ]


def specs_from_json(path: Path) -> list[TestSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [TestSpec(**d) for d in data]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="Run a single short smoke-test spec (6mo USDJPY H1)")
    ap.add_argument("--custom", help="Path to JSON file with list of TestSpec dicts")
    ap.add_argument("--out", default=str(RESULTS_DIR),
                    help="Output directory for CSVs + scoreboard")
    args = ap.parse_args()

    out_dir = Path(args.out) / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.custom:
        specs = specs_from_json(Path(args.custom))
    elif args.quick:
        specs = quick_smoke_spec()
    else:
        specs = default_specs()

    print(f"Output dir: {out_dir}")
    print(f"Specs to run: {len(specs)}")
    for s in specs:
        print(f"  - {s.label or s.expert}  {s.symbol} {s.timeframe}  "
              f"{s.start_date}->{s.end_date}")

    runner = MT5CliRunner()
    all_results = []
    t0 = time.time()
    for i, spec in enumerate(specs, 1):
        print(f"\n[{i}/{len(specs)}]")
        r = run_one(spec, runner, out_dir)
        all_results.append(r)
        # Persist after every run so a crash mid-batch doesn't lose the rest
        (out_dir / "results.json").write_text(
            json.dumps(all_results, indent=2, default=str),
            encoding="utf-8",
        )
        (out_dir / "scoreboard.md").write_text(
            f"# Validation scoreboard — {time.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"Specs run: {i}/{len(specs)}\n"
            f"Total elapsed so far: {(time.time()-t0)/60:.1f} min\n\n"
            + scoreboard_md(all_results) + "\n",
            encoding="utf-8",
        )

    print(f"\n{'='*70}")
    print(f"DONE — {len(specs)} specs in {(time.time()-t0)/60:.1f} min")
    print(f"Scoreboard: {out_dir / 'scoreboard.md'}")
    print(f"Raw JSON:   {out_dir / 'results.json'}")
    print(f"{'='*70}\n")
    print(scoreboard_md(all_results))


if __name__ == "__main__":
    main()
