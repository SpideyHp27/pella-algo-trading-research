#!/usr/bin/env python3
"""Portfolio capital-allocation optimizer.

Given N validated strategy trade CSVs, computes optimal capital weights via
multiple methods and reports expected portfolio metrics.

METHODS (per Carver "Smart Portfolios" + López de Prado AFML Ch 16):

1. EQUAL-WEIGHT
   Trivial baseline. w_i = 1/N for all strategies.

2. INVERSE-VOLATILITY (vol-parity)
   w_i ∝ 1 / σ_i, normalized so Σw_i = 1.
   Each strategy contributes equal RISK (not equal capital).
   Robust, doesn't require return forecasts.

3. CARVER HANDCRAFTED
   Group strategies by asset class (or trading style); equal-weight within
   groups; equal-weight across groups. Avoids over-concentration in any one
   asset/style. Robust to return-estimation noise.

4. HIERARCHICAL RISK PARITY (HRP) — López de Prado AFML Ch 16
   Builds a hierarchical clustering on the correlation matrix, then
   recursively bisects, allocating inversely to cluster variance. Falls back
   to inverse-vol if scipy not available.

OUTPUT:
- Weight table per method
- Implied portfolio: expected return, vol, Sharpe (annualized)
- Diversification multiplier (DM) per Carver: ratio of portfolio Sharpe to
  weighted-avg of individual Sharpes. DM > 1 means real diversification.

USAGE:
    uv run python tools/portfolio_optimizer.py --custom NT8Bridge/tools/specs_portfolio.json

Spec format:
    [
      {"label": "ChBVIP_USDJPY", "csv": "C:/Lab/.../trades_xxx.csv", "asset_class": "FX_major"},
      {"label": "ChBVIP_XAUUSD", "csv": "C:/Lab/.../trades_yyy.csv", "asset_class": "metal"},
      ...
    ]

asset_class is optional but used by the Carver handcrafted method.
"""
from __future__ import annotations
import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path


def read_daily_pnl(csv_path: Path) -> dict[str, float]:
    """Aggregate per-trade profits into per-day totals (YYYY-MM-DD keys)."""
    daily: dict[str, float] = defaultdict(float)
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "profit" not in reader.fieldnames or "date" not in reader.fieldnames:
            return {}
        for row in reader:
            try:
                d = row["date"][:10]
                p = float(row["profit"])
                daily[d] += p
            except (ValueError, KeyError):
                continue
    return dict(daily)


def union_calendar(series_list: list[dict[str, float]]) -> list[str]:
    """Return the union of trading dates across all series, sorted."""
    all_dates = set()
    for s in series_list:
        all_dates.update(s.keys())
    return sorted(all_dates)


def aligned_matrix(series_list: list[dict[str, float]], starting_equity: float
                   ) -> tuple[list[str], list[list[float]]]:
    """Build [date_idx][strategy_idx] = daily return (P&L / starting_equity).

    Days where a strategy didn't trade are filled with 0.0 (standard practice).
    """
    dates = union_calendar(series_list)
    matrix: list[list[float]] = []
    for d in dates:
        row = [s.get(d, 0.0) / starting_equity for s in series_list]
        matrix.append(row)
    return dates, matrix


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def covariance_matrix(matrix: list[list[float]]) -> list[list[float]]:
    """Cov matrix between strategies (columns)."""
    n = len(matrix)
    if n < 2:
        return [[0.0]]
    k = len(matrix[0])
    means = [mean([row[j] for row in matrix]) for j in range(k)]
    cov = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            s = sum((matrix[r][i] - means[i]) * (matrix[r][j] - means[j])
                    for r in range(n))
            cov[i][j] = s / (n - 1)
    return cov


def correlation_matrix(cov: list[list[float]]) -> list[list[float]]:
    k = len(cov)
    cor = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            si = math.sqrt(cov[i][i]) if cov[i][i] > 0 else 0
            sj = math.sqrt(cov[j][j]) if cov[j][j] > 0 else 0
            if si > 0 and sj > 0:
                cor[i][j] = cov[i][j] / (si * sj)
    return cor


def portfolio_metrics(weights: list[float], matrix: list[list[float]],
                      cov: list[list[float]]) -> dict:
    """Annualized portfolio return, vol, Sharpe given weights.

    Assumes daily returns; annualizes by sqrt(252) for vol, by 252 for return.
    """
    k = len(weights)
    means = [mean([row[j] for row in matrix]) for j in range(k)]
    port_mean = sum(w * m for w, m in zip(weights, means))
    port_var = sum(weights[i] * weights[j] * cov[i][j]
                   for i in range(k) for j in range(k))
    port_vol = math.sqrt(max(port_var, 0))
    port_sharpe_daily = (port_mean / port_vol) if port_vol > 0 else 0
    return {
        "ann_return_pct": port_mean * 252 * 100,
        "ann_vol_pct": port_vol * math.sqrt(252) * 100,
        "ann_sharpe": port_sharpe_daily * math.sqrt(252),
        "daily_mean": port_mean,
        "daily_vol": port_vol,
    }


def equal_weight(n: int) -> list[float]:
    return [1.0 / n] * n


def inverse_volatility(matrix: list[list[float]]) -> list[float]:
    k = len(matrix[0])
    vols = [stdev([row[j] for row in matrix]) for j in range(k)]
    inv = [1.0 / v if v > 0 else 0.0 for v in vols]
    total = sum(inv)
    return [x / total for x in inv] if total > 0 else equal_weight(k)


def carver_handcrafted(matrix: list[list[float]], asset_classes: list[str]) -> list[float]:
    """Equal weight within asset_class buckets, equal weight across buckets."""
    k = len(matrix[0])
    if not asset_classes or len(asset_classes) != k:
        return equal_weight(k)
    groups: dict[str, list[int]] = defaultdict(list)
    for idx, ac in enumerate(asset_classes):
        groups[ac].append(idx)
    n_groups = len(groups)
    weights = [0.0] * k
    for ac, idxs in groups.items():
        per_group = 1.0 / n_groups
        per_strategy = per_group / len(idxs)
        for i in idxs:
            weights[i] = per_strategy
    return weights


def hrp_weights(matrix: list[list[float]]) -> list[float]:
    """Hierarchical Risk Parity (López de Prado AFML Ch 16).

    Uses scipy if available; otherwise falls back to inverse-volatility.
    Pure-stdlib HRP is non-trivial — we accept the dependency fallback.
    """
    try:
        import numpy as np
        from scipy.cluster.hierarchy import linkage
        from scipy.spatial.distance import squareform
    except ImportError:
        return inverse_volatility(matrix)

    k = len(matrix[0])
    cov = covariance_matrix(matrix)
    cor = correlation_matrix(cov)

    # Distance metric: d_ij = sqrt(0.5 * (1 - rho_ij))
    dist = [[math.sqrt(max(0.5 * (1 - cor[i][j]), 0.0)) for j in range(k)]
            for i in range(k)]
    cov_np = np.array(cov)
    dist_np = np.array(dist)

    try:
        link = linkage(squareform(dist_np, checks=False), method="single")
    except Exception:
        return inverse_volatility(matrix)

    # Quasi-diagonalization (sort linkage tree)
    def get_quasi_diag(link):
        link = link.astype(int)
        sort_ix = list(link[-1, 0:2])
        num_items = link[-1, 3]
        while max(sort_ix) >= num_items:
            new_sort = []
            for i in sort_ix:
                if i < num_items:
                    new_sort.append(i)
                else:
                    new_sort.extend(list(link[i - num_items, 0:2]))
            sort_ix = new_sort
        return sort_ix

    sort_ix = get_quasi_diag(link)

    # Recursive bisection
    def cluster_var(c_ix):
        c_cov = cov_np[np.ix_(c_ix, c_ix)]
        ivp = 1.0 / np.diag(c_cov)
        ivp = ivp / ivp.sum()
        return float(ivp @ c_cov @ ivp)

    weights = np.ones(k)
    clusters = [sort_ix]
    while clusters:
        new_clusters = []
        for c in clusters:
            if len(c) <= 1:
                continue
            mid = len(c) // 2
            left, right = c[:mid], c[mid:]
            v_left = cluster_var(left)
            v_right = cluster_var(right)
            alpha = 1 - v_left / (v_left + v_right) if (v_left + v_right) > 0 else 0.5
            for i in left:
                weights[i] *= alpha
            for i in right:
                weights[i] *= (1 - alpha)
            new_clusters.extend([left, right])
        clusters = new_clusters

    weights = weights / weights.sum()
    return weights.tolist()


def diversification_multiplier(port_sharpe: float, weights: list[float],
                                matrix: list[list[float]], cov: list[list[float]]) -> float:
    """Carver's DM = port_sharpe / weighted_avg_individual_sharpe.

    DM > 1 means combining the strategies reduces variance more than it
    reduces return — real diversification benefit.
    """
    k = len(weights)
    indiv_sharpes = []
    for j in range(k):
        col = [row[j] for row in matrix]
        m = mean(col)
        s = stdev(col)
        sh = (m / s) * math.sqrt(252) if s > 0 else 0
        indiv_sharpes.append(sh)
    weighted_avg = sum(w * sh for w, sh in zip(weights, indiv_sharpes))
    return port_sharpe / weighted_avg if weighted_avg != 0 else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--custom", required=True,
                    help="JSON spec listing strategies to combine")
    ap.add_argument("--equity", type=float, default=50000)
    args = ap.parse_args()

    spec = json.loads(Path(args.custom).read_text(encoding="utf-8"))
    labels = [s["label"] for s in spec]
    csv_paths = [Path(s["csv"]) for s in spec]
    asset_classes = [s.get("asset_class", "default") for s in spec]

    # Load
    series_list = []
    for label, p in zip(labels, csv_paths):
        if not p.is_file():
            print(f"MISSING: {label} ({p})")
            return
        s = read_daily_pnl(p)
        series_list.append(s)
        print(f"Loaded {label}: {len(s)} trading days, "
              f"net P&L ${sum(s.values()):,.0f}")

    print()
    dates, matrix = aligned_matrix(series_list, args.equity)
    cov = covariance_matrix(matrix)
    cor = correlation_matrix(cov)

    print(f"Aligned series: {len(dates)} dates × {len(labels)} strategies")
    print()

    # Per-strategy individual stats
    print("Per-strategy stats:")
    print(f"  {'Label':<30} {'Daily Sh':>10} {'Ann Sh':>8} {'Ann Vol%':>9}")
    for j, label in enumerate(labels):
        col = [row[j] for row in matrix]
        m = mean(col)
        s = stdev(col)
        sh_d = m / s if s > 0 else 0
        sh_a = sh_d * math.sqrt(252)
        vol_a = s * math.sqrt(252) * 100
        print(f"  {label:<30} {sh_d:>10.4f} {sh_a:>8.2f} {vol_a:>8.2f}%")

    print()
    print("Correlation matrix:")
    hdr = " " * 30 + " ".join(f"{i:>6}" for i in range(len(labels)))
    print(hdr)
    for i, label in enumerate(labels):
        row = " ".join(f"{cor[i][j]:>+6.2f}" for j in range(len(labels)))
        print(f"  {i}: {label:<26} {row}")

    print()
    print("=== ALLOCATION METHODS ===\n")

    methods = {
        "Equal-Weight":         equal_weight(len(labels)),
        "Inverse-Volatility":   inverse_volatility(matrix),
        "Carver Handcrafted":   carver_handcrafted(matrix, asset_classes),
        "HRP (López de Prado)": hrp_weights(matrix),
    }

    print(f"{'Method':<25} " + " ".join(f"{lab[:12]:>12}" for lab in labels) + "  Sharpe   Vol%   DM")
    print("-" * (25 + 13 * len(labels) + 30))
    for name, w in methods.items():
        m = portfolio_metrics(w, matrix, cov)
        dm = diversification_multiplier(m["ann_sharpe"], w, matrix, cov)
        weight_str = " ".join(f"{wi*100:>11.1f}%" for wi in w)
        print(f"{name:<25} {weight_str}  {m['ann_sharpe']:>5.2f}  "
              f"{m['ann_vol_pct']:>5.2f}  {dm:>4.2f}")

    print()
    print("Diversification Multiplier (DM) interpretation:")
    print("  DM > 1.0 = real diversification benefit (variance reduces faster than return)")
    print("  DM = 1.0 = no benefit (all strategies perfectly correlated)")
    print("  DM < 1.0 = bug or anti-diversification (rare)")


if __name__ == "__main__":
    main()
