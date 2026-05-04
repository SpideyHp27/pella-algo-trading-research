"""Cross-pollinator: generate strategy variants by combining deployed
archetypes with available filters.

Sharing tier: methodology-shareable (queue management is generic; queue
CONTENTS are INNER ONLY).

Outputs candidate `test_spec` stubs that the user hand-completes (filter
params) before queueing for backtest.

CLI:
    python tools/cross_pollinator.py [--max-combos N] [--push-to-queue]

Without --push-to-queue: prints candidates as JSON to stdout for review.
With --push-to-queue: imports via idea_queue_manager.import_entries.
Idempotent: dedup on (base_strategy, filter_name) against existing queue.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# allow running from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

import idea_queue_manager  # noqa: E402
from pella import config as _config  # noqa: E402
from pella import logger as _pella_logger  # noqa: E402
from pella import state as _state  # noqa: E402

_log = _pella_logger.get_logger("cross_pollinator")


# ---------------------------------------------------------------------------
# Built-in catalog
# ---------------------------------------------------------------------------
# Map deployment_config archetype slugs -> coarse archetype groups used here.
_ARCHETYPE_GROUP = {
    "trend_breakout_commodity": "trend_breakout",
    "trend_breakout_jpy": "trend_breakout",
    "calendar_pattern": "calendar_pattern",
    "mean_reversion_index": "mean_reversion_index",
}

FILTERS: dict[str, dict] = {
    "vcp": {
        "name": "Volatility Contraction",
        "include_file": "PellaVCP.mqh",
        "compatible_with": ["trend_breakout"],
    },
    "stationarity": {
        "name": "ADF / Hurst gate",
        "compatible_with": ["mean_reversion_index"],
    },
    "tuesday_only": {
        "name": "Tuesday-only entries",
        "compatible_with": ["trend_breakout", "calendar_pattern"],
    },
    "above_200sma": {
        "name": "Above 200-SMA regime filter",
        "compatible_with": ["trend_breakout", "calendar_pattern", "mean_reversion_index"],
    },
    "vol_regime_atr_pct": {
        "name": "ATR% > N regime gate",
        "compatible_with": ["trend_breakout"],
    },
    "ny_session_only": {
        "name": "NY session only entries",
        "compatible_with": ["trend_breakout", "calendar_pattern", "mean_reversion_index"],
    },
    "no_news_window": {
        "name": "Block entries 5min around HIGH news",
        "compatible_with": ["trend_breakout", "calendar_pattern", "mean_reversion_index"],
    },
}

# Hand-written incompatibility list. (archetype_group, filter_key, reason).
INCOMPATIBLE: list[tuple[str, str, str]] = [
    ("mean_reversion_index", "vcp", "VCP is a breakout pattern; doesn't apply to mean-reversion entries"),
    ("calendar_pattern", "stationarity", "Calendar effects don't depend on stationarity"),
    ("trend_breakout", "stationarity", "Trend-following doesn't need mean-reversion stationarity gate"),
]
_INCOMPATIBLE_SET = {(a, f) for a, f, _ in INCOMPATIBLE}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _archetype_group(strategy: dict) -> str | None:
    raw = strategy.get("archetype")
    if not raw:
        return None
    return _ARCHETYPE_GROUP.get(raw, raw)


def _make_test_spec_template(base: dict, filter_key: str) -> dict:
    return {
        "expert": base.get("expert"),
        "symbol": base.get("symbol"),
        "tf": base.get("tf"),
        "start_date": "2020.01.01",
        "end_date": "2026.04.30",
        "modelling": 8,
        "deposit": 50000,
        "currency": "USD",
        "leverage": 100,
        "delays": 1,
        "inputs": {"_TODO_filter_specific_params": "hand-fill before queueing"},
        "label": f"{base.get('label')}__{filter_key}",
        "base_strategy": base.get("label"),
        "filter_name": filter_key,
    }


def _hypothesis_text(base_strategy: str, base_archetype: str, filter_name: str) -> str:
    return (
        f"{base_strategy} (archetype: {base_archetype}) gated by {filter_name}. "
        "Hypothesis: filter reduces false signals; expect lower trade count, "
        "higher PF, similar Sharpe."
    )


# ---------------------------------------------------------------------------
# Core generator
# ---------------------------------------------------------------------------
def generate_candidates(max_combos: int = 10) -> list[dict]:
    """Return a list of candidate dicts. Idempotent for fixed input config."""
    strategies = _config.get_strategies()

    # Popularity = how many deployed strategies share each archetype group.
    pop = Counter(_archetype_group(s) for s in strategies if _archetype_group(s))

    candidates: list[dict] = []
    # Stable iteration: deployment order, then alphabetic filter key.
    for base in strategies:
        group = _archetype_group(base)
        if not group:
            continue
        for filter_key in sorted(FILTERS.keys()):
            spec = FILTERS[filter_key]
            if group not in spec["compatible_with"]:
                continue
            if (group, filter_key) in _INCOMPATIBLE_SET:
                continue
            cand = {
                "base_strategy": base.get("label"),
                "base_archetype": base.get("archetype"),
                "filter_key": filter_key,
                "filter_name": spec["name"],
                "hypothesis_text": _hypothesis_text(base.get("label", ""), base.get("archetype", ""), spec["name"]),
                "test_spec_template": _make_test_spec_template(base, filter_key),
            }
            candidates.append(cand)

    # Sort by popularity of base archetype (desc), then base strategy (asc),
    # then filter_key (asc) — fully deterministic.
    candidates.sort(
        key=lambda c: (
            -pop.get(_ARCHETYPE_GROUP.get(c["base_archetype"], c["base_archetype"]), 0),
            c["base_strategy"] or "",
            c["filter_key"],
        )
    )
    return candidates[: max(0, int(max_combos))]


# ---------------------------------------------------------------------------
# Archive + push
# ---------------------------------------------------------------------------
def _archive_path() -> Path:
    research_root = Path(_config.get_paths()["research_root"])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return research_root / "cross_pollinator" / f"{today}.json"


def _archive(candidates: list[dict]) -> Path:
    path = _archive_path()
    _state.write_json_atomic(path, {"generated_at": datetime.now(timezone.utc).isoformat(), "candidates": candidates})
    return path


def _push_to_queue(candidates: list[dict]) -> tuple[int, int]:
    """Push candidates that aren't already in the queue. Returns (added, skipped)."""
    existing = idea_queue_manager.existing_keys()
    fresh: list[dict] = []
    skipped = 0
    for c in candidates:
        key = (c.get("base_strategy"), c.get("filter_key"))
        if key in existing:
            skipped += 1
            continue
        fresh.append({
            "source": "cross_pollinator",
            "source_ref": f"{c['base_strategy']}__{c['filter_key']}",
            "hypothesis": c["hypothesis_text"],
            "test_spec": c["test_spec_template"],
            "priority": 3,
        })
    if fresh:
        idea_queue_manager.import_entries(fresh)
    return len(fresh), skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pella cross-pollinator")
    cfg_max = int(_config.get_discovery().get("cross_pollinator", {}).get("max_combos_per_run", 10))
    p.add_argument("--max-combos", type=int, default=cfg_max, help=f"max candidates (default {cfg_max})")
    p.add_argument("--push-to-queue", action="store_true", help="import candidates into idea queue")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    candidates = generate_candidates(max_combos=args.max_combos)
    archive = _archive(candidates)
    _log.event(  # type: ignore[attr-defined]
        "INFO",
        "cross_pollinator_generated",
        count=len(candidates),
        archive=str(archive),
    )
    if args.push_to_queue:
        added, skipped = _push_to_queue(candidates)
        _log.event(  # type: ignore[attr-defined]
            "INFO", "cross_pollinator_pushed", added=added, skipped=skipped
        )
        print(json.dumps({"added": added, "skipped": skipped, "archive": str(archive)}, indent=2))
        return 0
    print(json.dumps({"candidates": candidates, "archive": str(archive)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
