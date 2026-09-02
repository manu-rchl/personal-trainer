"""Täglicher Morgen-Check-in (08:00): kurz, datengetrieben, darf schweigen.

Usage:
    uv run python -m trainer.jobs.morning_checkin [--dry-run]

Synct vorher Oura (die Nacht ist um 08:00 schon im Ring, der reguläre Sync
läuft erst 10:30), dann ein Agent-Turn mit festem Auftrag.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

from trainer.db import init_db
from trainer.ingest import oura as oura_ingest
from trainer.jobs.agent_job import run_agent_job
from trainer.jobs.notify import run_job

logger = logging.getLogger(__name__)

MORNING_INSTRUCTION = """[System: Morgen-Check-in {today}, {weekday}] Schau kurz auf den Tag:
- get_health_summary(days=2): Schlaf/Readiness der letzten Nacht (fehlen Daten, sag es nicht extra — Ring vielleicht nicht getragen).
- get_calendar(days=1): Termine/Reisen heute.
- get_training_plan + get_workouts(days=7) + get_muscle_frequency(weeks=2): was ist heute dran (welcher Tag des Splits, Erholung seit letztem Training, Wochenziel)?
- Ernährung: er isst chronisch zu wenig und will besonders morgens erinnert werden — aber nicht jeden Tag gleich.
Schreib dann max. 5 Zeilen an Manuel: die eine wichtigste Sache für heute (Training ja/nein/wie hart, oder Essen, oder Recovery), konkret und persönlich. Kein Standard-Guten-Morgen. Wenn es heute wirklich nichts zu sagen gibt (Ruhetag, normale Werte, nichts offen), antworte NO_MESSAGE."""

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _sync_oura_best_effort() -> None:
    try:
        oura_ingest.sync(days=2)
    except Exception:
        logger.exception("Oura-Sync vor Morgen-Check-in fehlgeschlagen — nutze vorhandene Daten")


def run(dry_run: bool = False) -> None:
    today = date.today()
    _sync_oura_best_effort()
    instruction = MORNING_INSTRUCTION.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()])
    run_agent_job("morning-checkin", instruction, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trainer.jobs.morning_checkin")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    init_db()
    run_job("morning-checkin", lambda: run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
