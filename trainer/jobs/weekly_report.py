"""Wöchentlicher Health-Report für Isa.

Usage:
    uv run python -m trainer.jobs.weekly_report

Aggregiert die aktuelle Woche (Mo-So, lokale Zeit) deterministisch per SQL
gegen den Durchschnitt der 4 Vorwochen, lässt Claude daraus EINEN Report in
Isas Ton formulieren und verschickt ihn per Telegram. Der Report wird
zusätzlich in `messages` (role='assistant') gespeichert, damit Isa im Chat
darauf Bezug nehmen kann.

Aggregation (SQL, `aggregate_period`/`build_facts_block`) und
Formulierung/Versand (`generate_report_text`/`run`) sind bewusst getrennte
Funktionen: die Aggregation lässt sich isoliert testen, ohne einen
Anthropic-API-Call auszulösen oder eine Telegram-Nachricht zu verschicken.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

from trainer.agent.core import persist_exchange
from trainer.db import get_connection, init_db
from trainer.jobs.agent_job import run_agent_job
from trainer.jobs.notify import run_job, send_telegram

logger = logging.getLogger(__name__)

PREV_WEEKS = 4
LAST_REPORT_KEY = "last_weekly_report_week"
SYNTHETIC_USER_TURN = "[System: Wochenreport So 18:00]"

NO_DATA_MESSAGE = (
    "Hey, für diese Woche hab ich noch keine Oura-Daten gefunden – kann dir "
    "deshalb keinen Wochenreport bauen. Check kurz, ob der Sync noch läuft "
    "(uv run python -m trainer.ingest.oura sync), dann hol ich das nächste "
    "Woche nach."
)


# ---------------------------------------------------------------------------
# Zeitraum-Helfer
# ---------------------------------------------------------------------------


def week_bounds(reference: date) -> tuple[date, date]:
    """Montag..Sonntag der Woche, die `reference` enthält (lokale Zeit)."""
    monday = reference - timedelta(days=reference.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def previous_period_bounds(monday: date, weeks: int = PREV_WEEKS) -> tuple[date, date]:
    """Zeitraum der `weeks` Wochen unmittelbar vor der aktuellen Woche."""
    prev_start = monday - timedelta(days=7 * weeks)
    prev_end = monday - timedelta(days=1)
    return prev_start, prev_end


# ---------------------------------------------------------------------------
# Deterministische SQL-Aggregation
# ---------------------------------------------------------------------------


def _round(value: Any, ndigits: int = 1) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return value


def _aggregate_oura(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            AVG(CASE WHEN kind = 'sleep' THEN sleep_score END) AS sleep_score_avg,
            AVG(CASE WHEN kind = 'readiness' THEN readiness_score END) AS readiness_score_avg,
            AVG(CASE WHEN kind = 'sleep_detail' THEN hrv_avg END) AS hrv_avg,
            AVG(CASE WHEN kind = 'sleep_detail' THEN resting_hr END) AS resting_hr_avg,
            AVG(CASE WHEN kind = 'sleep_detail' THEN sleep_duration_min END) AS sleep_duration_min_avg,
            AVG(CASE WHEN kind = 'activity' THEN steps END) AS steps_avg,
            COUNT(CASE WHEN kind = 'sleep' THEN 1 END) AS sleep_days_n,
            COUNT(CASE WHEN kind = 'readiness' THEN 1 END) AS readiness_days_n,
            COUNT(CASE WHEN kind = 'sleep_detail' THEN 1 END) AS sleep_detail_days_n,
            COUNT(CASE WHEN kind = 'activity' THEN 1 END) AS activity_days_n
        FROM oura_daily
        WHERE date BETWEEN ? AND ?
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    return dict(row)


def _aggregate_workouts(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS workout_count,
            (
                SELECT COUNT(*) FROM workout_sets ws
                JOIN workouts w2 ON ws.workout_id = w2.id
                WHERE w2.date BETWEEN ? AND ?
            ) AS set_count
        FROM workouts
        WHERE date BETWEEN ? AND ?
        """,
        (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
    ).fetchone()
    return dict(row)


def _aggregate_meals(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS meal_count,
            COUNT(DISTINCT substr(ts, 1, 10)) AS days_with_meals,
            SUM(protein_g) AS protein_g_sum
        FROM meals
        WHERE substr(ts, 1, 10) BETWEEN ? AND ?
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    result = dict(row)
    days = result["days_with_meals"] or 0
    protein_sum = result["protein_g_sum"] or 0.0
    result["protein_g_avg_per_logged_day"] = round(protein_sum / days, 1) if days else None
    return result


def aggregate_period(conn: sqlite3.Connection, start: date, end: date) -> dict[str, Any]:
    """Aggregiert Oura-, Workout- und Mahlzeit-Kennzahlen für [start, end] (inklusive)."""
    data: dict[str, Any] = {}
    data.update(_aggregate_oura(conn, start, end))
    data.update(_aggregate_workouts(conn, start, end))
    data.update(_aggregate_meals(conn, start, end))
    return data


def has_oura_data(current: dict[str, Any]) -> bool:
    """True, wenn für den Zeitraum mindestens ein Oura-Datenpunkt vorliegt."""
    return bool(
        (current.get("sleep_days_n") or 0)
        or (current.get("readiness_days_n") or 0)
        or (current.get("sleep_detail_days_n") or 0)
        or (current.get("activity_days_n") or 0)
    )


def build_facts_block(
    current: dict[str, Any],
    previous: dict[str, Any],
    monday: date,
    sunday: date,
    prev_weeks: int = PREV_WEEKS,
) -> str:
    """Baut einen kompakten, rein deterministischen Fakten-Text für den LLM-Call."""

    def per_week(agg: dict[str, Any], field: str, n_weeks: int) -> float:
        return round((agg.get(field) or 0) / n_weeks, 1)

    def hours(minutes: Any) -> Any:
        if minutes is None:
            return None
        return round(float(minutes) / 60.0, 2)

    lines = [
        f"Aktuelle Woche: {monday.isoformat()} bis {sunday.isoformat()}",
        "",
        "AKTUELLE WOCHE:",
        f"- Ø Sleep Score: {_round(current.get('sleep_score_avg'))} "
        f"({current.get('sleep_days_n') or 0} Tage mit Daten)",
        f"- Ø Readiness Score: {_round(current.get('readiness_score_avg'))} "
        f"({current.get('readiness_days_n') or 0} Tage mit Daten)",
        f"- Ø HRV: {_round(current.get('hrv_avg'))} ms",
        f"- Ø Ruhepuls: {_round(current.get('resting_hr_avg'))} bpm",
        f"- Ø Schlafdauer: {hours(current.get('sleep_duration_min_avg'))} h",
        f"- Ø Schritte/Tag: {_round(current.get('steps_avg'), 0)}",
        f"- Workouts: {current.get('workout_count') or 0} "
        f"(insgesamt {current.get('set_count') or 0} Sätze)",
        f"- Mahlzeiten geloggt: {current.get('meal_count') or 0}",
    ]
    if current.get("protein_g_avg_per_logged_day") is not None:
        lines.append(
            f"- Ø Protein/Tag (an Tagen mit geloggten Mahlzeiten): "
            f"{current['protein_g_avg_per_logged_day']} g"
        )

    lines += [
        "",
        f"DURCHSCHNITT LETZTE {prev_weeks} WOCHEN (Vergleichszeitraum):",
        f"- Ø Sleep Score: {_round(previous.get('sleep_score_avg'))}",
        f"- Ø Readiness Score: {_round(previous.get('readiness_score_avg'))}",
        f"- Ø HRV: {_round(previous.get('hrv_avg'))} ms",
        f"- Ø Ruhepuls: {_round(previous.get('resting_hr_avg'))} bpm",
        f"- Ø Schlafdauer: {hours(previous.get('sleep_duration_min_avg'))} h",
        f"- Ø Schritte/Tag: {_round(previous.get('steps_avg'), 0)}",
        f"- Ø Workouts/Woche: {per_week(previous, 'workout_count', prev_weeks)} "
        f"(Ø {per_week(previous, 'set_count', prev_weeks)} Sätze/Woche)",
    ]
    if previous.get("protein_g_avg_per_logged_day") is not None:
        lines.append(
            f"- Ø Protein/Tag (an Tagen mit geloggten Mahlzeiten): "
            f"{previous['protein_g_avg_per_logged_day']} g"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM-Formulierung + Versand
# ---------------------------------------------------------------------------


REPORT_INSTRUCTION = """[System: Wochenreport {week_key}] Hier die deterministischen Wochenzahlen:

{facts}

Bau daraus Manuels Wochenreport (Telegram-Stil, max. ~20 Zeilen):
1. Wochenüberblick: Schlaf, Recovery, Aktivität, Training, Ernährung — mit Bezug auf die Vorwochen (Ups & Downs klar benennen).
2. Ziel-Tracking: Gewicht Richtung 70 kg (get_profile, ggf. Body-Measurements/Memories), Protein-/Kalorienziel vs. geloggt, Trainingstage vs. Plan (get_training_plan).
3. Kraft: rufe get_exercise_progress für die 3 wichtigsten Übungen der Woche auf (aus get_workouts(days=7)) und nenne e1RM-Trend/Plateaus; get_muscle_frequency(weeks=4) für den Frequenz-Check.
4. Kontext: get_calendar(days=7) — Reisen/Termine nächste Woche berücksichtigen, statt pauschal mehr Training zu fordern.
5. 1–2 konkrete Änderungen für nächste Woche, die in seinen Alltag passen.
Nutze nur echte Zahlen, erfinde nichts; fehlende Werte einfach weglassen."""


def build_report_instruction(facts_block: str, week_key: str) -> str:
    return REPORT_INSTRUCTION.format(facts=facts_block, week_key=week_key)


def _week_key(monday: date) -> str:
    iso_year, iso_week, _ = monday.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def run(reference: date | None = None, force: bool = False, dry_run: bool = False) -> None:
    """Aggregiert, formuliert (Agent-Turn mit Tools) und verschickt den Wochenreport.

    Dedupe über sync_state[LAST_REPORT_KEY] — der Report für dieselbe Woche
    ging früher zweimal raus, wenn der Job doppelt getriggert wurde.
    """
    today = reference or date.today()
    monday, sunday = week_bounds(today)
    prev_start, prev_end = previous_period_bounds(monday)
    week_key = _week_key(monday)

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (LAST_REPORT_KEY,)
        ).fetchone()
        if row and row["value"] == week_key and not force:
            logger.info("Wochenreport %s wurde schon gesendet — nichts zu tun.", week_key)
            return
        current = aggregate_period(conn, monday, sunday)
        previous = aggregate_period(conn, prev_start, prev_end)
    finally:
        conn.close()

    if not has_oura_data(current):
        logger.warning("Keine Oura-Daten für %s — Hinweis-Nachricht statt Report.", week_key)
        if dry_run:
            print(NO_DATA_MESSAGE)
        else:
            send_telegram(NO_DATA_MESSAGE)
            persist_exchange(SYNTHETIC_USER_TURN, NO_DATA_MESSAGE, agent="isa")
    else:
        facts_block = build_facts_block(current, previous, monday, sunday)
        run_agent_job("weekly-report", build_report_instruction(facts_block, week_key), dry_run=dry_run)

    if dry_run:
        return

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LAST_REPORT_KEY, week_key),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Wochenreport %s gesendet und in messages gespeichert.", week_key)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m trainer.jobs.weekly_report")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="auch wenn diese Woche schon gesendet")
    args = parser.parse_args()
    init_db()
    run_job("weekly-report", lambda: run(force=args.force, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
