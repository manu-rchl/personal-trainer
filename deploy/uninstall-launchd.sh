#!/usr/bin/env bash
# Entfernt die launchd-Agents für Bot, Web und Jobs.
#
# Usage: bash deploy/uninstall-launchd.sh

set -euo pipefail

AGENTS_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

PLISTS=(
    com.manuel.trainer.bot.plist
    com.manuel.trainer.web.plist
    com.manuel.trainer.oura-sync.plist
    com.manuel.trainer.hevy-sync.plist
    com.manuel.trainer.hevy-full-sync.plist
    com.manuel.trainer.weekly-report.plist
    com.manuel.trainer.reminder.plist
    com.manuel.trainer.health-check.plist
)

for plist in "${PLISTS[@]}"; do
    label="${plist%.plist}"
    echo "== $label =="
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS_DIR/$plist"
    echo "Entfernt: $AGENTS_DIR/$plist"
done

echo ""
echo "Fertig. Logs unter ~/Library/Logs/trainer wurden NICHT gelöscht."
