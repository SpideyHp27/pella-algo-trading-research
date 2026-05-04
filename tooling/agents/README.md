# Pella agent stack

The runtime layer that watches deployed strategies, scores live signals,
and feeds new ideas back into research. Five daily/hourly agents + one
nightly worker + one weekly idea generator, all sharing one infrastructure
package.

## Layout

```
agents/
├── pella/                    Shared infrastructure package
│   ├── config.py             mtime-cached YAML/JSON loaders
│   ├── logger.py             JSON-line + console logger with daily rotation
│   ├── state.py              Atomic JSON / JSONL helpers
│   └── clients.py            MT5Client, AristhrottleClient, AlertClient
│
├── live_monitor_agent.py     Hourly MT5 poll + Aristhrottle push
├── edge_decay_watchdog.py    Daily rolling-90d Sharpe scan + KILL flag
├── paper_replay_agent.py     Daily signal-vs-fill divergence in pips
├── discovery_agent.py        Nightly idea-queue worker
├── idea_queue_manager.py     Append-only JSONL queue (lib + CLI)
├── cross_pollinator.py       Weekly archetype × filter idea generator
├── youtube_extractor.py      Transcript fetcher with hypothesis extraction
│
└── scheduler/                Cron installers (Win Task Scheduler + systemd)
```

The MQ5 include `PellaSignalLog.mqh` lives at `../mql5/Include/`. EAs
that want their signals scored by `paper_replay_agent` must
`#include` it and call `PellaLogSignal()` at every entry/exit decision.

## Pattern

Every agent is one-shot, idempotent, and structured around three contracts:

1. Read `agent_config.yaml` for thresholds, paths, alerting rules.
   Read `deployment_config.json` for the list of deployed strategies +
   broker accounts.
2. Do its scan / poll / run.
3. Emit a verdict via `AlertClient.alert(level, title, body)` —
   `INFO` to disk + log, `WARN` to disk + jsonl + (Day-2 wires for
   Telegram), `KILL` to disk + jsonl + strategy pause flag at
   `<lab_root>/agent_state/pause_flags/<label>.PAUSE`.

State is persisted under `<lab_root>/agent_state/` per-agent. Logs are
JSON-line so they're directly tail-able + jq-able.

## Configuration

Agents read two files at runtime:

- `agent_config.yaml` — thresholds, paths, alerting (per-host)
- `deployment_config.json` — accounts + deployed strategies (per-trader)

These are **NEVER committed**. Both contain account IDs, magic numbers,
broker paths, and baseline performance metrics. Each user maintains their
own pair locally; this repo carries the *code* that consumes them.

A starter `agent_config.yaml.example` and `deployment_config.json.example`
are noticeably absent from this drop — when wiring this for a fresh host,
follow the schema referenced in each agent's docstring and the
`pella.config.get_*()` accessor list.

## Running

Until the scheduler installer is registered, fire any agent manually:

```bash
python agents/edge_decay_watchdog.py --dry-run
python agents/paper_replay_agent.py  --dry-run
python agents/discovery_agent.py     --dry-run --ignore-window
python agents/live_monitor_agent.py  --dry-run
python agents/cross_pollinator.py
```

For the scheduler, see `agents/scheduler/README.md`.

## Status

| Phase | Component | Status |
|---|---|---|
| 0 | `pella/` shared infra | shipped + smoke-passed |
| 1 | 5 runtime agents | shipped + smoke-passed |
| 2 | `discovery_agent.py` | shipped + smoke-passed |
| 3 | `paper_replay_agent.py` + `PellaSignalLog.mqh` | shipped + smoke-passed |
| 4 | scheduler installers (Win + Linux) | shipped + smoke-passed |

Production-running validation pending: needs the `PellaLogSignal()` calls
wired into deployed EAs + a host the scheduler installs onto.

## License

MIT (per repo root `LICENSE`). Trading-software disclaimer applies — the
agents are infrastructure, not investment advice.
