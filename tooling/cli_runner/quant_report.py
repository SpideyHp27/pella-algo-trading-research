#!/usr/bin/env python3
"""Local equivalent of QuantDash Pro / Aristhrottle quant analysis report.

Computes the standard institutional retail metric set from a backtest CSV:
  - Net Profit / %, total trades, win rate, PF
  - Sharpe ratio (daily + annualized)
  - Max drawdown (% + dollars)
  - Recovery Factor
  - SQN (System Quality Number)
  - Stagnation (longest drawdown in trading days)
  - p-value IID (Gaussian)
  - p-value HAC (Newey-West autocorrelation-corrected)
  - ACF(1) autocorrelation of daily returns
  - Trading days

Same output as Aristhrottle/QuantDash Pro for direct comparison.

Usage:
  uv run python quant_report.py --csv trades.csv [--equity 50000] [--label name]
  uv run python quant_report.py --batch dir/  # process every *FROMLOG.csv
"""
from __future__ import annotations
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def read_profits(csv_path: Path):
    """Return list of (close_date, profit) tuples in chronological order."""
    out = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "profit" not in reader.fieldnames or "date" not in reader.fieldnames:
            raise ValueError("CSV must have 'profit' and 'date' columns")
        for row in reader:
            try:
                out.append((row["date"][:10], float(row["profit"])))
            except (ValueError, KeyError):
                continue
    return out


def daily_pnl_dict(trades):
    daily = defaultdict(float)
    for d, p in trades:
        daily[d] += p
    return dict(daily)


def fill_trading_days(daily, start_pad=0):
    if not daily:
        return []
    dates = sorted(daily)
    start = datetime.strptime(dates[0], "%Y-%m-%d")
    end = datetime.strptime(dates[-1], "%Y-%m-%d")
    out = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            k = cur.strftime("%Y-%m-%d")
            out.append((k, daily.get(k, 0.0)))
        cur += timedelta(days=1)
    return out


def equity_curve(daily_series, starting_equity):
    eq = starting_equity
    out = [(None, eq)]
    for d, pnl in daily_series:
        eq += pnl
        out.append((d, eq))
    return out


def max_drawdown_and_stagnation(equity_series, starting_equity):
    if not equity_series:
        return 0, 0, 0
    peak = starting_equity
    max_dd_pct = 0
    max_dd_usd = 0
    stagnation = 0
    current_dd_days = 0
    for _, eq in equity_series:
        if eq > peak:
            peak = eq
            current_dd_days = 0
        else:
            current_dd_days += 1
            stagnation = max(stagnation, current_dd_days)
            if peak > 0:
                dd_pct = (peak - eq) / peak * 100
                if dd_pct > max_dd_pct: max_dd_pct = dd_pct
            dd_usd = peak - eq
            if dd_usd > max_dd_usd: max_dd_usd = dd_usd
    return max_dd_pct, max_dd_usd, stagnation


def autocorr(series, lag=1):
    n = len(series)
    if n <= lag + 1:
        return 0
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / n
    if var == 0:
        return 0
    cov = sum((series[i] - mean) * (series[i + lag] - mean) for i in range(n - lag)) / n
    return cov / var


def newey_west_hac_se(series, max_lag=None):
    """Newey-West HAC standard error for the mean of a series.

    Returns (mean, hac_std_error). Uses Bartlett kernel weights.
    Lag length L = floor(0.75 * N^(1/3)) per common practice.
    """
    n = len(series)
    if n < 2:
        return 0, 0
    mean = sum(series) / n
    if max_lag is None:
        max_lag = max(1, int(0.75 * (n ** (1/3))))
    # gamma_0 = sample variance
    gamma_0 = sum((x - mean) ** 2 for x in series) / n
    # Bartlett-weighted autocovariances
    s = gamma_0
    for k in range(1, max_lag + 1):
        weight = 1 - k / (max_lag + 1)
        gamma_k = sum((series[i] - mean) * (series[i + k] - mean) for i in range(n - k)) / n
        s += 2 * weight * gamma_k
    if s < 0: s = gamma_0  # fallback if NW gives negative (rare with Bartlett)
    hac_se = math.sqrt(s / n)
    return mean, hac_se


def gaussian_p_value(t_stat):
    """Approximate one-tailed p-value for Gaussian test stat."""
    if t_stat >= 3.090: return "<0.001"
    if t_stat >= 2.326: return "<0.01"
    if t_stat >= 1.960: return "<0.025"
    if t_stat >= 1.645: return "<0.05"
    if t_stat >= 1.282: return "<0.10"
    return ">=0.10"


def monthly_pnl(daily_dict):
    monthly = defaultdict(float)
    for d, v in daily_dict.items():
        monthly[d[:7]] += v
    return dict(monthly)


def compute_report(csv_path: Path, starting_equity: float = 50000):
    trades = read_profits(csv_path)
    n_trades = len(trades)
    if n_trades == 0:
        return {"label": csv_path.stem, "error": "no trades"}

    profits = [p for _, p in trades]
    sum_wins = sum(p for p in profits if p > 0)
    sum_losses = abs(sum(p for p in profits if p < 0))
    n_wins = sum(1 for p in profits if p > 0)
    n_losses = sum(1 for p in profits if p < 0)
    win_rate = n_wins / n_trades * 100
    pf = sum_wins / sum_losses if sum_losses > 0 else float("inf")
    net = sum(profits)

    # SQN
    mean_p = net / n_trades
    var_p = sum((p - mean_p) ** 2 for p in profits) / max(1, n_trades - 1)
    std_p = math.sqrt(var_p)
    sqn = (mean_p / std_p) * math.sqrt(n_trades) if std_p > 0 else 0

    # Daily aggregation
    daily = daily_pnl_dict(trades)
    daily_series = fill_trading_days(daily)
    daily_pnls = [pnl for _, pnl in daily_series]
    daily_returns = [pnl / starting_equity for pnl in daily_pnls]
    n_days = len(daily_returns)

    # Sharpe (IID)
    if n_days >= 2 and len(daily_returns) > 0:
        mean_r = sum(daily_returns) / n_days
        var_r = sum((r - mean_r) ** 2 for r in daily_returns) / (n_days - 1)
        std_r = math.sqrt(var_r)
        sharpe_daily = mean_r / std_r if std_r > 0 else 0
        sharpe_annual = sharpe_daily * math.sqrt(252)
    else:
        sharpe_daily = sharpe_annual = 0
        mean_r = std_r = 0

    # p-value IID (Gaussian)
    t_stat_iid = sharpe_daily * math.sqrt(n_days) if std_r > 0 else 0
    p_iid = gaussian_p_value(t_stat_iid)

    # p-value HAC (Newey-West)
    if n_days >= 4:
        nw_mean, nw_se = newey_west_hac_se(daily_returns)
        t_stat_hac = nw_mean / nw_se if nw_se > 0 else 0
    else:
        t_stat_hac = 0
    p_hac = gaussian_p_value(t_stat_hac)

    # ACF(1)
    acf1 = autocorr(daily_returns, lag=1)

    # Equity curve + max DD + stagnation
    eq_series = equity_curve(daily_series, starting_equity)
    max_dd_pct, max_dd_usd, stagnation = max_drawdown_and_stagnation(eq_series, starting_equity)

    # Recovery factor
    recovery = net / max_dd_usd if max_dd_usd > 0 else float("inf")

    # Profitable months
    monthly = monthly_pnl(daily)
    n_months = len(monthly)
    n_months_pos = sum(1 for v in monthly.values() if v > 0)
    profitable_months_pct = n_months_pos / n_months * 100 if n_months > 0 else 0

    return {
        "label": csv_path.stem,
        "trades": n_trades,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate_pct": round(win_rate, 2),
        "net_profit_usd": round(net, 2),
        "net_profit_pct": round(net / starting_equity * 100, 2),
        "profit_factor": round(pf, 3),
        "sharpe_daily": round(sharpe_daily, 4),
        "sharpe_annual": round(sharpe_annual, 3),
        "t_stat_iid": round(t_stat_iid, 3),
        "p_value_iid": p_iid,
        "t_stat_hac": round(t_stat_hac, 3),
        "p_value_hac": p_hac,
        "acf_lag1": round(acf1, 4),
        "max_dd_pct": round(max_dd_pct, 2),
        "max_dd_usd": round(max_dd_usd, 2),
        "stagnation_days": stagnation,
        "recovery_factor": round(recovery, 2) if recovery != float("inf") else None,
        "sqn": round(sqn, 3),
        "trading_days": n_days,
        "profitable_months_pct": round(profitable_months_pct, 1),
        "n_months": n_months,
    }


def print_report(r):
    if "error" in r:
        print(f"=== {r['label']}: {r['error']} ===")
        return
    print(f"\n=== {r['label']} ===")
    print(f"  Trades:                 {r['trades']:>10}     ({r['wins']}W / {r['losses']}L = {r['win_rate_pct']}% WR)")
    print(f"  Net Profit:             ${r['net_profit_usd']:>10,.2f}     ({r['net_profit_pct']:.2f}%)")
    print(f"  Profit Factor:          {r['profit_factor']:>10}")
    print(f"  Sharpe (annual):        {r['sharpe_annual']:>10}")
    print(f"  Max Drawdown:           {r['max_dd_pct']:>10}%    (${r['max_dd_usd']:,.2f})")
    print(f"  Recovery Factor:        {r['recovery_factor']:>10}")
    print(f"  SQN:                    {r['sqn']:>10}")
    print(f"  Stagnation (days):      {r['stagnation_days']:>10}")
    print(f"  p-value (IID):          {r['p_value_iid']:>10}     (t={r['t_stat_iid']})")
    print(f"  p-value (HAC):          {r['p_value_hac']:>10}     (t={r['t_stat_hac']})")
    print(f"  ACF(1):                 {r['acf_lag1']:>10}")
    print(f"  Profitable Months:      {r['profitable_months_pct']:>10}%    ({r['n_months']} months total)")
    print(f"  Trading Days:           {r['trading_days']:>10}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="single CSV path")
    ap.add_argument("--batch", help="directory; process every *FROMLOG*.csv")
    ap.add_argument("--equity", type=float, default=50000.0)
    ap.add_argument("--json", action="store_true", help="output JSON instead of pretty")
    args = ap.parse_args()

    targets = []
    if args.csv:
        targets.append(Path(args.csv))
    elif args.batch:
        targets.extend(sorted(Path(args.batch).glob("*FROMLOG*.csv")))
    else:
        raise SystemExit("Provide --csv or --batch")

    results = []
    for p in targets:
        if not p.is_file():
            print(f"MISSING: {p}")
            continue
        r = compute_report(p, args.equity)
        results.append(r)
        if not args.json:
            print_report(r)

    if args.json:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
