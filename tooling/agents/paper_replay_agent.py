# Sharing tier: methodology-shareable (pure plumbing, no edge)
"""Pella paper-replay agent (Phase 3 skeleton).

Reconstructs WHAT THE EA INTENDED from the JSONL files written by
`PellaSignalLog.mqh`, matches each signal to the broker fill recorded by MT5
(magic + side + tight time-window), measures the slippage in pips, and
issues OK/WARN/KILL verdicts per strategy.

Thresholds (`agent_config.yaml::monitoring.paper_replay`):

    divergence_warn_pips        per-day median > N pips -> WARN
    divergence_kill_pips        per-day median > N pips -> kill candidate
    sustained_days_for_kill     consecutive kill-candidate days -> KILL

KILL touches the strategy's pause flag (same convention as edge_decay).

LIMITATIONS (this is the Phase 3 skeleton):

    1. Pip-size lookup is digits-based with a hand-coded override map for
       symbols where the digits-rule is wrong (XAUUSD, NDX, JPY pairs).
       Add overrides as new symbols come online.
    2. Signal/deal matching uses (magic, side, time-window) only. If two
       signals fire within the window for the same magic+side, they are
       paired in chronological order and one may match the wrong fill.
       Acceptable for the Day-1 build; revisit if false-pair rates climb.
    3. The "replay against historical ticks" step is NOT implemented yet --
       this build measures realised broker slippage, which is the actionable
       proxy. Re-simulating the EA's signal price against tick data is a
       Day-2 enhancement.

CLI:
    python tools/paper_replay_agent.py [--dry-run] [--strategy LABEL]
                                       [--days N] [--ignore-no-signals]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))

from pella import config, logger as pella_logger, state  # noqa: E402
from pella.clients import AlertClient, MT5Client  # noqa: E402

_AGENT_NAME = "paper_replay"
_log = pella_logger.get_logger(_AGENT_NAME)

# How wide a window (seconds) to allow between EA-emitted signal time and
# broker-reported deal time when pairing. 60s is generous given EAs typically
# log immediately and broker fills land within a few seconds.
_MATCH_WINDOW_SECONDS = 60

# Hand-coded pip sizes for symbols whose default `10 * point` rule is wrong.
# Keys are symbol prefixes; values are the pip size in price units.
# (XAUUSD: 1 pip = 0.10. JPY pairs: 1 pip = 0.01. NDX: 1 pip = 1 index point.)
_PIP_OVERRIDES: dict[str, float] = {
    "XAUUSD": 0.10,
    "XAGUSD": 0.01,
    "NDX":    1.0,
    "SP500":  0.1,
    "DJ30":   1.0,
}


# ---------------------------------------------------------------------------
# Pip arithmetic
# ---------------------------------------------------------------------------
def _pip_size_for(symbol: str, mt5: MT5Client) -> float:
    """Return the price-units value of one pip. Falls back to a digits-based
    rule if the symbol isn't in `_PIP_OVERRIDES` and MT5 returns sane info."""
    for prefix, val in _PIP_OVERRIDES.items():
        if symbol.upper().startswith(prefix):
            return val
    # Best-effort fallback: 5/3-digit FX -> 10*point, 4/2-digit -> point.
    try:
        import MetaTrader5 as _m  # noqa
        info = _m.symbol_info(symbol) if _m else None  # type: ignore[attr-defined]
    except Exception:
        info = None
    if info is not None:
        digits = int(getattr(info, "digits", 5) or 5)
        point = float(getattr(info, "point", 0.0) or 0.0)
        if point > 0:
            return point * (10.0 if digits in (3, 5) else 1.0)
    return 0.0001  # generic FX fallback


def _diff_in_pips(price_a: float, price_b: float, pip: float) -> float:
    if pip <= 0:
        return 0.0
    return abs(float(price_a) - float(price_b)) / pip


# ---------------------------------------------------------------------------
# Signal-log discovery
# ---------------------------------------------------------------------------
def _signals_dir() -> Path:
    paths = config.get_paths()
    common = paths.get("mt5_common_files")
    if not common:
        raise FileNotFoundError(
            "agent_config.yaml::paths.mt5_common_files is not set; "
            "PellaSignalLog.mqh writes there"
        )
    return Path(common) / "Pella" / "signals"


def _read_signals_for(label: str, expert: str, symbol: str,
                      since_dt: datetime) -> list[dict]:
    """Read all JSONL signal files matching `<expert>_<symbol>_*.jsonl` whose
    filename-date is `>= since_dt`. Returns parsed records sorted by ts."""
    sdir = _signals_dir()
    if not sdir.is_dir():
        return []
    out: list[dict] = []
    since_date = since_dt.date()
    # The EA's filename pattern is <ea>_<symbol>_<YYYY-MM-DD>.jsonl, where
    # both `ea` and `symbol` are sanitised (alnum + underscore + dot + dash).
    safe_expert = "".join(c if (c.isalnum() or c in "._-") else "_" for c in expert)
    safe_symbol = "".join(c if (c.isalnum() or c in "._-") else "_" for c in symbol)
    glob = f"{safe_expert}_{safe_symbol}_*.jsonl"
    for fp in sorted(sdir.glob(glob)):
        # Extract date from filename suffix
        try:
            datepart = fp.stem.rsplit("_", 1)[1]
            file_date = datetime.strptime(datepart, "%Y-%m-%d").date()
        except (ValueError, IndexError):
            file_date = since_date  # parse-fail: include rather than drop
        if file_date < since_date:
            continue
        try:
            with fp.open("r", encoding="utf-8") as f:
                for ln, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError as e:
                        _log.warning("bad JSONL %s:%d: %s", fp, ln, e)
                        continue
                    rec["_label"] = label
                    rec["_source_file"] = str(fp)
                    out.append(rec)
        except OSError as e:
            _log.warning("could not read %s: %s", fp, e)
    out.sort(key=lambda r: r.get("ts", ""))
    return out


# ---------------------------------------------------------------------------
# Signal <-> deal matching
# ---------------------------------------------------------------------------
def _parse_signal_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _match_signals_to_deals(signals: list[dict], deals: list[dict],
                            magic: int) -> list[dict]:
    """Pair each EA-side signal with the closest broker deal on the same
    magic+side within `_MATCH_WINDOW_SECONDS`. Each deal is matched at most
    once. Returns a list of {signal, deal, divergence_pips, side, ts}.

    `signals` is the EA-side intent log; `deals` is the broker-side fill log.
    A signal with no matching deal is reported with deal=None (the EA fired
    but the broker either rejected, hadn't filled yet, or filled outside the
    match window).
    """
    relevant_deals = [d for d in deals if int(d.get("magic") or 0) == int(magic)]
    consumed: set[Any] = set()
    pairs: list[dict] = []

    for sig in signals:
        sig_ts = _parse_signal_ts(sig.get("ts"))
        if sig_ts is None:
            continue
        side = (sig.get("side") or "").upper()
        signal_type = (sig.get("signal_type") or "").lower()

        # entry signal -> entry IN deal; exit signal -> entry OUT deal.
        wanted_entry = "OUT" if signal_type == "exit" else "IN"

        best: tuple[float, dict] | None = None  # (delta_seconds, deal)
        for d in relevant_deals:
            if id(d) in consumed:
                continue
            if (d.get("entry") or "") != wanted_entry:
                continue
            if (d.get("type") or "").upper() != side:
                continue
            d_ts = d.get("time")
            if not isinstance(d_ts, datetime):
                continue
            d_naive = d_ts.replace(tzinfo=timezone.utc) if d_ts.tzinfo is None else d_ts
            delta = abs((d_naive - sig_ts).total_seconds())
            if delta > _MATCH_WINDOW_SECONDS:
                continue
            if best is None or delta < best[0]:
                best = (delta, d)
        if best is not None:
            consumed.add(id(best[1]))
        pairs.append({
            "signal": sig,
            "deal": best[1] if best else None,
            "match_delta_seconds": best[0] if best else None,
            "ts": sig_ts,
            "side": side,
            "signal_type": signal_type,
        })
    return pairs


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
def _bucket_by_day(pairs: list[dict]) -> dict[str, list[float]]:
    """Group divergence_pips by UTC date. Unmatched signals contribute
    `None` divergence (counted but not in median)."""
    out: dict[str, list[float]] = {}
    for p in pairs:
        d = p["ts"].astimezone(timezone.utc).date().isoformat()
        if "divergence_pips" not in p:
            continue
        out.setdefault(d, []).append(p["divergence_pips"])
    return out


def _verdict_from_buckets(per_day: dict[str, list[float]],
                          warn_pips: float, kill_pips: float,
                          sustained_days_for_kill: int) -> tuple[str, dict]:
    """Walk days in chronological order, tracking median divergence. Verdicts:

        OK    : last day median <= warn_pips and no sustained-kill streak.
        WARN  : last day median > warn_pips OR ANY day's median > warn_pips
                in the window, but no sustained kill streak.
        KILL  : >= sustained_days_for_kill consecutive trailing days with
                median > kill_pips.
    """
    days = sorted(per_day.keys())
    summary: dict[str, Any] = {
        "n_days": len(days),
        "by_day": [],
        "warn_pips": warn_pips,
        "kill_pips": kill_pips,
        "sustained_days_for_kill": sustained_days_for_kill,
    }
    over_kill_streak_tail = 0
    any_warn_seen = False
    last_median: float | None = None

    for d in days:
        vals = per_day[d]
        if not vals:
            continue
        m = float(statistics.median(vals))
        last_median = m
        if m > kill_pips:
            over_kill_streak_tail += 1
        else:
            over_kill_streak_tail = 0
        if m > warn_pips:
            any_warn_seen = True
        summary["by_day"].append({
            "date": d, "n": len(vals), "median_pips": round(m, 3),
            "p95_pips": round(_p95(vals), 3),
        })

    if over_kill_streak_tail >= sustained_days_for_kill:
        return "KILL", summary
    if any_warn_seen or (last_median is not None and last_median > warn_pips):
        return "WARN", summary
    if last_median is None:
        return "INSUFFICIENT_DATA", summary
    return "OK", summary


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(0.95 * (len(s) - 1)))))
    return float(s[idx])


# ---------------------------------------------------------------------------
# Pause-flag (re-uses convention from edge_decay)
# ---------------------------------------------------------------------------
def _resolve_pause_flag(strategy: dict) -> Path:
    raw = strategy.get("pause_flag_path") or ""
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(config.get_paths()["lab_root"]) / raw


def _touch_pause_flag(strategy: dict) -> str:
    flag = _resolve_pause_flag(strategy)
    flag.parent.mkdir(parents=True, exist_ok=True)
    if flag.exists():
        return "already_set"
    state.write_json_atomic(flag, {
        "set_by": _AGENT_NAME,
        "set_at": datetime.now(timezone.utc).isoformat(),
        "label": strategy.get("label"),
        "reason": "paper_replay_kill",
    })
    return "created"


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
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
        lines.append(f"# Paper-replay scan {today:%Y-%m-%d}\n")
    lines.append(f"\n## Run @ {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n")
    lines.append("| label | n_signals | n_matched | unmatched_pct | "
                 "median_pips | p95_pips | verdict | pause_flag |\n")
    lines.append("|---|---:|---:|---:|---:|---:|---|---|\n")
    for r in rows:
        lines.append(
            f"| {r.get('label','?')} | {r.get('n_signals',0)} | "
            f"{r.get('n_matched',0)} | {r.get('unmatched_pct','-')}% | "
            f"{r.get('median_pips','-')} | {r.get('p95_pips','-')} | "
            f"{r.get('verdict','?')} | {r.get('pause_flag_action','-')} |\n"
        )
    with md_path.open("a", encoding="utf-8") as f:
        f.writelines(lines)
    return md_path


# ---------------------------------------------------------------------------
# Per-strategy core
# ---------------------------------------------------------------------------
def _process_strategy(
    strategy: dict, mt5: MT5Client, monitoring: dict,
    days: int, alerts: AlertClient, dry_run: bool,
) -> dict:
    label = strategy.get("label") or "?"
    expert = strategy.get("expert") or ""
    symbol = strategy.get("symbol") or ""
    magic = int(strategy.get("magic") or 0)

    warn_pips = float(monitoring.get("divergence_warn_pips") or 5.0)
    kill_pips = float(monitoring.get("divergence_kill_pips") or 10.0)
    sustained = int(monitoring.get("sustained_days_for_kill") or 3)

    since = datetime.now(timezone.utc) - timedelta(days=days)
    signals = _read_signals_for(label, expert, symbol, since)
    if not signals:
        row = {
            "label": label, "n_signals": 0, "n_matched": 0,
            "unmatched_pct": 0.0, "median_pips": 0.0, "p95_pips": 0.0,
            "verdict": "INSUFFICIENT_DATA",
            "pause_flag_action": "skip",
            "note": "no signals in window",
        }
        _log.event("INFO", "verdict", **row)
        return row

    deals = mt5.deals(since, datetime.now(timezone.utc) + timedelta(hours=1))
    pairs = _match_signals_to_deals(signals, deals, magic)

    pip = _pip_size_for(symbol, mt5)
    matched = 0
    divergences: list[float] = []
    for p in pairs:
        if p["deal"] is None:
            continue
        matched += 1
        sig_price = float(p["signal"].get("price") or 0.0)
        deal_price = float(p["deal"].get("price") or 0.0)
        if sig_price > 0 and deal_price > 0:
            d_pips = _diff_in_pips(sig_price, deal_price, pip)
            p["divergence_pips"] = d_pips
            divergences.append(d_pips)

    n_signals = len(pairs)
    unmatched_pct = (1.0 - (matched / n_signals)) * 100.0 if n_signals else 0.0
    median_pips = float(statistics.median(divergences)) if divergences else 0.0
    p95_pips = _p95(divergences)

    by_day = _bucket_by_day(pairs)
    verdict, summary = _verdict_from_buckets(
        by_day, warn_pips, kill_pips, sustained,
    )

    pause_action = "skip"
    body = (
        f"label={label} n_signals={n_signals} matched={matched} "
        f"median_pips={median_pips:.2f} p95_pips={p95_pips:.2f} "
        f"warn={warn_pips} kill={kill_pips}"
    )
    if verdict == "WARN":
        if dry_run:
            pause_action = "[DRY] would WARN"
        else:
            alerts.alert("WARN", f"{label} paper-replay divergence", body)
            pause_action = "warn_alert"
    elif verdict == "KILL":
        if dry_run:
            pause_action = "[DRY] would KILL + touch pause flag"
        else:
            alerts.alert("KILL", f"{label} paper-replay sustained divergence", body)
            pause_action = _touch_pause_flag(strategy)

    row = {
        "label": label,
        "n_signals": n_signals,
        "n_matched": matched,
        "unmatched_pct": round(unmatched_pct, 1),
        "median_pips": round(median_pips, 3),
        "p95_pips": round(p95_pips, 3),
        "pip_size": pip,
        "verdict": verdict,
        "pause_flag_action": pause_action,
        "by_day": summary.get("by_day", []),
    }
    _log.event("INFO", "verdict", **row)
    return row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(
    dry_run: bool = False,
    only_strategy: str | None = None,
    days: int = 14,
    ignore_no_signals: bool = False,
) -> int:
    started = time.monotonic()
    alerts = AlertClient(_AGENT_NAME)

    try:
        monitoring = config.get_monitoring().get("paper_replay", {}) or {}
        strategies = config.get_strategies()
        if only_strategy:
            strategies = [s for s in strategies if s.get("label") == only_strategy]
        if not strategies:
            _log.event("WARN", "no_strategies", note="nothing to scan")
            print("paper_replay: no strategies to scan")
            return 0

        # Heads-up if the signals dir is missing entirely. Doesn't fail; the
        # per-strategy loop just reports INSUFFICIENT_DATA for everyone.
        try:
            sdir = _signals_dir()
            if not sdir.is_dir() and not ignore_no_signals:
                _log.event("WARN", "signals_dir_missing", path=str(sdir))
                print(f"paper_replay: signals dir does not exist yet: {sdir}")
                print("  EAs need #include <PellaSignalLog.mqh> + a "
                      "PellaLogSignal() call at signal time before this "
                      "agent has anything to score.")
        except FileNotFoundError as e:
            _log.event("WARN", "signals_dir_unconfigured", error=str(e))
            print(f"paper_replay: {e}")
            return 0

        mt5 = MT5Client()
        try:
            rows: list[dict] = []
            for s in strategies:
                try:
                    rows.append(_process_strategy(
                        s, mt5, monitoring, days, alerts, dry_run,
                    ))
                except Exception as e:
                    _log.warning("strategy %s scan raised: %s", s.get("label"), e)
                    _log.event("WARN", "strategy_error",
                               label=s.get("label"), error=str(e))
                    rows.append({
                        "label": s.get("label") or "?",
                        "n_signals": 0, "n_matched": 0,
                        "unmatched_pct": 0.0, "median_pips": 0.0,
                        "p95_pips": 0.0, "verdict": "ERROR",
                        "pause_flag_action": "skip", "note": str(e),
                    })
        finally:
            mt5.close()

        today = datetime.now(timezone.utc)
        if not dry_run and rows:
            state.write_json_atomic(
                _state_path_for(today),
                {"ts": today.isoformat(), "rows": rows,
                 "monitoring": monitoring, "window_days": days},
            )
            _append_markdown(rows, today)

        runtime = round(time.monotonic() - started, 3)
        _log.event(
            "INFO", "scan_complete",
            n_strategies=len(rows),
            n_kill=sum(1 for r in rows if r["verdict"] == "KILL"),
            n_warn=sum(1 for r in rows if r["verdict"] == "WARN"),
            n_ok=sum(1 for r in rows if r["verdict"] == "OK"),
            n_insufficient=sum(1 for r in rows
                               if r["verdict"] == "INSUFFICIENT_DATA"),
            runtime_seconds=runtime, dry_run=dry_run,
        )
        print(f"\npaper_replay scan ({today:%Y-%m-%d}) — runtime={runtime}s "
              f"dry_run={dry_run} window={days}d")
        print(f"{'label':<35}{'sigs':>6}{'match':>7}{'unm%':>7}"
              f"{'med_p':>8}{'p95_p':>8}  {'verdict':<18}")
        for r in rows:
            print(
                f"{(r.get('label','?')):<35}"
                f"{r.get('n_signals',0):>6}"
                f"{r.get('n_matched',0):>7}"
                f"{r.get('unmatched_pct','-'):>6}%"
                f"{str(r.get('median_pips','-')):>8}"
                f"{str(r.get('p95_pips','-')):>8}  "
                f"{r.get('verdict','?'):<18}"
            )
        return 0

    except Exception as e:
        tb = traceback.format_exc()
        _log.warning("paper_replay.main() crashed: %s", e)
        _log.event("WARN", "agent_crash", error=str(e), traceback=tb)
        try:
            alerts.alert("WARN", "paper_replay crashed", f"{e}\n\n{tb}")
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
    ap.add_argument("--days", type=int, default=14,
                    help="Look-back window in days (default 14).")
    ap.add_argument("--ignore-no-signals", action="store_true",
                    help="Suppress the 'signals dir missing' notice on first runs.")
    args = ap.parse_args()
    rc = main(
        dry_run=args.dry_run,
        only_strategy=args.strategy,
        days=args.days,
        ignore_no_signals=args.ignore_no_signals,
    )
    sys.exit(rc)


if __name__ == "__main__":
    _cli()
