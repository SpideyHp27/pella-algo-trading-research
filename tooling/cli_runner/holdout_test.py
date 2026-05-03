#!/usr/bin/env python3
"""G4d Holdout test — adopted from Discord 8-gate framework.

The most direct curve-fit detector in the framework. Splits each strategy's
backtest window into:
  - IS (in-sample): everything BEFORE the holdout cutoff (used for all prior
    validation: walk-forward, MC, sensitivity)
  - OOS (out-of-sample) HOLDOUT: a fresh year that the strategy has never been
    "seen" against during research

Annual rollover convention (per Discord framework):
  - Default holdout: most recent 1 year (2025.04.06 -> 2026.04.06)
  - Annual rollover: each Jan, oldest holdout year joins IS, newest year
    becomes new holdout. We don't auto-rollover; just run with current cutoff.

Verdict per strategy:
  - PASS — holdout Sharpe is within 50% of IS Sharpe AND holdout PF >= 1.0
  - WARN — holdout Sharpe drop >50% but PF stays above 1.0 (edge degraded but
    not dead)
  - FAIL — holdout PF < 1.0 (strategy doesn't generalize forward)
  - COLLAPSE — holdout Sharpe < 0 (strategy actively LOSES money out-of-sample)

USAGE:
  uv run python tools/holdout_test.py --custom NT8Bridge/tools/specs_holdout_survivors.json

Spec format: list of TestSpec dicts (same as run_validation). The tool splits
each spec into IS + OOS variants automatically using --is-end and --oos-end
arguments (defaults: 2025.04.06 and 2026.04.06).
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


RESULTS_DIR = Path(__file__).parent.parent / "Results" / "holdout"


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
    for w in cli.get("warnings", []):
        print(f"     WARN: {w}")
    for d in cli.get("diagnostics", []):
        print(f"     DIAG: {d}")
    if not cli["success"]:
        return {"spec": asdict(spec), "error": cli.get("error", "CLI failed")}
    log_path = Path(cli["log_path"])
    csv_path = out_dir / f"trades_{spec.label}.csv"
    parse = parse_log_to_csv(log_path, spec.expert, csv_path)
    if "error" in parse:
        return {"spec": asdict(spec), "error": parse["error"]}
    if parse.get("trade_count", 0) == 0:
        return {"spec": asdict(spec), "error": "0 trades"}
    report = compute_report(csv_path, starting_equity=spec.deposit)
    return {"spec": asdict(spec),
            "trades": parse["trade_count"],
            "final_balance": parse["final_balance"],
            "metrics": report}


def split_spec(base: TestSpec, original_start: str, is_end: str, oos_end: str) -> tuple[TestSpec, TestSpec]:
    """Return (is_spec, oos_spec) versions of the base spec."""
    is_d = asdict(base)
    is_d["start_date"] = original_start
    is_d["end_date"] = is_end
    is_d["label"] = f"{base.label}_IS"

    oos_d = asdict(base)
    oos_d["start_date"] = is_end
    oos_d["end_date"] = oos_end
    oos_d["label"] = f"{base.label}_OOS"

    return TestSpec(**is_d), TestSpec(**oos_d)


def verdict_for_holdout(is_result: dict, oos_result: dict) -> tuple[str, dict]:
    """Compare OOS vs IS metrics, return (verdict, deltas)."""
    if "metrics" not in oos_result:
        return ("ERROR", {"error": oos_result.get("error", "no metrics")})

    is_m = is_result["metrics"]
    oos_m = oos_result["metrics"]

    is_pf = is_m.get("profit_factor", 0) or 0
    oos_pf = oos_m.get("profit_factor", 0) or 0
    is_sh = is_m.get("sharpe_annual", 0) or 0
    oos_sh = oos_m.get("sharpe_annual", 0) or 0

    sh_drop_pct = ((oos_sh - is_sh) / abs(is_sh) * 100) if is_sh != 0 else 0

    deltas = {
        "is_pf": is_pf, "oos_pf": oos_pf,
        "is_sharpe": is_sh, "oos_sharpe": oos_sh,
        "sharpe_change_pct": round(sh_drop_pct, 1),
        "is_trades": is_m.get("trades"), "oos_trades": oos_m.get("trades"),
    }

    # Verdict logic
    if oos_sh < 0:
        return ("COLLAPSE", deltas)
    if oos_pf < 1.0:
        return ("FAIL", deltas)
    if sh_drop_pct < -50:
        return ("WARN", deltas)
    return ("PASS", deltas)


def write_report(results: list[dict], out_path: Path) -> None:
    lines = ["# G4d Holdout Validation Report\n"]
    lines.append("Methodology: each strategy's full window split into IS (everything before holdout cutoff) and OOS holdout (most recent year). The OOS year is data the strategy was NEVER tested on during research — the most direct curve-fit detector.\n")
    lines.append("Verdicts:")
    lines.append("- PASS — holdout Sharpe within 50% of IS AND holdout PF ≥ 1.0")
    lines.append("- WARN — holdout Sharpe drop > 50% (edge degraded but PF still positive)")
    lines.append("- FAIL — holdout PF < 1.0 (strategy didn't generalize)")
    lines.append("- COLLAPSE — holdout Sharpe < 0 (actively losing OOS)\n")

    lines.append("| Strategy | IS Sharpe | OOS Sharpe | Δ Sharpe % | IS PF | OOS PF | IS Trades | OOS Trades | Verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        label = r["base_label"]
        if "error" in r:
            lines.append(f"| {label} | ERROR | — | — | — | — | — | — | {r['error']} |")
            continue
        v, d = r["verdict"], r["deltas"]
        lines.append(
            f"| {label} | {d['is_sharpe']} | {d['oos_sharpe']} | "
            f"{d['sharpe_change_pct']}% | {d['is_pf']} | {d['oos_pf']} | "
            f"{d['is_trades']} | {d['oos_trades']} | **{v}** |"
        )

    lines.append("")
    lines.append("## Decision rule")
    lines.append("- Anything PASS stays at its current Tier.")
    lines.append("- Anything WARN gets demoted from T1 → T2 (real edge but not as robust as we thought).")
    lines.append("- Anything FAIL or COLLAPSE gets shelved regardless of prior gates.")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--custom", required=True,
                    help="JSON file with list of base TestSpec dicts")
    ap.add_argument("--is-end", default="2025.04.06",
                    help="End date of in-sample window (also start of holdout)")
    ap.add_argument("--oos-end", default="2026.04.06",
                    help="End date of out-of-sample holdout window")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    args = ap.parse_args()

    base_specs = [TestSpec(**d) for d in json.loads(Path(args.custom).read_text(encoding="utf-8"))]

    out_dir = Path(args.out) / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output dir: {out_dir}")
    print(f"IS window:  base.start_date -> {args.is_end}")
    print(f"OOS window: {args.is_end} -> {args.oos_end}")
    print(f"Specs to test: {len(base_specs)}")
    print(f"Total backtests: {2 * len(base_specs)}")
    print()

    runner = MT5CliRunner()
    results = []
    t0 = time.time()

    for i, base in enumerate(base_specs, 1):
        print(f"=== [{i}/{len(base_specs)}] {base.label} ===")
        is_spec, oos_spec = split_spec(base, base.start_date, args.is_end, args.oos_end)

        is_result = run_one(is_spec, runner, out_dir)
        if "error" in is_result:
            results.append({"base_label": base.label, "error": f"IS failed: {is_result['error']}"})
            continue
        is_m = is_result["metrics"]
        print(f"     IS:  Sharpe={is_m.get('sharpe_annual')} PF={is_m.get('profit_factor')} "
              f"Trades={is_m.get('trades')}")

        oos_result = run_one(oos_spec, runner, out_dir)
        if "error" in oos_result:
            results.append({"base_label": base.label,
                            "error": f"OOS failed: {oos_result['error']}",
                            "is_result": is_result})
            continue
        oos_m = oos_result["metrics"]
        print(f"     OOS: Sharpe={oos_m.get('sharpe_annual')} PF={oos_m.get('profit_factor')} "
              f"Trades={oos_m.get('trades')}")

        verdict, deltas = verdict_for_holdout(is_result, oos_result)
        print(f"     -> {verdict}  (Sharpe change: {deltas.get('sharpe_change_pct')}%)")

        results.append({
            "base_label": base.label,
            "is_result": is_result,
            "oos_result": oos_result,
            "verdict": verdict,
            "deltas": deltas,
        })

        # Persist after each
        (out_dir / "results.json").write_text(
            json.dumps(results, indent=2, default=str), encoding="utf-8")
        write_report(results, out_dir / "report.md")

    print(f"\nDONE in {(time.time()-t0)/60:.1f} min")
    print(f"Report: {out_dir / 'report.md'}")
    print()
    print((out_dir / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
