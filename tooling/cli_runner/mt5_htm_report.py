#!/usr/bin/env python3
"""Parse MT5 Tester HTM reports for ACCURATE per-trade profits.

WHY THIS EXISTS:
  mt5_tester_report.py estimates profits from price * volume * contract_size.
  This works for FX (well-known contract sizes) but is fragile for indices
  (NDX, SP500, WS30, etc.) where contract sizes vary by broker. We discovered
  this when TT NDX PCT showed $14k CSV profit but $128k MT5 actual — 9× under.

HOW THIS WORKS:
  When MT5's tester completes a run, it writes a `.htm` report file at
  <MT5_DATA>/tester_<label>.htm (we set Report=tester_<label> in tester.ini).
  This HTM contains a Deals table with REAL Profit, Commission, Swap,
  Balance columns — no estimation needed.

  This parser:
  1. Locates the HTM by spec label
  2. Reads the UTF-16 LE HTML
  3. Parses the Deals table rows
  4. Pairs entries with exits to build trade records
  5. Outputs CSV in the same schema as mt5_tester_report.py — drop-in
     replacement that uses real profits.

USAGE:
    uv run python tools/mt5_htm_report.py --label BLHCLC_NDX_Tick_DiscordRepro \
        --csv path/to/output.csv
"""
from __future__ import annotations
import argparse
import csv
import re
import sys
from pathlib import Path

MT5_DATA_DEFAULT = r"C:\Users\hoysa\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075"


def read_htm(path: Path) -> str:
    """Read UTF-16 LE HTM file → UTF-8 string."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-16-le", errors="replace") if b"\x00" in raw[:100] \
        else raw.decode("utf-8", errors="replace")


# Match a deal row in the HTM Deals table.
# Columns (in order, per MT5 standard format):
#   Time, Deal#, Symbol, Type (buy/sell/balance), Direction (in/out/in_out),
#   Volume, Price, Order#, Commission, Swap, Profit, Balance, Comment
DEAL_ROW = re.compile(
    r'<tr[^>]*>\s*'
    r'<td>([\d.]+\s+[\d:]+)</td>\s*'      # Time
    r'<td>(\d+)</td>\s*'                  # Deal#
    r'<td>([^<]*)</td>\s*'                # Symbol
    r'<td>([^<]*)</td>\s*'                # Type
    r'<td>([^<]*)</td>\s*'                # Direction
    r'<td>([\d.]*)</td>\s*'               # Volume
    r'<td>([\d.]*)</td>\s*'               # Price
    r'<td>(\d+)</td>\s*'                  # Order#
    r'<td>([-\d.\s ]+)</td>\s*'           # Commission
    r'<td>([-\d.\s ]+)</td>\s*'           # Swap
    r'<td>([-\d.\s ]+)</td>\s*'           # Profit
    r'<td>([-\d.\s ]+)</td>\s*'           # Balance
    r'<td>([^<]*)</td>\s*'                # Comment
    r'</tr>',
    re.DOTALL
)


def _num(s: str) -> float:
    """Parse MT5's number format (thousands with space, decimal as '.')."""
    s = s.replace(" ", "").replace("&nbsp;", "").strip()
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_deals(html: str) -> list[dict]:
    """Extract all deal rows from the Deals table."""
    out = []
    for m in DEAL_ROW.finditer(html):
        time_s, deal_no, symbol, dtype, direction, vol, price, order_no, \
            commission, swap, profit, balance, comment = m.groups()
        out.append({
            "time": time_s,
            "deal_no": int(deal_no),
            "symbol": symbol.strip(),
            "type": dtype.strip().lower(),
            "direction": direction.strip().lower(),
            "volume": _num(vol),
            "price": _num(price),
            "order_no": int(order_no),
            "commission": _num(commission),
            "swap": _num(swap),
            "profit": _num(profit),         # REAL MT5-computed profit
            "balance": _num(balance),
            "comment": comment.strip(),
        })
    return out


def pair_deals_to_trades(deals: list[dict]) -> list[dict]:
    """Build matched trades by pairing entries with exits.

    MT5 deal direction:
      'in' = position opening
      'out' = position closing
      'in_out' = position reversal

    For 'out' deals, the profit is realized on that deal — we attribute it to
    the matching entry. Commission + swap on entry + exit summed.
    """
    trades = []
    open_positions = []  # FIFO: list of entry deals waiting for an exit
    for d in deals:
        if d["type"] == "balance":
            continue  # initial deposit, not a trade
        if d["direction"] == "in":
            open_positions.append(d)
        elif d["direction"] in ("out", "in_out"):
            # Find matching entry (same symbol, opposite type, FIFO)
            opposite = "sell" if d["type"] == "buy" else "buy"
            entry = None
            for p in open_positions:
                if p["symbol"] == d["symbol"] and p["type"] == opposite \
                        and abs(p["volume"] - d["volume"]) < 1e-9:
                    entry = p
                    break
            if entry:
                open_positions.remove(entry)
                # Side from the ENTRY perspective
                side = "long" if entry["type"] == "buy" else "short"
                gross_profit = d["profit"]
                total_commission = entry["commission"] + d["commission"]
                total_swap = entry["swap"] + d["swap"]
                net_profit = gross_profit + total_commission + total_swap
                trades.append({
                    "open_time": entry["time"],
                    "close_time": d["time"],
                    "symbol": entry["symbol"],
                    "side": side,
                    "volume": entry["volume"],
                    "open_price": entry["price"],
                    "close_price": d["price"],
                    "gross_profit": round(gross_profit, 2),
                    "commission": round(total_commission, 2),
                    "swap": round(total_swap, 2),
                    "profit": round(net_profit, 2),  # net of cost
                })
    return trades


def write_csv(trades: list[dict], csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "symbol", "side", "profit", "volume",
                    "open_price", "close_price", "price_diff", "open_time",
                    "gross_profit", "commission", "swap"])
        for t in trades:
            price_diff = (t["close_price"] - t["open_price"]) if t["side"] == "long" \
                         else (t["open_price"] - t["close_price"])
            w.writerow([
                t["close_time"].replace(".", "-"),
                t["symbol"], t["side"],
                t["profit"], t["volume"],
                t["open_price"], t["close_price"],
                round(price_diff, 6),
                t["open_time"].replace(".", "-"),
                t["gross_profit"], t["commission"], t["swap"],
            ])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True,
                    help="Spec label as used in tester.ini Report= line")
    ap.add_argument("--csv", required=True, help="Output trades CSV path")
    ap.add_argument("--mt5-data", default=MT5_DATA_DEFAULT)
    args = ap.parse_args()

    htm_path = Path(args.mt5_data) / f"tester_{args.label}.htm"
    if not htm_path.is_file():
        sys.exit(f"HTM report not found: {htm_path}\n"
                 f"Was the spec.label set to '{args.label}' and Report= "
                 f"line generated in tester.ini?")

    html = read_htm(htm_path)
    deals = parse_deals(html)
    trades = pair_deals_to_trades(deals)

    # Quick summary for stderr
    n_trades = len(trades)
    if n_trades:
        gross = sum(t["gross_profit"] for t in trades)
        commission = sum(t["commission"] for t in trades)
        swap = sum(t["swap"] for t in trades)
        net = sum(t["profit"] for t in trades)
        wins = sum(1 for t in trades if t["profit"] > 0)
        print(f"Parsed {n_trades} trades from {htm_path.name}", file=sys.stderr)
        print(f"  Gross profit: ${gross:,.2f}", file=sys.stderr)
        print(f"  Commission:   ${commission:,.2f}", file=sys.stderr)
        print(f"  Swap:         ${swap:,.2f}", file=sys.stderr)
        print(f"  Net profit:   ${net:,.2f}", file=sys.stderr)
        print(f"  Win rate:     {wins/n_trades*100:.2f}%", file=sys.stderr)

    write_csv(trades, Path(args.csv))
    print(f"CSV written: {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
