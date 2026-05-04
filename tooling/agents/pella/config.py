"""Pella config loaders.

Sharing tier: methodology-shareable (no edge here, just plumbing).

Loads two config files, both single-source-of-truth for runtime agents:

    agent_config.yaml       - paths, alerting, monitoring, discovery, logging
    deployment_config.json  - accounts + deployed strategies

All loaders are cached by file mtime; mutating the source files on disk
is picked up automatically on the next call (no process restart needed).

No path string is hardcoded outside of the bootstrap constants below.
Callers must always go through `get_paths()` to obtain a directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml  # pyyaml

# ---------------------------------------------------------------------------
# Bootstrap: where agent_config.yaml lives. This is the ONLY hardcoded path
# in the whole package and is overrideable via PELLA_AGENT_CONFIG env var
# (used by the Linux VPS deploy).
# ---------------------------------------------------------------------------
_DEFAULT_AGENT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "agent_config.yaml"
AGENT_CONFIG_PATH = Path(os.environ.get("PELLA_AGENT_CONFIG", str(_DEFAULT_AGENT_CONFIG)))

_CREDS_PATH = Path.home() / ".pella_aristhrottle_creds"

# mtime-keyed caches
_cache: dict[str, tuple[float, Any]] = {}


def _load_yaml_cached(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Pella config not found: {path}")
    mtime = path.stat().st_mtime
    key = f"yaml:{path}"
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _cache[key] = (mtime, data)
    return data


def _load_json_cached(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Pella config not found: {path}")
    mtime = path.stat().st_mtime
    key = f"json:{path}"
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    _cache[key] = (mtime, data)
    return data


# ---------------------------------------------------------------------------
# agent_config.yaml accessors
# ---------------------------------------------------------------------------
def get_config() -> dict:
    """Return the full parsed agent_config.yaml as a dict."""
    return _load_yaml_cached(AGENT_CONFIG_PATH)


def _resolve_paths(raw: dict) -> dict:
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            out[k] = v
            continue
        expanded = os.path.expanduser(v)
        out[k] = os.path.normpath(expanded)
    return out


def get_paths() -> dict:
    """Return the `paths:` subsection with ~ expanded and normpath'd."""
    cfg = get_config()
    return _resolve_paths(cfg.get("paths", {}))


def get_alerting() -> dict:
    return get_config().get("alerting", {})


def get_monitoring() -> dict:
    return get_config().get("monitoring", {})


def get_discovery() -> dict:
    return get_config().get("discovery", {})


def get_backtesting() -> dict:
    return get_config().get("backtesting", {})


def get_logging_config() -> dict:
    return get_config().get("logging", {})


# ---------------------------------------------------------------------------
# deployment_config.json accessors
# ---------------------------------------------------------------------------
def get_deployment() -> dict:
    """Return the full parsed deployment_config.json dict."""
    paths = get_paths()
    dep_path = Path(paths["deployment_config"])
    return _load_json_cached(dep_path)


def get_strategies() -> list[dict]:
    """Return the list of strategy dicts from deployment_config."""
    return list(get_deployment().get("strategies", []))


def get_strategy(label: str) -> dict | None:
    """Return a single strategy dict by `label`, or None."""
    for s in get_strategies():
        if s.get("label") == label:
            return s
    return None


def get_account(account_id: str) -> dict | None:
    """Return account dict by id, or None."""
    accounts = get_deployment().get("accounts", {})
    return accounts.get(account_id)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def get_aristhrottle_creds() -> dict:
    """Read ~/.pella_aristhrottle_creds and return parsed key=value dict.

    Returns at minimum:
        {ARISTHROTTLE_API_KEY: ..., ARISTHROTTLE_BASE: ...}

    Empty dict if creds file missing.
    """
    if not _CREDS_PATH.is_file():
        return {}
    out: dict[str, str] = {}
    for line in _CREDS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out
