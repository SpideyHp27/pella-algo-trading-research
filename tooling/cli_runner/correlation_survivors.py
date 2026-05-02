#!/usr/bin/env python3
"""Correlation matrix for the Pella surviving portfolio.

Reusable wrapper over correlation_matrix.py — defines the canonical 5-spec
survivor list and prints the matrix + Carver/Clenow verdict.

Update the SURVIVORS list as new specs graduate or get demoted.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from correlation_matrix import daily_pnl, aligned_series, pearson


SURVIVORS = [
    ("ChBVIP_USDJPY_H1_PCT",
     r"C:\Lab\NT8Bridge\Results\cli_validation\20260502_171816\trades_ChBVIP_USDJPY_PCT1.csv"),
    ("ChBVIP_XAUUSD_H1_PCT",
     r"C:\Lab\NT8Bridge\Results\cli_validation\20260502_171816\trades_ChBVIP_XAUUSD_PCT1.csv"),
    ("TT_NDX_H1_FIXED",
     r"C:\Lab\NT8Bridge\Results\cli_validation\20260502_173436\trades_TT_NDX_FIXED.csv"),
    ("TT_NDX_H1_PCT",
     r"C:\Lab\NT8Bridge\Results\cli_validation\20260502_173436\trades_TT_NDX_PCT1.csv"),
    ("IDNR4_XAUUSD_H4_PCT",
     r"C:\Lab\NT8Bridge\Results\cli_validation\20260502_182352\trades_IDNR4_XAUUSD_H4_PCT1.csv"),
]


def main():
    series = {}
    for name, p in SURVIVORS:
        path = Path(p)
        if not path.is_file():
            print(f"MISSING: {name} ({p})")
            continue
        s = daily_pnl(path)
        series[name] = s

    names = list(series.keys())
    n = len(names)

    print("\nLoaded series:")
    for nm in names:
        print(f"  {nm:<25} {len(series[nm])} trading days")
    print()

    matrix = [[None] * n for _ in range(n)]
    overlap = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 1.0
            elif j < i:
                matrix[i][j] = matrix[j][i]
                overlap[i][j] = overlap[j][i]
            else:
                xs, ys = aligned_series(series[names[i]], series[names[j]])
                overlap[i][j] = len(xs)
                if len(xs) < 5:
                    matrix[i][j] = float("nan")
                else:
                    matrix[i][j] = pearson(xs, ys)

    # Heatmap
    print(" " * 27 + "".join(f"{i:>8}" for i in range(n)))
    for i, nm in enumerate(names):
        cells = ""
        for j in range(n):
            v = matrix[i][j]
            cells += "    --  " if (v is None or v != v) else f"{v:>+8.2f}"
        print(f"{i}: {nm:<24} {cells}")

    # Pair table
    print("\nPair verdicts (sorted desc):")
    print("-" * 95)
    print(f"{'Pair':<58} {'Corr':>6}  {'Days':>5}  Verdict")
    print("-" * 95)
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            r = matrix[i][j]
            if r is None or r != r:
                continue
            pairs.append((names[i], names[j], r, overlap[i][j]))
    pairs.sort(key=lambda x: x[2], reverse=True)
    for a, b, r, d in pairs:
        if r > 0.60:
            v = "REDUNDANT — same return stream"
        elif r > 0.35:
            v = "warm — same-regime correlation"
        elif r < -0.30:
            v = "negative diversifier"
        else:
            v = "INDEPENDENT (good portfolio fit)"
        print(f"{(a + ' x ' + b):<58} {r:>+6.2f}  {d:>5}  {v}")

    avg = sum(r for _, _, r, _ in pairs) / len(pairs) if pairs else 0
    print(f"\nAverage pairwise correlation: {avg:+.3f}")
    print(f"Pipeline v1.3 / Carver gate: avg < 0.35 = PASS, any pair > 0.60 = REDUNDANT")
    print(f"Portfolio diversification verdict: {'PASS' if avg < 0.35 else 'WARN'}")


if __name__ == "__main__":
    main()
