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

import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

import anthropic

from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.jobs.notify import send_telegram

MAX_TOKENS = 1200
PREV_WEEKS = 4

SYSTEM_PROMPT = """Du bist "Isa", Manuels persönlicher Fitness-Trainer & Health-Coach.

Ton: Direkt, motivierend, Kumpel-Ton (du duzt Manuel). Wissenschaftlich fundiert,
aber keine Vorlesung. Du schreibst für Telegram: kurze Absätze, KEINE
Markdown-Tabellen, sparsame Emojis (höchstens vereinzelt).

Du bekommst unten einen Fakten-Block mit Kennzahlen der aktuellen Woche im
Vergleich zum Durchschnitt der letzten 4 Wochen. Formuliere daraus einen
kompakten Wochenreport mit drei Teilen:
1. Kurzer Wochenüberblick (Schlaf, Recovery, Aktivität, Training, Ernährung)
2. Was ist auffällig — Trends, Verbesserungen oder Verschlechterungen im
   Vergleich zu den Vorwochen (Ups & Downs klar benennen)
3. 1-2 konkrete, umsetzbare Änderungsempfehlungen für nächste Woche

Nutze NUR die gegebenen Zahlen, erfinde nichts dazu. Wenn ein Wert fehlt
(None/nicht vorhanden), erwähne das nicht explizit als Datenlücke, lass den
Punkt einfach weg statt darüber zu lamentieren."""

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


def generate_report_text(facts_block: str) -> str:
    """Genau EIN Anthropic-API-Call, KEINE Tools — formuliert Isas Wochenreport."""
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    response = client.messages.create(
        model=config.trainer_model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": facts_block}],
    )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


def _persist_message(text: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO messages (ts, role, content) VALUES (?, 'assistant', ?)",
            (datetime.now(timezone.utc).isoformat(), text),
        )
        conn.commit()
    finally:
        conn.close()


def run(reference: date | None = None) -> None:
    """Aggregiert, formuliert und verschickt den Wochenreport für die Woche um `reference`."""
    init_db()
    today = reference or date.today()
    monday, sunday = week_bounds(today)
    prev_start, prev_end = previous_period_bounds(monday)

    conn = get_connection()
    try:
        current = aggregate_period(conn, monday, sunday)
        previous = aggregate_period(conn, prev_start, prev_end)
    finally:
        conn.close()

    if not has_oura_data(current):
        send_telegram(NO_DATA_MESSAGE)
        _persist_message(NO_DATA_MESSAGE)
        print("Keine Oura-Daten für die aktuelle Woche — Hinweis-Nachricht gesendet.")
        return

    facts_block = build_facts_block(current, previous, monday, sunday)
    report_text = generate_report_text(facts_block)
    send_telegram(report_text)
    _persist_message(report_text)
    print("Weekly Report gesendet und in messages gespeichert.")


if __name__ == "__main__":
    run()
