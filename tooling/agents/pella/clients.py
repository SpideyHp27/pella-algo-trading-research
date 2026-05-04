"""Pella shared clients.

Sharing tier: methodology-shareable (no edge here, just plumbing).

Three thin clients all agents share. Every method is fail-quiet: errors are
logged via the shared logger and a sensible default (empty list / None /
False) is returned. Only un-loadable config is fatal.

    MT5Client            - wraps the MetaTrader5 Python package
    AristhrottleClient   - Bearer-auth GET/PUT against /api/state
    AlertClient          - level-routed alerts to disk + (Day-2) wires

Day-2 wires (Telegram, email, Aristhrottle banner) are stubbed and emit a
TODO log line if their config block is enabled. Wiring them is a separate
follow-up.
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config as _config
from . import state as _state
from .logger import get_logger

# MetaTrader5 is optional; the MT5Client must fail-quiet when unavailable.
try:
    import MetaTrader5 as _mt5  # type: ignore
    _MT5_OK = True
except Exception:  # pragma: no cover - environment-dependent
    _mt5 = None  # type: ignore
    _MT5_OK = False


_STATE_KEY = "aristhrottle_state_v1"


# ---------------------------------------------------------------------------
# MT5Client
# ---------------------------------------------------------------------------
class MT5Client:
    """Thin wrapper over the MetaTrader5 Python package.

    Fail-quiet: every call returns [] / None and logs a warning when MT5 is
    not installed, not connected, or raises.
    """

    def __init__(self) -> None:
        self._log = get_logger("mt5_client")
        self._initialized = False
        if not _MT5_OK:
            self._log.warning("MetaTrader5 package not importable; client is a no-op.")
            return
        try:
            paths = _config.get_paths()
            exe = paths.get("mt5_terminal_exe")
            ok = _mt5.initialize(path=exe) if exe else _mt5.initialize()
            if not ok:
                err = _mt5.last_error() if hasattr(_mt5, "last_error") else "unknown"
                self._log.warning("mt5.initialize() failed: %s", err)
            else:
                self._initialized = True
        except Exception as e:
            self._log.warning("MT5Client init exception: %s", e)

    # context manager sugar
    def __enter__(self) -> "MT5Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        if self._initialized and _MT5_OK:
            try:
                _mt5.shutdown()
            except Exception:
                pass
            self._initialized = False

    # ---- queries ----------------------------------------------------------
    def positions(self, symbol: str | None = None) -> list[dict]:
        if not self._initialized:
            return []
        try:
            raw = _mt5.positions_get(symbol=symbol) if symbol else _mt5.positions_get()
            if raw is None:
                return []
            return [self._pos_to_dict(p) for p in raw]
        except Exception as e:
            self._log.warning("positions() failed: %s", e)
            return []

    def deals(self, from_dt: datetime, to_dt: datetime) -> list[dict]:
        if not self._initialized:
            return []
        try:
            raw = _mt5.history_deals_get(from_dt, to_dt)
            if raw is None:
                return []
            return [self._deal_to_dict(d) for d in raw]
        except Exception as e:
            self._log.warning("deals() failed: %s", e)
            return []

    def account_info(self) -> dict | None:
        if not self._initialized:
            return None
        try:
            info = _mt5.account_info()
            if info is None:
                return None
            return {
                "login": getattr(info, "login", None),
                "balance": getattr(info, "balance", 0.0),
                "equity": getattr(info, "equity", 0.0),
                "margin": getattr(info, "margin", 0.0),
                "free_margin": getattr(info, "margin_free", 0.0),
                "margin_level": getattr(info, "margin_level", 0.0),
                "profit": getattr(info, "profit", 0.0),
                "currency": getattr(info, "currency", ""),
                "leverage": getattr(info, "leverage", 0),
            }
        except Exception as e:
            self._log.warning("account_info() failed: %s", e)
            return None

    # ---- internal mappers -------------------------------------------------
    @staticmethod
    def _type_to_side(tp: int) -> str:
        # MT5: 0=BUY, 1=SELL for positions
        if tp == 0:
            return "BUY"
        if tp == 1:
            return "SELL"
        return f"TYPE_{tp}"

    @staticmethod
    def _entry_to_str(e: int) -> str:
        # MT5 deal entry: 0=IN, 1=OUT, 2=INOUT, 3=OUT_BY
        return {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}.get(e, f"ENTRY_{e}")

    def _pos_to_dict(self, p) -> dict:
        return {
            "ticket": getattr(p, "ticket", None),
            "symbol": getattr(p, "symbol", ""),
            "magic": getattr(p, "magic", 0),
            "type": self._type_to_side(getattr(p, "type", -1)),
            "volume": getattr(p, "volume", 0.0),
            "price_open": getattr(p, "price_open", 0.0),
            "sl": getattr(p, "sl", 0.0),
            "tp": getattr(p, "tp", 0.0),
            "profit": getattr(p, "profit", 0.0),
            "time": datetime.fromtimestamp(getattr(p, "time", 0), tz=timezone.utc),
        }

    def _deal_to_dict(self, d) -> dict:
        return {
            "ticket": getattr(d, "ticket", None),
            "time": datetime.fromtimestamp(getattr(d, "time", 0), tz=timezone.utc),
            "symbol": getattr(d, "symbol", ""),
            "magic": getattr(d, "magic", 0),
            "type": self._type_to_side(getattr(d, "type", -1)),
            "entry": self._entry_to_str(getattr(d, "entry", -1)),
            "volume": getattr(d, "volume", 0.0),
            "price": getattr(d, "price", 0.0),
            "profit": getattr(d, "profit", 0.0),
            "swap": getattr(d, "swap", 0.0),
            "commission": getattr(d, "commission", 0.0),
        }


# ---------------------------------------------------------------------------
# AristhrottleClient
# ---------------------------------------------------------------------------
class AristhrottleClient:
    """Bearer-auth GET/PUT against the MCP REST surface.

    The dashboard stores everything inside a single key:
        keys[aristhrottle_state_v1] = json.dumps({trades, strategies, ...})

    Helpers `append_trades` and `update_account_snapshot` perform a
    GET-modify-PUT round-trip to keep prior data intact.
    """

    DEFAULT_BASE = "https://aristhrottle.netlify.app"

    def __init__(self) -> None:
        self._log = get_logger("aristhrottle_client")
        creds = _config.get_aristhrottle_creds()
        self._key = creds.get("ARISTHROTTLE_API_KEY", "")
        self._base = creds.get("ARISTHROTTLE_BASE") or self.DEFAULT_BASE
        if not self._key:
            self._log.warning("ARISTHROTTLE_API_KEY not in creds file; client is a no-op.")

    # ---- low-level --------------------------------------------------------
    def _request(self, method: str, url: str, body: dict | None = None,
                 timeout: int = 30) -> tuple[int, str]:
        if not self._key:
            return 0, "no-credentials"
        headers = {"authorization": f"Bearer {self._key}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["content-type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            try:
                body_txt = e.read().decode("utf-8", errors="replace") if e.fp else ""
            except Exception:
                body_txt = ""
            self._log.warning("HTTPError %s on %s %s", e.code, method, url)
            return e.code, body_txt
        except (urllib.error.URLError, socket.timeout) as e:
            self._log.warning("URLError on %s %s: %s", method, url, e)
            return 0, f"URLError: {e}"
        except Exception as e:
            self._log.warning("Unexpected error on %s %s: %s", method, url, e)
            return 0, str(e)

    # ---- public state I/O -------------------------------------------------
    def get_state(self, bucket: str = "default") -> dict:
        suffix = f"?bucket={bucket}" if bucket and bucket != "default" else ""
        code, body = self._request("GET", f"{self._base}/api/state{suffix}")
        if code != 200 or not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            self._log.warning("get_state: response was not JSON")
            return {}

    def put_state(self, blob: dict, bucket: str = "default") -> tuple[int, str]:
        suffix = f"?bucket={bucket}" if bucket and bucket != "default" else ""
        return self._request("PUT", f"{self._base}/api/state{suffix}", body=blob)

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _trade_dedup_key(t: dict) -> tuple:
        return (
            t.get("exitDate") or t.get("date") or "",
            t.get("entryDate") or "",
            t.get("symbol") or "",
            t.get("magic") or 0,
        )

    def _load_inner(self, bucket: str = "default") -> dict:
        outer = self.get_state(bucket=bucket)
        keys = (outer.get("keys") or {}) if isinstance(outer, dict) else {}
        raw = keys.get(_STATE_KEY)
        if not raw:
            return {"trades": [], "strategies": [], "live_snapshots": []}
        try:
            inner = json.loads(raw)
        except json.JSONDecodeError:
            return {"trades": [], "strategies": [], "live_snapshots": []}
        inner.setdefault("trades", [])
        inner.setdefault("strategies", [])
        inner.setdefault("live_snapshots", [])
        return inner

    def _save_inner(self, inner: dict, bucket: str = "default") -> tuple[int, str]:
        blob = {"keys": {_STATE_KEY: json.dumps(inner)}}
        return self.put_state(blob, bucket=bucket)

    def append_trades(self, strategy_name: str, trade_rows: list[dict],
                      bucket: str = "default") -> tuple[int, str]:
        if not trade_rows:
            return 0, "no-rows"
        inner = self._load_inner(bucket=bucket)
        existing = {self._trade_dedup_key(t) for t in inner["trades"]}
        added = 0
        for r in trade_rows:
            r = dict(r)
            r["strategy"] = strategy_name
            if self._trade_dedup_key(r) in existing:
                continue
            inner["trades"].append(r)
            existing.add(self._trade_dedup_key(r))
            added += 1
        self._log.info("append_trades %s: +%d (total=%d)",
                       strategy_name, added, len(inner["trades"]))
        return self._save_inner(inner, bucket=bucket)

    def update_account_snapshot(self, account_id: str, balance: float,
                                equity: float, dd_pct: float,
                                margin_level: float,
                                bucket: str = "default") -> tuple[int, str]:
        inner = self._load_inner(bucket=bucket)
        snap = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "account_id": account_id,
            "balance": balance,
            "equity": equity,
            "dd_pct": dd_pct,
            "margin_level": margin_level,
        }
        inner["live_snapshots"].append(snap)
        return self._save_inner(inner, bucket=bucket)


# ---------------------------------------------------------------------------
# AlertClient
# ---------------------------------------------------------------------------
class AlertClient:
    """Level-routed alerts.

    Routing matrix (Day-1):

        INFO  -> log.event only
        WARN  -> alerts_dir/WARN/<ts>_<agent>.json + alerts_jsonl + (TODO Telegram)
        KILL  -> alerts_dir/KILL/<ts>_<agent>.json + alerts_jsonl + (TODO email + banner)

    Returns True on dispatch, False on any internal error. Never raises.
    """

    LEVELS = ("INFO", "WARN", "KILL")

    def __init__(self, agent_name: str = "alert_client") -> None:
        self._agent = agent_name
        self._log = get_logger(agent_name)
        self._paths = _config.get_paths()
        self._alerting = _config.get_alerting()

    def alert(self, level: str, title: str, body: str, route: str = "auto") -> bool:
        try:
            level = level.upper()
            if level not in self.LEVELS:
                self._log.warning("alert(): unknown level %r — defaulting to WARN", level)
                level = "WARN"

            self._log.event(  # type: ignore[attr-defined]
                level, "alert",
                title=title, body=body, route=route, alert_level=level,
            )

            if level == "INFO":
                return True

            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": level,
                "agent": self._agent,
                "title": title,
                "body": body,
                "route": route,
            }

            # Disk: per-level folder + master JSONL
            alerts_dir = Path(self._paths["alerts_dir"]) / level
            file_path = alerts_dir / f"{ts}_{self._agent}.json"
            _state.write_json_atomic(file_path, record)
            _state.append_jsonl(self._paths["alerts_jsonl"], record)

            # Day-2 stubs
            if level in ("WARN", "KILL"):
                tg = self._alerting.get("telegram", {}) or {}
                if tg.get("enabled"):
                    self._log.info("TODO: wire Telegram dispatch (level=%s)", level)
            if level == "KILL":
                em = self._alerting.get("email", {}) or {}
                if em.get("enabled"):
                    self._log.info("TODO: wire email dispatch (level=KILL)")
                ar = self._alerting.get("aristhrottle_banner", {}) or {}
                if ar.get("enabled"):
                    self._log.info("TODO: wire Aristhrottle banner dispatch (level=KILL)")

            return True
        except Exception as e:
            # fail-quiet
            try:
                self._log.warning("AlertClient.alert() failed: %s", e)
            except Exception:
                pass
            return False
