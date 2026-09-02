"""Post-Workout-Loop + fällige Follow-ups (stündlich 07–23 Uhr).

Usage:
    uv run python -m trainer.jobs.post_workout
    uv run python -m trainer.jobs.post_workout --dry-run [--workout-id N] [--no-hevy-write]

Das ist DER Baustein, der Isa von "antwortet" zu "kommt von sich aus" macht:
1. Hevy-Sync (neue Workouts landen in der DB).
2. Für jedes Workout ohne `checkin_sent_at` (max. 2 Tage alt) ein Agent-Turn:
   jede Übung mit get_exercise_progress bewerten, Ziel fürs nächste Mal via
   set_exercise_target setzen (→ Hevy-Notiz), Plateaus benennen, kurzes
   Check-in an Manuel.
3. Fällige `scheduled_checkins` als eigene Agent-Turns.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from trainer.agent import coach_tools
from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.ingest import hevy as hevy_ingest
from trainer.jobs.agent_job import run_agent_job
from trainer.jobs.notify import run_job

logger = logging.getLogger(__name__)

MAX_WORKOUT_AGE_DAYS = 2
LAST_RUN_KEY = "post_workout_last_run"

POST_WORKOUT_INSTRUCTION = """[System: Post-Workout-Check-in] Neues Workout #{workout_id} ist synchronisiert: "{type}" am {date} ({set_count} Sätze).
Übungen und Sätze (reps×weight_kg in Manuels Logging-Konvention):
{sets_block}

Dein Auftrag als Trainerin:
1. Rufe für JEDE Übung get_exercise_progress auf und vergleiche mit den letzten Sessions (effektive Last, e1RM, Plateau-Flag, Hinweis).
2. Setze für JEDE Übung das Ziel fürs nächste Mal mit set_exercise_target (Double Progression; bei Plateau bewusst entscheiden: halten, Variante, Deload — und das begründen). Das landet automatisch als Notiz in seiner Hevy-Routine.
3. Wenn eine Übung plateau=true hat und du eine fundierte Strategie brauchst, darfst du EINMAL query_notebooklm nutzen (Notebook 1, Progressive Overload) und das Ergebnis mit source als Memory speichern.
4. Berücksichtige Recovery (get_health_summary 3) und Muskelfrequenz (get_muscle_frequency), falls relevant.
5. Speichere nur wirklich Neues als Memory (Form-Probleme, Schmerzen, Präferenzen) — keine Session-Details.
6. Schreib Manuel dann ein kurzes Check-in (max. ~10 Zeilen, Telegram-Stil): was lief gut, was auffällig war, die konkreten Ziele fürs nächste Mal, ggf. eine Frage zu Form/Gefühl. Erwähne, dass die Ziele in Hevy stehen.
Wenn das Workout offensichtlich kein Krafttraining war (z.B. Squash, Spaziergang), reicht ein Zweizeiler ohne Ziele."""

CHECKIN_INSTRUCTION = """[System: Fälliger Follow-up #{checkin_id}, angelegt am {created}] Du hattest dir vorgemerkt: "{text}"
Frag Manuel jetzt kurz und natürlich danach (1–3 Zeilen). Wenn es sich durch den Chatverlauf inzwischen erledigt hat, antworte NO_MESSAGE."""


def _sets_block(conn: sqlite3.Connection, workout_id: int) -> tuple[str, int]:
    rows = conn.execute(
        "SELECT exercise, reps, weight_kg FROM workout_sets WHERE workout_id = ? ORDER BY exercise, set_no",
        (workout_id,),
    ).fetchall()
    by_ex: dict[str, list[str]] = {}
    for r in rows:
        w = f"{r['weight_kg']:g}" if r["weight_kg"] is not None else "BW"
        by_ex.setdefault(r["exercise"] or "?", []).append(f"{r['reps']}×{w}")
    lines = [f"- {ex}: {', '.join(sets)}" for ex, sets in by_ex.items()]
    return ("\n".join(lines) or "- (keine Sätze)"), len(rows)


def pending_workouts(conn: sqlite3.Connection, today: date, workout_id: int | None = None) -> list[dict[str, Any]]:
    if workout_id is not None:
        rows = conn.execute("SELECT id, date, type FROM workouts WHERE id = ?", (workout_id,)).fetchall()
    else:
        cutoff = (today - timedelta(days=MAX_WORKOUT_AGE_DAYS)).isoformat()
        rows = conn.execute(
            "SELECT id, date, type FROM workouts WHERE checkin_sent_at IS NULL AND date >= ? ORDER BY date, id",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def _mark_checkin(conn: sqlite3.Connection, workout_id: int) -> None:
    conn.execute(
        "UPDATE workouts SET checkin_sent_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), workout_id),
    )
    conn.commit()


def _sync_hevy_best_effort() -> None:
    if not config.hevy_api_key:
        return
    try:
        logger.info("Hevy-Sync: %s", hevy_ingest.sync(full=False))
    except Exception:
        logger.exception("Hevy-Sync fehlgeschlagen — arbeite mit vorhandenen Daten")


def run(dry_run: bool = False, workout_id: int | None = None, hevy_write: bool = True) -> None:
    coach_tools.HEVY_WRITE_ENABLED = hevy_write
    today = date.today()
    if workout_id is None:
        _sync_hevy_best_effort()

    conn = get_connection()
    try:
        workouts = pending_workouts(conn, today, workout_id)
        for w in workouts:
            sets_block, set_count = _sets_block(conn, w["id"])
            instruction = POST_WORKOUT_INSTRUCTION.format(
                workout_id=w["id"], type=w["type"] or "Workout", date=w["date"],
                set_count=set_count, sets_block=sets_block,
            )
            run_agent_job(f"post-workout #{w['id']}", instruction, dry_run=dry_run)
            if not dry_run:
                _mark_checkin(conn, w["id"])

        if workout_id is None:
            due = conn.execute(
                "SELECT id, due_date, text, created_ts FROM scheduled_checkins "
                "WHERE sent_at IS NULL AND due_date <= ? ORDER BY due_date, id",
                (today.isoformat(),),
            ).fetchall()
            for c in due:
                instruction = CHECKIN_INSTRUCTION.format(
                    checkin_id=c["id"], created=(c["created_ts"] or "")[:10], text=c["text"]
                )
                run_agent_job(f"checkin #{c['id']}", instruction, dry_run=dry_run)
                if not dry_run:
                    conn.execute(
                        "UPDATE scheduled_checkins SET sent_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), c["id"]),
                    )
                    conn.commit()

        if not dry_run:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (LAST_RUN_KEY, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        logger.info("Post-Workout: %d Workout(s) bearbeitet.", len(workouts))
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trainer.jobs.post_workout")
    parser.add_argument("--dry-run", action="store_true", help="Antwort nur ausgeben, nichts senden/markieren")
    parser.add_argument("--workout-id", type=int, help="Nur dieses Workout (auch wenn schon bearbeitet)")
    parser.add_argument("--no-hevy-write", action="store_true", help="Zielnotizen NICHT nach Hevy schreiben")
    args = parser.parse_args()
    init_db()
    run_job(
        "post-workout",
        lambda: run(dry_run=args.dry_run, workout_id=args.workout_id, hevy_write=not args.no_hevy_write),
    )


if __name__ == "__main__":
    main()
