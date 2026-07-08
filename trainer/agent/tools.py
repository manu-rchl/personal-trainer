"""Tool-Definitionen (Anthropic tool-use Schema) + Python-Implementierungen.

Jedes Tool ist eine reine Python-Funktion, die JSON-serialisierbare Werte
zurückgibt (dict/list/primitives). `TOOL_SCHEMAS` wird 1:1 als `tools`-Parameter
an `anthropic.messages.create` übergeben; `TOOL_FUNCTIONS` mappt Tool-Namen auf
die Implementierung (Dispatch passiert in `trainer.agent.core`).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import recurring_ical_events
from icalendar import Calendar

from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.exercise_norm import normalize_name
from trainer.ingest import hevy as hevy_ingest

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def _cutoff_date(days: int) -> str:
    """ISO-Datum (YYYY-MM-DD) für 'vor `days` Tagen', als Untergrenze für Filter."""
    return (date.today() - timedelta(days=days)).isoformat()


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _round(value: Any, ndigits: int = 1) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# get_health_summary
# ---------------------------------------------------------------------------


def get_health_summary(days: int = 7) -> dict[str, Any]:
    """Aggregierte Oura-Kennzahlen + health_metrics-Übersicht der letzten `days` Tage."""
    init_db()
    conn = get_connection()
    try:
        cutoff = _cutoff_date(days)

        agg_row = conn.execute(
            """
            SELECT
                AVG(sleep_score) AS sleep_score_avg,
                MIN(sleep_score) AS sleep_score_min,
                MAX(sleep_score) AS sleep_score_max,
                AVG(readiness_score) AS readiness_score_avg,
                MIN(readiness_score) AS readiness_score_min,
                MAX(readiness_score) AS readiness_score_max,
                AVG(activity_score) AS activity_score_avg,
                MIN(activity_score) AS activity_score_min,
                MAX(activity_score) AS activity_score_max,
                AVG(hrv_avg) AS hrv_avg_avg,
                MIN(hrv_avg) AS hrv_avg_min,
                MAX(hrv_avg) AS hrv_avg_max,
                AVG(resting_hr) AS resting_hr_avg,
                MIN(resting_hr) AS resting_hr_min,
                MAX(resting_hr) AS resting_hr_max,
                AVG(sleep_duration_min) AS sleep_duration_min_avg,
                MIN(sleep_duration_min) AS sleep_duration_min_min,
                MAX(sleep_duration_min) AS sleep_duration_min_max,
                AVG(steps) AS steps_avg,
                MIN(steps) AS steps_min,
                MAX(steps) AS steps_max
            FROM oura_daily
            WHERE date >= ?
            """,
            (cutoff,),
        ).fetchone()

        aggregates = {k: _round(v) for k, v in dict(agg_row).items()} if agg_row else {}

        daily_rows = conn.execute(
            """
            SELECT date, kind, sleep_score, readiness_score, activity_score,
                   hrv_avg, resting_hr, sleep_duration_min, steps
            FROM oura_daily
            WHERE date >= ?
            ORDER BY date DESC
            """,
            (cutoff,),
        ).fetchall()

        metrics_rows = conn.execute(
            """
            SELECT metric, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
            FROM health_metrics
            WHERE ts >= ?
            GROUP BY metric
            ORDER BY metric
            """,
            (cutoff,),
        ).fetchall()

        return {
            "period_days": days,
            "since": cutoff,
            "oura_aggregates": aggregates,
            "oura_daily": _rows_to_dicts(daily_rows),
            "health_metrics_counts": _rows_to_dicts(metrics_rows),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_workouts
# ---------------------------------------------------------------------------


def get_workouts(days: int = 14) -> dict[str, Any]:
    """Workouts inkl. Sätze der letzten `days` Tage (alle Quellen)."""
    init_db()
    conn = get_connection()
    try:
        cutoff = _cutoff_date(days)

        workout_rows = conn.execute(
            """
            SELECT id, date, type, source, notes
            FROM workouts
            WHERE date >= ?
            ORDER BY date DESC, id DESC
            """,
            (cutoff,),
        ).fetchall()

        workouts: list[dict[str, Any]] = []
        for w in workout_rows:
            w_dict = dict(w)
            set_rows = conn.execute(
                """
                SELECT exercise, set_no, reps, weight_kg
                FROM workout_sets
                WHERE workout_id = ?
                ORDER BY exercise, set_no
                """,
                (w_dict["id"],),
            ).fetchall()
            w_dict["sets"] = _rows_to_dicts(set_rows)
            workouts.append(w_dict)

        return {
            "period_days": days,
            "since": cutoff,
            "workout_count": len(workouts),
            "workouts": workouts,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# log_workout
# ---------------------------------------------------------------------------


def log_workout(
    type: str,
    sets: list[dict[str, Any]],
    date: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Schreibt ein Workout (source='chat') + zugehörige Sätze."""
    init_db()
    # Lokale Zeit, nicht UTC — sonst landet ein Workout nach Mitternacht am Vortag.
    workout_date = date or datetime.now().date().isoformat()

    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO workouts (date, type, source, notes) VALUES (?, ?, 'chat', ?)",
            (workout_date, type, notes),
        )
        workout_id = cur.lastrowid

        inserted = 0
        for s in sets:
            conn.execute(
                """
                INSERT INTO workout_sets (workout_id, exercise, set_no, reps, weight_kg)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    workout_id,
                    s.get("exercise"),
                    s.get("set_no"),
                    s.get("reps"),
                    s.get("weight_kg"),
                ),
            )
            inserted += 1

        conn.commit()
        return {
            "workout_id": workout_id,
            "date": workout_date,
            "type": type,
            "sets_inserted": inserted,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# log_meal / get_meals
# ---------------------------------------------------------------------------


def log_meal(
    description: str,
    calories_kcal: float,
    protein_g: float,
    carbs_g: float,
    fat_g: float,
    ts: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Speichert eine (i.d.R. per Foto analysierte) Mahlzeit inkl. Makros."""
    init_db()
    # Lokale Zeit, nicht UTC — Mahlzeiten sollen am Kalendertag von Manuel landen.
    meal_ts = ts or datetime.now().isoformat(timespec="seconds")

    conn = get_connection()
    try:
        cur = conn.execute(
            """
            INSERT INTO meals (ts, description, calories_kcal, protein_g, carbs_g, fat_g, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'chat', ?)
            """,
            (meal_ts, description, calories_kcal, protein_g, carbs_g, fat_g, notes),
        )
        conn.commit()
        return {
            "meal_id": cur.lastrowid,
            "ts": meal_ts,
            "description": description,
            "calories_kcal": calories_kcal,
            "protein_g": protein_g,
            "carbs_g": carbs_g,
            "fat_g": fat_g,
        }
    finally:
        conn.close()


def get_meals(days: int = 7) -> dict[str, Any]:
    """Mahlzeiten der letzten `days` Tage + Tages-Summen (kcal/Protein/Carbs/Fett)."""
    init_db()
    conn = get_connection()
    try:
        cutoff = _cutoff_date(days)

        meal_rows = conn.execute(
            """
            SELECT id, ts, description, calories_kcal, protein_g, carbs_g, fat_g, source, notes
            FROM meals
            WHERE ts >= ?
            ORDER BY ts DESC
            """,
            (cutoff,),
        ).fetchall()

        daily_rows = conn.execute(
            """
            SELECT
                substr(ts, 1, 10) AS day,
                SUM(calories_kcal) AS calories_kcal_sum,
                SUM(protein_g) AS protein_g_sum,
                SUM(carbs_g) AS carbs_g_sum,
                SUM(fat_g) AS fat_g_sum
            FROM meals
            WHERE ts >= ?
            GROUP BY day
            ORDER BY day DESC
            """,
            (cutoff,),
        ).fetchall()

        return {
            "period_days": days,
            "since": cutoff,
            "meal_count": len(meal_rows),
            "meals": _rows_to_dicts(meal_rows),
            "daily_totals": [
                {k: _round(v) if k != "day" else v for k, v in dict(r).items()}
                for r in daily_rows
            ],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# query_db
# ---------------------------------------------------------------------------

MAX_QUERY_ROWS = 200


def query_db(sql: str) -> dict[str, Any]:
    """Read-only SQL-Zugriff, nur SELECT erlaubt. Max. 200 Zeilen.

    Öffnet die DB über eine echte SQLite-Readonly-URI-Verbindung
    (file:...?mode=ro), sodass auch versehentliche/böswillige Schreibversuche
    auf DB-Ebene fehlschlagen — nicht nur durch den String-Check.
    """
    stripped = sql.strip()
    # Führende SQL-Kommentare entfernen, bevor wir auf SELECT prüfen.
    lowered = stripped.lstrip()
    if not lowered.lower().startswith("select"):
        return {
            "error": "Nur SELECT-Statements sind erlaubt.",
            "rejected_sql": sql,
        }

    init_db()  # stellt sicher, dass die Datei existiert, bevor wir read-only öffnen
    uri = f"file:{config.db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(MAX_QUERY_ROWS)
        columns = [d[0] for d in cur.description] if cur.description else []
        return {
            "columns": columns,
            "rows": _rows_to_dicts(rows),
            "row_count": len(rows),
            "truncated": len(rows) == MAX_QUERY_ROWS,
        }
    except sqlite3.Error as exc:
        return {"error": str(exc), "sql": sql}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_profile / update_profile
# ---------------------------------------------------------------------------


def get_profile() -> dict[str, Any]:
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute("SELECT key, value FROM profile ORDER BY key").fetchall()
        return {"profile": {r["key"]: r["value"] for r in rows}}
    finally:
        conn.close()


def update_profile(key: str, value: str) -> dict[str, Any]:
    init_db()
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO profile (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
        return {"key": key, "value": value, "status": "gespeichert"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# save_memory / search_memories (Langzeit-Gedächtnis)
# ---------------------------------------------------------------------------

MAX_MEMORY_SEARCH_ROWS = 50


def save_memory(content: str, category: str) -> dict[str, Any]:
    """Speichert ein dauerhaft relevantes Fakt über Manuel im Langzeit-Gedächtnis."""
    init_db()
    ts = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO memories (ts, category, content) VALUES (?, ?, ?)",
            (ts, category, content),
        )
        conn.commit()
        return {
            "memory_id": cur.lastrowid,
            "ts": ts,
            "category": category,
            "content": content,
            "status": "gespeichert",
        }
    finally:
        conn.close()


def search_memories(query: str | None = None, category: str | None = None) -> dict[str, Any]:
    """Sucht im Langzeit-Gedächtnis (LIKE-Suche über content, optional nach Kategorie gefiltert).

    Ohne `query`: alle Memories (ggf. nach Kategorie gefiltert), max. 50, neueste zuerst.
    """
    init_db()
    conn = get_connection()
    try:
        sql = "SELECT id, ts, category, content FROM memories WHERE 1=1"
        params: list[Any] = []
        if query:
            sql += " AND content LIKE ?"
            params.append(f"%{query}%")
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(MAX_MEMORY_SEARCH_ROWS)

        rows = conn.execute(sql, params).fetchall()
        return {"memory_count": len(rows), "memories": _rows_to_dicts(rows)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# get_calendar (Google Kalender, read-only via geheime iCal-Links, mehrere möglich)
# ---------------------------------------------------------------------------

_CALENDAR_CACHE_TTL_SECONDS = 15 * 60
# Cache-Key: (url, days) — Fehler bei einem Kalender dürfen die anderen nicht blockieren.
_calendar_cache: dict[tuple[str, int], tuple[float, list[dict[str, Any]]]] = {}


def _parse_ics(ics_bytes: bytes, days: int, fallback_name: str = "") -> list[dict[str, Any]]:
    """Parst ICS-Bytes und expandiert Events der nächsten `days` Tage (inkl. Wiederholungen).

    Als eigene Funktion ausgelagert, damit sie ohne HTTP-Request testbar ist.
    Jedes Event bekommt ein "calendar"-Feld (X-WR-CALNAME der ICS, sonst fallback_name).
    """
    calendar = Calendar.from_ical(ics_bytes)
    calendar_name = str(calendar.get("X-WR-CALNAME", "")) or fallback_name

    now = datetime.now().astimezone()
    start = now
    end = now + timedelta(days=days)

    events = recurring_ical_events.of(calendar).between(start, end)

    results: list[dict[str, Any]] = []
    for event in events:
        summary = str(event.get("summary", "(ohne Titel)"))
        dtstart = event.get("dtstart").dt
        dtend_prop = event.get("dtend")
        dtend = dtend_prop.dt if dtend_prop is not None else dtstart

        all_day = not isinstance(dtstart, datetime)

        def _to_local_iso(value: Any) -> str:
            if isinstance(value, datetime):
                return value.astimezone().isoformat(timespec="minutes")
            # date (all-day)
            return value.isoformat()

        results.append(
            {
                "start": _to_local_iso(dtstart),
                "end": _to_local_iso(dtend),
                "summary": summary,
                "all_day": all_day,
                "calendar": calendar_name,
            }
        )

    results.sort(key=lambda e: e["start"])
    return results


def get_calendar(days: int = 3) -> dict[str, Any]:
    """Liest Manuels Google-Kalender (read-only, via geheime iCal-Links) für die nächsten `days` Tage.

    Unterstützt mehrere Kalender (CALENDAR_ICS_URLS, kommasepariert). Events aller
    Kalender werden chronologisch gemerged; Fehler bei einem Kalender blockieren
    die anderen nicht. In-Memory-Caching (15 Min TTL) pro URL.
    """
    urls = config.calendar_ics_urls
    if not urls:
        return {"error": "Kein Kalender konfiguriert (CALENDAR_ICS_URLS fehlt)"}

    now = time.monotonic()
    all_events: list[dict[str, Any]] = []
    errors: list[str] = []

    for i, url in enumerate(urls):
        cache_key = (url, days)
        cached = _calendar_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < _CALENDAR_CACHE_TTL_SECONDS:
            all_events.extend(cached[1])
            continue

        try:
            response = httpx.get(url, timeout=15.0, follow_redirects=True)
            response.raise_for_status()
            events = _parse_ics(response.content, days, fallback_name=f"kalender_{i + 1}")
        except Exception as exc:  # HTTP- wie Parse-Fehler: nur diesen Kalender überspringen
            errors.append(f"Kalender {i + 1} fehlgeschlagen: {exc}")
            continue

        _calendar_cache[cache_key] = (now, events)
        all_events.extend(events)

    all_events.sort(key=lambda e: e["start"])
    result: dict[str, Any] = {"period_days": days, "events": all_events}
    if errors:
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# search_notes / read_note (Obsidian-Vault, read-only)
# ---------------------------------------------------------------------------

MAX_NOTE_SEARCH_RESULTS = 10
NOTE_SNIPPET_RADIUS = 200
MAX_NOTE_READ_CHARS = 8000
_OBSIDIAN_EXCLUDED_DIRS = {".obsidian", ".trash"}


def _obsidian_vault_root() -> Path | None:
    raw = config.obsidian_vault_path
    if not raw:
        return None
    vault = Path(raw).expanduser()
    if not vault.is_dir():
        return None
    return vault.resolve()


def search_notes(query: str) -> dict[str, Any]:
    """Case-insensitive Volltextsuche über alle *.md-Dateien im Obsidian-Vault.

    Schließt .obsidian/ und .trash/ aus. Liefert max. 10 Treffer mit Datei
    (relativ zum Vault) und einem ±200-Zeichen-Ausschnitt um den Treffer.
    """
    vault = _obsidian_vault_root()
    if vault is None:
        return {"error": "Kein gültiger Obsidian-Vault konfiguriert (OBSIDIAN_VAULT_PATH fehlt oder ungültig)"}

    query_lower = query.lower()
    results: list[dict[str, Any]] = []

    for md_path in sorted(vault.rglob("*.md")):
        if len(results) >= MAX_NOTE_SEARCH_RESULTS:
            break
        try:
            relative = md_path.relative_to(vault)
        except ValueError:
            continue
        if any(part in _OBSIDIAN_EXCLUDED_DIRS for part in relative.parts):
            continue

        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        idx = text.lower().find(query_lower)
        if idx == -1:
            continue

        snippet_start = max(0, idx - NOTE_SNIPPET_RADIUS)
        snippet_end = min(len(text), idx + len(query) + NOTE_SNIPPET_RADIUS)
        snippet = text[snippet_start:snippet_end].strip()

        results.append({"file": str(relative), "snippet": snippet})

    return {"query": query, "result_count": len(results), "results": results}


def read_note(file: str) -> dict[str, Any]:
    """Liest eine einzelne Markdown-Datei aus dem Obsidian-Vault (max. 8000 Zeichen).

    Verhindert Pfad-Traversal: der aufgelöste Pfad muss unterhalb des Vaults liegen.
    """
    vault = _obsidian_vault_root()
    if vault is None:
        return {"error": "Kein gültiger Obsidian-Vault konfiguriert (OBSIDIAN_VAULT_PATH fehlt oder ungültig)"}

    candidate = (vault / file).resolve()
    if candidate != vault and vault not in candidate.parents:
        return {"error": "Ungültiger Dateipfad (außerhalb des Vaults)."}
    if not candidate.is_file():
        return {"error": f"Datei nicht gefunden: {file}"}

    try:
        text = candidate.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return {"error": f"Datei konnte nicht gelesen werden: {exc}"}

    truncated = len(text) > MAX_NOTE_READ_CHARS
    return {
        "file": file,
        "content": text[:MAX_NOTE_READ_CHARS],
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# merge_exercises (manueller Override für die Übungs-Namens-Normalisierung)
# ---------------------------------------------------------------------------


def merge_exercises(from_name: str, into_name: str) -> dict[str, Any]:
    """Ordnet `from_name` manuell `into_name` zu (exercise_aliases-Override).

    Überschreibt einen bestehenden Eintrag für denselben (normalisierten)
    `from_name`, falls vorhanden.
    """
    init_db()
    alias = normalize_name(from_name)
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO exercise_aliases (alias, canonical) VALUES (?, ?)
            ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical
            """,
            (alias, into_name),
        )
        conn.commit()
        return {
            "from_name": from_name,
            "alias": alias,
            "into_name": into_name,
            "status": "gespeichert",
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hevy-Integration: Sofort-Sync, Übungs-Suche, Routinen-Erstellung
# ---------------------------------------------------------------------------

MAX_HEVY_EXERCISE_RESULTS = 15
# Jaccard-Ähnlichkeit (Token-Overlap) der normalisierten Namen, ab der ein
# Fuzzy-Match akzeptiert wird. Bewusst konservativ — lieber "unmatched"
# zurückgeben, als eine falsche Übung in eine Routine zu schreiben.
HEVY_MATCH_MIN_SCORE = 0.5


def sync_hevy_now() -> dict[str, Any]:
    """Synchronisiert sofort die neuesten Hevy-Workouts (kein Backfill).

    Für "hab grad in Hevy trainiert, zieh's rein" — holt nur die ersten Seiten
    (neueste Workouts), nicht die komplette Historie.
    """
    if not config.hevy_api_key:
        return {"error": "Hevy ist nicht konfiguriert (HEVY_API_KEY fehlt)."}
    try:
        result = hevy_ingest.sync(full=False)
    except Exception as exc:  # Netzwerk-/API-Fehler nicht crashen lassen
        return {"error": f"Hevy-Sync fehlgeschlagen: {exc}"}
    return {
        "workouts_new": result["inserted"],
        "workouts_updated": result["updated"],
    }


def search_hevy_exercises(query: str) -> dict[str, Any]:
    """Durchsucht den gecachten Hevy-Übungskatalog per Fuzzy-Teilstring-Suche.

    Vergleicht normalisierte Namen (siehe `normalize_name`), damit Groß-/
    Kleinschreibung und Satzzeichen keine Rolle spielen. Liefert max. 15
    Treffer ({id, title, primary_muscle}).
    """
    init_db()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, primary_muscle FROM hevy_exercise_templates"
        ).fetchall()
        if not rows:
            return {
                "error": (
                    "Der Hevy-Übungs-Katalog ist noch nicht gecached. Erst "
                    "`python -m trainer.ingest.hevy templates` ausführen."
                )
            }

        norm_query = normalize_name(query)
        matches = [
            dict(r) for r in rows if norm_query in normalize_name(r["title"] or "")
        ]
        matches.sort(key=lambda m: len(m["title"] or ""))
        results = matches[:MAX_HEVY_EXERCISE_RESULTS]
        return {"query": query, "result_count": len(results), "results": results}
    finally:
        conn.close()


def _best_hevy_template_match(
    name: str, templates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Fuzzy-Match eines freien Übungsnamens gegen den Template-Katalog.

    Exakter Treffer (normalisiert) gewinnt sofort. Sonst Jaccard-Token-Overlap
    der normalisierten Namen; unterhalb `HEVY_MATCH_MIN_SCORE` gilt der Name
    als ungematcht (wird NICHT geraten).
    """
    norm_name = normalize_name(name)
    name_tokens = set(norm_name.split())
    if not name_tokens:
        return None

    best: dict[str, Any] | None = None
    best_score = 0.0
    for t in templates:
        norm_title = normalize_name(t.get("title") or "")
        if norm_title == norm_name:
            return t
        title_tokens = set(norm_title.split())
        if not title_tokens:
            continue
        overlap = name_tokens & title_tokens
        if not overlap:
            continue
        score = len(overlap) / len(name_tokens | title_tokens)
        if score > best_score:
            best_score = score
            best = t

    if best is not None and best_score >= HEVY_MATCH_MIN_SCORE:
        return best
    return None


def _build_hevy_routine_payload(
    conn: sqlite3.Connection, title: str, exercises: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Baut den POST-Payload für /v1/routines + Match-/Unmatch-Listen.

    Reine Funktion ohne Netzwerk-Aufruf — separat testbar (Dry-Run) und von
    `create_hevy_routine` genutzt.
    """
    templates = [
        dict(r)
        for r in conn.execute(
            "SELECT id, title, primary_muscle FROM hevy_exercise_templates"
        ).fetchall()
    ]

    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    payload_exercises: list[dict[str, Any]] = []

    for ex in exercises:
        name = ex.get("name", "")
        sets = int(ex.get("sets") or 1)
        reps = ex.get("reps")

        best = _best_hevy_template_match(name, templates)
        if best is None:
            unmatched.append(name)
            continue

        matched.append(
            {"name": name, "matched_title": best["title"], "template_id": best["id"]}
        )
        exercise_payload: dict[str, Any] = {
            "exercise_template_id": best["id"],
            "sets": [
                {"type": "normal", "reps": reps, "weight_kg": None}
                for _ in range(max(sets, 1))
            ],
        }
        rest_seconds = ex.get("rest_seconds")
        if rest_seconds is not None:
            exercise_payload["rest_seconds"] = rest_seconds
        payload_exercises.append(exercise_payload)

    # folder_id MUSS explizit gesendet werden — Hevy lehnt POST /v1/routines
    # sonst mit 400 "Invalid routine folder id: undefined" ab. `null` legt die
    # Routine im Standard-Ordner "My Routines" an (siehe Hevy-OpenAPI-Doku).
    payload = {
        "routine": {"title": title, "folder_id": None, "exercises": payload_exercises}
    }
    return payload, matched, unmatched


def create_hevy_routine(title: str, exercises: list[dict[str, Any]]) -> dict[str, Any]:
    """Erstellt eine neue Hevy-Routine aus einer Liste von Übungen.

    `exercises`: Liste von {name, sets, reps, rest_seconds?}. Jeder Name wird
    per Fuzzy-Match (normalisierte Token-Überlappung) gegen den gecachten
    Hevy-Übungskatalog (`search_hevy_exercises`/`cache_templates`) aufgelöst.
    NICHT gematchte Namen werden NICHT geraten, sondern als "unmatched"
    zurückgegeben, damit Isa beim Nutzer nachfragen kann. Bei Erfolg wird die
    Routine live in Manuels Hevy-Account angelegt (POST /v1/routines).
    """
    if not config.hevy_api_key:
        return {"error": "Hevy ist nicht konfiguriert (HEVY_API_KEY fehlt)."}

    init_db()
    conn = get_connection()
    try:
        template_count = conn.execute(
            "SELECT COUNT(*) AS n FROM hevy_exercise_templates"
        ).fetchone()["n"]
        if template_count == 0:
            return {
                "error": (
                    "Der Hevy-Übungs-Katalog ist noch nicht gecached. Erst "
                    "`python -m trainer.ingest.hevy templates` ausführen."
                )
            }

        payload, matched, unmatched = _build_hevy_routine_payload(conn, title, exercises)
    finally:
        conn.close()

    if not matched:
        return {
            "error": "Keine der angegebenen Übungen konnte gematcht werden.",
            "unmatched": unmatched,
        }

    try:
        resp = httpx.post(
            f"{hevy_ingest.API_BASE}/v1/routines",
            headers={"api-key": config.hevy_api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        return {"error": f"Hevy-API-Fehler beim Erstellen der Routine: {exc}"}

    data = resp.json()
    # Live beobachtet: Hevy liefert "routine" als Array mit einem Element
    # zurück, nicht als einzelnes Objekt (weicht von der eigenen OpenAPI-Doku
    # ab, die ein plain object dokumentiert) — beide Formen abfangen.
    routine_obj: dict[str, Any] = data.get("routine") or {}
    if isinstance(routine_obj, list):
        routine_obj = routine_obj[0] if routine_obj else {}
    routine_id = data.get("id") or routine_obj.get("id")

    return {
        "routine_id": routine_id,
        "matched": matched,
        "unmatched": unmatched,
    }


# ---------------------------------------------------------------------------
# Anthropic tool-use Schema
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_health_summary",
        "description": (
            "Aggregierte Health-/Recovery-Kennzahlen der letzten N Tage: "
            "Durchschnitt/Min/Max von Oura-Schlaf-, Readiness- und Aktivitäts-Score, "
            "HRV, Ruheherzfrequenz, Schlafdauer und Schritten, plus Tageswerte und "
            "eine Übersicht, wie viele Apple-Health-Datenpunkte pro Metrik vorliegen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Anzahl der Tage rückwirkend (default 7).",
                }
            },
            "required": ["days"],
        },
    },
    {
        "name": "get_workouts",
        "description": (
            "Liefert alle geloggten Workouts (inkl. Sätze: Übung, Satznummer, "
            "Wiederholungen, Gewicht) der letzten N Tage, unabhängig von der Quelle "
            "(Strong-CSV-Import oder Chat-Logging)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Anzahl der Tage rückwirkend (default 14).",
                }
            },
            "required": ["days"],
        },
    },
    {
        "name": "log_workout",
        "description": (
            "Speichert ein vom Nutzer im Chat berichtetes Workout inkl. aller Sätze "
            "in der Datenbank (Quelle 'chat')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Datum im Format YYYY-MM-DD. Weglassen = heute.",
                },
                "type": {
                    "type": "string",
                    "description": "Trainingsart/Name des Workouts, z.B. 'Push Day'.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optionale Notiz zum Workout.",
                },
                "sets": {
                    "type": "array",
                    "description": "Liste der Sätze.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "exercise": {"type": "string"},
                            "set_no": {"type": "integer"},
                            "reps": {"type": "integer"},
                            "weight_kg": {"type": "number"},
                        },
                        "required": ["exercise"],
                    },
                },
            },
            "required": ["type", "sets"],
        },
    },
    {
        "name": "query_db",
        "description": (
            "Führt ein rohes read-only SELECT-Statement gegen die SQLite-Datenbank "
            "aus (max. 200 Zeilen). Nur verwenden, wenn get_health_summary/"
            "get_workouts/get_profile die Frage nicht abdecken. Jeder Nicht-SELECT-"
            "Befehl wird abgelehnt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "Ein einzelnes SELECT-Statement.",
                }
            },
            "required": ["sql"],
        },
    },
    {
        "name": "get_profile",
        "description": "Liest das komplette Nutzerprofil (Ziele, Gewicht, Präferenzen) aus.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "update_profile",
        "description": "Setzt/aktualisiert einen einzelnen Profil-Key (z.B. Ziel, Gewicht, Präferenz).",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "log_meal",
        "description": (
            "Speichert eine Mahlzeit (typischerweise aus einer Foto-Analyse) inkl. "
            "geschätzter Makros in der Datenbank (Quelle 'chat')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Kurzbeschreibung des Gerichts, z.B. 'Hähnchen mit Reis und Brokkoli'.",
                },
                "calories_kcal": {"type": "number", "description": "Geschätzte Kalorien in kcal."},
                "protein_g": {"type": "number", "description": "Geschätztes Protein in Gramm."},
                "carbs_g": {"type": "number", "description": "Geschätzte Kohlenhydrate in Gramm."},
                "fat_g": {"type": "number", "description": "Geschätztes Fett in Gramm."},
                "ts": {
                    "type": "string",
                    "description": "Zeitstempel ISO-Format. Weglassen = jetzt (lokale Zeit).",
                },
                "notes": {
                    "type": "string",
                    "description": "Optionale Notiz (z.B. Portionsgröße, Einschätzung).",
                },
            },
            "required": ["description", "calories_kcal", "protein_g", "carbs_g", "fat_g"],
        },
    },
    {
        "name": "get_meals",
        "description": (
            "Liefert alle geloggten Mahlzeiten der letzten N Tage inkl. Tages-Summen "
            "(kcal, Protein, Carbs, Fett pro Tag)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Anzahl der Tage rückwirkend (default 7).",
                }
            },
            "required": ["days"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Speichert einen dauerhaft relevanten Fakt über Manuel im Langzeit-"
            "Gedächtnis (z.B. Job/Alltag, Verletzungen, Vorlieben, Gewohnheiten, "
            "Ziele, wichtige Lebensumstände). Kurz und faktisch, keine Duplikate "
            "zu bereits bekannten Memories."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Der Fakt, kurz und faktisch formuliert.",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Kategorie, z.B. 'person', 'gesundheit', 'vorlieben', "
                        "'arbeit', 'ziele'."
                    ),
                },
            },
            "required": ["content", "category"],
        },
    },
    {
        "name": "search_memories",
        "description": (
            "Durchsucht das Langzeit-Gedächtnis über Manuel (LIKE-Suche über den "
            "Inhalt, optional nach Kategorie gefiltert). Ohne query: alle Memories "
            "(max. 50, neueste zuerst)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Suchbegriff (Teilstring, case-insensitive). Weglassen = alle.",
                },
                "category": {
                    "type": "string",
                    "description": "Optionaler Kategorie-Filter.",
                },
            },
        },
    },
    {
        "name": "get_calendar",
        "description": (
            "Liest Manuels Kalender (read-only, ggf. mehrere Google-Kalender) für "
            "die nächsten N Tage inkl. wiederkehrender Termine. Jedes Event hat "
            "ein 'calendar'-Feld mit dem Kalendernamen. Nutze das, um Gym-Slots "
            "um Arbeit/Termine herum vorzuschlagen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Anzahl der Tage voraus (default 3).",
                }
            },
        },
    },
    {
        "name": "search_notes",
        "description": (
            "Durchsucht Manuels persönliche Notizen (Obsidian-Vault, read-only) "
            "per Volltextsuche. Hilfreich, um Manuel besser zu verstehen oder "
            "Fragen zu seinen Notizen zu beantworten. Max. 10 Treffer mit Ausschnitt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Suchbegriff (case-insensitive).",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_note",
        "description": (
            "Liest eine einzelne Notiz aus dem Obsidian-Vault vollständig (max. "
            "8000 Zeichen), z.B. eine über search_notes gefundene Datei."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Relativer Dateipfad innerhalb des Vaults.",
                }
            },
            "required": ["file"],
        },
    },
    {
        "name": "merge_exercises",
        "description": (
            "Fasst zwei Übungsnamen zusammen, z.B. wenn dieselbe Übung durch "
            "App-Wechsel unterschiedlich heißt (Strong vs. Hevy). Danach fällt "
            "`from_name` im Trainings-Verlauf und im Fortschritts-Graph unter "
            "`into_name`. Nutzen, wenn Manuel eine automatische Gruppierung "
            "korrigieren will oder zwei Übungen manuell zusammenlegen möchte."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_name": {
                    "type": "string",
                    "description": "Der Übungsname, der zusammengefasst werden soll.",
                },
                "into_name": {
                    "type": "string",
                    "description": "Der Ziel-Übungsname (kanonisch), unter dem beide fortan erscheinen.",
                },
            },
            "required": ["from_name", "into_name"],
        },
    },
    {
        "name": "sync_hevy_now",
        "description": (
            "Synchronisiert sofort die neuesten Workouts aus Hevy (nicht die "
            "komplette Historie). Nutzen, wenn Manuel sagt, er hat gerade in "
            "Hevy trainiert und die Daten sollen sofort ins System."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_hevy_exercises",
        "description": (
            "Durchsucht den gecachten Hevy-Übungskatalog per Fuzzy-Suche "
            "(Teilstring, normalisiert) und liefert max. 15 Treffer mit "
            "{id, title, primary_muscle}. Nutzen, um vor create_hevy_routine "
            "zu prüfen, welche Übungen es in Hevy gibt bzw. wie sie heißen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Suchbegriff, z.B. 'bench' oder 'kniebeuge'.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "create_hevy_routine",
        "description": (
            "Erstellt eine neue Trainings-Routine direkt in Manuels Hevy-App. "
            "Jede angegebene Übung wird per Fuzzy-Match gegen den Hevy-"
            "Übungskatalog aufgelöst; nicht eindeutig zuordenbare Übungen "
            "werden als 'unmatched' zurückgegeben statt geraten — dann bei "
            "Manuel nachfragen, statt selbst zu entscheiden. Schreibt live "
            "in Hevy, also nur nach klarer Absicht/Bestätigung von Manuel "
            "aufrufen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Name der Routine."},
                "exercises": {
                    "type": "array",
                    "description": "Liste der Übungen in der Routine.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Übungsname (wird gegen Hevy-Katalog gematcht).",
                            },
                            "sets": {"type": "integer", "description": "Anzahl Sätze."},
                            "reps": {"type": "integer", "description": "Ziel-Wiederholungen pro Satz."},
                            "rest_seconds": {
                                "type": "integer",
                                "description": "Optionale Satzpause in Sekunden.",
                            },
                        },
                        "required": ["name", "sets"],
                    },
                },
            },
            "required": ["title", "exercises"],
        },
    },
]

TOOL_FUNCTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_health_summary": get_health_summary,
    "get_workouts": get_workouts,
    "log_workout": log_workout,
    "query_db": query_db,
    "get_profile": get_profile,
    "update_profile": update_profile,
    "log_meal": log_meal,
    "get_meals": get_meals,
    "save_memory": save_memory,
    "search_memories": search_memories,
    "get_calendar": get_calendar,
    "search_notes": search_notes,
    "read_note": read_note,
    "merge_exercises": merge_exercises,
    "sync_hevy_now": sync_hevy_now,
    "search_hevy_exercises": search_hevy_exercises,
    "create_hevy_routine": create_hevy_routine,
}


# ---------------------------------------------------------------------------
# Dev-Task-Tools (Selbst-Erweiterung, nur für den Assistant — siehe agents.py)
# ---------------------------------------------------------------------------

from trainer.agent.dev_tasks import DEV_TOOL_FUNCTIONS, DEV_TOOL_SCHEMAS  # noqa: E402

TOOL_SCHEMAS.extend(DEV_TOOL_SCHEMAS)
TOOL_FUNCTIONS.update(DEV_TOOL_FUNCTIONS)
