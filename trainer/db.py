"""SQLite-Zugriff: Verbindung im WAL-Modus + Schema-Initialisierung + Migrationen.

Single-User-Projekt, eine DB-Datei (siehe config.DB_PATH).

- `SCHEMA` beschreibt den ZIELZUSTAND für eine frische DB (CREATE ... IF NOT
  EXISTS, idempotent).
- `MIGRATIONS` bringt bestehende DBs auf diesen Stand (neue Spalten, Daten-
  Umzüge, Drop alter Tabellen). Welche Migrationen schon gelaufen sind, hält
  `PRAGMA user_version` fest — jede Migration läuft genau einmal, in einer
  Transaktion.

`init_db()` wird NUR an Prozess-Einstiegspunkten aufgerufen (Bot-Start,
Web-Start, Job-/Ingest-`main()`), nicht in jeder Tool-Funktion.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from trainer.config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS oura_daily (
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT,
    sleep_score INTEGER,
    readiness_score INTEGER,
    activity_score INTEGER,
    hrv_avg REAL,
    resting_hr REAL,
    sleep_duration_min REAL,
    steps INTEGER,
    PRIMARY KEY (date, kind)
);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    type TEXT,
    source TEXT,
    notes TEXT,
    ext_id TEXT,
    checkin_sent_at TEXT
);

CREATE TABLE IF NOT EXISTS workout_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id INTEGER REFERENCES workouts (id),
    exercise TEXT,
    set_no INTEGER,
    reps INTEGER,
    weight_kg REAL
);

CREATE TABLE IF NOT EXISTS profile (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    role TEXT,
    content TEXT,
    agent TEXT DEFAULT 'isa'
);

-- Interne, UNKRITISCHE Sync-Metadaten (Zeitstempel, Dedupe-Keys). Für
-- Geheimnisse (OAuth-Tokens) gibt es `secrets` — die Trennung erlaubt es,
-- `query_db` den Zugriff auf `secrets` hart zu verweigern.
CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS secrets (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    description TEXT,
    calories_kcal REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    source TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    category TEXT,
    content TEXT,
    source TEXT,
    valid_from TEXT,
    updated_at TEXT,
    pinned INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS exercise_aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

-- Coach-Kern (Phase 1): Plan + Ziele als DATEN statt Freitext in Memories.
-- load_mode: wie `workout_sets.weight_kg` zu lesen ist (barbell_per_side |
-- per_hand | total) — siehe trainer.analytics.effective_load.
CREATE TABLE IF NOT EXISTS exercise_meta (
    exercise TEXT PRIMARY KEY,
    load_mode TEXT NOT NULL,
    primary_muscle TEXT,
    hevy_template_id TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS training_plan (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    active INTEGER NOT NULL DEFAULT 1,
    name TEXT,
    split TEXT,
    days_per_week INTEGER,
    block_start TEXT,
    block_weeks INTEGER,
    progression_rule TEXT,
    deload_rule TEXT,
    notes TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS exercise_targets (
    exercise TEXT PRIMARY KEY,
    target_weight_kg REAL,
    rep_min INTEGER,
    rep_max INTEGER,
    sets INTEGER,
    reason TEXT,
    source TEXT,
    updated_at TEXT
);

-- Körpergewicht (lokale Wahrheit; Hevy-Body-Measurements werden best-effort
-- mitgeschrieben). Ein Wert pro Tag.
CREATE TABLE IF NOT EXISTS body_weight (
    date TEXT PRIMARY KEY,
    weight_kg REAL NOT NULL,
    source TEXT,
    ts TEXT
);

-- Follow-ups, die Isa sich selbst setzt ("Legs in Düsseldorf?"); der
-- Post-Workout-Job schickt fällige als Agent-Turn raus.
CREATE TABLE IF NOT EXISTS scheduled_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    due_date TEXT NOT NULL,
    text TEXT NOT NULL,
    created_ts TEXT,
    sent_at TEXT
);

-- Rollende Zusammenfassung der Chat-Historie, die aus dem Kontextfenster
-- gefallen ist (Phase 2).
CREATE TABLE IF NOT EXISTS history_summaries (
    agent TEXT PRIMARY KEY,
    upto_message_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS hevy_exercise_templates (
    id TEXT PRIMARY KEY,
    title TEXT,
    primary_muscle TEXT,
    equipment TEXT
);

-- Protokoll jedes Tool-Aufrufs des Agenten (Input + gekapptes Ergebnis).
-- Grund: Isa hat in der Vergangenheit "gespeichert" behauptet, ohne dass ein
-- Tool lief — ohne Log war das nicht rekonstruierbar.
CREATE TABLE IF NOT EXISTS tool_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    tool TEXT NOT NULL,
    input_json TEXT,
    result_json TEXT,
    ok INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_workout_sets_workout_id ON workout_sets (workout_id);
CREATE INDEX IF NOT EXISTS idx_workouts_date ON workouts (date);
CREATE INDEX IF NOT EXISTS idx_meals_ts ON meals (ts);
CREATE INDEX IF NOT EXISTS idx_tool_log_ts ON tool_log (ts);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Öffnet eine SQLite-Verbindung im WAL-Modus.

    busy_timeout: Bot, Web und Jobs schreiben aus getrennten Prozessen —
    ohne Timeout fliegt bei einer Überschneidung sofort "database is locked".
    """
    path = db_path or config.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Migrationen
# ---------------------------------------------------------------------------


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _migration_1_legacy_columns(conn: sqlite3.Connection) -> None:
    """Spalten, die früher per try/except-ALTER nachgezogen wurden."""
    if not _has_column(conn, "messages", "agent"):
        conn.execute("ALTER TABLE messages ADD COLUMN agent TEXT DEFAULT 'isa'")
    if not _has_column(conn, "workouts", "ext_id"):
        conn.execute("ALTER TABLE workouts ADD COLUMN ext_id TEXT")


_OURA_SECRET_KEYS = ("oura_access_token", "oura_refresh_token", "oura_token_expires_at")


def _migration_2_secrets_and_cleanup(conn: sqlite3.Connection) -> None:
    """Oura-Tokens aus sync_state nach secrets; Strong-Dedupe-Müll und
    Apple-Health-Tabelle entfernen (beides seit 2026-09 verworfen)."""
    placeholders = ",".join("?" for _ in _OURA_SECRET_KEYS)
    conn.execute(
        f"INSERT OR REPLACE INTO secrets (key, value) "
        f"SELECT key, value FROM sync_state WHERE key IN ({placeholders})",
        _OURA_SECRET_KEYS,
    )
    conn.execute(f"DELETE FROM sync_state WHERE key IN ({placeholders})", _OURA_SECRET_KEYS)
    conn.execute("DELETE FROM sync_state WHERE key LIKE 'strong_row_%'")
    conn.execute("DROP TABLE IF EXISTS health_metrics")


DEFAULT_TRAINING_PLAN = {
    "name": "PPL Basis",
    "split": "Push / Pull / Legs (Hevy-Master-Routinen)",
    "days_per_week": 3,
    "block_weeks": 6,
    "progression_rule": (
        "Double Progression: Rep-Range pro Übung (Standard 8-12). Alle Arbeitssätze "
        "sauber am oberen Ende -> kleinste Steigerung (Langhantel +1,25 kg pro Seite, "
        "Kurzhantel +2,5 kg, Maschine/Kabel +1 Platte). Reps brechen ein -> halten. "
        "3 Sessions ohne neues e1RM -> Plateau: Variante rotieren oder Deload."
    ),
    "deload_rule": (
        "Alle 6-8 Wochen oder bei Plateau + Readiness-Tief (Oura < 60 an mehreren "
        "Tagen): eine Woche ~60 % Last, gleiche Übungen."
    ),
    "notes": "Seed aus Phase 1 (2026-09-03). Über set_training_plan anpassen.",
}


def _migration_3_coach_core(conn: sqlite3.Connection) -> None:
    """Phase 1: Spalten für Check-in-Dedupe und Memory-Metadaten, Plan-Seed.

    Bestehende Workouts gelten als 'Check-in erledigt', sonst würde der
    Post-Workout-Job beim ersten Lauf alle Alt-Workouts abarbeiten.
    """
    if not _has_column(conn, "workouts", "checkin_sent_at"):
        conn.execute("ALTER TABLE workouts ADD COLUMN checkin_sent_at TEXT")
    conn.execute(
        "UPDATE workouts SET checkin_sent_at = COALESCE(checkin_sent_at, 'migrated-3')"
    )
    for col, ddl in (
        ("source", "TEXT"),
        ("valid_from", "TEXT"),
        ("updated_at", "TEXT"),
        ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if not _has_column(conn, "memories", col):
            conn.execute(f"ALTER TABLE memories ADD COLUMN {col} {ddl}")
    if conn.execute("SELECT COUNT(*) FROM training_plan").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO training_plan (active, name, split, days_per_week, block_start, "
            "block_weeks, progression_rule, deload_rule, notes, updated_at) "
            "VALUES (1, ?, ?, ?, date('now'), ?, ?, ?, ?, datetime('now'))",
            (
                DEFAULT_TRAINING_PLAN["name"],
                DEFAULT_TRAINING_PLAN["split"],
                DEFAULT_TRAINING_PLAN["days_per_week"],
                DEFAULT_TRAINING_PLAN["block_weeks"],
                DEFAULT_TRAINING_PLAN["progression_rule"],
                DEFAULT_TRAINING_PLAN["deload_rule"],
                DEFAULT_TRAINING_PLAN["notes"],
            ),
        )


# (Versionsnummer, Funktion). Neue Migrationen unten anhängen, Nummer +1.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_1_legacy_columns),
    (2, _migration_2_secrets_and_cleanup),
    (3, _migration_3_coach_core),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def get_schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def init_db(db_path: Path | None = None) -> None:
    """Legt alle Tabellen an (idempotent) und führt ausstehende Migrationen aus."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()

        current = get_schema_version(conn)
        for version, migrate in MIGRATIONS:
            if version <= current:
                continue
            # executescript hat implizit committed; hier explizit eine Transaktion
            # pro Migration, damit ein Fehler nichts Halbes hinterlässt.
            conn.execute("BEGIN")
            try:
                migrate(conn)
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    with get_connection() as _conn:
        print(f"DB initialisiert: {config.db_path} (Schema-Version {get_schema_version(_conn)})")
