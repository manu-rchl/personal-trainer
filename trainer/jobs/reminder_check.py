"""Täglicher Gym-Reminder-Check für Isa.

Usage:
    uv run python -m trainer.jobs.reminder_check

Rein deterministisch (Template-Text, KEIN Anthropic-API-Call) — der Job läuft
täglich per launchd und soll nichts kosten.

Logik:
    0. Hevy-Sync anstoßen, damit das heutige Workout gezählt wird (der
       nächtliche Sync um 23:00 kam bisher systematisch einen Tag zu spät).
    1. Schon heute eine Reminder-Nachricht verschickt? -> nichts tun (Dedupe
       über sync_state["last_reminder_date"]).
    2. Schon heute trainiert (echtes Workout mit date=heute)? -> nichts tun.
    3. Wochenziel (profile["gym_goal_per_week"], Default 3) mit den echten
       Trainingstagen dieser Woche (Mo..heute, COUNT DISTINCT date aus
       workouts) abgleichen.
    4. Nur senden, wenn es zum Wochenziel eng wird: verbleibende
       Trainingstage der Woche (heute eingeschlossen) <= noch offene
       Workouts. Sonst: noch entspannt, kein Reminder.

Der gesendete Text wird in `messages` persistiert (mit synthetischem
User-Turn), damit Isa im Chat weiß, dass sie erinnert hat.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from trainer.agent.core import persist_exchange
from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.ingest import hevy as hevy_ingest
from trainer.jobs.agent_job import run_agent_job
from trainer.jobs.notify import run_job, send_telegram

logger = logging.getLogger(__name__)

DEFAULT_GYM_GOAL = 3
GYM_GOAL_KEY = "gym_goal_per_week"
LAST_REMINDER_KEY = "last_reminder_date"
SYNTHETIC_USER_TURN = "[System: täglicher Reminder-Check 16:30]"

REMINDER_INSTRUCTION = """[System: Gym-Reminder 16:30] Stand: {done}/{goal} Workouts diese Woche, noch {days} Tag(e) inkl. heute — es wird eng, heute wurde noch nicht trainiert.
Prüfe get_calendar(days=1) und die Memories: Ist Manuel unterwegs (Stuttgart/Reise), krank oder ist ein Ruhetag sinnvoll (get_health_summary 2)? Dann schlag statt Gym etwas Passendes vor (Home-Workout, Mobility) oder antworte NO_MESSAGE, wenn ein Reminder heute keinen Sinn hat.
Sonst: erinnere ihn kurz und konkret (2–4 Zeilen), am besten mit dem Trainingstag, der laut Plan dran ist, und dem Slot nach 17:30. Kein Vorwurf.
Zur Orientierung, so hätte der alte Template-Text gelautet: "{fallback}\""""

# 3 Varianten in Isas Ton, Auswahl nach Wochentag-Index (deterministisch,
# damit nicht jeden Tag dieselbe Formulierung kommt).
TEMPLATES = [
    "Kurzer Reality-Check: {done}/{goal} Workouts diese Woche geschafft, und "
    "dir bleiben noch {days} Tag(e) für {remaining} Einheit(en). Heute ist "
    "ein guter Tag dafür 💪",
    "Die Woche wird knapp: {done} von {goal} Workouts sind im Kasten, "
    "{remaining} fehlen noch bei {days} Tag(en) Zeit. Lass uns das heute "
    "angehen.",
    "Kleiner Schubs von mir: {remaining} Workout(s) fehlen dir noch zum "
    "Wochenziel ({goal}), und die Zeit wird eng ({days} Tag(e) übrig). Heute "
    "wär ein guter Moment dafür.",
]


def _get_profile_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM profile WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _get_sync_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_sync_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def parse_goal(raw: str | None) -> int:
    """Parst das Wochenziel aus profile["gym_goal_per_week"], Fallback bei Fehlern."""
    if raw is None:
        return DEFAULT_GYM_GOAL
    try:
        parsed = int(float(raw))
        return parsed if parsed > 0 else DEFAULT_GYM_GOAL
    except (TypeError, ValueError):
        return DEFAULT_GYM_GOAL


def should_remind(done: int, goal: int, today: date, trained_today: bool) -> bool:
    """Reine Entscheidungslogik (testbar ohne DB/Telegram).

    Erinnert nur, wenn heute nicht trainiert wurde, das Ziel offen ist und
    die verbleibenden Tage der Woche (inkl. heute) nicht mehr ausreichen, um
    noch einen Tag Puffer zu haben.
    """
    if trained_today:
        return False
    remaining_goal = goal - done
    if remaining_goal <= 0:
        return False
    remaining_days = 7 - today.weekday()  # Montag -> 7, Sonntag -> 1
    return remaining_days <= remaining_goal


def build_reminder_text(done: int, goal: int, remaining: int, days: int, weekday: int) -> str:
    template = TEMPLATES[weekday % len(TEMPLATES)]
    return template.format(done=done, goal=goal, remaining=remaining, days=days)


def _sync_hevy_best_effort() -> None:
    if not config.hevy_api_key:
        return
    try:
        result = hevy_ingest.sync(full=False)
        logger.info("Hevy-Sync vor Reminder: %s", result)
    except Exception:  # Sync-Fehler darf den Reminder nicht verhindern
        logger.exception("Hevy-Sync vor Reminder fehlgeschlagen — zähle mit vorhandenen Daten")


def run(today: date | None = None) -> None:
    today = today or date.today()
    today_iso = today.isoformat()

    _sync_hevy_best_effort()

    conn = get_connection()
    try:
        if _get_sync_state(conn, LAST_REMINDER_KEY) == today_iso:
            logger.info("Heute bereits eine Reminder-Nachricht verschickt — nichts zu tun.")
            return

        trained_today = (
            conn.execute(
                "SELECT COUNT(*) AS n FROM workouts WHERE date = ?", (today_iso,)
            ).fetchone()["n"]
            > 0
        )
        goal = parse_goal(_get_profile_value(conn, GYM_GOAL_KEY))

        monday = today - timedelta(days=today.weekday())
        workouts_done = conn.execute(
            "SELECT COUNT(DISTINCT date) AS n FROM workouts WHERE date BETWEEN ? AND ?",
            (monday.isoformat(), today_iso),
        ).fetchone()["n"]

        if not should_remind(workouts_done, goal, today, trained_today):
            logger.info(
                "Kein Reminder nötig (heute trainiert=%s, %d/%d, Wochentag %d).",
                trained_today,
                workouts_done,
                goal,
                today.weekday(),
            )
            return

        fallback = build_reminder_text(
            done=workouts_done,
            goal=goal,
            remaining=goal - workouts_done,
            days=7 - today.weekday(),
            weekday=today.weekday(),
        )
        # Ab jetzt (heute) nicht nochmal — auch wenn der Agent NO_MESSAGE sagt.
        _set_sync_state(conn, LAST_REMINDER_KEY, today_iso)
    finally:
        conn.close()

    instruction = REMINDER_INSTRUCTION.format(
        done=workouts_done, goal=goal, days=7 - today.weekday(), fallback=fallback
    )
    try:
        sent = run_agent_job("reminder", instruction)
    except Exception:
        # Agent nicht verfügbar -> deterministischer Text, wie früher.
        logger.exception("Agent-Reminder fehlgeschlagen, sende Template-Text")
        send_telegram(fallback)
        persist_exchange(SYNTHETIC_USER_TURN, fallback, agent="isa")
        sent = fallback
    logger.info("Reminder: %s", "gesendet" if sent else "vom Agenten unterdrückt (NO_MESSAGE)")


if __name__ == "__main__":
    init_db()
    run_job("reminder-check", run)
