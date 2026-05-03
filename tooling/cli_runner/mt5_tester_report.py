"""
Parse the MT5 Strategy Tester log to recover the full trade list and
metrics for the most recent test — no manual export required.

Usage:
    uv run python mt5_tester_report.py                # latest test
    uv run python mt5_tester_report.py --strategy X   # latest test of EA X

Reads:  C:/Users/hoysa/AppData/Roaming/MetaQuotes/Terminal/D0E8209F77C8CF37AD8BF550E51FF075/Tester/logs/<YYYYMMDD>.log
Writes: stdout JSON with trades + computed metrics.
"""

from __future__ import annotations
import argparse, glob, io, json, os, re, sys
from collections import defaultdict
from datetime import datetime, date
from math import sqrt

LOG_DIR = r"C:\Users\hoysa\AppData\Roaming\MetaQuotes\Terminal\D0E8209F77C8CF37AD8BF550E51FF075\Tester\logs"


def latest_log() -> str:
    files = sorted(glob.glob(os.path.join(LOG_DIR, "*.log")))
    if not files:
        sys.exit(f"No tester logs in {LOG_DIR}")
    return files[-1]


def read_utf16(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


# ── Log line patterns (whitespace tolerant) ───────────────────────────
TEST_HEADER = re.compile(
    r"testing of Experts\\(.+?\.ex5) from ([\d.]+) [\d:]+ to ([\d.]+) [\d:]+"
)
START_INPUTS = re.compile(r"started with inputs:")
SETTING_LINE = re.compile(r"^\s+(\w+)=(.+)$")
DEAL_PERFORMED = re.compile(
    r"(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2}).*?deal performed \[#(\d+) (buy|sell) ([\d.]+) (\w+) at ([\d.]+)\]"
)
FINAL_BALANCE = re.compile(r"final balance ([\d.\-]+)")
TEST_PASSED = re.compile(r"Test passed in")


def parse_test_segments(text: str):
    """Yield (start_idx, end_idx, header_match) for each test in the log."""
    lines = text.splitlines()
    headers = []
    for i, ln in enumerate(lines):
        m = TEST_HEADER.search(ln)
        if m:
            headers.append((i, m))
    for k, (idx, m) in enumerate(headers):
        end = headers[k + 1][0] if k + 1 < len(headers) else len(lines)
        yield idx, end, m, lines


def parse_segment(start: int, end: int, header_m, lines):
    expert = header_m.group(1)
    test_start = header_m.group(2)
    test_end = header_m.group(3)

    inputs = {}
    in_inputs = False
    deals = []          # raw entries/exits with timestamps
    final_balance = None

    for ln in lines[start:end]:
        if START_INPUTS.search(ln):
            in_inputs = True
            continue
        if in_inputs:
            sm = SETTING_LINE.match(ln)
            if sm:
                inputs[sm.group(1)] = sm.group(2).strip()
                continue
            else:
                # End of inputs block when we hit the first non-input log line
                if ln.strip() and not ln.strip().startswith(("CF", "JS", "EJ", "MF", "KS", "CG", "CJ")):
                    in_inputs = False

        dm = DEAL_PERFORMED.search(ln)
        if dm:
            ts, deal_id, side, vol, sym, price = dm.groups()
            deals.append({
                "time": datetime.strptime(ts, "%Y.%m.%d %H:%M:%S"),
                "deal_id": int(deal_id),
                "side": side,           # buy / sell
                "volume": float(vol),
                "symbol": sym,
                "price": float(price),
            })
            continue

        fb = FINAL_BALANCE.search(ln)
        if fb:
            final_balance = float(fb.group(1))

    return {
        "expert": expert,
        "start": test_start,
        "end": test_end,
        "inputs": inputs,
        "deals": deals,
        "final_balance": final_balance,
    }


# Contract size by symbol family — empirically verified from MT5 HTM reports
# IMPORTANT: index contract sizes vary by broker. These are for Darwinex Demo.
# For 100% accurate per-trade profits use mt5_htm_report.py instead — it reads
# the REAL profit values MT5 already computed (no estimation needed).
CONTRACT_SIZE = {
    # FX majors / minors
    "default_fx": 100_000,
    # Metals
    "XAUUSD": 100,
    "XAGUSD": 5_000,
    # Indices on Darwinex Demo (verified by comparing CSV vs HTM Profit column)
    "NDX": 10,        # NASDAQ-100 CFD: 1 point × 1 lot = $10. Was 1 (10× under)
    "SP500": 50,      # S&P 500 CFD typical
    "WS30": 5,        # Dow Jones CFD typical
    "default_index": 10,  # safer default than 1 (was the source of NDX bug)
}


def _index_contract(symbol: str) -> float:
    """Look up index contract size, fall back to default_index."""
    return CONTRACT_SIZE.get(symbol, CONTRACT_SIZE["default_index"])


def estimate_usd_profit(symbol: str, side: str, volume: float,
                        open_price: float, close_price: float) -> float:
    """Approximate USD profit for a closed trade.

    Heuristics by symbol class. Good enough for Sharpe/DD computation;
    cents of error vs broker's exact P&L is acceptable for screening.
    """
    price_diff = (close_price - open_price) if side == "long" \
                 else (open_price - close_price)

    # Metals XAUUSD / XAGUSD — quote in USD. CHECK FIRST: these match
    # endswith("USD") so they would otherwise fall into the FX branch and
    # use the 100_000 contract size (1000x too large for gold, 20x for silver).
    if symbol == "XAUUSD":
        return price_diff * volume * CONTRACT_SIZE["XAUUSD"]
    if symbol == "XAGUSD":
        return price_diff * volume * CONTRACT_SIZE["XAGUSD"]

    # USD-quoted (USD is base, X is quote): e.g. USDJPY, USDCHF, USDCAD
    # P&L in USD = price_diff * volume * contract / close_price
    if symbol.startswith("USD") and len(symbol) == 6:
        contract = CONTRACT_SIZE["default_fx"]
        return price_diff * volume * contract / close_price

    # Quote-USD pair (XXX/USD): EURUSD, GBPUSD, AUDUSD, NZDUSD
    # P&L in USD = price_diff * volume * contract
    if symbol.endswith("USD") and len(symbol) == 6:
        contract = CONTRACT_SIZE["default_fx"]
        return price_diff * volume * contract

    # Cross-pair without USD (e.g. EURJPY, GBPCHF) — need second conversion;
    # use rough approximation via price_diff * volume * 100_000 / close_price.
    # Will be off by USD/quote_ccy conversion. Fix later per-pair.
    if len(symbol) == 6:
        return price_diff * volume * 100_000 / close_price

    # Indices, futures, others — look up symbol-specific contract size,
    # fall back to default_index (10) which works for NDX-class CFDs
    return price_diff * volume * _index_contract(symbol)


def pair_trades(deals):
    """Build matched trades from raw deals (FIFO per symbol+side)."""
    open_pos = []   # (deal,) entries waiting for opposite-side close
    trades = []
    for d in deals:
        opposite = next((p for p in open_pos
                         if p["symbol"] == d["symbol"] and p["side"] != d["side"]
                         and abs(p["volume"] - d["volume"]) < 1e-9), None)
        if opposite:
            entry = opposite
            exit_ = d
            open_pos.remove(opposite)
            direction = "long" if entry["side"] == "buy" else "short"
            price_diff = (exit_["price"] - entry["price"]) if direction == "long" \
                         else (entry["price"] - exit_["price"])
            profit_usd = estimate_usd_profit(entry["symbol"], direction,
                                             entry["volume"],
                                             entry["price"], exit_["price"])
            trades.append({
                "open_time": entry["time"].strftime("%Y-%m-%d %H:%M:%S"),
                "close_time": exit_["time"].strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": entry["symbol"],
                "side": direction,
                "volume": entry["volume"],
                "open_price": entry["price"],
                "close_price": exit_["price"],
                "price_diff": round(price_diff, 6),
                "profit": round(profit_usd, 2),
            })
        else:
            open_pos.append(d)
    return trades


def compute_metrics(trades, deposit_estimate=50000):
    """Compute basic metrics — uses price_diff as proxy for relative profit.

    NOTE: for $-denominated metrics we need broker tick-value, which the
    log doesn't expose. The CSV/HTML still gives QuantDash everything
    it needs to recompute Sharpe etc.
    """
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    diffs = [t["price_diff"] for t in trades]
    wins = sum(1 for d in diffs if d > 0)
    losses = sum(1 for d in diffs if d <= 0)
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n * 100, 2),
        "sum_price_diff": round(sum(diffs), 6),
        "avg_win_price_diff": round(sum(d for d in diffs if d > 0) / wins, 6) if wins else 0,
        "avg_loss_price_diff": round(sum(d for d in diffs if d < 0) / losses, 6) if losses else 0,
        "longs": sum(1 for t in trades if t["side"] == "long"),
        "shorts": sum(1 for t in trades if t["side"] == "short"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=None, help="path to tester log (default: latest)")
    ap.add_argument("--strategy", default=None, help="filter to latest test of this EA")
    ap.add_argument("--all", action="store_true", help="emit every test in the log")
    ap.add_argument("--csv", default=None, help="also write trades to this CSV")
    args = ap.parse_args()

    log_path = args.log or latest_log()
    text = read_utf16(log_path)
    segments = list(parse_test_segments(text))
    if not segments:
        sys.exit(f"No tests found in {log_path}")

    parsed = [parse_segment(s, e, m, lines) for s, e, m, lines in segments]

    if args.strategy:
        parsed = [p for p in parsed if p["expert"].startswith(args.strategy)]
        if not parsed:
            sys.exit(f"No tests of '{args.strategy}' found")

    target = parsed if args.all else [parsed[-1]]

    out = []
    for p in target:
        trades = pair_trades(p["deals"])
        out.append({
            "log": log_path,
            "expert": p["expert"],
            "period": f"{p['start']} -> {p['end']}",
            "inputs": p["inputs"],
            "final_balance": p["final_balance"],
            "deal_count": len(p["deals"]),
            "trade_count": len(trades),
            "metrics": compute_metrics(trades),
            "trades": trades,
        })

    # CSV
    if args.csv and out:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "symbol", "side", "profit", "volume",
                        "open_price", "close_price", "price_diff", "open_time"])
            for r in out:
                for t in r["trades"]:
                    w.writerow([t["close_time"], t["symbol"], t["side"],
                                t["profit"], t["volume"],
                                t["open_price"], t["close_price"],
                                t["price_diff"], t["open_time"]])
        print(f"CSV written: {args.csv}", file=sys.stderr)

    # Compact JSON: hide trades unless --all (keeps stdout sane)
    if args.all:
        print(json.dumps(out, indent=2, default=str))
    else:
        # Single most-recent test: full detail
        print(json.dumps(out[0], indent=2, default=str))


if __name__ == "__main__":
    main()
