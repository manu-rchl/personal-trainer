# Personal Trainer Agent — Umsetzungsplan

> **Stand 2026-09-02:** Phasen 0–4 dieses Plans sind umgesetzt, danach hat ein
> Audit einiges wieder entfernt: Apple-Health-Webhook (nie genutzt), Strong-CSV
> (durch Hevy ersetzt), ein zweiter „Assistant"-Agent und eine
> Dev-Task-Selbsterweiterung (Sicherheitsrisiko, nie genutzt). Aktueller
> Betriebsstand: `README.md`. Nächste Phasen (nicht in diesem Dokument):
> **Phase 1 Coach-Kern** — `trainer/analytics.py` mit `effective_load`/e1RM,
> Tabellen `training_plan`/`exercise_targets`, Tools `get_exercise_progress`
> + `get_hevy_routines`, Post-Workout-Reflexions-Job, Report/Reminder über den
> Agenten statt Template. **Phase 2 Lernen** — Memory-Konsolidierung
> (`update/delete_memory`, pinned/source/valid_from), Wissens-Loop über
> NotebookLM bei Plateau, Historien-Zusammenfassung beim Fenstersprung.

## Kontext

Persönlicher AI-Fitness-Trainer für einen einzelnen Nutzer (Manuel). Der Agent:
- sammelt automatisch Health-Daten (Oura Ring, Apple Health, Strong-Workouts, manuelles Logging) in einer eigenen Datenbank,
- ist 24/7 per **Telegram** erreichbar (Trainingsfragen, Ernährung, Push-Day-Varianten, Workout-Logging per Chat),
- schickt **jeden Sonntag** einen Weekly-Health-Report (Status, Trends, Ups & Downs, 1–2 konkrete Änderungsvorschläge),
- erinnert ans Gym (Ziel: mind. 3×/Woche), später kalender-bewusst,
- ist erweiterbar (weitere MCP-Anbindungen, Web-Dashboard).

## Entscheidungen (Stand 2026-07-07)

| Thema | Entscheidung |
|---|---|
| Sprache/Stack | Python (FastAPI, python-telegram-bot, Claude Agent SDK, pandas) |
| Datenbank | SQLite (eine Datei, Single-User; Dashboard liest später dieselbe DB) |
| Hosting | **Phase A: lokal auf dem Mac** (Entwicklung + Test), Phase B: Umzug auf Hetzner-VPS (~4€/Mo) — Code von Anfang an portabel (env-config, keine Mac-Abhängigkeiten) |
| Scheduling | Cron/launchd auf dem Host (So. 18:00 Weekly-Report, täglich Reminder-Check) |
| Messenger | Telegram Bot API (Bot via @BotFather), Zugriff nur für Manuels Chat-ID |
| LLM | Anthropic API Key + Claude Agent SDK (Python) |

## Research-Erkenntnisse (Quellen siehe unten; ⚠ = vor Implementierung nachprüfen)

- **Oura API v2**: liefert Daily Sleep, Readiness, Activity, Heart Rate, HRV (in Sleep/Readiness), Workouts, SpO2, Stress, Resilience, VO2max. Basis-URL `https://api.ouraring.com/v2/`. Benötigt aktive Oura-Membership. Rate Limit 5000 req/5min (irrelevant für uns).
  - ⚠ **PATs wurden lt. mehreren Quellen Dez 2025 deprecated** → OAuth2 nötig (Server-Side Flow, **Single-Use-Refresh-Tokens**: jeder Refresh liefert neuen Token → muss persistiert werden). Vor Phase 1 gegen https://cloud.ouraring.com/docs/authentication verifizieren; falls PAT doch noch geht: einfacherer Weg.
- **Apple Health**: iOS-App **Health Auto Export** pusht 100+ Metriken als JSON per HTTP POST an eigenen REST-Endpoint (Auth-Header konfigurierbar, Intervall einstellbar; Premium-Feature, kleiner Preis). Einschränkung: Syncs feuern nur bei entsperrtem iPhone → unregelmäßige Pushes einplanen (idempotente Upserts). Referenz-Pipeline: FastAPI + DB (ladvien.com).
  - Lokal-Phase: iPhone muss den Mac erreichen → gleiche WLAN-IP oder **Tailscale** (empfohlen, funktioniert später auch für VPS nahtlos).
- **Strong App**: keine API; nur manueller CSV-Export (Settings → Export Strong Data). Apple-Health-Sync überträgt KEINE Sätze/Gewichte. → Lösung: CSV-Datei per Telegram an den Bot schicken, Bot parst + importiert (dedupe). Alltag: Chat-Logging („Bankdrücken 4x8 80kg").
- **DB-Wahl**: SQLite für Single-User optimal (zero setup, eine Datei); Turso/Supabase erst bei Multi-Device/Cloud-Bedarf.

Quellen: cloud.ouraring.com/docs/authentication, support.ouraring.com (The Oura API), cloud.ouraring.com/docs/error-handling, github.com/Lybron/health-auto-export, help.healthyapps.dev (REST API automation), github.com/irvinlim/apple-health-ingester, ladvien.com/syncing-apple-health-kit-data-postgres, help.strongapp.io/article/235-export-workout-data, code.claude.com/docs/en/agent-sdk/hosting.
*(Hinweis: Claims aus Primärquellen extrahiert, adversariale Verifikation scheiterte an Session-Limits — kritische Punkte (⚠) bei Umsetzung einzeln prüfen.)*

## Architektur

```
iPhone (Health Auto Export) ──JSON push──┐
Oura Cloud API ◄──täglicher Sync─────────┤
Strong CSV ──per Telegram-Upload─────────┤
Chat-Logging ("Bankdrücken 4x8")─────────┤
                                         ▼
        Host (Mac → später VPS), ein Python-Projekt:
        ├─ data/trainer.db (SQLite)
        ├─ ingest/   Oura-Sync, FastAPI-Webhook (HAE), Strong-CSV-Parser
        ├─ agent/    Claude Agent SDK: Trainer-Persona + Tools
        │            (query_health, log_workout, weekly_stats, calendar)
        ├─ bot/      python-telegram-bot (nur Manuels Chat-ID)
        └─ jobs/     weekly_report.py, reminder_check.py (cron/launchd)
                                         │
        Später: Web-Dashboard ───────────┘ (liest dieselbe SQLite-DB)
```

## Datenbank-Schema (Kern)

- `oura_daily` — date, sleep_score, readiness_score, activity_score, hrv_avg, resting_hr, sleep_duration, … (ein Upsert pro Tag/Typ)
- `health_metrics` — source, metric_name, timestamp, value, unit (generisch für Apple-Health-Pushes)
- `workouts` — date, type, source (strong_csv | chat | apple_health), notes
- `workout_sets` — workout_id, exercise, set_no, reps, weight_kg
- `profile` — key/value (Ziele, Gewicht, Ernährungspräferenzen, „3×/Woche Gym")
- `messages` — Telegram-Konversationshistorie (Session-Memory des Trainers)
- `sync_state` — letzter Oura-Sync, OAuth-Tokens (Refresh-Token-Rotation!), Import-Hashes (CSV-Dedupe)

## Phasen

### Phase 0 — Fundament
`pyproject.toml` (uv), `.env` (ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, OURA_*, WEBHOOK_SECRET), SQLite-Schema + Migrations-Skript, Config-Modul. Telegram-Bot bei @BotFather anlegen (macht Manuel, 2 Min).

### Phase 1 — Daten-Ingestion
1. **Oura**: Auth-Frage verifizieren (⚠ oben) → Sync-Modul `ingest/oura.py` (holt Sleep/Readiness/Activity/HR täglich + Backfill 90 Tage), Token-Persistenz in `sync_state`.
2. **Apple Health**: FastAPI-App `ingest/webhook.py` (POST /health-export, Bearer-Auth) → Upserts in `health_metrics`. Setup-Anleitung für Health Auto Export (+ Tailscale) schreiben.
3. **Strong**: `ingest/strong_csv.py` — Parser (pandas) + Dedupe über Datei-/Zeilen-Hash.

### Phase 2 — Telegram-Bot + Trainer-Agent
- Bot-Grundgerüst (long polling, Chat-ID-Whitelist), Datei-Handler für CSV-Uploads.
- Agent-Loop mit Claude Agent SDK: Trainer-System-Prompt (deutsch, Kumpel-Ton, kennt Profil + Ziele), Tools: `query_health_db`, `log_workout`, `get_weekly_stats`, `update_profile`.
- Session-Memory: letzte N Nachrichten aus `messages` + Profil-Fakten in den Kontext.

### Phase 3 — Scheduling
- `jobs/weekly_report.py`: So. 18:00 — Wochendaten + 4-Wochen-Vergleich aggregieren (SQL, deterministisch) → Agent formuliert Report (Status, Trends, 1–2 Änderungen) → Telegram.
- `jobs/reminder_check.py`: täglich 16:00 — Gym-Besuche der Woche vs. 3×-Ziel → ggf. Erinnerung.
- Mac: launchd-plists; VPS später: crontab. Beides im Repo unter `deploy/`.

### Phase 4 — Google Calendar
Google Calendar API (OAuth, read-only) als Agent-Tool `get_calendar` → Reminder werden kalender-bewusst („heute bis 18 Uhr Arbeit → geh um 18:30").

### Phase 5 — später
Web-Dashboard (liest SQLite), VPS-Umzug (rsync + crontab, Code ist portabel), weitere MCP-Quellen, ggf. WhatsApp.

## Verifikation
- Phase 1: Oura-Backfill laufen lassen → Stichproben gegen Oura-App vergleichen; Test-Push aus Health Auto Export → Zeile in DB; echten Strong-CSV-Export importieren → Satz-Zahlen prüfen.
- Phase 2: echte Telegram-Konversation (Logging, Abfrage „wie war mein Schlaf diese Woche?").
- Phase 3: Report-Job manuell triggern → Nachricht kommt in Telegram an, Zahlen stimmen mit SQL-Abfrage überein.
