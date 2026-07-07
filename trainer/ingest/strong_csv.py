"""Strong-App CSV-Import.

Usage:
    uv run python -m trainer.ingest.strong_csv <datei.csv>

Strong-Exporte (Settings → Export Strong Data) enthalten typischerweise Spalten
wie Date, Workout Name, Exercise Name, Set Order, Weight, Reps (+ ggf. Duration,
Distance, Seconds, Notes, RPE, ...). Header-Matching ist bewusst flexibel
(case-insensitive, mehrere Aliase), Delimiter wird per csv.Sniffer autodetected
(Strong exportiert i.d.R. mit Komma, manche Locales nutzen Semikolon).

Dedupe: Pro Datenzeile wird ein SHA256-Hash gebildet (Delimiter-gejointe,
geparste Felder) und als sync_state-Key "strong_row_<hash>" abgelegt. Bereits
importierte Zeilen werden beim nächsten Lauf übersprungen — Strong-Exporte sind
volle Historien-Exports, spätere Läufe enthalten also größtenteils bereits
importierte Zeilen plus ein paar neue.
"""

from __future__ import annotations

import csv
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from trainer.db import get_connection, init_db

COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["date"],
    "workout_name": ["workout name", "workout"],
    "exercise": ["exercise name", "exercise"],
    "set_order": ["set order", "set"],
    "weight": ["weight", "weight (kg)", "weight (lbs)", "weight(kg)", "weight(lbs)"],
    "reps": ["reps"],
}

REQUIRED_FIELDS = {"date", "workout_name", "exercise"}


def _detect_dialect(sample: str) -> type[csv.Dialect]:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return csv.excel  # Fallback: Komma


def _build_column_index(header: list[str]) -> dict[str, int]:
    normalized = [h.strip().lower() for h in header]
    index: dict[str, int] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                index[field] = normalized.index(alias)
                break
    return index


def _row_hash(delimiter: str, row: list[str]) -> str:
    raw = delimiter.join(row)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(value: str | None) -> int | None:
    f = _to_float(value)
    return int(f) if f is not None else None


def import_csv(path: Path) -> None:
    init_db()
    conn = get_connection()
    try:
        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        if not lines:
            print("Leere Datei.")
            return

        sample = "\n".join(lines[:5])
        dialect = _detect_dialect(sample)
        delimiter = dialect.delimiter

        reader = csv.reader(lines, dialect=dialect)
        rows = list(reader)
        header, *data_rows = rows

        col = _build_column_index(header)
        missing = REQUIRED_FIELDS - col.keys()
        if missing:
            print(f"FEHLER: Pflichtspalten fehlen im CSV-Header: {sorted(missing)}", file=sys.stderr)
            print(f"Gefundene Spalten: {header}", file=sys.stderr)
            sys.exit(1)

        workout_cache: dict[tuple[str, str], int] = {}
        imported_workouts = 0
        imported_sets = 0
        skipped_rows = 0

        def get(row: list[str], field: str) -> str | None:
            idx = col.get(field)
            if idx is None or idx >= len(row):
                return None
            return row[idx]

        for row in data_rows:
            if not row or all(not c.strip() for c in row):
                continue

            row_hash = _row_hash(delimiter, row)
            state_key = f"strong_row_{row_hash}"
            already = conn.execute(
                "SELECT 1 FROM sync_state WHERE key = ?", (state_key,)
            ).fetchone()
            if already:
                skipped_rows += 1
                continue

            date = (get(row, "date") or "").strip()
            workout_name = (get(row, "workout_name") or "").strip()
            exercise = (get(row, "exercise") or "").strip()

            if not date or not exercise:
                skipped_rows += 1
                continue

            key = (date, workout_name)
            if key not in workout_cache:
                cur = conn.execute(
                    "INSERT INTO workouts (date, type, source, notes) VALUES (?, ?, 'strong_csv', NULL)",
                    (date, workout_name),
                )
                workout_cache[key] = cur.lastrowid
                imported_workouts += 1
            workout_id = workout_cache[key]

            set_no = _to_int(get(row, "set_order"))
            reps = _to_int(get(row, "reps"))
            weight_kg = _to_float(get(row, "weight"))

            conn.execute(
                """
                INSERT INTO workout_sets (workout_id, exercise, set_no, reps, weight_kg)
                VALUES (?, ?, ?, ?, ?)
                """,
                (workout_id, exercise, set_no, reps, weight_kg),
            )
            imported_sets += 1

            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?)",
                (state_key, datetime.now(timezone.utc).isoformat()),
            )

        conn.commit()
        print(f"Workouts importiert: {imported_workouts}")
        print(f"Sätze importiert: {imported_sets}")
        print(f"Zeilen übersprungen (Dedupe/leer/ungültig): {skipped_rows}")
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m trainer.ingest.strong_csv <datei.csv>", file=sys.stderr)
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"Datei nicht gefunden: {path}", file=sys.stderr)
        sys.exit(1)

    import_csv(path)


if __name__ == "__main__":
    main()
