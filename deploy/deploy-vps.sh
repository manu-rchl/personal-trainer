#!/usr/bin/env bash
# Deployt den aktuellen Arbeitsstand auf den netcup-VPS (Manuels Instanz).
#
#   bash deploy/deploy-vps.sh            # rsync + uv sync + Units + Restart
#   bash deploy/deploy-vps.sh --dry-run  # nur zeigen, was rsync übertragen würde
#
# Voraussetzungen: SSH-Alias `netcup` (root), uv unter /root/.local/bin/uv.
# Überträgt KEINE data/, .env, .venv, logs — die VPS-DB ist die Quelle der
# Wahrheit. Vor dem Restart wird die DB gesichert (data/backups/).
#
# Die Samed-Instanz (/root/samed/...) wird nicht angefasst.

set -euo pipefail

HOST="${TRAINER_VPS_HOST:-netcup}"
REMOTE_DIR="/root/manuel/personal-trainer-manuel"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

RSYNC_ARGS=(
    -az --delete
    --exclude .git --exclude .venv --exclude data --exclude .env
    --exclude logs --exclude .gstack --exclude .claude
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache
    --exclude '.env.*' --exclude '.DS_Store' --exclude 'data/.lock-*'
)

if [[ "${1:-}" == "--dry-run" ]]; then
    rsync "${RSYNC_ARGS[@]}" --dry-run -v "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/" | head -60
    exit 0
fi

echo "== Services stoppen (Bot/Web), DB sichern"
ssh "$HOST" bash -s <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
systemctl stop trainer-bot trainer-web 2>/dev/null || true
systemctl stop trainer-assistant 2>/dev/null || true
mkdir -p data/backups logs
cp data/trainer.db "data/backups/trainer.db.\$(date +%Y%m%d-%H%M%S)"
ls -t data/backups | tail -n +6 | sed 's#^#data/backups/#' | xargs -r rm -f   # max 5 Backups
EOF

echo "== Code übertragen"
rsync "${RSYNC_ARGS[@]}" "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

echo "== Abhängigkeiten, .env-Check, Units, Start"
ssh "$HOST" bash -s <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
export PATH="/root/.local/bin:\$PATH"
uv sync --frozen -q

# WEB_AUTH_TOKEN ist seit Phase 0 Pflicht — fehlt er, hier einmalig erzeugen.
if ! grep -q '^WEB_AUTH_TOKEN=.\+' .env; then
    tok=\$(python3 -c 'import secrets; print(secrets.token_hex(24))')
    printf '\n# Web-Dashboard Bearer-Token (vom Deploy erzeugt)\nWEB_AUTH_TOKEN=%s\n' "\$tok" >> .env
    echo "WEB_AUTH_TOKEN in .env erzeugt: \$tok"
fi

# Schema + Migrationen explizit VOR dem Start (statt beim ersten Bot-Start).
uv run python -m trainer.db

# Assistant-Unit ist seit Phase 0 weg.
if systemctl list-unit-files trainer-assistant.service >/dev/null 2>&1; then
    systemctl disable --now trainer-assistant.service 2>/dev/null || true
    rm -f /etc/systemd/system/trainer-assistant.service
fi

cp deploy/systemd/trainer-*.service deploy/systemd/trainer-*.timer /etc/systemd/system/
systemctl daemon-reload
for t in oura-sync hevy-sync hevy-full-sync reminder weekly-report health-check post-workout morning-checkin memory-review; do
    systemctl enable --now "trainer-\$t.timer" >/dev/null
done
systemctl enable --now trainer-bot trainer-web >/dev/null
sleep 4
echo
systemctl --no-pager --plain list-units 'trainer-*' | grep -v samed
echo
echo "-- letzte Bot-Logzeilen:"
tail -n 3 logs/bot.error.log
EOF

echo
echo "Fertig. Live-Log: ssh $HOST tail -f $REMOTE_DIR/logs/bot.error.log"
