"""Phase-0 smoke test for the pella shared infra package.

Sharing tier: methodology-shareable (no edge here, just plumbing).

Run:
    python -B tools/pella/_smoke_test.py

Exits 0 on success (warnings OK), nonzero on any uncaught traceback.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# allow running from any cwd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pella import clients, config, logger, state  # noqa: E402


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    failures = 0

    _section("config loaders")
    cfg = config.get_config()
    paths = config.get_paths()
    deployment = config.get_deployment()
    strategies = config.get_strategies()
    print(f"  agent_config keys     : {sorted(cfg.keys())}")
    print(f"  paths.logs_dir        : {paths.get('logs_dir')}")
    print(f"  deployment.accounts   : {len(deployment.get('accounts', {}))}")
    print(f"  strategies            : {len(strategies)}")
    if strategies:
        print(f"  first strategy label  : {strategies[0].get('label')}")
        single = config.get_strategy(strategies[0]["label"])
        print(f"  get_strategy() ok     : {single is not None}")

    _section("state round-trip")
    sample_path = Path(paths["agent_state_dir"]) / "_smoke_state.json"
    payload = {"hello": "world", "n": 42}
    state.write_json_atomic(sample_path, payload)
    rt = state.read_json(sample_path)
    print(f"  round-trip ok         : {rt == payload} ({rt})")
    if rt != payload:
        failures += 1

    _section("logger event")
    lg = logger.get_logger("smoke_test")
    lg.event("INFO", "ping", value=42, note="phase-0 smoke")  # type: ignore[attr-defined]
    # verify the event landed in today's jsonl
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = Path(paths["logs_dir"]) / "smoke_test" / f"{today}.jsonl"
    found = False
    if log_file.is_file():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "ping" and rec.get("value") == 42:
                found = True
                break
    print(f"  log file              : {log_file}")
    print(f"  event line found      : {found}")
    if not found:
        failures += 1

    _section("AlertClient INFO fire")
    ac = clients.AlertClient("smoke_test")
    ok = ac.alert("INFO", "Phase 0 smoke", "Shared infra loaded")
    print(f"  alert(INFO) returned  : {ok}")

    _section("MT5Client (fail-quiet if MT5 down)")
    try:
        with clients.MT5Client() as mc:
            ai = mc.account_info()
            pos = mc.positions()
            print(f"  account_info()        : {'<got data>' if ai else None}")
            print(f"  positions() count     : {len(pos)}")
    except Exception as e:
        print(f"  MT5Client raised      : {e}")
        failures += 1

    _section("AristhrottleClient.get_state() (network optional)")
    try:
        ar = clients.AristhrottleClient()
        st = ar.get_state()
        keys_ct = len((st.get("keys") or {})) if isinstance(st, dict) else 0
        print(f"  state outer keys      : {keys_ct}")
    except Exception as e:
        print(f"  AristhrottleClient raised: {e}")
        failures += 1

    _section("summary")
    print(f"  failures              : {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        rc = 2
    sys.exit(rc)
