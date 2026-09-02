"""Hevy API Ingestion — Workout-Sync + Exercise-Template-Cache.

Hevy ist die einzige Sync-Quelle für Workouts (daneben nur Chat-Logging).

Usage:
    uv run python -m trainer.ingest.hevy sync [--full]
    uv run python -m trainer.ingest.hevy templates

Auth: Header `api-key: <HEVY_API_KEY>` (kein OAuth, kein Bearer-Prefix).

Dedupe: `workouts.ext_id` hält die native Hevy-Workout-ID. Ein erneuter Sync
UPSERTed über `ext_id` — vorhandene Sätze werden bei einem Update komplett
ersetzt (DELETE + INSERT), nicht dupliziert.

Pagination: GET /v1/workouts und GET /v1/exercise_templates sind über
`page`/`pageSize` paginiert. Die tatsächliche maximale `pageSize` wird per
Live-Call ermittelt (siehe Verifikationsbericht) — die Konstanten unten sind
konservative Defaults, die von der API ggf. selbst gedeckelt werden.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from trainer.config import config
from trainer.db import get_connection, init_db

API_BASE = "https://api.hevyapp.com"

WORKOUTS_PAGE_SIZE = 10
TEMPLATES_PAGE_SIZE = 100

# Ohne --full nur die ersten Seiten holen (Alltags-Sync "hab grad trainiert").
DEFAULT_SYNC_PAGES = 2

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


# --------------------------------------------------------------------------
# sync_state Helfer (identisch zu trainer.ingest.oura)
# --------------------------------------------------------------------------


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# --------------------------------------------------------------------------
# HTTP-Helfer
# --------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"api-key": config.hevy_api_key, "Content-Type": "application/json"}


def _get_json(client: httpx.Client, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET mit einfachem Retry/Backoff bei 429 (max. `MAX_RETRIES` Versuche)."""
    resp: httpx.Response | None = None
    for attempt in range(MAX_RETRIES):
        resp = client.get(path, params=params)
        if resp.status_code == 429:
            if attempt == MAX_RETRIES - 1:
                break
            wait = RETRY_BACKOFF_SECONDS * (attempt + 1)
            print(
                f"429 (Rate-Limit) bei {path}, warte {wait:.0f}s "
                f"(Versuch {attempt + 1}/{MAX_RETRIES})...",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()

    assert resp is not None
    resp.raise_for_status()
    return resp.json()  # unreachable, aber für den Type-Checker


def _extract_list(data: dict[str, Any], candidates: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in candidates:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_page_count(data: dict[str, Any]) -> int | None:
    for key in ("page_count", "pageCount", "total_pages", "totalPages"):
        value = data.get(key)
        if isinstance(value, int):
            return value
    return None


def _require_api_key() -> None:
    if not config.hevy_api_key:
        print(
            "FEHLER: HEVY_API_KEY fehlt (siehe .env.example). Ohne Key ist "
            "keine Hevy-Synchronisierung möglich.",
            file=sys.stderr,
        )
        sys.exit(1)


# --------------------------------------------------------------------------
# Workout-Sync
# --------------------------------------------------------------------------


def _local_date_from_iso(ts: str) -> str:
    """Wandelt einen Hevy-Zeitstempel (ISO8601, meist UTC) in ein lokales YYYY-MM-DD."""
    value = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()


def upsert_hevy_workout(conn: sqlite3.Connection, workout: dict[str, Any]) -> str:
    """UPSERTed ein Hevy-Workout über `ext_id`. Gibt 'inserted'/'updated'/'skipped' zurück.

    Bei einem Update werden die alten `workout_sets` komplett ersetzt (nicht
    dedupliziert gemergt) — Hevy liefert bei jedem Sync den vollständigen
    aktuellen Stand des Workouts.
    """
    ext_id = workout.get("id")
    start_time = workout.get("start_time")
    if not ext_id or not start_time:
        return "skipped"

    date_str = _local_date_from_iso(start_time)
    type_ = workout.get("title") or "Workout"

    row = conn.execute("SELECT id FROM workouts WHERE ext_id = ?", (ext_id,)).fetchone()
    if row:
        workout_id = row["id"]
        conn.execute(
            "UPDATE workouts SET date = ?, type = ?, source = 'hevy' WHERE id = ?",
            (date_str, type_, workout_id),
        )
        conn.execute("DELETE FROM workout_sets WHERE workout_id = ?", (workout_id,))
        status = "updated"
    else:
        cur = conn.execute(
            "INSERT INTO workouts (date, type, source, notes, ext_id) VALUES (?, ?, 'hevy', NULL, ?)",
            (date_str, type_, ext_id),
        )
        workout_id = cur.lastrowid
        status = "inserted"

    for exercise in workout.get("exercises", []) or []:
        exercise_name = exercise.get("title") or ""
        for idx, s in enumerate(exercise.get("sets", []) or []):
            # Alle Sätze übernehmen (auch warmup) — kein Filter nach Satztyp.
            conn.execute(
                """
                INSERT INTO workout_sets (workout_id, exercise, set_no, reps, weight_kg)
                VALUES (?, ?, ?, ?, ?)
                """,
                (workout_id, exercise_name, idx + 1, s.get("reps"), s.get("weight_kg")),
            )

    return status


def sync(full: bool = False) -> dict[str, Any]:
    """Synchronisiert Hevy-Workouts in die DB.

    full=False: nur die ersten `DEFAULT_SYNC_PAGES` Seiten (neueste Workouts,
    für den Alltags-Sync). full=True: alle Seiten (Backfill/Migration).
    """
    _require_api_key()
    conn = get_connection()
    try:
        inserted = 0
        updated = 0
        skipped = 0
        page = 1

        with httpx.Client(base_url=API_BASE, headers=_headers(), timeout=30) as client:
            while True:
                data = _get_json(
                    client, "/v1/workouts", {"page": page, "pageSize": WORKOUTS_PAGE_SIZE}
                )
                workouts = _extract_list(data, ("workouts", "data"))
                if not workouts:
                    break

                for w in workouts:
                    status = upsert_hevy_workout(conn, w)
                    if status == "inserted":
                        inserted += 1
                    elif status == "updated":
                        updated += 1
                    else:
                        skipped += 1
                conn.commit()

                page_count = _extract_page_count(data)
                if not full and page >= DEFAULT_SYNC_PAGES:
                    break
                if page_count is not None and page >= page_count:
                    break
                if page_count is None and len(workouts) < WORKOUTS_PAGE_SIZE:
                    break
                page += 1

        _set_state(conn, "hevy_last_sync", str(time.time()))
        conn.commit()
        return {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "pages_fetched": page,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Exercise-Template-Cache
# --------------------------------------------------------------------------


def cache_templates() -> int:
    """Holt alle Hevy-Exercise-Templates paginiert und cached sie in der DB."""
    _require_api_key()
    conn = get_connection()
    try:
        count = 0
        page = 1

        with httpx.Client(base_url=API_BASE, headers=_headers(), timeout=30) as client:
            while True:
                data = _get_json(
                    client,
                    "/v1/exercise_templates",
                    {"page": page, "pageSize": TEMPLATES_PAGE_SIZE},
                )
                templates = _extract_list(data, ("exercise_templates", "data"))
                if not templates:
                    break

                for t in templates:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO hevy_exercise_templates
                            (id, title, primary_muscle, equipment)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            t.get("id"),
                            t.get("title"),
                            t.get("primary_muscle_group") or t.get("primary_muscle"),
                            t.get("equipment") or t.get("equipment_category"),
                        ),
                    )
                    count += 1
                conn.commit()

                page_count = _extract_page_count(data)
                if page_count is not None and page >= page_count:
                    break
                if page_count is None and len(templates) < TEMPLATES_PAGE_SIZE:
                    break
                page += 1

        _set_state(conn, "hevy_templates_cached_at", str(time.time()))
        conn.commit()
        return count
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trainer.ingest.hevy")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="Workouts von Hevy synchronisieren")
    sync_parser.add_argument(
        "--full",
        action="store_true",
        help="Alle Seiten holen (Backfill), statt nur die neuesten (Alltags-Sync)",
    )

    sub.add_parser("templates", help="Exercise-Templates cachen")

    args = parser.parse_args()
    init_db()

    if args.command == "sync":
        result = sync(full=args.full)
        print(
            f"Fertig. Neu: {result['inserted']}, aktualisiert: {result['updated']}, "
            f"übersprungen: {result['skipped']}."
        )
    elif args.command == "templates":
        count = cache_templates()
        print(f"Fertig. {count} Exercise-Templates gecached.")


if __name__ == "__main__":
    main()
