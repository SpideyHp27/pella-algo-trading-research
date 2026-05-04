# Sharing tier: methodology-shareable (pure plumbing, no edge)
"""Pella live-monitor agent.

Cron-driven. Reads MT5 positions + recent deals + account snapshot for every
account in the deployment_config, attributes deals to deployed strategies via
their `magic` numbers, and pushes new trade rows + an account snapshot to the
Aristhrottle dashboard. Solves the live track-record gap.

Design contract:
    - Idempotent: re-running on the same MT5 history produces the same state.
    - Crash-safe: state is written atomically via pella.state.write_json_atomic.
    - Fail-quiet: any unhandled exception is logged + alerted, then sys.exit(0).

CLI:
    python tools/live_monitor_agent.py [--dry-run] [--account ACCOUNT_ID]
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# allow `from pella import ...` when invoked via `python tools/live_monitor_agent.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pella import config, logger as pella_logger, state  # noqa: E402
from pella.clients import AlertClient, AristhrottleClient, MT5Client  # noqa: E402

_AGENT_NAME = "live_monitor"
_log = pella_logger.get_logger(_AGENT_NAME)

# Per-agent state location: <agent_state_dir>/state/live_monitor/last_poll.json
_STATE_FILENAME = "last_poll.json"
_DEFAULT_STATE: dict[str, Any] = {
    "last_poll_ts": None,
    "last_deal_tickets_seen": [],
    "last_account_snapshots": {},
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _state_path() -> Path:
    paths = config.get_paths()
    return Path(paths["agent_state_dir"]) / "state" / _AGENT_NAME / _STATE_FILENAME


def _live_trades_csv_path(strategy_label: str) -> Path:
    paths = config.get_paths()
    return Path(paths["live_trades_dir"]) / f"{strategy_label}.csv"


def _build_magic_to_label(strategies: list[dict]) -> dict[int, str]:
    """Build {magic: label} lookup. If magic collides across strategies, last
    one wins; we log a WARN so the operator can fix the deployment_config."""
    out: dict[int, str] = {}
    seen: dict[int, str] = {}
    for s in strategies:
        magic = int(s.get("magic") or 0)
        label = s.get("label") or ""
        if not magic or not label:
            continue
        if magic in seen and seen[magic] != label:
            _log.warning(
                "magic collision: %s already mapped to %s, overwriting with %s",
                magic, seen[magic], label,
            )
        seen[magic] = label
        out[magic] = label
    return out


def _deal_to_trade_row(deal: dict, strategy_label: str) -> dict:
    """Map a raw MT5 deal dict (from MT5Client.deals) to the Aristhrottle
    trade-row schema documented in tools/aristhrottle_push.py:load_trades_csv.
    """
    ts = deal.get("time")
    iso = ts.isoformat() if isinstance(ts, datetime) else str(ts or "")
    side_raw = (deal.get("type") or "").upper()
    side = "LONG" if side_raw == "BUY" else "SHORT" if side_raw == "SELL" else side_raw
    profit = float(deal.get("profit") or 0.0) + float(deal.get("swap") or 0.0) \
        + float(deal.get("commission") or 0.0)
    volume = float(deal.get("volume") or 0.0)
    price = float(deal.get("price") or 0.0)
    return {
        "strategy": strategy_label,
        "side": side,
        "date": iso,
        "entryDate": iso,
        "exitDate": iso,
        "pnl": profit,
        "profit": profit,
        "symbol": deal.get("symbol") or "",
        "size": volume,
        "volume": volume,
        "entryPrice": price,
        "exitPrice": price,
    }


def _append_live_trades_csv(strategy_label: str, rows: list[dict]) -> None:
    """Append trade rows to <live_trades_dir>/<label>.csv so the
    edge_decay_watchdog can read them locally without hitting Aristhrottle.
    """
    if not rows:
        return
    path = _live_trades_csv_path(strategy_label)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date", "symbol", "side", "profit", "volume",
        "open_price", "close_price", "price_diff", "open_time",
    ]
    write_header = not path.is_file()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        for r in rows:
            iso = r.get("date") or ""
            side = (r.get("side") or "").lower()
            if side == "long":
                side = "long"
            elif side == "short":
                side = "short"
            else:
                side = side or ""
            w.writerow({
                "date": iso[:19] if iso else "",
                "symbol": r.get("symbol") or "",
                "side": side,
                "profit": r.get("pnl") or r.get("profit") or 0.0,
                "volume": r.get("size") or r.get("volume") or 0.0,
                "open_price": r.get("entryPrice") or 0.0,
                "close_price": r.get("exitPrice") or 0.0,
                "price_diff": 0.0,
                "open_time": iso[:19] if iso else "",
            })


def _compute_dd_pct(account: dict, equity: float) -> float:
    starting = float(account.get("starting_balance") or 0.0)
    if starting <= 0:
        return 0.0
    return max(0.0, (starting - equity) / starting * 100.0)


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------
def _process_account(
    account_id: str,
    account: dict,
    magic_to_label: dict[int, str],
    persisted: dict,
    aris: AristhrottleClient,
    alerts: AlertClient,
    monitoring: dict,
    dry_run: bool,
) -> tuple[int, dict]:
    """Process a single account; returns (n_new_deals, updated_persisted_for_this_account)."""
    seen_tickets: set[int] = set(int(t) for t in persisted.get("last_deal_tickets_seen", []))
    last_poll_ts_iso = persisted.get("last_poll_ts")
    now = datetime.now(timezone.utc)

    if last_poll_ts_iso:
        try:
            from_dt = datetime.fromisoformat(last_poll_ts_iso)
            if from_dt.tzinfo is None:
                from_dt = from_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            from_dt = now - timedelta(hours=24)
    else:
        from_dt = now - timedelta(hours=24)

    n_new_deals = 0
    new_tickets: set[int] = set()
    margin_warn_pct = float(monitoring.get("margin_level_warn_pct") or 200.0)

    with MT5Client() as mt5:
        positions = mt5.positions()
        deals = mt5.deals(from_dt=from_dt, to_dt=now)
        info = mt5.account_info()

    # --- per-strategy bucket of new deals ---
    by_label: dict[str, list[dict]] = {}
    for d in deals:
        try:
            ticket = int(d.get("ticket") or 0)
        except (TypeError, ValueError):
            ticket = 0
        if not ticket or ticket in seen_tickets:
            continue
        # only OUT entries are realised P/L closures
        entry = d.get("entry") or ""
        if entry not in ("OUT", "OUT_BY", "INOUT"):
            continue
        magic = int(d.get("magic") or 0)
        label = magic_to_label.get(magic) or ("manual" if magic == 0 else None)
        if label is None:
            continue  # unattributed; not ours
        new_tickets.add(ticket)
        by_label.setdefault(label, []).append(_deal_to_trade_row(d, label))
        n_new_deals += 1

    # --- push trades per strategy ---
    for label, rows in by_label.items():
        if dry_run:
            print(f"  [DRY] would push {len(rows)} trade(s) to Aristhrottle for {label}")
            continue
        code, body = aris.append_trades(label, rows)
        _log.event("INFO", "trades_pushed", account=account_id, strategy=label,
                   n_rows=len(rows), http_status=code)
        _append_live_trades_csv(label, rows)

    # --- account snapshot ---
    snap_payload: dict[str, Any] = {}
    if info:
        balance = float(info.get("balance") or 0.0)
        equity = float(info.get("equity") or 0.0)
        margin_level = float(info.get("margin_level") or 0.0)
        dd_pct = _compute_dd_pct(account, equity)
        snap_payload = {
            "balance": balance,
            "equity": equity,
            "dd_pct": dd_pct,
            "margin_level": margin_level,
            "n_open_positions": len(positions),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if dry_run:
            print(f"  [DRY] would push snapshot for {account_id}: {snap_payload}")
        else:
            code, _ = aris.update_account_snapshot(
                account_id=account_id, balance=balance, equity=equity,
                dd_pct=dd_pct, margin_level=margin_level,
            )
            _log.event("INFO", "snapshot_pushed", account=account_id,
                       balance=balance, equity=equity, dd_pct=round(dd_pct, 3),
                       margin_level=margin_level, http_status=code)

        # margin level alert (0 means no open positions on most brokers; ignore)
        if 0.0 < margin_level < margin_warn_pct:
            alerts.alert(
                "WARN",
                f"{account_id} margin level low",
                f"margin_level={margin_level:.1f}% < threshold={margin_warn_pct:.0f}%; "
                f"equity={equity:.2f}, balance={balance:.2f}",
            )

    seen_tickets |= new_tickets
    return n_new_deals, {
        "last_deal_tickets_seen": sorted(seen_tickets)[-5000:],  # cap memory
        "last_account_snapshot": snap_payload,
    }


def main(dry_run: bool = False, only_account: str | None = None) -> int:
    """Run one polling pass. Returns exit code (0 always — fail-quiet)."""
    started = time.monotonic()
    alerts = AlertClient(_AGENT_NAME)

    try:
        paths = config.get_paths()
        monitoring = config.get_monitoring().get("live_monitor", {}) or {}
        strategies = config.get_strategies()
        accounts = config.get_deployment().get("accounts", {}) or {}
        magic_to_label = _build_magic_to_label(strategies)

        persisted = state.read_json(_state_path(), default=dict(_DEFAULT_STATE))
        persisted.setdefault("last_poll_ts", None)
        persisted.setdefault("last_deal_tickets_seen", [])
        persisted.setdefault("last_account_snapshots", {})

        # Tickets are global (MT5 deal tickets are unique system-wide); store
        # in one set across accounts so dedup works regardless of which account
        # the deal landed under.
        global_seen = set(int(t) for t in persisted.get("last_deal_tickets_seen", []))

        aris = AristhrottleClient()
        n_total_new = 0
        n_accounts_processed = 0

        for acct_id, acct in accounts.items():
            if only_account and acct_id != only_account:
                continue

            # Per-account view shares the global ticket cache so dedup is consistent
            per_acct_persisted = {
                "last_poll_ts": persisted.get("last_poll_ts"),
                "last_deal_tickets_seen": list(global_seen),
            }
            try:
                n_new, updated = _process_account(
                    account_id=acct_id, account=acct,
                    magic_to_label=magic_to_label,
                    persisted=per_acct_persisted,
                    aris=aris, alerts=alerts,
                    monitoring=monitoring, dry_run=dry_run,
                )
            except Exception as e:
                _log.warning("account %s processing raised: %s", acct_id, e)
                _log.event("WARN", "account_error", account=acct_id, error=str(e))
                continue

            n_total_new += n_new
            n_accounts_processed += 1
            global_seen.update(int(t) for t in updated.get("last_deal_tickets_seen", []))
            if updated.get("last_account_snapshot"):
                persisted["last_account_snapshots"][acct_id] = updated["last_account_snapshot"]

        # write updated state atomically (skip writes during dry-run)
        runtime = round(time.monotonic() - started, 3)
        if not dry_run:
            persisted["last_poll_ts"] = datetime.now(timezone.utc).isoformat()
            persisted["last_deal_tickets_seen"] = sorted(global_seen)[-5000:]
            state.write_json_atomic(_state_path(), persisted)

        _log.event(
            "INFO", "poll_complete",
            n_new_deals=n_total_new,
            n_accounts=n_accounts_processed,
            n_strategies_mapped=len(magic_to_label),
            runtime_seconds=runtime,
            dry_run=dry_run,
        )
        print(
            f"live_monitor: poll_complete n_new_deals={n_total_new} "
            f"n_accounts={n_accounts_processed} runtime={runtime}s "
            f"dry_run={dry_run}"
        )
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        _log.warning("live_monitor.main() crashed: %s", e)
        _log.event("WARN", "agent_crash", error=str(e), traceback=tb)
        try:
            alerts.alert("WARN", "live_monitor crashed", f"{e}\n\n{tb}")
        except Exception:
            pass
        return 0


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not push to Aristhrottle, do not write state.")
    ap.add_argument("--account", default=None,
                    help="Process only this account_id (default: all).")
    args = ap.parse_args()
    rc = main(dry_run=args.dry_run, only_account=args.account)
    sys.exit(rc)


if __name__ == "__main__":
    _cli()
