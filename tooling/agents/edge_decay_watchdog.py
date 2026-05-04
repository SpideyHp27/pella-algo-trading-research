# Sharing tier: methodology-shareable (pure plumbing, no edge)
"""Pella edge-decay watchdog.

Daily cron. For each deployed strategy:

    1. Build a rolling 90-day trade pool from the baseline CSV + any live
       trades CSV in <paths.live_trades_dir>.
    2. Compute Sharpe via tools/quant_report.py:compute_report (re-used, not
       re-implemented).
    3. Apply a verdict (OK / WARN / KILL / INSUFFICIENT_DATA) per
       monitoring.edge_decay thresholds in agent_config.yaml.
    4. WARN -> AlertClient.alert("WARN", ...).
    5. KILL -> AlertClient.alert("KILL", ...) PLUS create the strategy's
       pause_flag file (touch). EAs read this on day-2 to halt new entries.
    6. Persist all rows to <agent_state_dir>/state/edge_decay/YYYY-MM-DD.json
       atomically + append a markdown table to <research_logs>/edge_decay/.

CLI:
    python tools/edge_decay_watchdog.py [--dry-run] [--strategy LABEL]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# allow `from pella import ...` regardless of cwd
_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from pella import config, logger as pella_logger, state  # noqa: E402
from pella.clients import AlertClient, AristhrottleClient  # noqa: E402

# Re-use the quant report so we don't re-implement Sharpe.
from quant_report import compute_report  # noqa: E402

_AGENT_NAME = "edge_decay"
_log = pella_logger.get_logger(_AGENT_NAME)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolve_baseline_csv(rel_or_abs: str) -> Path:
    """deployment_config.json stores baseline_csv as a path relative to
    NT8Bridge/. Resolve to an absolute Path."""
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    paths = config.get_paths()
    return Path(paths["ntbridge_root"]) / rel_or_abs


def _resolve_pause_flag(strategy: dict) -> Path:
    """pause_flag_path is relative to lab_root in deployment_config."""
    raw = strategy.get("pause_flag_path") or ""
    p = Path(raw)
    if p.is_absolute():
        return p
    paths = config.get_paths()
    return Path(paths["lab_root"]) / raw


def _read_trade_csv_filtered(path: Path, cutoff: datetime) -> list[dict]:
    """Read a Pella trades CSV, return rows with `date >= cutoff`.

    The CSV's `date` field is the close timestamp ("YYYY-MM-DD HH:MM:SS").
    Falls back to entire file if `date` is missing/unparseable.
    """
    if not path.is_file():
        return []
    out: list[dict] = []
    cutoff_naive = cutoff.replace(tzinfo=None)
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = _parse_csv_date(row.get("date"))
            if dt is None:
                # keep the row; don't lose data on a parse miss
                out.append(row)
                continue
            if dt >= cutoff_naive:
                out.append(row)
    return out


def _parse_csv_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    # try iso w/ tz
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def _build_rolling_pool(
    strategy: dict, rolling_days: int,
) -> tuple[Path | None, int]:
    """Materialise the rolling-`rolling_days` trade pool to a temp CSV the
    quant_report tool can read. Returns (tmp_csv_path, n_rows).

    Pool = baseline CSV (trade rows within window) + live trades CSV (within
    window). If neither exists or both are empty, returns (None, 0).
    """
    paths = config.get_paths()
    label = strategy.get("label") or ""
    baseline_csv = _resolve_baseline_csv(strategy.get("baseline_csv") or "")
    live_csv = Path(paths["live_trades_dir"]) / f"{label}.csv"

    cutoff = datetime.now(timezone.utc) - timedelta(days=rolling_days)
    rows: list[dict] = []
    rows.extend(_read_trade_csv_filtered(baseline_csv, cutoff))
    rows.extend(_read_trade_csv_filtered(live_csv, cutoff))

    if not rows:
        return None, 0

    fieldnames = [
        "date", "symbol", "side", "profit", "volume",
        "open_price", "close_price", "price_diff", "open_time",
    ]
    tmp = Path(tempfile.gettempdir()) / f"pella_edge_decay_{label}.csv"
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row_clean = {k: r.get(k, "") for k in fieldnames}
            w.writerow(row_clean)
    return tmp, len(rows)


def _verdict_for(
    sharpe_90d: float, baseline_sharpe: float, n_trades: int,
    monitoring: dict,
) -> tuple[str, float]:
    """Return (verdict, drop_pct).

    Verdicts: OK, WARN, KILL, INSUFFICIENT_DATA.
    """
    min_for_verdict = int(monitoring.get("min_trades_for_verdict") or 20)
    min_for_kill = int(monitoring.get("min_trades_for_kill") or 30)
    warn_drop_pct = float(monitoring.get("sharpe_warn_drop_pct") or 50.0)
    kill_threshold = float(monitoring.get("sharpe_kill_threshold") or 0.0)

    drop_pct = 0.0
    if baseline_sharpe and baseline_sharpe > 0:
        drop_pct = max(0.0, (baseline_sharpe - sharpe_90d) / baseline_sharpe * 100.0)

    if n_trades < min_for_verdict:
        return "INSUFFICIENT_DATA", drop_pct

    # KILL takes precedence
    if n_trades >= min_for_kill and sharpe_90d < kill_threshold:
        return "KILL", drop_pct

    # WARN if drop crosses threshold (regardless of trade count above min_for_verdict)
    if drop_pct > warn_drop_pct:
        return "WARN", drop_pct

    return "OK", drop_pct


def _state_path_for(today: datetime) -> Path:
    paths = config.get_paths()
    return Path(paths["agent_state_dir"]) / "state" / _AGENT_NAME / f"{today:%Y-%m-%d}.json"


def _markdown_path_for(today: datetime) -> Path:
    paths = config.get_paths()
    return Path(paths["research_logs"]) / _AGENT_NAME / f"{today:%Y-%m-%d}.md"


def _append_markdown(rows: list[dict], today: datetime) -> Path:
    md_path = _markdown_path_for(today)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if not md_path.exists():
        lines.append(f"# Edge-decay scan {today:%Y-%m-%d}\n")
    lines.append(f"\n## Run @ {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n")
    lines.append("| label | n_trades_90d | sharpe_90d | baseline_sharpe | drop_pct | verdict | pause_flag |\n")
    lines.append("|---|---:|---:|---:|---:|---|---|\n")
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['n_trades_90d']} | {r['sharpe_90d']} | "
            f"{r['baseline_sharpe']} | {r['drop_pct']:.1f}% | {r['verdict']} | "
            f"{r['pause_flag_action']} |\n"
        )
    with md_path.open("a", encoding="utf-8") as f:
        f.writelines(lines)
    return md_path


def _touch_pause_flag(strategy: dict) -> str:
    """Atomic-touch the strategy pause flag. Returns action string."""
    flag = _resolve_pause_flag(strategy)
    if not str(flag):
        return "no_path_configured"
    flag.parent.mkdir(parents=True, exist_ok=True)
    if flag.exists():
        return "already_set"
    payload = {
        "set_by": _AGENT_NAME,
        "set_at": datetime.now(timezone.utc).isoformat(),
        "label": strategy.get("label"),
        "reason": "edge_decay_kill",
    }
    state.write_json_atomic(flag, payload)
    return "created"


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------
def _process_strategy(
    strategy: dict, monitoring: dict, alerts: AlertClient, dry_run: bool,
) -> dict:
    """Compute one row of the verdict report for `strategy`."""
    label = strategy.get("label") or "?"
    baseline_sharpe = float(strategy.get("baseline_sharpe_oos") or 0.0)
    rolling_days = int(monitoring.get("rolling_days") or 90)
    starting_equity = float(
        config.get_backtesting().get("default_starting_equity") or 50000.0
    )

    tmp_csv, n_pool = _build_rolling_pool(strategy, rolling_days)
    if tmp_csv is None or n_pool == 0:
        row = {
            "label": label,
            "n_trades_90d": 0,
            "sharpe_90d": 0.0,
            "baseline_sharpe": baseline_sharpe,
            "drop_pct": 0.0,
            "verdict": "INSUFFICIENT_DATA",
            "pause_flag_action": "skip",
            "note": "no rolling-window trades found",
        }
        _log.event("INFO", "verdict", **row)
        return row

    try:
        report = compute_report(tmp_csv, starting_equity=starting_equity) or {}
    except Exception as e:
        _log.warning("compute_report failed for %s: %s", label, e)
        report = {"error": str(e)}

    if "error" in report:
        row = {
            "label": label,
            "n_trades_90d": n_pool,
            "sharpe_90d": 0.0,
            "baseline_sharpe": baseline_sharpe,
            "drop_pct": 0.0,
            "verdict": "INSUFFICIENT_DATA",
            "pause_flag_action": "skip",
            "note": f"report error: {report.get('error')}",
        }
        _log.event("INFO", "verdict", **row)
        return row

    n_trades = int(report.get("trades") or 0)
    sharpe_90d = float(report.get("sharpe_annual") or 0.0)
    pf = float(report.get("profit_factor") or 0.0)
    verdict, drop_pct = _verdict_for(
        sharpe_90d=sharpe_90d, baseline_sharpe=baseline_sharpe,
        n_trades=n_trades, monitoring=monitoring,
    )

    pause_action = "skip"
    body = (
        f"label={label} n_trades_90d={n_trades} sharpe_90d={sharpe_90d:.3f} "
        f"baseline={baseline_sharpe:.3f} drop_pct={drop_pct:.1f}% pf={pf:.2f}"
    )
    if verdict == "WARN":
        if dry_run:
            pause_action = "[DRY] would WARN"
        else:
            alerts.alert("WARN", f"{label} Sharpe drop", body)
            pause_action = "warn_alert"
    elif verdict == "KILL":
        if dry_run:
            pause_action = "[DRY] would KILL + touch pause flag"
        else:
            alerts.alert("KILL", f"{label} edge collapsed", body)
            pause_action = _touch_pause_flag(strategy)

    row = {
        "label": label,
        "n_trades_90d": n_trades,
        "sharpe_90d": round(sharpe_90d, 3),
        "baseline_sharpe": round(baseline_sharpe, 3),
        "drop_pct": round(drop_pct, 2),
        "verdict": verdict,
        "pause_flag_action": pause_action,
        "pf_90d": round(pf, 3),
    }
    _log.event("INFO", "verdict", **row)
    return row


def main(dry_run: bool = False, only_strategy: str | None = None) -> int:
    """Run one daily edge-decay scan. Always returns 0 (fail-quiet)."""
    started = time.monotonic()
    alerts = AlertClient(_AGENT_NAME)

    try:
        monitoring = config.get_monitoring().get("edge_decay", {}) or {}
        strategies = config.get_strategies()
        if only_strategy:
            strategies = [s for s in strategies if s.get("label") == only_strategy]
        if not strategies:
            _log.event("WARN", "no_strategies", note="nothing to scan")
            print("edge_decay: no strategies to scan")
            return 0

        rows: list[dict] = []
        for s in strategies:
            try:
                rows.append(_process_strategy(s, monitoring, alerts, dry_run))
            except Exception as e:
                _log.warning("strategy %s scan raised: %s", s.get("label"), e)
                _log.event("WARN", "strategy_error", label=s.get("label"),
                           error=str(e))
                rows.append({
                    "label": s.get("label") or "?",
                    "n_trades_90d": 0,
                    "sharpe_90d": 0.0,
                    "baseline_sharpe": float(s.get("baseline_sharpe_oos") or 0.0),
                    "drop_pct": 0.0,
                    "verdict": "ERROR",
                    "pause_flag_action": "skip",
                    "note": str(e),
                })

        today = datetime.now(timezone.utc)
        if not dry_run:
            state.write_json_atomic(
                _state_path_for(today),
                {"ts": today.isoformat(), "rows": rows},
            )
            _append_markdown(rows, today)

        runtime = round(time.monotonic() - started, 3)
        _log.event(
            "INFO", "scan_complete",
            n_strategies=len(rows),
            n_kill=sum(1 for r in rows if r["verdict"] == "KILL"),
            n_warn=sum(1 for r in rows if r["verdict"] == "WARN"),
            n_ok=sum(1 for r in rows if r["verdict"] == "OK"),
            runtime_seconds=runtime,
            dry_run=dry_run,
        )
        # Console summary
        print(f"\nedge_decay scan ({today:%Y-%m-%d}) — runtime={runtime}s dry_run={dry_run}")
        print(f"{'label':<35} {'n':>5} {'sharpe':>8} {'base':>8} {'drop%':>8} {'verdict':<18}")
        for r in rows:
            print(
                f"{r['label']:<35} {r['n_trades_90d']:>5} "
                f"{r['sharpe_90d']:>8.3f} {r['baseline_sharpe']:>8.3f} "
                f"{r['drop_pct']:>7.1f}% {r['verdict']:<18}"
            )
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        _log.warning("edge_decay.main() crashed: %s", e)
        _log.event("WARN", "agent_crash", error=str(e), traceback=tb)
        try:
            alerts.alert("WARN", "edge_decay crashed", f"{e}\n\n{tb}")
        except Exception:
            pass
        return 0


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print verdicts without alerting/pause-flagging/state-writing.")
    ap.add_argument("--strategy", default=None,
                    help="Process only this strategy label (default: all).")
    args = ap.parse_args()
    rc = main(dry_run=args.dry_run, only_strategy=args.strategy)
    sys.exit(rc)


if __name__ == "__main__":
    _cli()
