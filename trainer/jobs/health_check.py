"""Täglicher System-Health-Check (deterministisch, kein API-Call).

Usage:
    uv run python -m trainer.jobs.health_check [--dry-run]

Prüft, ob die Datenpipeline und der Bot leben, und schickt NUR bei
Auffälligkeiten eine ⚠️-Nachricht. Hintergrund (Audit 2026-09): Das System
war drei Wochen komplett aus, ohne dass es jemand gemerkt hat — Oura-Sync,
Hevy-Sync und Bot sind einfach stehen geblieben.

Geprüft wird:
- oura_last_sync / hevy_last_sync älter als MAX_SYNC_AGE
- Oura-Access-Token läuft in < TOKEN_WARN_DAYS ab (Refresh sollte das fangen,
  aber wenn der Refresh-Token tot ist, will man es VOR dem Ausfall wissen)
- bot_heartbeat älter als MAX_HEARTBEAT_AGE (Bot schreibt alle 5 Minuten)
- letztes Workout älter als WORKOUT_GAP_DAYS (Info, kein Systemfehler — aber
  genau das, was ein Trainer bemerken würde)
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from trainer.db import get_connection, init_db
from trainer.jobs.notify import run_job, send_telegram

logger = logging.getLogger(__name__)

MAX_SYNC_AGE = timedelta(hours=36)
MAX_HEARTBEAT_AGE = timedelta(minutes=15)
TOKEN_WARN_DAYS = 3
WORKOUT_GAP_DAYS = 10


def _parse_epoch(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_text(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.1f} Tage"


def evaluate(state: dict[str, Any], now: datetime) -> list[str]:
    """Reine Bewertungslogik über einem Zustands-Dict (testbar ohne DB).

    Erwartete Keys (alle optional): oura_last_sync, hevy_last_sync (Epoch als
    String), oura_token_expires_at (Epoch als String), bot_heartbeat (ISO),
    last_workout_date (YYYY-MM-DD).
    """
    problems: list[str] = []

    for key, label in (("oura_last_sync", "Oura-Sync"), ("hevy_last_sync", "Hevy-Sync")):
        ts = _parse_epoch(state.get(key))
        if ts is None:
            problems.append(f"{label}: noch nie gelaufen")
        elif now - ts > MAX_SYNC_AGE:
            problems.append(f"{label}: letzter Lauf vor {_age_text(now - ts)}")

    expires = _parse_epoch(state.get("oura_token_expires_at"))
    if expires is not None and expires - now < timedelta(days=TOKEN_WARN_DAYS):
        if expires < now:
            problems.append("Oura-Token: abgelaufen (Refresh beim nächsten Sync nötig)")
        else:
            problems.append(f"Oura-Token: läuft in {_age_text(expires - now)} ab")

    hb = _parse_iso(state.get("bot_heartbeat"))
    if hb is None:
        problems.append("Bot: kein Heartbeat vorhanden (läuft er?)")
    elif now - hb > MAX_HEARTBEAT_AGE:
        problems.append(f"Bot: letzter Heartbeat vor {_age_text(now - hb)}")

    last_workout = state.get("last_workout_date")
    if last_workout:
        try:
            gap = (now.date() - date.fromisoformat(str(last_workout)[:10])).days
        except ValueError:
            gap = None
        if gap is not None and gap >= WORKOUT_GAP_DAYS:
            problems.append(f"Training: letztes Workout vor {gap} Tagen")

    return problems


def collect_state() -> dict[str, Any]:
    conn = get_connection()
    try:
        state: dict[str, Any] = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM sync_state "
                "WHERE key IN ('oura_last_sync', 'hevy_last_sync', 'bot_heartbeat')"
            ).fetchall()
        }
        row = conn.execute(
            "SELECT value FROM secrets WHERE key = 'oura_token_expires_at'"
        ).fetchone()
        if row:
            state["oura_token_expires_at"] = row["value"]
        row = conn.execute("SELECT MAX(date) AS d FROM workouts").fetchone()
        if row and row["d"]:
            state["last_workout_date"] = row["d"]
        return state
    finally:
        conn.close()


def run(dry_run: bool = False) -> None:
    now = datetime.now(timezone.utc)
    problems = evaluate(collect_state(), now)
    if not problems:
        logger.info("Health-Check: alles ok.")
        return
    text = "⚠️ Health-Check:\n" + "\n".join(f"- {p}" for p in problems)
    if dry_run:
        print(text)
        return
    send_telegram(text)
    logger.warning("Health-Check meldet: %s", "; ".join(problems))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trainer.jobs.health_check")
    parser.add_argument("--dry-run", action="store_true", help="nur ausgeben, nicht senden")
    args = parser.parse_args()
    init_db()
    run_job("health-check", lambda: run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
