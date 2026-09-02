#!/usr/bin/env bash
# Installiert (oder aktualisiert) die launchd-Agents für Bot, Web und Jobs.
#
# WICHTIG: Läuft bereits eine Bot-Instanz manuell (z.B. `uv run python -m
# trainer.bot.main` im Terminal), MUSS die erst gestoppt werden, bevor dieses
# Skript ausgeführt wird — sonst pollen zwei Instanzen gleichzeitig gegen die
# Telegram-API.
#
# Nutzt die moderne launchctl-API (bootout/enable/bootstrap statt
# unload/load): `launchctl load` allein hebt einen persistenten
# "disabled"-Override NICHT auf — genau so blieben die Jobs im August 2026
# nach dem Abschalten tot, obwohl die plists wieder da waren.
#
# Usage: bash deploy/install-launchd.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$HOME/Library/Logs/trainer"
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

echo "Lege Log-Ordner an: $LOG_DIR"
mkdir -p "$LOG_DIR" "$AGENTS_DIR"

for plist in "${PLISTS[@]}"; do
    label="${plist%.plist}"
    echo ""
    echo "== $label =="
    cp "$SCRIPT_DIR/$plist" "$AGENTS_DIR/$plist"

    # Falls schon geladen: raus (Fehler beim Erstinstall ignorieren).
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    # Persistenten disabled-Override aufheben (no-op, wenn nicht gesetzt).
    launchctl enable "$DOMAIN/$label"
    launchctl bootstrap "$DOMAIN" "$AGENTS_DIR/$plist"
    echo "Geladen: $AGENTS_DIR/$plist"
done

echo ""
echo "Status (PID/Exit-Code, Label):"
launchctl list | grep "com.manuel.trainer" || echo "  (nichts gefunden — Fehler beim Laden?)"

echo ""
echo "Disabled-Overrides (sollte leer sein):"
launchctl print-disabled "$DOMAIN" | grep "com.manuel.trainer" || echo "  keine"

if [ -d "$AGENTS_DIR/_disabled-trainer-backup" ]; then
    echo ""
    echo "Hinweis: $AGENTS_DIR/_disabled-trainer-backup/ existiert noch (Backup vom"
    echo "Abschalten am 13.08.2026). Kann gelöscht werden, sobald alles läuft:"
    echo "  rm -r \"$AGENTS_DIR/_disabled-trainer-backup\""
fi

echo ""
echo "Fertig. Logs unter $LOG_DIR/*.log und *.error.log."
