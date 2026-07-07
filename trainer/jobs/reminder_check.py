"""Täglicher Gym-Reminder-Check für Isa.

Usage:
    uv run python -m trainer.jobs.reminder_check

Rein deterministisch (Template-Text, KEIN Anthropic-API-Call) — der Job läuft
täglich per launchd und soll nichts kosten.

Logik:
    1. Schon heute eine Reminder-Nachricht verschickt? -> nichts tun (Dedupe
       über sync_state["last_reminder_date"]).
    2. Schon heute trainiert (echtes Workout mit date=heute)? -> nichts tun.
    3. Wochenziel (profile["gym_goal_per_week"], Default 3) mit den echten
       Trainingstagen dieser Woche (Mo..heute, COUNT DISTINCT date aus
       workouts) abgleichen.
    4. Nur senden, wenn es zum Wochenziel eng wird: verbleibende
       Trainingstage der Woche (heute eingeschlossen) <= noch offene
       Workouts. Sonst: noch entspannt, kein Reminder.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from trainer.db import get_connection, init_db
from trainer.jobs.notify import send_telegram

DEFAULT_GYM_GOAL = 3
GYM_GOAL_KEY = "gym_goal_per_week"
LAST_REMINDER_KEY = "last_reminder_date"

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


def build_reminder_text(done: int, goal: int, remaining: int, days: int, weekday: int) -> str:
    template = TEMPLATES[weekday % len(TEMPLATES)]
    return template.format(done=done, goal=goal, remaining=remaining, days=days)


def run(today: date | None = None) -> None:
    init_db()
    today = today or date.today()
    today_iso = today.isoformat()

    conn = get_connection()
    try:
        if _get_sync_state(conn, LAST_REMINDER_KEY) == today_iso:
            print("Heute bereits eine Reminder-Nachricht verschickt — nichts zu tun.")
            return

        trained_today = conn.execute(
            "SELECT COUNT(*) AS n FROM workouts WHERE date = ?", (today_iso,)
        ).fetchone()["n"]
        if trained_today > 0:
            print("Heute schon trainiert — kein Reminder nötig.")
            return

        goal = parse_goal(_get_profile_value(conn, GYM_GOAL_KEY))

        monday = today - timedelta(days=today.weekday())
        workouts_done = conn.execute(
            "SELECT COUNT(DISTINCT date) AS n FROM workouts WHERE date BETWEEN ? AND ?",
            (monday.isoformat(), today_iso),
        ).fetchone()["n"]

        remaining_goal = goal - workouts_done
        if remaining_goal <= 0:
            print(
                f"Wochenziel bereits erreicht ({workouts_done}/{goal}) — "
                "kein Reminder nötig."
            )
            return

        # Resttage der Woche inkl. heute: Montag (weekday=0) -> 7, Sonntag (weekday=6) -> 1.
        remaining_days = 7 - today.weekday()

        if remaining_days > remaining_goal:
            print(
                f"Noch entspannt: {workouts_done}/{goal} Workouts, {remaining_days} "
                f"Tag(e) für {remaining_goal} verbleibende Einheit(en) — kein "
                "Reminder nötig."
            )
            return

        text = build_reminder_text(
            done=workouts_done,
            goal=goal,
            remaining=remaining_goal,
            days=remaining_days,
            weekday=today.weekday(),
        )
        send_telegram(text)
        _set_sync_state(conn, LAST_REMINDER_KEY, today_iso)
        print(f"Reminder gesendet: {text}")
    finally:
        conn.close()


if __name__ == "__main__":
    run()
