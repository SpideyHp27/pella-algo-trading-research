#!/usr/bin/env python3
"""G4b Param Sensitivity gate — Pella adoption of the Discord framework.

For a baseline strategy config that passed all other gates, perturb each
parameter +/- 1 step (one at a time, others held at baseline) and re-test.

Verdict per param:
  PASS      — variant PF stays > 1.0 AND Sharpe degradation < 40%
  WARN      — variant PF stays > 1.0 BUT Sharpe degradation >= 40% (steep gradient)
  CLIFF     — variant PF drops below 1.0 (cliff edge — strong curve-fit signal)
  COLLAPSE  — variant PF stays positive but Sharpe goes negative (regime sensitive)

Overall verdict:
  PASS  — every param-step neighbor passes
  WARN  — at least one steep gradient, no cliff/collapse
  FAIL  — at least one cliff edge or collapse — strategy is curve-fit

USAGE:
  uv run python tools/param_sensitivity.py --custom path/to/sensitivity_spec.json

Spec JSON format:
  {
    "baseline": { ... full TestSpec dict ... },
    "params": [
      {"name": "lookback", "step": 10},
      {"name": "EMA", "step": 10},
      {"name": "TP", "step": 10}
    ]
  }

For each param, two variants are generated: baseline_value - step, baseline_value + step.
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


RESULTS_DIR = Path(__file__).parent.parent / "Results" / "param_sensitivity"


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
    """Execute one TestSpec, parse, return metric dict."""
    print(f"  -> {spec.label}", flush=True)
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


def make_variant(baseline: TestSpec, param_name: str, new_value) -> TestSpec:
    """Return a new TestSpec with one input param overridden."""
    d = asdict(baseline)
    d["inputs"] = dict(d["inputs"])
    d["inputs"][param_name] = new_value
    sign = "+" if new_value > baseline.inputs[param_name] else "-"
    d["label"] = f"{baseline.label}_{param_name}_{sign}"
    return TestSpec(**d)


def verdict_for_variant(baseline_metrics: dict, variant_metrics: dict) -> tuple[str, dict]:
    """Compare a variant's metrics to baseline; return (verdict, deltas)."""
    if "metrics" not in variant_metrics:
        return ("ERROR", {})
    b = baseline_metrics["metrics"]
    v = variant_metrics["metrics"]

    b_pf = b.get("profit_factor", 0) or 0
    v_pf = v.get("profit_factor", 0) or 0
    b_sh = b.get("sharpe_annual", 0) or 0
    v_sh = v.get("sharpe_annual", 0) or 0

    sh_delta_pct = ((v_sh - b_sh) / abs(b_sh) * 100) if b_sh != 0 else 0
    deltas = {
        "pf_baseline": b_pf, "pf_variant": v_pf,
        "sharpe_baseline": b_sh, "sharpe_variant": v_sh,
        "sharpe_delta_pct": round(sh_delta_pct, 1),
    }

    if v_pf < 1.0:
        return ("CLIFF", deltas)
    if v_sh < 0:
        return ("COLLAPSE", deltas)
    if abs(sh_delta_pct) > 40 and sh_delta_pct < 0:
        return ("WARN", deltas)
    return ("PASS", deltas)


def overall_verdict(per_param_verdicts: list[str]) -> str:
    if any(v in ("CLIFF", "COLLAPSE", "ERROR") for v in per_param_verdicts):
        return "FAIL — at least one neighbor is a cliff edge or collapse"
    if any(v == "WARN" for v in per_param_verdicts):
        return "WARN — at least one steep gradient (>40% Sharpe drop)"
    return "PASS — strategy is parameter-robust"


def write_report(baseline_result: dict, variant_results: list[dict], out_path: Path) -> None:
    lines = ["# G4b Param Sensitivity Report\n"]
    lines.append(f"## Baseline\n")
    bm = baseline_result.get("metrics", {})
    lines.append(f"- Label: `{baseline_result['spec']['label']}`")
    lines.append(f"- Trades: {bm.get('trades')}")
    lines.append(f"- PF: {bm.get('profit_factor')}")
    lines.append(f"- Sharpe: {bm.get('sharpe_annual')}")
    lines.append(f"- MaxDD%: {bm.get('max_dd_pct')}")
    lines.append(f"- p-HAC: {bm.get('p_value_hac')}\n")

    lines.append("## Per-param sensitivity\n")
    lines.append("| Param variant | Trades | PF | Sharpe | Δ Sharpe % | Verdict |")
    lines.append("|---|---|---|---|---|---|")
    per_param_verdicts: list[str] = []
    for r in variant_results:
        label = r["spec"]["label"]
        if "error" in r:
            lines.append(f"| {label} | ERROR | — | — | — | ERROR ({r['error']}) |")
            per_param_verdicts.append("ERROR")
            continue
        verdict, deltas = verdict_for_variant(baseline_result, r)
        per_param_verdicts.append(verdict)
        m = r["metrics"]
        lines.append(
            f"| {label} | {m.get('trades')} | {m.get('profit_factor')} | "
            f"{m.get('sharpe_annual')} | {deltas.get('sharpe_delta_pct')}% | {verdict} |"
        )
    lines.append("")
    lines.append(f"## Overall\n")
    lines.append(f"**{overall_verdict(per_param_verdicts)}**")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--custom", required=True,
                    help="JSON spec with 'baseline' and 'params' keys")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    args = ap.parse_args()

    spec_data = json.loads(Path(args.custom).read_text(encoding="utf-8"))
    baseline_spec = TestSpec(**spec_data["baseline"])
    params = spec_data["params"]

    out_dir = Path(args.out) / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output dir: {out_dir}")
    print(f"Baseline label: {baseline_spec.label}")
    print(f"Params to test: {[p['name'] for p in params]}")
    print(f"Total runs: {1 + 2 * len(params)} (1 baseline + 2 per param)")
    print()

    runner = MT5CliRunner()
    t0 = time.time()

    print("=== BASELINE ===")
    baseline_result = run_one(baseline_spec, runner, out_dir)
    if "error" in baseline_result:
        sys.exit(f"Baseline failed: {baseline_result['error']}")
    bm = baseline_result["metrics"]
    print(f"     Sharpe={bm.get('sharpe_annual')}  PF={bm.get('profit_factor')}  "
          f"MaxDD%={bm.get('max_dd_pct')}  Trades={bm.get('trades')}")

    variant_results = []
    for p in params:
        name = p["name"]
        step = p["step"]
        base_val = baseline_spec.inputs[name]
        for delta in [-step, +step]:
            new_val = base_val + delta
            print(f"\n=== {name} {'+' if delta > 0 else '-'}{abs(step)} (={new_val}) ===")
            variant = make_variant(baseline_spec, name, new_val)
            r = run_one(variant, runner, out_dir)
            variant_results.append(r)
            if "metrics" in r:
                rm = r["metrics"]
                verdict, deltas = verdict_for_variant(baseline_result, r)
                print(f"     Sharpe={rm.get('sharpe_annual')}  PF={rm.get('profit_factor')}  "
                      f"ΔSh={deltas.get('sharpe_delta_pct')}%  -> {verdict}")
            else:
                print(f"     ERROR: {r.get('error')}")

            # Persist incremental report
            (out_dir / "results.json").write_text(
                json.dumps({"baseline": baseline_result, "variants": variant_results},
                           indent=2, default=str),
                encoding="utf-8",
            )
            write_report(baseline_result, variant_results, out_dir / "report.md")

    print(f"\nDONE in {(time.time()-t0)/60:.1f} min")
    print(f"Report: {out_dir / 'report.md'}")
    print()
    print((out_dir / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
