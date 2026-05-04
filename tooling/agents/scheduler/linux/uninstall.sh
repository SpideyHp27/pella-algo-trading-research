#!/usr/bin/env bash
# Uninstalls Pella systemd timers + services. INNER ONLY.
#
# Usage:
#   ./uninstall.sh                # remove user units
#   ./uninstall.sh --system       # remove system units (sudo)
#   ./uninstall.sh --keep-generated   # leave ./generated/ in place
set -euo pipefail

SYSTEM_SCOPE=0
KEEP_GEN=0
for arg in "$@"; do
    case "$arg" in
        --system)         SYSTEM_SCOPE=1 ;;
        --keep-generated) KEEP_GEN=1 ;;
        -h|--help)
            sed -n '2,9p' "$0"
            exit 0 ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2 ;;
    esac
done

if [[ $SYSTEM_SCOPE -eq 1 ]]; then
    DEST="/etc/systemd/system"
    SUDO="sudo"
    DISABLE_PREFIX="$SUDO systemctl"
    DAEMON_RELOAD="$SUDO systemctl daemon-reload"
else
    DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    SUDO=""
    DISABLE_PREFIX="systemctl --user"
    DAEMON_RELOAD="systemctl --user daemon-reload"
fi

UNITS=(
    pella-live-monitor-day.timer
    pella-live-monitor-overnight.timer
    pella-edge-decay.timer
    pella-paper-replay.timer
    pella-discovery.timer
    pella-cross-pollinator.timer
    pella-live-monitor.service
    pella-edge-decay.service
    pella-paper-replay.service
    pella-discovery.service
    pella-cross-pollinator.service
)

for u in "${UNITS[@]}"; do
    if [[ -f "$DEST/$u" ]]; then
        # Disable + stop where applicable (timers can be stopped; oneshot services
        # mostly aren't running, so stop/disable will no-op without error).
        $DISABLE_PREFIX disable --now "$u" 2>/dev/null || true
        $SUDO rm -f "$DEST/$u"
        echo "  removed $u"
    else
        echo "  (skip) $u was not installed"
    fi
done

$DAEMON_RELOAD

HERE="$(cd "$(dirname "$0")" && pwd)"
if [[ $KEEP_GEN -eq 0 && -d "$HERE/generated" ]]; then
    rm -rf "$HERE/generated"
    echo "  removed generated/"
fi

echo "Done."
