"""FastAPI-App für das Web-Dashboard ("Agent Hub").

Bietet eine schlanke REST-API für Chat (delegiert an `trainer.agent.core.run_agent`
im Thread-Pool, da der Anthropic-Call blockierend ist) sowie einen
Health-Overview-Endpoint, der Oura-/Workout-/Meal-Daten fürs Dashboard
aufbereitet (Rohdaten pro `date` über die verschiedenen `oura_daily.kind`-Zeilen
zusammengeführt). Single-User, bindet lokal (127.0.0.1).

Sicherheit: Alle `/api/*`-Routen verlangen `Authorization: Bearer
<WEB_AUTH_TOKEN>` — der Web-Chat ist ein voller Agent mit Schreib-Tools
(Hevy, Obsidian, Memories), und der Health-Overview enthält Gesundheitsdaten.
Ohne Token wäre das für jeden lokalen Prozess und per DNS-Rebinding für jede
Webseite erreichbar. Zusätzlich wird der Host-Header geprüft.

Start:
    uv run uvicorn trainer.web.app:app --host 127.0.0.1 --port 8090
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trainer import analytics
from trainer.agent.core import run_agent
from trainer.agent.tools import get_calendar, get_meals, get_workouts
from trainer.agents import AGENTS
from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.logging_setup import configure_logging

logger = logging.getLogger(__name__)

CHAT_TIMEOUT_S = 240
HISTORY_LIMIT_MAX = 200
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})

app = FastAPI(title="Agent Hub")

# Prozess-Einstiegspunkt: Logging + Schema/Migrationen einmal beim Import
# (uvicorn importiert das Modul genau einmal pro Worker), nicht pro Request.
configure_logging()
init_db()
if not config.web_auth_token:
    raise RuntimeError(
        "WEB_AUTH_TOKEN ist nicht gesetzt (siehe .env.example) — ohne Token startet "
        "das Web-Dashboard nicht, weil der Chat-Endpoint ein voller Agent mit "
        "Schreib-Tools ist."
    )

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _host_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    return host in ALLOWED_HOSTS


@app.middleware("http")
async def _auth_and_headers(request, call_next):
    """Bearer-Auth für /api/*, Host-Check, Cache-Control für die Statics."""
    if not _host_allowed(request.headers.get("host")):
        return JSONResponse(status_code=421, content={"error": "Host nicht erlaubt."})

    if request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, config.web_auth_token):
            return JSONResponse(status_code=401, content={"error": "Token fehlt oder falsch."})

    response = await call_next(request)
    # Ohne explizite Cache-Control-Header greift Chromes Heuristik-Caching auf
    # Basis von Last-Modified — nach einem Deploy zeigt der Browser dann
    # stillschweigend die alte Version, bis manuell hart neu geladen wird.
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
    limit = max(1, min(limit, HISTORY_LIMIT_MAX))

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
    als {"error": str} zurückgegeben (Frontend erwartet dieses Feld) — niemals
    ein Traceback an den Client. Hartes Timeout, damit ein hängender Turn den
    Browser nicht ewig warten lässt.
    """
    if agent not in AGENTS:
        return JSONResponse(status_code=404, content={"error": f"Unbekannter Agent: {agent}"})

    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(run_agent, body.message, agent=agent), timeout=CHAT_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.error("Web-Chat Timeout nach %ss", CHAT_TIMEOUT_S)
        return JSONResponse(
            status_code=504,
            content={"error": f"Isa hat nach {CHAT_TIMEOUT_S}s nicht geantwortet — nochmal versuchen."},
        )
    except Exception as exc:  # Fehler abfangen statt Traceback nach außen zu geben
        logger.exception("Web-Chat fehlgeschlagen")
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
        workouts_per_week = analytics.workouts_per_week(conn, weeks=8, today=today)

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
            canon_map = analytics.build_canon_map(conn)
        finally:
            conn.close()
        filtered = [
            w
            for w in filtered
            if any(
                canon_map.get(s.get("exercise") or "", s.get("exercise")) == exercise
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


@app.get("/api/exercises")
def list_exercises() -> list[dict[str, Any]]:
    """Kanonische Übungen mit Session-Anzahl, PR (effektive Last), Load-Modus,
    Plateau-Flag und Zielgewicht — alles aus `trainer.analytics`."""
    conn = get_connection()
    try:
        return analytics.exercise_summaries(conn)
    finally:
        conn.close()


@app.get("/api/exercise/progress")
def exercise_progress(name: str) -> dict[str, Any]:
    """Verlauf einer kanonischen Übung: pro Session der schwerste Satz, effektive
    Last, e1RM (auf effektiver Last), Satzzahl. Leere Liste bei unbekannter Übung."""
    conn = get_connection()
    try:
        points, _categories, metas = analytics.exercise_points(conn)
        target = analytics.get_target(conn, name)
    finally:
        conn.close()
    meta = metas.get(name) or {}
    return {
        "name": name,
        "load_mode": meta.get("load_mode"),
        "target_weight_kg": target["target_weight_kg"] if target else None,
        "points": points.get(name, []),
    }


@app.get("/api/weight")
def weight(weeks: int = 12) -> dict[str, Any]:
    """Körpergewicht: Rohwerte + Wochenschnitte + Ziel-Abgleich (aus profile)."""
    conn = get_connection()
    try:
        profile = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile")}
        goal = float(profile["goal_weight_kg"]) if profile.get("goal_weight_kg") else None
        deadline = date.fromisoformat(profile["goal_deadline"]) if profile.get("goal_deadline") else None
        trend = analytics.weight_trend(conn, weeks=weeks, goal_kg=goal, deadline=deadline)
        cutoff = analytics.week_buckets(weeks)[0].isoformat()
        entries = [
            {"date": r["date"], "weight_kg": r["weight_kg"]}
            for r in conn.execute(
                "SELECT date, weight_kg FROM body_weight WHERE date >= ? ORDER BY date", (cutoff,)
            )
        ]
    finally:
        conn.close()
    return {**trend, "entries": entries}


@app.get("/api/training/volume")
def training_volume(weeks: int = 12) -> dict[str, Any]:
    """Effektives Trainingsvolumen pro Kalenderwoche (Montag-Start), Zero-Fill."""
    conn = get_connection()
    try:
        return {"weeks": weeks, "volume_per_week": analytics.weekly_volume(conn, weeks=weeks)}
    finally:
        conn.close()


# Statische Dateien (index.html, style.css, app.js) unter / — MUSS nach den
# /api/*-Routen registriert werden, sonst würde der Mount sie verdecken.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
