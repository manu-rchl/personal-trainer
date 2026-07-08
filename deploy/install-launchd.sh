#!/usr/bin/env bash
# Installiert die launchd-Agents für den Trainer-Bot + geplante Jobs (Phase 3).
#
# WICHTIG: Läuft bereits eine Bot-Instanz manuell (z.B. `uv run python -m
# trainer.bot.main` im Terminal), MUSS die erst gestoppt werden, bevor dieses
# Skript ausgeführt wird — sonst pollen zwei Instanzen gleichzeitig gegen die
# Telegram-API.
#
# Usage: bash deploy/install-launchd.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$HOME/Library/Logs/trainer"
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

echo "Lege Log-Ordner an: $LOG_DIR"
mkdir -p "$LOG_DIR"

echo "Lege LaunchAgents-Ordner an (falls nötig): $AGENTS_DIR"
mkdir -p "$AGENTS_DIR"

for plist in "${PLISTS[@]}"; do
    echo ""
    echo "== $plist =="
    cp "$SCRIPT_DIR/$plist" "$AGENTS_DIR/$plist"

    # Falls schon geladen: unload, Fehler ignorieren (z.B. beim Erstinstall).
    launchctl unload "$AGENTS_DIR/$plist" 2>/dev/null || true

    launchctl load "$AGENTS_DIR/$plist"
    echo "Geladen: $AGENTS_DIR/$plist"
done

echo ""
echo "Status:"
for plist in "${PLISTS[@]}"; do
    label="${plist%.plist}"
    if launchctl list | grep -q "$label"; then
        launchctl list | grep "$label"
    else
        echo "$label: NICHT in 'launchctl list' gefunden — Fehler beim Laden?"
    fi
done

echo ""
echo "Fertig. Logs unter $LOG_DIR/*.log und *.error.log."
