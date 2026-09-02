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
    ext_id TEXT
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
    content TEXT
);

CREATE TABLE IF NOT EXISTS exercise_aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
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


# (Versionsnummer, Funktion). Neue Migrationen unten anhängen, Nummer +1.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _migration_1_legacy_columns),
    (2, _migration_2_secrets_and_cleanup),
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
