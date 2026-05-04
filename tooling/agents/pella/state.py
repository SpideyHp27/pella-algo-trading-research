"""Pella atomic state I/O.

Sharing tier: methodology-shareable (no edge here, just plumbing).

All agents share state via small JSON / JSONL files on disk. To keep
multi-process safety we always:

    - write to a sibling .tmp file then os.replace() to the final path
    - flush + fsync before close on appends

Public surface:
    read_json(path, default=None)
    write_json_atomic(path, data)
    read_jsonl(path)
    append_jsonl(path, record)
    read_state(path)            # alias for read_json
    write_state(path, data)     # alias for write_json_atomic
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

PathLike = "str | Path"


def _to_path(p) -> Path:
    return p if isinstance(p, Path) else Path(p)


def read_json(path, default: Any = None) -> Any:
    """Read JSON from `path`. Return `default` if missing/unparseable."""
    p = _to_path(path)
    if not p.is_file():
        return default if default is not None else {}
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("read_json failed for %s: %s", p, e)
        return default if default is not None else {}


def write_json_atomic(path, data: Any) -> None:
    """Atomic-write `data` as JSON to `path` (parent dir created if needed)."""
    p = _to_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def read_jsonl(path) -> list[dict]:
    """Read a JSONL file. Skip blank lines, log unparseable ones."""
    p = _to_path(path)
    if not p.is_file():
        return []
    out: list[dict] = []
    with p.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                _log.warning("read_jsonl skipping bad line %s:%d: %s", p, ln, e)
    return out


def append_jsonl(path, record: dict) -> None:
    """Append `record` as one JSON line + newline to `path`."""
    p = _to_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # fsync not always available (e.g. some virtualised FS)
            pass


# convenience aliases
read_state = read_json
write_state = write_json_atomic
