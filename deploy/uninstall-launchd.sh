#!/usr/bin/env bash
# Entfernt die launchd-Agents für den Trainer-Bot + geplante Jobs (Phase 3).
#
# Usage: bash deploy/uninstall-launchd.sh

set -euo pipefail

AGENTS_DIR="$HOME/Library/LaunchAgents"

PLISTS=(
    com.manuel.trainer.bot.plist
    com.manuel.trainer.assistant.plist
    com.manuel.trainer.oura-sync.plist
    com.manuel.trainer.hevy-sync.plist
    com.manuel.trainer.weekly-report.plist
    com.manuel.trainer.reminder.plist
    com.manuel.trainer.web.plist
)

for plist in "${PLISTS[@]}"; do
    echo "== $plist =="
    launchctl unload "$AGENTS_DIR/$plist" 2>/dev/null || true
    rm -f "$AGENTS_DIR/$plist"
    echo "Entfernt: $AGENTS_DIR/$plist"
done

echo ""
echo "Fertig. Logs unter ~/Library/Logs/trainer wurden NICHT gelöscht."
