"""Pella structured logger.

Sharing tier: methodology-shareable (no edge here, just plumbing).

Every agent calls `get_logger("agent_name")` exactly once at startup.
The returned logger:

    - writes JSON-line records to <paths.logs_dir>/<agent>/YYYY-MM-DD.jsonl
      (daily rotation via TimedRotatingFileHandler)
    - mirrors human-readable INFO+ to console
    - exposes a `.event(level, event_type, **fields)` helper that emits a
      structured record:
          {"ts": iso8601, "agent": ..., "level": ..., "event": ..., **fields}

Configuration (level, retain_days) comes from `pella.config.get_logging_config()`.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from . import config as _config

# Cache so multiple imports return the same logger
_loggers: dict[str, logging.Logger] = {}


class _JsonLineFormatter(logging.Formatter):
    """Formats records that already carry a JSON dict in `record.msg`.

    For plain string log() calls we wrap them in a minimal envelope so the
    file always stays valid JSONL.
    """

    def format(self, record: logging.LogRecord) -> str:
        if isinstance(record.msg, dict):
            payload = dict(record.msg)
        else:
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent": getattr(record, "agent", record.name),
                "level": record.levelname,
                "event": "log",
                "message": record.getMessage(),
            }
        # Always ensure the canonical fields exist
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        payload.setdefault("agent", getattr(record, "agent", record.name))
        payload.setdefault("level", record.levelname)
        return json.dumps(payload, default=str)


def _make_event_method(agent: str, logger: logging.Logger):
    def event(level: str, event_type: str, **fields: Any) -> None:
        lvl = getattr(logging, level.upper(), logging.INFO)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": agent,
            "level": level.upper(),
            "event": event_type,
            **fields,
        }
        logger.log(lvl, record)

    return event


def get_logger(agent_name: str) -> logging.Logger:
    """Return a configured JSON-line + console logger for `agent_name`.

    Idempotent: subsequent calls with the same name return the same logger.
    """
    if agent_name in _loggers:
        return _loggers[agent_name]

    paths = _config.get_paths()
    logging_cfg = _config.get_logging_config()
    level_name = (logging_cfg.get("level") or "INFO").upper()
    retain_days = int(logging_cfg.get("retain_days") or 90)

    logs_root = Path(paths["logs_dir"]) / agent_name
    logs_root.mkdir(parents=True, exist_ok=True)
    today_path = logs_root / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"

    logger = logging.getLogger(f"pella.{agent_name}")
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    # In case the same logger was partially configured by another import
    for h in list(logger.handlers):
        logger.removeHandler(h)

    # File: daily-rotating JSONL
    file_handler = TimedRotatingFileHandler(
        filename=str(today_path),
        when="midnight",
        interval=1,
        backupCount=retain_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_JsonLineFormatter())
    logger.addHandler(file_handler)

    # Console: human-readable INFO+
    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    logger.addHandler(console)

    # Bind the structured event helper directly on the logger instance
    logger.event = _make_event_method(agent_name, logger)  # type: ignore[attr-defined]

    _loggers[agent_name] = logger
    return logger
