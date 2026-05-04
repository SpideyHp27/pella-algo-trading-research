# Pella scheduler installers

Phase 4. Drops the 5 runtime agents on a recurring schedule.

| Agent | UTC schedule | Notes |
|---|---|---|
| `live_monitor_agent.py` | hourly 09:00-22:00 | poll_interval_market_hours = 3600 |
| `live_monitor_agent.py` | 04:00 daily | poll_interval_overnight = 21600 (one fire mid-overnight) |
| `edge_decay_watchdog.py` | 22:30 daily | after US cash close |
| `paper_replay_agent.py` | 23:00 daily | after edge_decay |
| `discovery_agent.py` | 00:30 daily | within `discovery.agent.nightly_run_window_utc` |
| `cross_pollinator.py --push-to-queue` | Sunday 18:00 | weekly idea-queue refill |

Two parallel installer trees ship; pick the one matching your VPS:

```
scheduler/
├── windows/    -- QuantVPS, local Windows host
│   ├── tasks/  -- 6 Task Scheduler XML templates with placeholders
│   ├── install.ps1
│   └── uninstall.ps1
└── linux/      -- Vultr / any systemd Linux VPS
    ├── units/  -- 5 .service + 6 .timer templates with placeholders
    ├── install.sh
    └── uninstall.sh
```

The shipped XML / unit files are **templates**: they contain
`__PELLA_PYTHON__`, `__PELLA_LAB_ROOT__`, and `__PELLA_USER__` placeholders.
The installer scripts substitute these (resolved from env vars or sensible
defaults) before registering with the OS.

## Windows install

```powershell
# Defaults: PELLA_PYTHON=python (PATH lookup), PELLA_LAB_ROOT=C:\Lab,
# PELLA_USER=current user.
cd C:\Lab\NT8Bridge\scheduler\windows
.\install.ps1
# inspect:
schtasks /Query /FO LIST | Select-String "Pella_"
# uninstall:
.\uninstall.ps1
```

To override:

```powershell
$env:PELLA_PYTHON   = "C:\Python314\python.exe"
$env:PELLA_LAB_ROOT = "D:\Pella"
.\install.ps1
```

`install.ps1 -DryRun` renders the resolved XMLs to `generated/` for inspection
without registering anything with `schtasks`.

## Linux install

```bash
# Run as the user the agents should execute under (NOT root).
cd /opt/pella/NT8Bridge/scheduler/linux
PELLA_PYTHON=/usr/bin/python3 PELLA_LAB_ROOT=/opt/pella ./install.sh
# inspect:
systemctl --user list-timers 'pella-*'
# uninstall:
./uninstall.sh
```

Units install under `~/.config/systemd/user/` so no root is needed. If you
prefer system-wide (always-on, even when no user logged in), pass
`--system` to `install.sh`; that requires sudo and writes to
`/etc/systemd/system/`.

## Running the agents in the meantime (no scheduler)

Until the installer runs, you can fire any agent manually:

```bash
python C:/Lab/NT8Bridge/tools/edge_decay_watchdog.py --dry-run
python C:/Lab/NT8Bridge/tools/paper_replay_agent.py  --dry-run
python C:/Lab/NT8Bridge/tools/discovery_agent.py     --dry-run --ignore-window
python C:/Lab/NT8Bridge/tools/live_monitor_agent.py  --dry-run
python C:/Lab/NT8Bridge/tools/cross_pollinator.py
```

## Sharing tier

INNER ONLY (the cron schedule itself isn't sensitive, but the agents it
schedules read INNER config).
