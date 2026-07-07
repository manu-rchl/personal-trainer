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
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from trainer.agent.core import run_agent
from trainer.agents import AGENTS
from trainer.db import get_connection, init_db

app = FastAPI(title="Agent Hub")

STATIC_DIR = Path(__file__).resolve().parent / "static"


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
    """Verfügbare Agenten aus der Registry, in Registrierungsreihenfolge (isa, assistant)."""
    return [{"name": a.name, "display_name": a.display_name} for a in AGENTS.values()]


@app.get("/api/chat/{agent}/history")
def chat_history(agent: str, limit: int = 50) -> list[dict[str, Any]]:
    """Letzte `limit` Nachrichten des Agenten, chronologisch (älteste zuerst)."""
    if agent not in AGENTS:
        raise HTTPException(status_code=404, detail=f"Unbekannter Agent: {agent}")

    init_db()
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
    init_db()
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
            "hrv_avg": None,
            "resting_hr": None,
        }
        for entry in reversed(daily):
            if any(
                entry[k] is not None
                for k in ("sleep_score", "readiness_score", "hrv_avg", "resting_hr")
            ):
                today_data = {
                    "sleep_score": entry["sleep_score"],
                    "readiness_score": entry["readiness_score"],
                    "hrv_avg": entry["hrv_avg"],
                    "resting_hr": entry["resting_hr"],
                }
                break

        return {
            "daily": daily,
            "workouts_per_week": workouts_per_week,
            "meals_daily": meals_daily,
            "today": today_data,
        }
    finally:
        conn.close()


# Statische Dateien (index.html, style.css, app.js) unter / — MUSS nach den
# /api/*-Routen registriert werden, sonst würde der Mount sie verdecken.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
