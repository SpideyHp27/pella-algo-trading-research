#!/usr/bin/env bash
# Installs Pella systemd timers + services. INNER ONLY.
#
# Defaults (override via env vars):
#   PELLA_PYTHON   = $(command -v python3)
#   PELLA_LAB_ROOT = directory two levels up from this script
#   PELLA_USER     = $USER (used for the install path; --system overrides)
#
# Usage:
#   ./install.sh                # user units under ~/.config/systemd/user/
#   ./install.sh --system       # system units under /etc/systemd/system/
#                               #  (requires sudo, runs even when no user logged in)
#   ./install.sh --dry-run      # render to ./generated/ without enabling
#
set -euo pipefail

DRY_RUN=0
SYSTEM_SCOPE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --system)  SYSTEM_SCOPE=1 ;;
        -h|--help)
            sed -n '2,16p' "$0"
            exit 0 ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATES="$HERE/units"
GENERATED="$HERE/generated"
mkdir -p "$GENERATED"

# ----- Resolve substitutions -----------------------------------------------
PELLA_PYTHON="${PELLA_PYTHON:-$(command -v python3 || true)}"
if [[ -z "$PELLA_PYTHON" ]]; then
    echo "ERROR: PELLA_PYTHON not set and python3 not on PATH" >&2
    exit 1
fi
if [[ ! -x "$PELLA_PYTHON" ]]; then
    echo "ERROR: PELLA_PYTHON is not executable: $PELLA_PYTHON" >&2
    exit 1
fi

if [[ -z "${PELLA_LAB_ROOT:-}" ]]; then
    PELLA_LAB_ROOT="$(cd "$HERE/../.." && pwd)"
fi
if [[ ! -d "$PELLA_LAB_ROOT" ]]; then
    echo "ERROR: PELLA_LAB_ROOT does not exist: $PELLA_LAB_ROOT" >&2
    exit 1
fi

PELLA_USER="${PELLA_USER:-${USER}}"

echo "Pella scheduler install:"
echo "  PELLA_PYTHON   = $PELLA_PYTHON"
echo "  PELLA_LAB_ROOT = $PELLA_LAB_ROOT"
echo "  PELLA_USER     = $PELLA_USER"
echo "  scope          = $([[ $SYSTEM_SCOPE -eq 1 ]] && echo system || echo user)"
echo "  dry_run        = $DRY_RUN"
echo

# ----- Verify each agent script exists -------------------------------------
for rel in \
    NT8Bridge/tools/live_monitor_agent.py \
    NT8Bridge/tools/edge_decay_watchdog.py \
    NT8Bridge/tools/paper_replay_agent.py \
    NT8Bridge/tools/discovery_agent.py \
    NT8Bridge/tools/cross_pollinator.py
do
    if [[ ! -f "$PELLA_LAB_ROOT/$rel" ]]; then
        echo "ERROR: agent script missing: $PELLA_LAB_ROOT/$rel" >&2
        exit 1
    fi
done

# ----- Render templates ----------------------------------------------------
shopt -s nullglob
rendered=()
for src in "$TEMPLATES"/*.service "$TEMPLATES"/*.timer; do
    name="$(basename "$src")"
    out="$GENERATED/$name"
    sed -e "s|__PELLA_PYTHON__|$PELLA_PYTHON|g" \
        -e "s|__PELLA_LAB_ROOT__|$PELLA_LAB_ROOT|g" \
        -e "s|__PELLA_USER__|$PELLA_USER|g" \
        "$src" > "$out"
    echo "  rendered $name -> $out"
    rendered+=("$out")
done
shopt -u nullglob

if [[ $DRY_RUN -eq 1 ]]; then
    echo
    echo "DryRun: rendered units left under $GENERATED/. Inspect before re-running."
    exit 0
fi

# ----- Install -------------------------------------------------------------
if [[ $SYSTEM_SCOPE -eq 1 ]]; then
    DEST="/etc/systemd/system"
    SUDO="sudo"
    DAEMON_RELOAD="$SUDO systemctl daemon-reload"
    ENABLE_PREFIX="$SUDO systemctl"
else
    DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    SUDO=""
    DAEMON_RELOAD="systemctl --user daemon-reload"
    ENABLE_PREFIX="systemctl --user"
fi

mkdir -p "$DEST"
$SUDO mkdir -p "$DEST"  # idempotent for both scopes
echo
echo "  installing into $DEST"

for f in "${rendered[@]}"; do
    name="$(basename "$f")"
    $SUDO install -m 0644 "$f" "$DEST/$name"
done

$DAEMON_RELOAD

# Enable + start each *.timer (services are oneshot, triggered by timers).
TIMERS=(
    pella-live-monitor-day.timer
    pella-live-monitor-overnight.timer
    pella-edge-decay.timer
    pella-paper-replay.timer
    pella-discovery.timer
    pella-cross-pollinator.timer
)
for t in "${TIMERS[@]}"; do
    $ENABLE_PREFIX enable --now "$t"
    echo "  enabled $t"
done

echo
echo "Done."
echo "Inspect with:"
if [[ $SYSTEM_SCOPE -eq 1 ]]; then
    echo "  systemctl list-timers 'pella-*'"
    echo "  journalctl -u pella-edge-decay.service --since today"
else
    echo "  systemctl --user list-timers 'pella-*'"
    echo "  journalctl --user -u pella-edge-decay.service --since today"
fi
