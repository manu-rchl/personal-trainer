"""FastAPI-Webhook für Apple Health (via Health Auto Export App).

Start:
    uv run uvicorn trainer.ingest.webhook:app --host 0.0.0.0 --port 8080

Health Auto Export sendet POST-Requests mit folgendem JSON-Format:
    {
      "data": {
        "metrics": [
          {"name": "heart_rate", "units": "bpm",
           "data": [{"date": "2026-07-01 08:00:00 +0200", "qty": 62.0}, ...]},
          ...
        ],
        "workouts": [
          {"name": "Functional Strength Training", "start": "...", "end": "...", ...},
          ...
        ]
      }
    }

Wir sind tolerant gegenüber fehlenden/unerwarteten Feldern: einzelne kaputte
Einträge werden übersprungen und gezählt, niemals führt das zu einem 500.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from trainer.config import config
from trainer.db import get_connection, init_db


@asynccontextmanager
async def _lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Personal Trainer — Health Webhook", lifespan=_lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _check_auth(authorization: str | None) -> None:
    expected = config.health_webhook_secret
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Invalid token")


def _import_metrics(conn, metrics: list[Any]) -> tuple[int, int]:
    imported = 0
    skipped = 0

    if not isinstance(metrics, list):
        return imported, skipped

    for metric in metrics:
        try:
            name = metric.get("name")
            unit = metric.get("units")
            points = metric.get("data", [])
            if not name or not isinstance(points, list):
                skipped += 1
                continue

            for point in points:
                try:
                    ts = point.get("date")
                    qty = point.get("qty")
                    if ts is None or qty is None:
                        skipped += 1
                        continue
                    conn.execute(
                        """
                        INSERT INTO health_metrics (source, metric, ts, value, unit)
                        VALUES ('apple_health', ?, ?, ?, ?)
                        ON CONFLICT(source, metric, ts) DO UPDATE SET
                            value = excluded.value,
                            unit = excluded.unit
                        """,
                        (name, str(ts), float(qty), unit),
                    )
                    imported += 1
                except Exception:
                    skipped += 1
        except Exception:
            skipped += 1

    return imported, skipped


def _import_workouts(conn, workouts: list[Any]) -> tuple[int, int]:
    imported = 0
    skipped = 0

    if not isinstance(workouts, list):
        return imported, skipped

    for workout in workouts:
        try:
            if not isinstance(workout, dict):
                skipped += 1
                continue
            date = (
                workout.get("start")
                or workout.get("date")
                or workout.get("startDate")
            )
            wtype = workout.get("name") or workout.get("workoutActivityType") or "unknown"
            notes = json.dumps(workout)
            # Health Auto Export sendet überlappende Zeitfenster — gleiche
            # Workouts kommen mehrfach an, daher Dedupe über (date, type).
            exists = conn.execute(
                "SELECT 1 FROM workouts WHERE source = 'apple_health' AND date = ? AND type = ? LIMIT 1",
                (date, wtype),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO workouts (date, type, source, notes) VALUES (?, ?, 'apple_health', ?)",
                (date, wtype, notes),
            )
            imported += 1
        except Exception:
            skipped += 1

    return imported, skipped


@app.post("/health-export")
async def health_export(
    request: Request, authorization: str | None = Header(default=None)
) -> dict:
    _check_auth(authorization)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    data = body.get("data", {}) if isinstance(body, dict) else {}
    metrics = data.get("metrics", []) if isinstance(data, dict) else []
    workouts = data.get("workouts", []) if isinstance(data, dict) else []

    conn = get_connection()
    try:
        m_imported, m_skipped = _import_metrics(conn, metrics)
        w_imported, w_skipped = _import_workouts(conn, workouts)
        conn.commit()
    finally:
        conn.close()

    return {
        "imported": m_imported + w_imported,
        "skipped": m_skipped + w_skipped,
    }
