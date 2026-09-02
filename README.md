# Personal Trainer — Isa

Persönlicher AI-Fitness-Coach für einen Nutzer (Manuel). Ein Telegram-Bot
("Isa") mit Tool-Use-Agent, SQLite als einzige Datenbank, tägliche Daten-Syncs
(Oura Ring, Hevy) und ein lokales Web-Dashboard. Läuft auf dem Mac per launchd.

Stand 2026-09 (Phase 0 abgeschlossen): ein Agent, keine Selbst-Erweiterung,
kein Apple-Health-Webhook, kein Strong-CSV — siehe `PLAN.md` für die
Historie und die nächsten Phasen (Coach-Kern, Lernen).

## Setup

```bash
uv sync
cp .env.example .env   # echte Werte eintragen
uv run python -m trainer.db   # Schema + Migrationen (idempotent)
```

Variablen in `.env` (siehe `.env.example`):

| Variable | Zweck |
|---|---|
| `ANTHROPIC_API_KEY`, `TRAINER_MODEL` | Agent (Default-Modell `claude-sonnet-5`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID` | Bot-Token von @BotFather + die einzige bediente Chat-ID |
| `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET` | Oura-OAuth2 (Tokens landen in der Tabelle `secrets`) |
| `HEVY_API_KEY` | Hevy Pro API-Key |
| `CALENDAR_ICS_URLS` | kommaseparierte geheime iCal-Links (read-only) |
| `OBSIDIAN_VAULT_PATH` | Vault, den Isa lesen und pflegen darf |
| `WEB_AUTH_TOKEN` | Bearer-Token fürs Dashboard (`openssl rand -hex 24`) |

Die DB liegt unter `data/trainer.db`. Schema-Änderungen laufen über die
versionierte `MIGRATIONS`-Liste in `trainer/db.py` (`PRAGMA user_version`);
`init_db()` wird nur an Prozess-Einstiegen aufgerufen.

## Datenquellen

**Oura** (Schlaf/Readiness/Aktivität/HRV):

```bash
uv run python -m trainer.ingest.oura auth          # einmalig, Browser-Flow
uv run python -m trainer.ingest.oura sync --days 7  # täglich per launchd
```

Refresh-Tokens sind single-use und werden atomar persistiert. Ist der
Refresh-Token tot, kommt eine Telegram-Nachricht mit dem `auth`-Hinweis.

**Hevy** (Workouts):

```bash
uv run python -m trainer.ingest.hevy sync          # 20 neueste Workouts (täglich)
uv run python -m trainer.ingest.hevy sync --full   # alle + Abgleich gelöschter (wöchentlich)
uv run python -m trainer.ingest.hevy templates     # Übungskatalog cachen
```

**Gewichts-Konvention:** `workout_sets.weight_kg` ist bei Langhantel das
Scheibengewicht EINER Seite (reale Last = 20 kg + 2×Wert), bei Kurzhanteln
das Gewicht pro Hantel. Isa kennt das aus dem Prompt; das Dashboard rechnet
bis Phase 1 noch mit den Rohwerten.

## Isa (Telegram-Bot)

```bash
uv run python -m trainer.bot.main
```

- Nur `TELEGRAM_ALLOWED_CHAT_ID` wird bedient.
- Text, Foto (Essens-Analyse → `meals`) und Sprachnachrichten (faster-whisper
  lokal) gehen an den Agenten (`trainer/agent/core.py`).
- Prompt (`trainer/agents.py`): Rolle/Ton, **fester Athleten-Steckbrief**,
  Gewichts-Konvention, Ehrlichkeitsregel („gespeichert" nur mit Tool-Result),
  Tool-Hinweise. Dynamisch dazu: Datum, `profile`, `memories`.
- Jeder Tool-Aufruf landet in `tool_log` (Input, gekapptes Ergebnis, ok-Flag).
- Antworten werden als Telegram-HTML gesendet; Turn-Timeout 240 s mit
  Typing-Indikator; alle 5 min ein Heartbeat in `sync_state`.
- Ein Datei-Lock pro Agent verhindert, dass Bot und Web-Chat gleichzeitig
  auf derselben Historie arbeiten.
- `query_db` ist read-only und kann `secrets`/`sync_state` nicht lesen
  (SQLite-Authorizer, auch in Subqueries).

## Web-Dashboard

```bash
uv run uvicorn trainer.web.app:app --host 127.0.0.1 --port 8090
```

Alle `/api/*`-Routen verlangen `Authorization: Bearer <WEB_AUTH_TOKEN>`; der
Browser fragt den Token einmal ab und merkt ihn sich (localStorage). Host
muss `127.0.0.1`/`localhost` sein. Vor einem Umzug auf einen VPS: nur hinter
Tailscale/WireGuard betreiben.

## Jobs

| Job | Wann | Was |
|---|---|---|
| `trainer.ingest.oura sync --days 7` | täglich 10:30 | Oura-Daten |
| `trainer.ingest.hevy sync` | täglich 23:00 | neueste Hevy-Workouts |
| `trainer.ingest.hevy sync --full` | So 22:30 | alle Workouts + Delete-Abgleich |
| `trainer.jobs.reminder_check` | täglich 16:30 | Gym-Reminder, wenn's zum Wochenziel eng wird (synct vorher Hevy) |
| `trainer.jobs.weekly_report` | So 18:00 | Wochenreport (ein API-Call), Dedupe pro ISO-Woche |
| `trainer.jobs.health_check` | täglich 09:00 | Sync-Alter, Token-Ablauf, Bot-Heartbeat, Trainingslücke |

Jeder Job läuft in `trainer.jobs.notify.run_job`: bei einer Exception geht
eine ⚠️-Telegram-Nachricht raus und der Job endet mit Exit 1. Reminder und
Report werden in `messages` persistiert (mit `[System: …]`-User-Turn), damit
Isa im Chat weiß, was sie geschickt hat.

## Betrieb

**Produktion = netcup-VPS** (`ssh netcup`, `/root/manuel/personal-trainer-manuel`,
systemd). Der Mac ist nur Entwicklungsrechner — dort darf **kein** Bot laufen
(gleicher Token → Telegram-Conflict, doppelte Reminder).

```bash
bash deploy/deploy-vps.sh --dry-run   # zeigt, was rsync übertragen würde
bash deploy/deploy-vps.sh             # stoppt Bot/Web, sichert DB, rsync, uv sync,
                                      # Migration, Units installieren, Timer + Services starten
ssh netcup tail -f /root/manuel/personal-trainer-manuel/logs/bot.error.log
ssh netcup systemctl list-timers 'trainer-*'
```

Units liegen in `deploy/systemd/` (Bot/Web: `Restart=on-failure`,
`RestartSec=60` — ein Konfigurationsfehler beendet mit Exit 0 und bleibt
liegen, statt einen Restart-Storm auszulösen). Die DB wird nie übertragen;
die VPS-DB ist die Quelle der Wahrheit. Für lokale Entwicklung eine Kopie
holen: `scp netcup:/root/manuel/personal-trainer-manuel/data/trainer.db data/`.

`deploy/*.plist` + `install-launchd.sh` sind das macOS-Pendant für
Testbetrieb auf dem Mac — nur benutzen, wenn der VPS-Bot gestoppt ist.

**Wenn eine ⚠️-Nachricht kommt:** Log des Jobs unter `logs/<job>.error.log`
auf dem VPS lesen; bei „Oura-Login abgelaufen" auf dem VPS
`uv run python -m trainer.ingest.oura auth` (braucht Browser → Token lokal
erzeugen und die drei `secrets`-Zeilen übertragen); bei fehlendem
Bot-Heartbeat `systemctl restart trainer-bot`.

## Tests

```bash
uv run pytest -q
```

Abgedeckt: Prompt↔Tool-Konsistenz, `query_db`-Authorizer, DB-Migrationen
(frisch/legacy/idempotent), Telegram-HTML + Chunking, Reminder-Logik,
Health-Check-Bewertung, Hevy-Routine-Payloads, Obsidian-Schreibpfade.
