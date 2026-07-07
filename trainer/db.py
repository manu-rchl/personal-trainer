"""SQLite-Zugriff: Verbindung im WAL-Modus + Schema-Initialisierung.

Single-User-Projekt, eine DB-Datei (siehe config.DB_PATH). Alle Tabellen werden
mit CREATE TABLE IF NOT EXISTS angelegt, damit init_db() idempotent ist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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

CREATE TABLE IF NOT EXISTS health_metrics (
    source TEXT NOT NULL,
    metric TEXT NOT NULL,
    ts TEXT NOT NULL,
    value REAL,
    unit TEXT,
    PRIMARY KEY (source, metric, ts)
);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    type TEXT,
    source TEXT,
    notes TEXT
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
    content TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
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
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Öffnet eine SQLite-Verbindung im WAL-Modus.

    Legt den übergeordneten Ordner an, falls er fehlt.
    """
    path = db_path or config.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Legt alle Tabellen an (idempotent, CREATE TABLE IF NOT EXISTS)."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"DB initialisiert: {config.db_path}")
