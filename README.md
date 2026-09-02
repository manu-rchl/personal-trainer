# Personal Trainer — Isa

Persönlicher AI-Fitness-Coach für einen Nutzer (Manuel). Ein Telegram-Bot
("Isa") mit Tool-Use-Agent, SQLite als einzige Datenbank, tägliche Daten-Syncs
(Oura Ring, Hevy) und ein lokales Web-Dashboard. Läuft auf dem Mac per launchd.

Stand 2026-09-03 (Phase 0–2 abgeschlossen): ein Agent, der von sich aus
kommt (Morgen-Check-in, Post-Workout-Auswertung mit Zielgewichten in Hevy,
Wochenreport, Memory-Pflege) — siehe „Coach-Loop" unten; `PLAN.md` hält die
Historie.

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
das Gewicht pro Hantel. `trainer/analytics.py` rechnet darüber (`load_mode`)
in effektive Last um — Isa, Dashboard und Jobs nutzen dieselbe Quelle.

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

## Coach-Loop (Isa kommt von sich aus)

Alles Proaktive läuft als **Agent-Turn** (`trainer/jobs/agent_job.py`): der Job
schickt Isa eine `[System: …]`-Instruction, Isa nutzt ihre Tools, die Antwort
geht per Telegram raus und landet als User/Assistant-Paar in der Historie.
Antwortet Isa exakt `NO_MESSAGE`, wird nichts gesendet.

| Job | Wann | Was |
|---|---|---|
| `jobs.morning_checkin` | 08:00 | Oura-Sync, dann: Readiness/Schlaf, Kalender, was laut Plan dran ist, Essen — max. 5 Zeilen oder Schweigen |
| `jobs.post_workout` | stündlich 07–23 | Hevy-Sync; pro neuem Workout: jede Übung mit `get_exercise_progress` bewerten, Ziel via `set_exercise_target` setzen (→ Hevy-Notiz), Plateau → ggf. NotebookLM, kurzes Check-in; danach fällige `scheduled_checkins` |
| `jobs.reminder_check` | 16:30 | Entscheidung deterministisch (`should_remind`), Text von Isa mit Kalender/Reise-Kontext |
| `jobs.weekly_report` | So 18:00 | Wochenzahlen + Ziel-Tracking, e1RM-Trends, Muskelfrequenz, Reisen; Dedupe pro ISO-Woche |
| `jobs.memory_review` | So 18:30 | Konsolidierungs-Vorschlag (Duplikate, Widersprüche, Veraltetes, Pins) — Manuel bestätigt mit „ok Memories" |
| `jobs.health_check` | 09:00 | Sync-Alter, Token-Ablauf, Bot-Heartbeat, Post-Workout-Job, Trainingslücke |
| `ingest.oura sync` / `ingest.hevy sync` | 10:30 / 23:00 | Daten |
| `ingest.hevy sync --full` | So 22:30 | alle Workouts + Delete-Abgleich |

Alle Jobs haben `--dry-run` (Antwort nur ausgeben); `post_workout` zusätzlich
`--workout-id N` und `--no-hevy-write`. Jeder Job läuft in
`trainer.jobs.notify.run_job`: Exception → ⚠️-Telegram + Exit 1.

**Coach-Daten** (`trainer/analytics.py` rechnet, Tools in
`trainer/agent/coach_tools.py`):
- `exercise_meta.load_mode` — wie `weight_kg` je Übung zu lesen ist
  (`barbell_per_side` = 20 kg Stange + 2×, `per_hand`, `total`); Heuristik
  aus Name/Hevy-Equipment, Override per `set_exercise_load_mode`.
- `training_plan` — aktiver Plan mit Progressions-/Deload-Regel (Seed: PPL,
  Double Progression 8–12, Deload alle 6–8 Wochen).
- `exercise_targets` — Ziel fürs nächste Mal pro Übung; wird als oberste
  Notizzeile in die Hevy-Routine gespiegelt („Ziel (Isa, 03.09.): 25,5 kg ·
  3×8–12 — …"), damit Manuel es im Training sieht.
- `get_exercise_progress` — letzte Sessions, e1RM auf effektiver Last,
  Trend, `plateau` (≥3 Sessions ohne neues e1RM-Hoch), Double-Progression-Hinweis.
- `scheduled_checkins` — Follow-ups, die Isa sich selbst setzt.
- `history_summaries` — rollende Zusammenfassung der Chat-Historie, die aus
  dem Kontextfenster gefallen ist (steht im dynamischen Prompt-Block).
- `memories.pinned/source` — gepinnte Kernfakten stehen immer im Prompt,
  Recherche-Wissen trägt seine Quelle.

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
Health-Check-Bewertung, Hevy-Routine-Payloads, Obsidian-Schreibpfade,
Analytics (Load-Modus, e1RM, Plateau, Double Progression, Buckets),
Coach-Tools (Ziel → DB + Hevy-Notiz, Plan, Follow-ups, Memory-Tools),
Agent-Jobs (NO_MESSAGE, Fehlertexte, Post-Workout-Dedupe).
