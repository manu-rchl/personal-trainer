# Personal Trainer — Fundament + Telegram-Bot (Phase 0-2)

Persönlicher AI-Fitness-Trainer, Single-User, SQLite. Dieses Repo enthält das
Fundament (Config, DB-Schema), die Daten-Ingestion (Oura, Apple Health, Strong)
sowie den Telegram-Bot mit Trainer-Agent "Isa" (Phase 2). Scheduling (Weekly-
Report, Reminder) und Google Calendar folgen in Phase 3/4 (siehe `PLAN.md`).

## Setup

```bash
uv sync
cp .env.example .env   # falls noch nicht geschehen, dann echte Werte eintragen
```

Benötigte Variablen in `.env` (siehe `.env.example`):

- `ANTHROPIC_API_KEY` — für den Trainer-Agenten (Isa)
- `TRAINER_MODEL` — Anthropic-Modell für den Agenten, default `claude-sonnet-5`
  (optional, weglassen = Default wird verwendet)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID` — Bot-Token von @BotFather +
  die einzige Chat-ID, die der Bot bedient (alle anderen werden höflich abgewiesen)
- `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET` — für den Oura-OAuth2-Flow
- `HEALTH_WEBHOOK_SECRET` — Bearer-Token, das der Health-Auto-Export-Webhook erwartet

Die DB-Datei liegt standardmäßig unter `data/trainer.db` (wird automatisch angelegt).

## Datenbank initialisieren

```bash
uv run python -c "from trainer.db import init_db; init_db()"
```

Legt alle Tabellen an (idempotent): `oura_daily`, `health_metrics`, `workouts`,
`workout_sets`, `profile`, `messages`, `sync_state`.

## Ingest-Befehle

### 1. Oura (OAuth2, Schlaf/Readiness/Aktivität)

```bash
# Einmalig: Browser-Autorisierung, Tokens werden in sync_state gespeichert
uv run python -m trainer.ingest.oura auth

# Tägliche Synchronisierung (default: letzte 7 Tage)
uv run python -m trainer.ingest.oura sync

# Backfill z.B. letzte 90 Tage
uv run python -m trainer.ingest.oura sync --days 90
```

`auth` startet kurzzeitig einen lokalen HTTP-Server auf `http://localhost:8484/oura/callback`,
öffnet den Browser zur Oura-Autorisierungsseite und tauscht den Code gegen
Access-/Refresh-Token. Oura-Refresh-Tokens sind **single-use** — bei jedem
Refresh (auch während `sync`) wird sofort ein neuer Refresh-Token gespeichert.

### 2. Apple Health Webhook (Health Auto Export)

Server starten:

```bash
uv run uvicorn trainer.ingest.webhook:app --host 0.0.0.0 --port 8080
```

Endpoints:
- `GET /health` → `{"status": "ok"}`
- `POST /health-export` → nimmt Health-Auto-Export-JSON entgegen, erwartet Header
  `Authorization: Bearer <HEALTH_WEBHOOK_SECRET>`. Antwort: `{"imported": N, "skipped": M}`.

**Health Auto Export App konfigurieren** (iPhone):
- REST-API-Automation anlegen, URL: `http://<mac-ip>:8080/health-export`
  (im gleichen WLAN, oder via Tailscale für unterwegs)
- Header: `Authorization: Bearer <dein HEALTH_WEBHOOK_SECRET>`
- Format: JSON, Struktur wie folgt:

  ```json
  {
    "data": {
      "metrics": [
        {
          "name": "heart_rate",
          "units": "bpm",
          "data": [{"date": "2026-07-01 08:00:00 +0200", "qty": 62.0}]
        }
      ],
      "workouts": [
        {"name": "Functional Strength Training", "start": "2026-07-01 18:00:00 +0200", "end": "..."}
      ]
    }
  }
  ```

- Sync-Intervall nach Bedarf (z.B. alle paar Stunden); Syncs feuern nur bei
  entsperrtem iPhone — Upserts sind idempotent, unregelmäßige Pushes sind ok.

### 3. Strong CSV-Import

```bash
uv run python -m trainer.ingest.strong_csv pfad/zur/export.csv
```

Export in der Strong-App: Settings → Export Strong Data. Import ist deduped
(SHA256 pro Zeile in `sync_state`) — mehrfacher Import derselben Datei ist safe.

## Telegram-Bot + Trainer-Agent (Isa)

Der Bot nutzt das offizielle `anthropic`-Python-SDK mit einem selbstgebauten
Tool-Use-Loop (bewusst kein `claude-agent-sdk`, wegen späterer Portabilität
auf einen VPS). Tools: `get_health_summary`, `get_workouts`, `log_workout`,
`query_db` (read-only SELECT), `get_profile`, `update_profile`.

Bot starten (Long Polling, blockiert das Terminal):

```bash
uv run python -m trainer.bot.main
```

Funktionen:
- Nur die in `TELEGRAM_ALLOWED_CHAT_ID` konfigurierte Chat-ID wird bedient,
  alle anderen Chats bekommen eine höfliche Ablehnung.
- `/start` — kurze Vorstellung, was Isa kann.
- Textnachrichten — gehen an den Trainer-Agenten (Health-Fragen, Workout-
  Logging per Chat, allgemeine Trainings-/Ernährungsfragen). Antworten werden
  bei 4096 Zeichen automatisch gesplittet.
- `.csv`-Datei-Upload — wird als Strong-Export importiert (nutzt denselben
  Importer wie `trainer.ingest.strong_csv`), Ergebnis-Zusammenfassung kommt
  als Nachricht zurück.

`TRAINER_MODEL` (env var, default `claude-sonnet-5`) steuert, welches
Anthropic-Modell der Agent verwendet.

## Automatisierung (Phase 3: geplante Jobs + Autostart)

### Jobs

- **`trainer.jobs.weekly_report`** (`uv run python -m trainer.jobs.weekly_report`,
  So. 18:00): aggregiert deterministisch per SQL die aktuelle Woche (Mo-So,
  lokale Zeit) gegen den Durchschnitt der 4 Vorwochen (Sleep/Readiness-Score,
  HRV + Ruhepuls + Schlafdauer aus `kind='sleep_detail'`, Ø Schritte/Tag,
  Workouts + Sätze, Mahlzeiten + Ø Protein/Tag). Genau EIN Anthropic-API-Call
  (kein Tool-Use) formuliert daraus Isas Wochenreport (Überblick, Auffälligkeiten
  vs. Vorwochen, 1-2 Änderungsempfehlungen). Wird per Telegram verschickt und
  zusätzlich in `messages` (role='assistant') gespeichert, damit Isa im Chat
  darauf Bezug nehmen kann. Sind für die aktuelle Woche keine Oura-Daten
  vorhanden, geht statt eines leeren Reports nur ein kurzer Hinweis raus.
  Aggregation (`aggregate_period`/`build_facts_block`) und
  Formulierung/Versand (`generate_report_text`/`run`) sind getrennte
  Funktionen — die Zahlen lassen sich isoliert testen, ohne API-Kosten oder
  eine Telegram-Nachricht auszulösen.

- **`trainer.jobs.reminder_check`** (`uv run python -m trainer.jobs.reminder_check`,
  täglich 16:30): vergleicht das Wochenziel (`profile["gym_goal_per_week"]`,
  Default 3) mit den echten Trainingstagen dieser Woche (Mo..heute, `COUNT
  DISTINCT date` aus `workouts`). Ist heute schon trainiert worden oder das
  Ziel bereits erreicht, passiert nichts. Sonst wird nur erinnert, wenn es
  eng wird: verbleibende Trainingstage der Woche (heute eingeschlossen) <=
  noch offene Workouts. Rein deterministisch (Template-Text, 2-3 Varianten
  nach Wochentag gewählt) — **kein** Anthropic-API-Call, da der Job täglich
  läuft. Dedupe über `sync_state["last_reminder_date"]`: höchstens eine
  Reminder-Nachricht pro Tag.

- **`trainer.jobs.notify`** — gemeinsamer Telegram-Versand (`send_telegram`)
  für beide Jobs, plain `httpx`-POST an die Bot-API (kein
  `python-telegram-bot`, da hier kein Application/Polling-Kontext existiert),
  splittet bei 4096 Zeichen.

### launchd-Autostart (macOS)

Unter `deploy/` liegen 4 launchd-Agents (Label-Prefix `com.manuel.trainer.*`):

| Label | Trigger | Kommando |
|---|---|---|
| `com.manuel.trainer.bot` | `RunAtLoad` + `KeepAlive` (läuft dauerhaft) | `trainer.bot.main` |
| `com.manuel.trainer.oura-sync` | täglich 10:30 | `trainer.ingest.oura sync --days 7` |
| `com.manuel.trainer.weekly-report` | So. 18:00 | `trainer.jobs.weekly_report` |
| `com.manuel.trainer.reminder` | täglich 16:30 | `trainer.jobs.reminder_check` |

Alle Agents nutzen den absoluten Pfad zu `uv` (launchd hat kein
Shell-/PATH-Profil), `WorkingDirectory` = Repo-Root, `EnvironmentVariables`
mit `PATH`/`HOME`, und schreiben Logs nach
`~/Library/Logs/trainer/<job>.log` bzw. `.error.log`.

**Installieren** (Ordner `deploy/`):

```bash
bash deploy/install-launchd.sh
```

Legt den Log-Ordner an, kopiert die plists nach `~/Library/LaunchAgents/`,
lädt sie (`launchctl unload` + `load`, Fehler beim erstmaligen `unload`
werden ignoriert) und zeigt den Status.

> Läuft der Bot bereits manuell in einem Terminal (`uv run python -m
> trainer.bot.main`), diesen VORHER stoppen — sonst pollen zwei Instanzen
> gleichzeitig gegen die Telegram-API.

**Deinstallieren:**

```bash
bash deploy/uninstall-launchd.sh
```

Entlädt alle 4 Agents und entfernt die plists aus `~/Library/LaunchAgents/`
(Logs bleiben erhalten).

## Status

Phase 0 (Fundament), Phase 1 (Daten-Ingestion), Phase 2 (Telegram-Bot +
Trainer-Agent) und Phase 3 (geplante Jobs + launchd-Autostart) sind
umgesetzt. Google Calendar (Phase 4) ist **out of scope** für diesen Stand —
siehe `PLAN.md` für die weiteren Phasen.
