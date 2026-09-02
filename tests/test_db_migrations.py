"""Migrationen: frische DB, Alt-DB (Tokens in sync_state, Strong-Müll), Idempotenz."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from trainer.db import SCHEMA_VERSION, get_connection, get_schema_version, init_db


def _legacy_db(path: Path) -> None:
    """Baut eine DB im Zustand VOR den Migrationen (Stand Juli 2026)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, content TEXT);
        CREATE TABLE workouts (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, type TEXT, source TEXT, notes TEXT);
        CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE health_metrics (source TEXT, metric TEXT, ts TEXT, value REAL, unit TEXT);
        INSERT INTO sync_state VALUES ('oura_access_token', 'AT'), ('oura_refresh_token', 'RT'),
            ('oura_token_expires_at', '123.0'), ('oura_last_sync', '1.0'),
            ('strong_row_abc', '1'), ('strong_row_def', '1');
        INSERT INTO messages (ts, role, content) VALUES ('t', 'user', 'hi');
        """
    )
    conn.commit()
    conn.close()


def test_fresh_db_is_at_current_version(tmp_path: Path):
    db = tmp_path / "fresh.db"
    init_db(db)
    conn = get_connection(db)
    try:
        assert get_schema_version(conn) == SCHEMA_VERSION
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"secrets", "tool_log", "messages", "workouts", "sync_state"} <= tables
        assert "health_metrics" not in tables
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        assert "agent" in cols
    finally:
        conn.close()


def test_legacy_db_is_migrated(tmp_path: Path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    init_db(db)
    conn = get_connection(db)
    try:
        assert get_schema_version(conn) == SCHEMA_VERSION
        # Migration 1: fehlende Spalten
        assert "agent" in {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        assert "ext_id" in {r["name"] for r in conn.execute("PRAGMA table_info(workouts)")}
        # Alte Zeilen bekommen den Default-Agenten
        assert conn.execute("SELECT agent FROM messages").fetchone()["agent"] == "isa"
        # Migration 2: Tokens umgezogen, Müll weg, Tabelle gedroppt
        secrets = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM secrets")}
        assert secrets == {
            "oura_access_token": "AT",
            "oura_refresh_token": "RT",
            "oura_token_expires_at": "123.0",
        }
        remaining = {r["key"] for r in conn.execute("SELECT key FROM sync_state")}
        assert remaining == {"oura_last_sync"}
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='health_metrics'"
        ).fetchone()
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path: Path):
    db = tmp_path / "twice.db"
    _legacy_db(db)
    init_db(db)
    init_db(db)  # darf weder failen noch etwas doppelt tun
    conn = get_connection(db)
    try:
        assert get_schema_version(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) AS n FROM secrets").fetchone()["n"] == 3
    finally:
        conn.close()
