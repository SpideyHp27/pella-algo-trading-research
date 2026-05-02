#!/usr/bin/env python3
"""Annualized Sharpe ratio + Chan-style hypothesis test for backtest CSVs.

Industry standard Sharpe per Chan (Quantitative Trading + Algorithmic Trading):
  daily_returns = per-day P/L / starting_equity
  daily_sharpe  = mean(daily_returns) / std(daily_returns)
  annualized    = daily_sharpe × sqrt(252)

Hypothesis test (Chan AT Ch 1):
  test_stat = daily_sharpe × sqrt(N_days)
                = annual_sharpe × sqrt(N_days / 252)
  p-value via Gaussian: 2.326 → 0.01, 1.645 → 0.05, 1.282 → 0.10
  Chan's gate: p < 0.01 = statistically significant.

Days with no trades count as 0 returns (standard practice — strategy
allocated capital but earned 0). Weekends excluded.

Usage:
  uv run python sharpe.py --csv trades.csv [--equity 50000] [--label name]
"""
from __future__ import annotations
import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def read_daily_pnl(csv_path: Path) -> dict[str, float]:
    """Aggregate per-trade profits by close date."""
    daily: dict[str, float] = defaultdict(float)
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "profit" not in reader.fieldnames or "date" not in reader.fieldnames:
            raise ValueError("CSV must have 'profit' and 'date' columns")
        for row in reader:
            try:
                d = row["date"][:10]
                p = float(row["profit"])
                daily[d] += p
            except (ValueError, KeyError):
                continue
    return dict(daily)


def fill_trading_days(daily_pnl: dict[str, float]) -> list[tuple[str, float]]:
    """Fill in zero-return weekdays between first and last trade."""
    if not daily_pnl:
        return []
    dates = sorted(daily_pnl.keys())
    start = datetime.strptime(dates[0], "%Y-%m-%d")
    end = datetime.strptime(dates[-1], "%Y-%m-%d")
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon-Fri
            key = cur.strftime("%Y-%m-%d")
            out.append((key, daily_pnl.get(key, 0.0)))
        cur += timedelta(days=1)
    return out


def compute_metrics(returns: list[float]) -> dict:
    """Sharpe + auxiliary stats."""
    n = len(returns)
    if n < 2:
        return {"n": n, "mean_daily_pct": 0, "std_daily_pct": 0,
                "sharpe_daily": 0, "sharpe_annual": 0,
                "t_stat": 0, "p_value": 1.0, "verdict": "INSUFFICIENT DATA"}

    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = var ** 0.5

    if std == 0:
        return {"n": n, "mean_daily_pct": mean * 100, "std_daily_pct": 0,
                "sharpe_daily": 0, "sharpe_annual": 0,
                "t_stat": 0, "p_value": 1.0, "verdict": "ZERO VARIANCE"}

    sharpe_daily = mean / std
    sharpe_annual = sharpe_daily * math.sqrt(252)
    t_stat = sharpe_daily * math.sqrt(n)

    # Gaussian one-tailed p-value (Chan's table)
    if t_stat >= 2.326:
        p_value = "<0.01"
        verdict_pchan = "PASS — p<0.01 (highly significant)"
    elif t_stat >= 1.645:
        p_value = "<0.05"
        verdict_pchan = "PASS — p<0.05"
    elif t_stat >= 1.282:
        p_value = "<0.10"
        verdict_pchan = "MARGINAL — p<0.10"
    else:
        p_value = ">=0.10"
        verdict_pchan = "FAIL — p>=0.10 (not significant)"

    # Zenom gate: Sharpe > 1.0
    if sharpe_annual >= 1.5:
        verdict_zenom = "STRONG (>=1.5)"
    elif sharpe_annual >= 1.0:
        verdict_zenom = "PASS (>=1.0)"
    elif sharpe_annual >= 0.5:
        verdict_zenom = "MARGINAL (0.5-1.0)"
    else:
        verdict_zenom = "FAIL (<0.5)"

    return {
        "n": n,
        "mean_daily_pct": mean * 100,
        "std_daily_pct": std * 100,
        "sharpe_daily": sharpe_daily,
        "sharpe_annual": sharpe_annual,
        "t_stat": t_stat,
        "p_value": p_value,
        "verdict_pchan": verdict_pchan,
        "verdict_zenom": verdict_zenom,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--equity", type=float, default=50000.0)
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    p = Path(args.csv)
    if not p.is_file():
        raise SystemExit(f"CSV not found: {p}")

    label = args.label or p.stem
    daily_pnl = read_daily_pnl(p)
    series = fill_trading_days(daily_pnl)
    returns = [pnl / args.equity for _, pnl in series]
    m = compute_metrics(returns)

    print(f"=== {label} ===")
    print(f"  CSV: {p}")
    print(f"  Trading days analyzed: {m['n']}")
    print(f"  Mean daily return: {m['mean_daily_pct']:.4f}%")
    print(f"  Std daily return:  {m['std_daily_pct']:.4f}%")
    print(f"  Daily Sharpe:      {m['sharpe_daily']:.4f}")
    print(f"  Annualized Sharpe: {m['sharpe_annual']:.3f}")
    print(f"  Chan t-stat:       {m['t_stat']:.3f}  →  p-value {m['p_value']}")
    print(f"  Verdict (Chan):    {m['verdict_pchan']}")
    print(f"  Verdict (Zenom):   {m['verdict_zenom']}")


if __name__ == "__main__":
    main()
