"""Append-only strategy idea queue.

Sharing tier: methodology-shareable (queue management is generic; queue
CONTENTS are INNER ONLY).

Each queue entry is one strategy candidate awaiting backtest. Storage is
append-only JSONL at `<paths.discovery_queue>.jsonl` (one line per add or
update). The configured `paths.discovery_queue` path itself stores the
LATEST snapshot view as JSON for human-readable inspection.

Entry schema:
    {
      "id": "0001",
      "source": "community_pin | book | paper | video | cross_pollinator | manual",
      "source_ref": "freeform string (URL, book chapter, etc.)",
      "hypothesis": "1-paragraph plain-english description",
      "test_spec": {... full mt5_optimizer grid JSON or specs JSON ...},
      "priority": 3,
      "created": "2026-05-04T11:30:00Z",
      "last_tested": null,
      "status": "pending | running | complete | failed",
      "verdict": null,
      "result_path": null
    }

Public library API (importable from other agents):
    add_entry(source, source_ref, hypothesis, test_spec, priority=3) -> str
    import_entries(entries: list[dict]) -> list[str]
    mark(id, status, verdict=None, result_path=None) -> None
    next_pending() -> dict | None
    list_entries(status_filter=None) -> list[dict]
    get_entry(id) -> dict | None

CLI: see `python tools/idea_queue_manager.py --help`.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# allow running from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pella import config as _config  # noqa: E402
from pella import logger as _pella_logger  # noqa: E402
from pella import state as _state  # noqa: E402

_log = _pella_logger.get_logger("idea_queue")

_VALID_STATUSES = {"pending", "running", "complete", "failed"}
_VALID_SOURCES = {
    "community_pin",
    "book",
    "paper",
    "video",
    "cross_pollinator",
    "manual",
}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def _snapshot_path() -> Path:
    return Path(_config.get_paths()["discovery_queue"])


def _jsonl_path() -> Path:
    snap = _snapshot_path()
    return snap.with_suffix(snap.suffix + ".jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Queue rebuild
# ---------------------------------------------------------------------------
def _rebuild_queue() -> dict[str, dict]:
    """Replay the JSONL into a dict keyed by id, latest record per id wins."""
    records = _state.read_jsonl(_jsonl_path())
    out: dict[str, dict] = {}
    for rec in records:
        rid = rec.get("id")
        if not rid:
            continue
        kind = rec.get("_kind", "entry")
        if kind == "entry":
            out[rid] = {k: v for k, v in rec.items() if not k.startswith("_")}
        elif kind == "update":
            base = out.get(rid)
            if base is None:
                # Update before entry — record orphan so we don't lose it
                base = {"id": rid}
            for k, v in rec.items():
                if k.startswith("_") or k == "id":
                    continue
                base[k] = v
            out[rid] = base
    return out


def _write_snapshot(queue: dict[str, dict]) -> None:
    rows = sorted(queue.values(), key=lambda r: r.get("id", ""))
    _state.write_json_atomic(_snapshot_path(), {"entries": rows, "rebuilt": _now_iso()})


def _next_id(queue: dict[str, dict]) -> str:
    if not queue:
        return "0001"
    nums = []
    for rid in queue.keys():
        try:
            nums.append(int(rid))
        except ValueError:
            continue
    return f"{(max(nums) + 1) if nums else 1:04d}"


# ---------------------------------------------------------------------------
# Public library API
# ---------------------------------------------------------------------------
def add_entry(
    source: str,
    source_ref: str,
    hypothesis: str,
    test_spec: dict,
    priority: int = 3,
) -> str:
    """Append a fresh entry; returns generated id."""
    if source not in _VALID_SOURCES:
        _log.warning("add_entry: non-standard source '%s' (allowed: %s)", source, sorted(_VALID_SOURCES))
    queue = _rebuild_queue()
    rid = _next_id(queue)
    entry = {
        "_kind": "entry",
        "id": rid,
        "source": source,
        "source_ref": source_ref,
        "hypothesis": hypothesis,
        "test_spec": test_spec,
        "priority": int(priority),
        "created": _now_iso(),
        "last_tested": None,
        "status": "pending",
        "verdict": None,
        "result_path": None,
    }
    _state.append_jsonl(_jsonl_path(), entry)
    queue[rid] = {k: v for k, v in entry.items() if not k.startswith("_")}
    _write_snapshot(queue)
    _log.event("INFO", "queue_add", id=rid, source=source, priority=priority)  # type: ignore[attr-defined]
    return rid


def import_entries(entries: list[dict]) -> list[str]:
    """Bulk-import. Each entry should at minimum carry source + hypothesis +
    test_spec. Missing fields are filled with defaults; explicit ids are
    ignored (queue assigns its own).
    """
    ids: list[str] = []
    for raw in entries:
        rid = add_entry(
            source=raw.get("source", "manual"),
            source_ref=raw.get("source_ref", ""),
            hypothesis=raw.get("hypothesis", ""),
            test_spec=raw.get("test_spec", {}),
            priority=int(raw.get("priority", 3)),
        )
        ids.append(rid)
    return ids


def mark(
    id: str,
    status: str,
    verdict: str | None = None,
    result_path: str | None = None,
) -> None:
    """Append an update record for `id`."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}")
    queue = _rebuild_queue()
    if id not in queue:
        raise KeyError(f"unknown queue id: {id}")
    update = {
        "_kind": "update",
        "id": id,
        "status": status,
        "last_tested": _now_iso(),
    }
    if verdict is not None:
        update["verdict"] = verdict
    if result_path is not None:
        update["result_path"] = result_path
    _state.append_jsonl(_jsonl_path(), update)
    # apply locally for snapshot
    base = queue[id]
    for k, v in update.items():
        if k.startswith("_") or k == "id":
            continue
        base[k] = v
    _write_snapshot(queue)
    _log.event("INFO", "queue_mark", id=id, status=status, verdict=verdict)  # type: ignore[attr-defined]


def next_pending() -> dict | None:
    """Return the highest-priority pending entry; ties broken by oldest created."""
    queue = _rebuild_queue()
    pending = [e for e in queue.values() if e.get("status") == "pending"]
    if not pending:
        return None
    pending.sort(key=lambda r: (-int(r.get("priority", 0)), r.get("created", "")))
    return pending[0]


def list_entries(status_filter: str | None = None) -> list[dict]:
    queue = _rebuild_queue()
    rows = list(queue.values())
    if status_filter and status_filter != "all":
        rows = [r for r in rows if r.get("status") == status_filter]
    rows.sort(key=lambda r: r.get("id", ""))
    return rows


def get_entry(id: str) -> dict | None:
    return _rebuild_queue().get(id)


# ---------------------------------------------------------------------------
# Internal helper used by cross_pollinator dedup
# ---------------------------------------------------------------------------
def existing_keys() -> set[tuple[str, str]]:
    """Return set of (base_strategy, filter_name) tuples for cross_pollinator dedup."""
    out: set[tuple[str, str]] = set()
    for e in _rebuild_queue().values():
        spec = e.get("test_spec") or {}
        bs = spec.get("base_strategy")
        fn = spec.get("filter_name")
        if bs and fn:
            out.add((bs, fn))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_add(args: argparse.Namespace) -> int:
    print("Add new queue entry — interactive prompts. Ctrl-C to abort.")
    source = input(f"  source [{ '|'.join(sorted(_VALID_SOURCES))}]: ").strip() or "manual"
    source_ref = input("  source_ref (URL/title): ").strip()
    hypothesis = input("  hypothesis (1 paragraph): ").strip()
    priority_raw = input("  priority [1-5, default 3]: ").strip() or "3"
    spec_path = input("  test_spec JSON file path: ").strip()
    try:
        priority = int(priority_raw)
    except ValueError:
        priority = 3
    try:
        with Path(spec_path).expanduser().open("r", encoding="utf-8") as f:
            test_spec = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  ERROR reading test_spec: {e}", file=sys.stderr)
        return 2
    rid = add_entry(source, source_ref, hypothesis, test_spec, priority)
    print(f"  added id={rid}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    src = Path(args.from_file).expanduser()
    try:
        with src.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR reading {src}: {e}", file=sys.stderr)
        return 2
    if not isinstance(data, list):
        print("ERROR: import file must be a JSON array of entries", file=sys.stderr)
        return 2
    ids = import_entries(data)
    print(f"imported {len(ids)} entries: {ids}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    rows = list_entries(status_filter=args.status)
    if not rows:
        print("(no entries)")
        return 0
    print(f"{'id':<6}{'src':<18}{'pri':<5}{'status':<10}label")
    print("-" * 80)
    for r in rows:
        spec = r.get("test_spec") or {}
        label = spec.get("label") or spec.get("expert") or ""
        print(
            f"{r.get('id',''):<6}"
            f"{(r.get('source') or '')[:17]:<18}"
            f"{r.get('priority',''):<5}"
            f"{(r.get('status') or '')[:9]:<10}"
            f"{label}"
        )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    e = get_entry(args.id)
    if not e:
        print(f"(no entry id={args.id})", file=sys.stderr)
        return 1
    print(json.dumps(e, indent=2, default=str))
    return 0


def _cmd_mark(args: argparse.Namespace) -> int:
    try:
        mark(args.id, args.status, verdict=args.verdict, result_path=args.result_path)
    except (KeyError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(f"marked {args.id} -> {args.status}")
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    e = next_pending()
    if not e:
        print("null")
        return 0
    print(json.dumps(e, indent=2, default=str))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pella idea queue manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("add", help="Interactively add a new entry").set_defaults(func=_cmd_add)

    p_imp = sub.add_parser("import", help="Bulk-import from JSON array file")
    p_imp.add_argument("--from", dest="from_file", required=True)
    p_imp.set_defaults(func=_cmd_import)

    p_list = sub.add_parser("list", help="List queue entries")
    p_list.add_argument("--status", default=None, choices=["pending", "running", "complete", "failed", "all"])
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Show single entry as JSON")
    p_show.add_argument("id")
    p_show.set_defaults(func=_cmd_show)

    p_mark = sub.add_parser("mark", help="Update entry status")
    p_mark.add_argument("id")
    p_mark.add_argument("--status", required=True, choices=sorted(_VALID_STATUSES))
    p_mark.add_argument("--verdict", default=None)
    p_mark.add_argument("--result-path", dest="result_path", default=None)
    p_mark.set_defaults(func=_cmd_mark)

    sub.add_parser("next", help="Print next pending entry as JSON").set_defaults(func=_cmd_next)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
