# Sharing tier: methodology-shareable (pure plumbing — queue contents are INNER)
"""Pella nightly discovery agent.

Pulls the highest-priority pending entry from the idea queue, runs it through
the MT5 CLI runner, scores it against the discovery-stage gates from
`agent_config.yaml`, and marks the queue entry complete with a verdict.

Verdicts:
    PASS_PROMOTE  - cleared all 4 discovery gates; survivor TestSpec written
                    to <research_root>/discovery/<YYYY-MM-DD>/<id>_<label>.json
                    so the operator (or holdout_test) can pick it up next.
    FAIL_GATES    - ran clean but missed one or more gates.
    NO_TRADES     - 0 trades produced (typical when symbol/timeframe data
                    coverage is wrong).
    BACKTEST_ERROR- CLI runner returned an error (compile fail, license, etc).
    SKIPPED_GRID  - test_spec carries a `grid`; this agent runs single combos
                    only. Operator should run param_grid_sweep manually.

Discovery-stage gates (looser than universal — survivors then face G3/G4/G5):
    pf_min, sharpe_min, trades_min, dd_max_pct from
    agent_config.yaml::discovery.thresholds.discovery_gate_*

Schedule:
    Designed for the nightly window
    `agent_config.yaml::discovery.agent.nightly_run_window_utc` (default
    00:30-01:00 UTC). Runs respect:
    - `max_runtime_seconds` hard cap (default 1800s)
    - `kill_mt5_before_run` (default true) -- terminates terminal64.exe so
      the tester boots clean
    - the agent-level pause flag at <pause_flags_dir>/discovery_agent.PAUSE

CLI:
    python tools/discovery_agent.py [--dry-run] [--max-entries N]
                                    [--ignore-window] [--id ENTRY_ID]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

import idea_queue_manager  # noqa: E402
from pella import config, logger as pella_logger, state  # noqa: E402
from pella.clients import AlertClient  # noqa: E402

# Re-use existing single-combo runner. param_grid_sweep.run_one returns a
# rich {label, trades, metrics, csv} dict and writes the trades CSV.
import param_grid_sweep as pgs  # noqa: E402
from mt5_cli import TestSpec, MT5CliRunner  # noqa: E402

_AGENT_NAME = "discovery_agent"
_log = pella_logger.get_logger(_AGENT_NAME)


# ---------------------------------------------------------------------------
# TestSpec normalisation
# ---------------------------------------------------------------------------
# Queue entries (cross_pollinator output, manual adds) carry a flat dict that
# is "TestSpec-like" but uses `tf` instead of `timeframe` and may include
# tracking fields the dataclass doesn't accept.
_TESTSPEC_FIELDS = {
    "expert", "symbol", "timeframe", "start_date", "end_date",
    "modelling", "deposit", "currency", "leverage", "delays",
    "inputs", "label", "timeout_seconds",
}


def _normalise_to_testspec(raw: dict) -> tuple[TestSpec | None, str | None]:
    """Return (TestSpec, None) on success or (None, reason) on failure."""
    if not isinstance(raw, dict):
        return None, "test_spec is not a dict"
    if "grid" in raw:
        return None, "grid"
    d = dict(raw)
    if "tf" in d and "timeframe" not in d:
        d["timeframe"] = d.pop("tf")
    cleaned = {k: v for k, v in d.items() if k in _TESTSPEC_FIELDS}
    for required in ("expert", "symbol", "timeframe", "start_date", "end_date"):
        if not cleaned.get(required):
            return None, f"missing required field: {required}"
    cleaned.setdefault("label", f"discovery_{int(time.time())}")
    cleaned.setdefault("inputs", {})
    try:
        return TestSpec(**cleaned), None
    except TypeError as e:
        return None, f"TestSpec() rejected payload: {e}"


# ---------------------------------------------------------------------------
# Window + pause-flag gating
# ---------------------------------------------------------------------------
def _within_window(now: datetime, window: str) -> bool:
    """`window` is "HH:MM-HH:MM" UTC. Inclusive of both ends, minute-precision.
    Wrap-around (e.g. "23:30-00:30") is supported."""
    try:
        start_s, end_s = window.split("-")
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
    except Exception:
        _log.warning("malformed nightly_run_window_utc: %r — running anyway", window)
        return True
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _agent_pause_flag() -> Path:
    paths = config.get_paths()
    return Path(paths["pause_flags_dir"]) / f"{_AGENT_NAME}.PAUSE"


# ---------------------------------------------------------------------------
# MT5 housekeeping
# ---------------------------------------------------------------------------
def _kill_mt5(dry_run: bool) -> str:
    """Terminate terminal64.exe so the tester gets a clean boot. Windows-only;
    on non-Windows it's a no-op. Returns a one-line status string for logging."""
    if sys.platform != "win32":
        return "skipped (non-windows)"
    if dry_run:
        return "[DRY] would kill terminal64.exe"
    try:
        proc = subprocess.run(
            ["taskkill", "/F", "/IM", "terminal64.exe", "/T"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            return "killed"
        # 128 = "process not found" — totally fine
        if proc.returncode == 128 or "not found" in (proc.stderr or "").lower():
            return "not running"
        return f"taskkill rc={proc.returncode}: {(proc.stderr or '').strip()}"
    except Exception as e:
        return f"error: {e}"


# ---------------------------------------------------------------------------
# Result paths
# ---------------------------------------------------------------------------
def _today_dir() -> Path:
    paths = config.get_paths()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Path(paths["research_root"]) / "discovery" / today


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
        lines.append(f"# Discovery agent {today:%Y-%m-%d}\n")
    lines.append(f"\n## Run @ {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n")
    lines.append("| id | label | trades | pf | sharpe | dd% | verdict |\n")
    lines.append("|---|---|---:|---:|---:|---:|---|\n")
    for r in rows:
        lines.append(
            f"| {r.get('id','?')} | {r.get('label','?')} | "
            f"{r.get('trades','-')} | {r.get('pf','-')} | "
            f"{r.get('sharpe','-')} | {r.get('dd_pct','-')} | "
            f"{r.get('verdict','?')} |\n"
        )
    with md_path.open("a", encoding="utf-8") as f:
        f.writelines(lines)
    return md_path


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------
def _gates_from_config() -> dict:
    th = config.get_discovery().get("thresholds", {}) or {}
    return {
        "min_pf": float(th.get("discovery_gate_min_pf", 1.20)),
        "min_sharpe": float(th.get("discovery_gate_min_sharpe", 0.70)),
        "min_trades": int(th.get("discovery_gate_min_trades", 100)),
        "max_dd_pct": float(th.get("discovery_gate_max_dd_pct", 30.0)),
    }


def _evaluate(metrics: dict, gates: dict) -> tuple[bool, list[str]]:
    fails: list[str] = []
    if (metrics.get("profit_factor") or 0) < gates["min_pf"]:
        fails.append(f"PF<{gates['min_pf']}")
    if (metrics.get("sharpe_annual") or 0) < gates["min_sharpe"]:
        fails.append(f"Sh<{gates['min_sharpe']}")
    if (metrics.get("trades") or 0) < gates["min_trades"]:
        fails.append(f"N<{gates['min_trades']}")
    if (metrics.get("max_dd_pct") or 100) > gates["max_dd_pct"]:
        fails.append(f"DD>{gates['max_dd_pct']}")
    return (len(fails) == 0, fails)


# ---------------------------------------------------------------------------
# Survivor persistence
# ---------------------------------------------------------------------------
def _write_survivor(entry_id: str, spec: TestSpec, metrics: dict,
                    csv_path: str | None) -> Path:
    out_dir = _today_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{entry_id}_{spec.label}.json"
    payload = {
        "entry_id": entry_id,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "test_spec": asdict(spec),
        "metrics": metrics,
        "trades_csv": csv_path,
    }
    state.write_json_atomic(out_path, payload)
    return out_path


# ---------------------------------------------------------------------------
# Core: process one entry
# ---------------------------------------------------------------------------
def _process_entry(
    entry: dict, runner: MT5CliRunner, gates: dict,
    alerts: AlertClient, dry_run: bool,
) -> dict:
    entry_id = entry.get("id", "?")
    raw_spec = entry.get("test_spec") or {}
    label_hint = raw_spec.get("label") or entry.get("source_ref") or entry_id

    spec, reason = _normalise_to_testspec(raw_spec)
    if spec is None:
        verdict = "SKIPPED_GRID" if reason == "grid" else "BACKTEST_ERROR"
        row = {"id": entry_id, "label": label_hint, "verdict": verdict,
               "note": reason}
        _log.event("INFO", "verdict", **row)
        if not dry_run:
            idea_queue_manager.mark(entry_id, "complete", verdict=verdict)
        return row

    if dry_run:
        row = {"id": entry_id, "label": spec.label, "verdict": "[DRY] would run"}
        _log.event("INFO", "verdict", **row)
        return row

    # Mark running before launching the (potentially long) backtest. If we
    # crash mid-run, the JSONL append-only history preserves the transition.
    try:
        idea_queue_manager.mark(entry_id, "running")
    except Exception as e:
        _log.warning("mark(running) failed for %s: %s", entry_id, e)

    out_dir = _today_dir() / f"{entry_id}_{spec.label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = pgs.run_one(spec, runner, out_dir)

    if "error" in result:
        verdict = "NO_TRADES" if result.get("error") == "0 trades" else "BACKTEST_ERROR"
        row = {"id": entry_id, "label": spec.label,
               "trades": result.get("trades", 0),
               "verdict": verdict, "note": result.get("error")}
        _log.event("INFO", "verdict", **row)
        idea_queue_manager.mark(entry_id, "complete", verdict=verdict)
        alerts.alert(
            "INFO" if verdict == "NO_TRADES" else "WARN",
            f"discovery {entry_id} {verdict}",
            f"{spec.label}: {result.get('error')}",
        )
        return row

    metrics = result.get("metrics") or {}
    ok, fails = _evaluate(metrics, gates)
    n = int(metrics.get("trades") or 0)
    pf = float(metrics.get("profit_factor") or 0.0)
    sharpe = float(metrics.get("sharpe_annual") or 0.0)
    dd = float(metrics.get("max_dd_pct") or 0.0)

    survivor_path: Path | None = None
    if ok:
        verdict = "PASS_PROMOTE"
        survivor_path = _write_survivor(entry_id, spec, metrics, result.get("csv"))
        alerts.alert(
            "WARN", f"discovery PROMOTE: {spec.label}",
            f"id={entry_id} N={n} PF={pf:.2f} Sh={sharpe:.2f} DD={dd:.1f}% "
            f"-> {survivor_path}",
        )
    else:
        verdict = "FAIL_GATES"
        alerts.alert(
            "INFO", f"discovery FAIL: {spec.label}",
            f"id={entry_id} fails={','.join(fails)} "
            f"N={n} PF={pf:.2f} Sh={sharpe:.2f} DD={dd:.1f}%",
        )

    idea_queue_manager.mark(
        entry_id, "complete", verdict=verdict,
        result_path=str(survivor_path) if survivor_path else result.get("csv"),
    )

    row = {
        "id": entry_id,
        "label": spec.label,
        "trades": n,
        "pf": round(pf, 3),
        "sharpe": round(sharpe, 3),
        "dd_pct": round(dd, 2),
        "verdict": verdict,
        "fails": fails,
        "result_path": str(survivor_path) if survivor_path else result.get("csv"),
    }
    _log.event("INFO", "verdict", **row)
    return row


# ---------------------------------------------------------------------------
# Entry-picking
# ---------------------------------------------------------------------------
def _pick_entries(only_id: str | None, max_entries: int,
                  dry_run: bool) -> list[dict]:
    if only_id:
        e = idea_queue_manager.get_entry(only_id)
        if e is None:
            _log.event("WARN", "id_not_found", id=only_id)
            return []
        if e.get("status") != "pending":
            _log.event("WARN", "id_not_pending", id=only_id, status=e.get("status"))
        return [e]
    picks: list[dict] = []
    seen: set[str] = set()
    # next_pending() doesn't take a skip-list, so to gather >1 we must either
    # mutate (mark running) or filter manually. In dry-run we filter to avoid
    # state mutation; live runs pre-mark so a parallel agent invocation can't
    # double-pick the same entry.
    if dry_run:
        pending = [r for r in idea_queue_manager.list_entries(status_filter="pending")]
        pending.sort(key=lambda r: (-int(r.get("priority", 0)), r.get("created", "")))
        return pending[: max(1, int(max_entries))]
    for _ in range(max(1, int(max_entries))):
        nxt = idea_queue_manager.next_pending()
        if nxt is None:
            break
        rid = nxt.get("id")
        if not rid or rid in seen:
            break
        seen.add(rid)
        picks.append(nxt)
        try:
            idea_queue_manager.mark(rid, "running")
        except Exception as e:
            _log.warning("pre-mark(running) failed for %s: %s", rid, e)
    return picks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(
    dry_run: bool = False,
    max_entries: int = 1,
    ignore_window: bool = False,
    only_id: str | None = None,
) -> int:
    started = time.monotonic()
    alerts = AlertClient(_AGENT_NAME)

    try:
        # 0. Pause flag
        flag = _agent_pause_flag()
        if flag.exists():
            _log.event("INFO", "paused", reason="pause_flag_present", flag=str(flag))
            print(f"discovery_agent paused via {flag}")
            return 0

        # 1. Window check
        cfg = config.get_discovery().get("agent", {}) or {}
        window = cfg.get("nightly_run_window_utc") or "00:30-01:00"
        max_runtime = int(cfg.get("max_runtime_seconds") or 1800)
        kill_mt5_flag = bool(cfg.get("kill_mt5_before_run", True))
        now = datetime.now(timezone.utc)
        if not ignore_window and not _within_window(now, window):
            _log.event("INFO", "outside_window",
                       now=now.strftime("%H:%M"), window=window)
            print(f"discovery_agent: outside window {window} (now {now:%H:%M} UTC); "
                  "use --ignore-window to override")
            return 0

        # 2. Pick entries
        entries = _pick_entries(only_id, max_entries, dry_run=dry_run)
        if not entries:
            _log.event("INFO", "no_pending_entries")
            print("discovery_agent: no pending entries")
            return 0

        # 3. Optional MT5 kill before run
        if kill_mt5_flag:
            kr = _kill_mt5(dry_run)
            _log.event("INFO", "mt5_killed", status=kr)

        # 4. Run
        gates = _gates_from_config()
        runner = MT5CliRunner() if not dry_run else None
        rows: list[dict] = []
        for e in entries:
            elapsed = time.monotonic() - started
            if elapsed >= max_runtime:
                _log.event("WARN", "runtime_cap_hit",
                           elapsed_seconds=round(elapsed, 1), cap=max_runtime,
                           remaining_entries=len(entries) - len(rows))
                # Restore the un-processed entries to pending so they get
                # picked up next run. Each was pre-marked running.
                for unprocessed in entries[len(rows):]:
                    try:
                        idea_queue_manager.mark(unprocessed["id"], "pending")
                    except Exception:
                        pass
                break
            try:
                rows.append(_process_entry(e, runner, gates, alerts, dry_run))
            except Exception as ex:
                tb = traceback.format_exc()
                _log.warning("entry %s raised: %s", e.get("id"), ex)
                _log.event("WARN", "entry_error",
                           id=e.get("id"), error=str(ex), traceback=tb)
                row = {"id": e.get("id"), "label": (e.get("test_spec") or {}).get("label"),
                       "verdict": "BACKTEST_ERROR", "note": str(ex)}
                rows.append(row)
                if not dry_run:
                    try:
                        idea_queue_manager.mark(
                            e["id"], "complete",
                            verdict="BACKTEST_ERROR",
                        )
                    except Exception:
                        pass

        # 5. Persist + summary
        today = datetime.now(timezone.utc)
        if not dry_run and rows:
            state.write_json_atomic(
                _state_path_for(today),
                {"ts": today.isoformat(), "rows": rows,
                 "gates": gates, "max_runtime_seconds": max_runtime},
            )
            _append_markdown(rows, today)

        runtime = round(time.monotonic() - started, 3)
        _log.event(
            "INFO", "run_complete",
            n_processed=len(rows),
            n_promote=sum(1 for r in rows if r.get("verdict") == "PASS_PROMOTE"),
            n_fail_gates=sum(1 for r in rows if r.get("verdict") == "FAIL_GATES"),
            n_no_trades=sum(1 for r in rows if r.get("verdict") == "NO_TRADES"),
            n_error=sum(1 for r in rows if r.get("verdict") == "BACKTEST_ERROR"),
            runtime_seconds=runtime,
            dry_run=dry_run,
        )
        print(f"\ndiscovery_agent ({today:%Y-%m-%d}) — runtime={runtime}s "
              f"dry_run={dry_run}")
        print(f"{'id':<6}{'label':<40}{'N':>6}{'PF':>7}{'Sh':>7}{'DD%':>7}  verdict")
        for r in rows:
            print(
                f"{str(r.get('id','?')):<6}"
                f"{(str(r.get('label',''))[:39]):<40}"
                f"{str(r.get('trades','-')):>6}"
                f"{str(r.get('pf','-')):>7}"
                f"{str(r.get('sharpe','-')):>7}"
                f"{str(r.get('dd_pct','-')):>7}  "
                f"{r.get('verdict','?')}"
            )
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        _log.warning("discovery_agent.main() crashed: %s", e)
        _log.event("WARN", "agent_crash", error=str(e), traceback=tb)
        try:
            alerts.alert("WARN", "discovery_agent crashed", f"{e}\n\n{tb}")
        except Exception:
            pass
        return 0


def _cli() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan without running backtests, alerting, or "
                         "marking the queue.")
    ap.add_argument("--max-entries", type=int, default=1,
                    help="Max entries to process this run (default 1).")
    ap.add_argument("--ignore-window", action="store_true",
                    help="Skip the nightly_run_window_utc check (manual runs).")
    ap.add_argument("--id", dest="only_id", default=None,
                    help="Process this queue entry id and stop.")
    args = ap.parse_args()
    rc = main(
        dry_run=args.dry_run,
        max_entries=args.max_entries,
        ignore_window=args.ignore_window,
        only_id=args.only_id,
    )
    sys.exit(rc)


if __name__ == "__main__":
    _cli()
