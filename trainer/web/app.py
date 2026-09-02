"""FastAPI-App für das Web-Dashboard ("Agent Hub").

Bietet eine schlanke REST-API für Chat (delegiert an `trainer.agent.core.run_agent`
im Thread-Pool, da der Anthropic-Call blockierend ist) sowie einen
Health-Overview-Endpoint, der Oura-/Workout-/Meal-Daten fürs Dashboard
aufbereitet (Rohdaten pro `date` über die verschiedenen `oura_daily.kind`-Zeilen
zusammengeführt). Läuft ausschließlich lokal (127.0.0.1) für Manuel als
Single User — bewusst keine Auth, kein CORS für fremde Origins.

Start (nur lokal binden!):
    uv run uvicorn trainer.web.app:app --host 127.0.0.1 --port 8090
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trainer.agent.core import run_agent
from trainer.agent.tools import get_calendar, get_meals, get_workouts
from trainer.agents import AGENTS
from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.exercise_norm import canonicalize

app = FastAPI(title="Agent Hub")

# Prozess-Einstiegspunkt: Schema + Migrationen einmal beim Import (uvicorn
# importiert das Modul genau einmal pro Worker), nicht pro Request.
init_db()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Erzwingt Revalidierung für index.html/app.js/style.css.

    Ohne explizite Cache-Control-Header greift Chromes Heuristik-Caching auf
    Basis von Last-Modified — nach einem Deploy zeigt der Browser dann
    stillschweigend die alte Version, bis manuell hart neu geladen wird.
    """
    response = await call_next(request)
    if request.url.path in ("/", "/app.js", "/style.css", "/index.html"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


class ChatMessage(BaseModel):
    message: str


def _round(value: Any, ndigits: int = 1) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return value


@app.get("/api/agents")
def list_agents() -> list[dict[str, str]]:
    """Verfügbare Agenten aus der Registry (aktuell nur isa)."""
    return [{"name": a.name, "display_name": a.display_name} for a in AGENTS.values()]


@app.get("/api/chat/{agent}/history")
def chat_history(agent: str, limit: int = 50) -> list[dict[str, Any]]:
    """Letzte `limit` Nachrichten des Agenten, chronologisch (älteste zuerst)."""
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unbekannter Agent: {agent}")

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT role, content, ts FROM messages
            WHERE role IN ('user', 'assistant') AND agent = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    finally:
        conn.close()

    ordered = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"], "ts": r["ts"]} for r in ordered]


@app.post("/api/chat/{agent}")
async def chat(agent: str, body: ChatMessage) -> JSONResponse:
    """Schickt eine Nachricht an den Agenten (echter Anthropic-Call) und liefert die Antwort.

    Läuft im Thread-Pool, da `run_agent` synchron/blockierend ist. Fehler werden
    als {"error": str} mit Status 500 zurückgegeben — niemals ein Traceback an
    den Client.
    """
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unbekannter Agent: {agent}")

    try:
        reply = await asyncio.to_thread(run_agent, body.message, agent=agent)
    except Exception as exc:  # Fehler abfangen statt Traceback nach außen zu geben
        return JSONResponse(status_code=500, content={"error": str(exc)})

    return JSONResponse(content={"reply": reply})


@app.get("/api/health/overview")
def health_overview(days: int = 30) -> dict[str, Any]:
    """Aufbereitete Health-/Trainings-/Ernährungsdaten fürs Dashboard.

    - daily: letzte `days` Tage, EIN Eintrag pro Kalendertag (auch ohne Daten,
      dann NULL-Felder — die Sparklines im Frontend lassen dafür Lücken statt
      zu crashen). Scores kommen aus den 'sleep'/'readiness'/'activity'-Zeilen
      von oura_daily, HRV/Ruhepuls/Schlafdauer aus 'sleep_detail'.
    - workouts_per_week: letzte 8 Kalenderwochen (Montag-Start), auch leere
      Wochen mit count=0.
    - meals_daily: letzte 14 Tage, Protein/Kalorien summiert (fehlende Tage = 0,
      da "kein Eintrag" hier faktisch "nichts geloggt" bedeutet, kein Messfehler).
    - today: neuester Tag MIT Health-Daten (nicht zwingend der Kalendertag, falls
      der Oura-Sync für heute noch nicht gelaufen ist).
    """
    conn = get_connection()
    try:
        today = date.today()
        cutoff = (today - timedelta(days=days - 1)).isoformat()

        rows = conn.execute(
            """
            SELECT
                date,
                MAX(CASE WHEN kind = 'sleep' THEN sleep_score END) AS sleep_score,
                MAX(CASE WHEN kind = 'readiness' THEN readiness_score END) AS readiness_score,
                MAX(CASE WHEN kind = 'activity' THEN activity_score END) AS activity_score,
                MAX(CASE WHEN kind = 'activity' THEN steps END) AS steps,
                MAX(CASE WHEN kind = 'sleep_detail' THEN hrv_avg END) AS hrv_avg,
                MAX(CASE WHEN kind = 'sleep_detail' THEN resting_hr END) AS resting_hr,
                MAX(CASE WHEN kind = 'sleep_detail' THEN sleep_duration_min END) AS sleep_duration_min
            FROM oura_daily
            WHERE date >= ?
            GROUP BY date
            ORDER BY date
            """,
            (cutoff,),
        ).fetchall()
        by_date = {r["date"]: dict(r) for r in rows}

        daily: list[dict[str, Any]] = []
        for i in range(days):
            d = (today - timedelta(days=days - 1 - i)).isoformat()
            entry = by_date.get(d, {})
            daily.append(
                {
                    "date": d,
                    "sleep_score": entry.get("sleep_score"),
                    "readiness_score": entry.get("readiness_score"),
                    "activity_score": entry.get("activity_score"),
                    "hrv_avg": _round(entry.get("hrv_avg")),
                    "resting_hr": _round(entry.get("resting_hr")),
                    "sleep_duration_min": entry.get("sleep_duration_min"),
                    "steps": entry.get("steps"),
                }
            )

        # --- workouts_per_week: letzte 8 Wochen (Montag-Start), auch leer ---
        current_monday = today - timedelta(days=today.weekday())
        week_starts = [current_monday - timedelta(weeks=n) for n in range(7, -1, -1)]
        week_cutoff = week_starts[0].isoformat()
        workout_rows = conn.execute(
            "SELECT date FROM workouts WHERE date IS NOT NULL AND date >= ?",
            (week_cutoff,),
        ).fetchall()
        week_counts = {ws.isoformat(): 0 for ws in week_starts}
        for wr in workout_rows:
            raw = wr["date"]
            if not raw:
                continue
            try:
                d = date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
            monday = (d - timedelta(days=d.weekday())).isoformat()
            if monday in week_counts:
                week_counts[monday] += 1
        workouts_per_week = [
            {"week": ws.isoformat(), "count": week_counts[ws.isoformat()]} for ws in week_starts
        ]

        # --- meals_daily: letzte 14 Tage ---
        meal_cutoff = (today - timedelta(days=13)).isoformat()
        meal_rows = conn.execute(
            """
            SELECT substr(ts, 1, 10) AS d,
                   SUM(protein_g) AS protein_g,
                   SUM(calories_kcal) AS calories_kcal
            FROM meals
            WHERE ts IS NOT NULL AND substr(ts, 1, 10) >= ?
            GROUP BY d
            """,
            (meal_cutoff,),
        ).fetchall()
        meals_by_date = {r["d"]: r for r in meal_rows}
        meals_daily: list[dict[str, Any]] = []
        for i in range(14):
            d = (today - timedelta(days=13 - i)).isoformat()
            r = meals_by_date.get(d)
            meals_daily.append(
                {
                    "date": d,
                    "protein_g": _round(r["protein_g"]) if r and r["protein_g"] is not None else 0,
                    "calories_kcal": (
                        _round(r["calories_kcal"], 0) if r and r["calories_kcal"] is not None else 0
                    ),
                }
            )

        # --- today: neuester Tag MIT Daten ---
        today_data: dict[str, Any] = {
            "sleep_score": None,
            "readiness_score": None,
            "activity_score": None,
            "hrv_avg": None,
            "resting_hr": None,
            "sleep_duration_min": None,
            "steps": None,
        }
        today_fields = (
            "sleep_score",
            "readiness_score",
            "activity_score",
            "hrv_avg",
            "resting_hr",
            "sleep_duration_min",
            "steps",
        )
        for entry in reversed(daily):
            if any(entry[k] is not None for k in today_fields):
                today_data = {k: entry[k] for k in today_fields}
                break

        return {
            "daily": daily,
            "workouts_per_week": workouts_per_week,
            "meals_daily": meals_daily,
            "today": today_data,
        }
    finally:
        conn.close()


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    """Verdichteter Überblick fürs neue Dashboard (Hub-Startseite).

    Bündelt Health-Readouts (neuester Tag mit Daten), den 30-Tage-HRV-Verlauf
    fürs Puls-Hero, die nächsten Kalender-Termine, die letzten Workouts, die
    heutige Ernährungs-Summe sowie ein paar System-Kennzahlen (letzter
    Oura-Sync, DB-Größe). Fehler in Teilbereichen (Kalender nicht erreichbar
    o.ä.) dürfen den restlichen Überblick nicht blockieren.
    """
    ov = health_overview(days=30)
    daily = ov["daily"]
    today_data = ov["today"]
    hrv_series_30d = [{"date": d["date"], "hrv_avg": d["hrv_avg"]} for d in daily]

    try:
        next_events = (get_calendar(2).get("events") or [])[:5]
    except Exception:
        next_events = []

    try:
        last_workouts = (get_workouts(14).get("workouts") or [])[:3]
    except Exception:
        last_workouts = []

    conn = get_connection()
    try:
        today_iso = date.today().isoformat()
        meal_row = conn.execute(
            """
            SELECT COUNT(*) AS n, SUM(protein_g) AS protein_g, SUM(calories_kcal) AS calories_kcal
            FROM meals
            WHERE substr(ts, 1, 10) = ?
            """,
            (today_iso,),
        ).fetchone()
        meals_today = {
            "protein_g": (
                _round(meal_row["protein_g"]) if meal_row and meal_row["protein_g"] is not None else 0
            ),
            "calories_kcal": (
                _round(meal_row["calories_kcal"], 0)
                if meal_row and meal_row["calories_kcal"] is not None
                else 0
            ),
            "count": meal_row["n"] if meal_row and meal_row["n"] is not None else 0,
        }

        week_cutoff = (date.today() - timedelta(days=6)).isoformat()
        avg_row = conn.execute(
            """
            SELECT
                COUNT(*) AS days_logged,
                AVG(calories_kcal) AS calories_kcal,
                AVG(protein_g) AS protein_g,
                AVG(carbs_g) AS carbs_g,
                AVG(fat_g) AS fat_g
            FROM (
                SELECT substr(ts, 1, 10) AS d,
                       SUM(calories_kcal) AS calories_kcal,
                       SUM(protein_g) AS protein_g,
                       SUM(carbs_g) AS carbs_g,
                       SUM(fat_g) AS fat_g
                FROM meals
                WHERE ts IS NOT NULL AND substr(ts, 1, 10) >= ?
                GROUP BY d
            )
            """,
            (week_cutoff,),
        ).fetchone()
        meals_7d_avg = {
            "calories_kcal": _round(avg_row["calories_kcal"], 0) if avg_row else None,
            "protein_g": _round(avg_row["protein_g"]) if avg_row else None,
            "carbs_g": _round(avg_row["carbs_g"]) if avg_row else None,
            "fat_g": _round(avg_row["fat_g"]) if avg_row else None,
            "days_logged": avg_row["days_logged"] if avg_row and avg_row["days_logged"] else 0,
        }

        sync_row = conn.execute(
            "SELECT value FROM sync_state WHERE key = 'oura_last_sync'"
        ).fetchone()
    finally:
        conn.close()

    oura_last_sync: str | None = None
    if sync_row and sync_row["value"]:
        try:
            # sync_state speichert einen Unix-Timestamp als String (str(time.time())).
            oura_last_sync = datetime.fromtimestamp(float(sync_row["value"])).astimezone().isoformat()
        except (TypeError, ValueError):
            oura_last_sync = None

    try:
        db_size_mb = _round(config.db_path.stat().st_size / (1024 * 1024), 2)
    except OSError:
        db_size_mb = None

    return {
        "today": today_data,
        "hrv_series_30d": hrv_series_30d,
        "next_events": next_events,
        "last_workouts": last_workouts,
        "meals_today": meals_today,
        "meals_7d_avg": meals_7d_avg,
        "system": {
            "oura_last_sync": oura_last_sync,
            "db_size_mb": db_size_mb,
            "agents": list(AGENTS.keys()),
        },
    }


@app.get("/api/workouts")
def workouts(
    days: int = 60,
    types: list[str] | None = Query(None, alias="type"),
    exercise: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """Workouts der letzten `days` Tage, optional gefiltert.

    `type` (mehrfach angebbar, exakter Match gegen `workouts.type`),
    `exercise` (kanonischer Name, wie von `/api/exercises` geliefert — matcht,
    wenn irgendein Satz des Workouts auf diesen Namen canonicalized),
    `q` (Freitext, case-insensitive Substring gegen type/notes/Übungsnamen).
    Filter laufen als Python-Post-Filter über `get_workouts(days)`, das
    Frontend holt bewusst einmal ein großes Zeitfenster und filtert danach
    clientseitig weiter — die Backend-Filter existieren trotzdem eigenständig
    nutzbar (z.B. für curl-Tests).

    `facets.types` listet alle im `days`-Fenster vorkommenden `workouts.type`-
    Werte, UNGEFILTERT, damit das Frontend Filter-Chips bauen kann, ohne dass
    ein aktiver Filter die restlichen Optionen verschwinden lässt.
    """
    result = get_workouts(days)
    all_workouts = result.get("workouts") or []

    facet_types = sorted({w["type"] for w in all_workouts if w.get("type")})

    filtered = all_workouts
    if types:
        wanted = {t.strip() for t in types if t.strip()}
        filtered = [w for w in filtered if (w.get("type") or "").strip() in wanted]

    if exercise:
        conn = get_connection()
        try:
            name_rows = conn.execute(
                "SELECT DISTINCT exercise FROM workout_sets WHERE exercise IS NOT NULL"
            ).fetchall()
            alias_rows = conn.execute("SELECT alias, canonical FROM exercise_aliases").fetchall()
        finally:
            conn.close()
        all_names = [r["exercise"] for r in name_rows]
        alias_map = {r["alias"]: r["canonical"] for r in alias_rows}
        filtered = [
            w
            for w in filtered
            if any(
                canonicalize(s.get("exercise") or "", all_names, alias_map) == exercise
                for s in (w.get("sets") or [])
            )
        ]

    if q and q.strip():
        needle = q.strip().lower()
        filtered = [
            w
            for w in filtered
            if needle in (w.get("type") or "").lower()
            or needle in (w.get("notes") or "").lower()
            or any(needle in (s.get("exercise") or "").lower() for s in (w.get("sets") or []))
        ]

    return {
        **result,
        "workouts": filtered,
        "workout_count": len(filtered),
        "facets": {"types": facet_types},
    }


@app.get("/api/meals")
def meals(days: int = 30) -> dict[str, Any]:
    """Mahlzeiten der letzten `days` Tage (Durchreiche von get_meals)."""
    return get_meals(days)


def _grouped_exercise_points(
    conn: Any,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Gruppiert alle Sätze über `canonicalize` und liefert pro kanonischer Übung
    die chronologische Punkte-Liste (ein Punkt pro Workout-Datum: schwerster Satz)
    sowie die dominante Trainings-Kategorie (workouts.type).

    "Schwerster Satz" = höchstes weight_kg, bei Gleichstand die meisten reps.
    est_1rm nach Epley: weight * (1 + reps/30). `set_count` je Punkt zählt alle
    Sätze dieser Übung am jeweiligen Tag (nicht nur den schwersten).

    Kategorie = Modus von workouts.type über alle Sessions (Workouts), die die
    Übung enthalten — pro Session (workout_id) genau ein Vote, nicht pro Satz,
    damit ein Workout mit vielen Sätzen die Kategorie nicht überstimmt. Leerer/
    NULL type zählt als "Sonstige".
    """
    rows = conn.execute(
        """
        SELECT w.id AS workout_id, w.date AS date, w.type AS type,
               s.exercise AS exercise, s.weight_kg AS weight_kg, s.reps AS reps
        FROM workout_sets s
        JOIN workouts w ON s.workout_id = w.id
        WHERE s.exercise IS NOT NULL AND w.date IS NOT NULL AND s.weight_kg IS NOT NULL
        ORDER BY w.date
        """
    ).fetchall()

    alias_rows = conn.execute("SELECT alias, canonical FROM exercise_aliases").fetchall()
    alias_map = {r["alias"]: r["canonical"] for r in alias_rows}

    all_names = [r["exercise"] for r in rows]
    canon_cache: dict[str, str] = {}

    # canonical -> date -> (weight_kg, reps, set_count) — schwerster Satz + Satzzahl je Tag
    per_group_per_date: dict[str, dict[str, tuple[float, int | None, int]]] = {}
    # canonical -> workout_id -> type — ein Vote pro Session, nicht pro Satz
    category_sessions: dict[str, dict[int, str | None]] = {}

    for r in rows:
        raw = r["exercise"]
        canon = canon_cache.get(raw)
        if canon is None:
            canon = canonicalize(raw, all_names, alias_map)
            canon_cache[raw] = canon

        d = str(r["date"])[:10]
        w = float(r["weight_kg"])
        reps = r["reps"]

        per_date = per_group_per_date.setdefault(canon, {})
        current = per_date.get(d)
        if current is None:
            per_date[d] = (w, reps, 1)
        elif w > current[0] or (w == current[0] and (reps or 0) > (current[1] or 0)):
            per_date[d] = (w, reps, current[2] + 1)
        else:
            per_date[d] = (current[0], current[1], current[2] + 1)

        category_sessions.setdefault(canon, {})[r["workout_id"]] = r["type"]

    result: dict[str, list[dict[str, Any]]] = {}
    for canon, per_date in per_group_per_date.items():
        points: list[dict[str, Any]] = []
        for d in sorted(per_date.keys()):
            w, reps, set_count = per_date[d]
            est_1rm = round(w * (1 + (reps or 0) / 30), 1)
            points.append(
                {
                    "date": d,
                    "top_weight_kg": _round(w),
                    "top_reps": reps,
                    "est_1rm": est_1rm,
                    "set_count": set_count,
                }
            )
        result[canon] = points

    categories: dict[str, str] = {}
    for canon, sessions in category_sessions.items():
        counts = Counter(
            (t.strip() if t and t.strip() else "Sonstige") for t in sessions.values()
        )
        categories[canon] = counts.most_common(1)[0][0]

    return result, categories


@app.get("/api/exercises")
def list_exercises() -> list[dict[str, Any]]:
    """Kanonische Übungen mit Session-Anzahl (Workout-Tage), letztem Gewicht und
    dominanter Trainings-Kategorie (workouts.type), sortiert nach Häufigkeit
    (sessions DESC). Namensvarianten (Strong vs. Hevy) fallen dank `canonicalize`
    zu einer Zeile zusammen."""
    conn = get_connection()
    try:
        grouped, categories = _grouped_exercise_points(conn)
    finally:
        conn.close()

    items = []
    for name, points in grouped.items():
        # PR = Datum des ERSTEN Erreichens des Maximalgewichts (nicht der
        # letzten Wiederholung), damit ein "Neuer PR"-Badge den echten
        # Fortschrittsmoment zeigt statt eines zufälligen späteren Tages mit
        # gleichem Gewicht.
        pr_weight_kg: float | None = None
        pr_date: str | None = None
        pr_est_1rm: float | None = None
        running_max = float("-inf")
        for p in points:
            w = p["top_weight_kg"]
            if w is not None and w > running_max:
                running_max = w
                pr_weight_kg, pr_date, pr_est_1rm = w, p["date"], p["est_1rm"]

        items.append(
            {
                "name": name,
                "sessions": len(points),
                "last_weight_kg": points[-1]["top_weight_kg"] if points else None,
                "category": categories.get(name, "Sonstige"),
                "pr_weight_kg": pr_weight_kg,
                "pr_date": pr_date,
                "pr_est_1rm": pr_est_1rm,
            }
        )
    items.sort(key=lambda x: x["sessions"], reverse=True)
    return items


@app.get("/api/exercise/progress")
def exercise_progress(name: str) -> dict[str, Any]:
    """Gewichts-Verlauf einer kanonischen Übung: pro Workout-Datum der schwerste Satz
    inkl. Satzzahl (chronologisch), Varianten via `canonicalize` gemergt. Leere Liste
    bei unbekannter Übung."""
    conn = get_connection()
    try:
        grouped, _categories = _grouped_exercise_points(conn)
    finally:
        conn.close()

    return {"name": name, "points": grouped.get(name, [])}


@app.get("/api/training/volume")
def training_volume(weeks: int = 12) -> dict[str, Any]:
    """Trainingsvolumen (Summe weight_kg × reps) pro Kalenderwoche (Montag-Start),
    auch leere Wochen mit 0 — gleiches Zero-Fill-Pattern wie `workouts_per_week`
    in `health_overview`, nur über `workout_sets` statt `workouts` aggregiert."""
    conn = get_connection()
    try:
        today = date.today()
        current_monday = today - timedelta(days=today.weekday())
        week_starts = [current_monday - timedelta(weeks=n) for n in range(weeks - 1, -1, -1)]
        cutoff = week_starts[0].isoformat()

        rows = conn.execute(
            """
            SELECT w.date AS date, w.id AS workout_id, s.weight_kg AS weight_kg, s.reps AS reps
            FROM workout_sets s
            JOIN workouts w ON s.workout_id = w.id
            WHERE w.date IS NOT NULL AND w.date >= ?
              AND s.weight_kg IS NOT NULL AND s.reps IS NOT NULL
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    buckets: dict[str, dict[str, Any]] = {
        ws.isoformat(): {"volume_kg": 0.0, "set_count": 0, "workout_ids": set()}
        for ws in week_starts
    }
    for r in rows:
        try:
            d = date.fromisoformat(str(r["date"])[:10])
        except ValueError:
            continue
        monday = (d - timedelta(days=d.weekday())).isoformat()
        bucket = buckets.get(monday)
        if bucket is None:
            continue
        bucket["volume_kg"] += float(r["weight_kg"]) * float(r["reps"])
        bucket["set_count"] += 1
        bucket["workout_ids"].add(r["workout_id"])

    volume_per_week = [
        {
            "week": ws.isoformat(),
            "volume_kg": _round(buckets[ws.isoformat()]["volume_kg"], 0),
            "set_count": buckets[ws.isoformat()]["set_count"],
            "workout_count": len(buckets[ws.isoformat()]["workout_ids"]),
        }
        for ws in week_starts
    ]
    return {"weeks": weeks, "volume_per_week": volume_per_week}


# Statische Dateien (index.html, style.css, app.js) unter / — MUSS nach den
# /api/*-Routen registriert werden, sonst würde der Mount sie verdecken.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
