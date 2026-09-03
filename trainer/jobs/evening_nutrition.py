"""Abend-Ernährungsfrage (20:30): nur fragen, wenn heute zu wenig geloggt ist.

Usage:
    uv run python -m trainer.jobs.evening_nutrition [--dry-run]

Manuel isst chronisch zu wenig und loggt fast nie (Audit: 10 Mahlzeiten in
zwei Monaten). Statt Tagesziele zu predigen fragt Isa abends kurz, was er
gegessen hat — er antwortet per Text/Foto/Sprache, Isa loggt (log_meal) und
ordnet ein. Ist heute schon genug geloggt: NO_MESSAGE.
"""

from __future__ import annotations

import argparse
from datetime import date

from trainer.db import get_connection, init_db
from trainer.jobs.agent_job import run_agent_job
from trainer.jobs.notify import run_job

EVENING_INSTRUCTION = """[System: Abend-Ernährungsfrage {today}] Heute bisher geloggt: {meal_count} Mahlzeit(en), {kcal} kcal, {protein} g Protein (Ziel {kcal_target} kcal / {protein_target} g).
- Ist das Tagesziel schon (fast) erreicht oder hat Manuel dir heute schon gesagt, dass er fertig ist mit Essen → NO_MESSAGE.
- Sonst frag ihn in 1–3 Zeilen, was er heute gegessen hat (Foto, Sprache oder Text reicht) — konkret, ohne Vorwurf, ggf. mit dem Hinweis, was für die Lücke noch schnell ginge (Shake, Brot, Nüsse). Wenn er antwortet, loggst du jede Mahlzeit per log_meal.
- War er heute im Training (get_workouts 1), erwähne kurz, dass der Tag mehr braucht."""


def run(dry_run: bool = False) -> None:
    today = date.today().isoformat()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(calories_kcal), 0) AS kcal, COALESCE(SUM(protein_g), 0) AS protein "
            "FROM meals WHERE substr(ts, 1, 10) = ?",
            (today,),
        ).fetchone()
        profile = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile")}
    finally:
        conn.close()
    instruction = EVENING_INSTRUCTION.format(
        today=today,
        meal_count=row["n"],
        kcal=round(row["kcal"]),
        protein=round(row["protein"]),
        kcal_target=profile.get("daily_kcal_target", "2500"),
        protein_target=profile.get("daily_protein_target_g", "140"),
    )
    run_agent_job("evening-nutrition", instruction, dry_run=dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trainer.jobs.evening_nutrition")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    init_db()
    run_job("evening-nutrition", lambda: run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
