"""Coach-Tools (Phase 1): Fortschritt, Plan, Zielgewichte, Routinen, Follow-ups.

Ausgelagert aus `trainer.agent.tools` (das ist mit >2000 Zeilen voll). Wird
am Ende von tools.py in TOOL_SCHEMAS/TOOL_FUNCTIONS registriert. Rechnen tut
`trainer.analytics`; hier ist nur die Tool-Hülle + Hevy-Spiegelung.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

from trainer import analytics
from trainer.config import config
from trainer.db import get_connection
from trainer.exercise_norm import normalize_name
from trainer.ingest import hevy as hevy_ingest

logger = logging.getLogger(__name__)

TARGET_NOTE_PREFIX = "Ziel (Isa"
MAX_NOTE_HISTORY_LINES = 1  # so viele alte Notizzeilen bleiben unter der neuen stehen
# Jobs können das im Dry-Run abschalten (post_workout --no-hevy-write).
HEVY_WRITE_ENABLED = True


# ---------------------------------------------------------------------------
# Fortschritt
# ---------------------------------------------------------------------------


def get_exercise_progress(exercise: str, sessions: int = 8) -> dict[str, Any]:
    """Letzte Sessions, Bestwert, Trend, Plateau-Flag und Double-Progression-Hinweis."""
    conn = get_connection()
    try:
        return analytics.exercise_progress(conn, exercise, sessions=max(1, min(sessions, 30)))
    finally:
        conn.close()


def get_muscle_frequency(weeks: int = 4) -> dict[str, Any]:
    """Trainingstage pro Muskelgruppe in den letzten `weeks` Wochen."""
    conn = get_connection()
    try:
        return analytics.muscle_frequency(conn, weeks=max(1, min(weeks, 12)))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Trainingsplan
# ---------------------------------------------------------------------------

_PLAN_FIELDS = (
    "name",
    "split",
    "days_per_week",
    "block_start",
    "block_weeks",
    "progression_rule",
    "deload_rule",
    "notes",
)


def get_training_plan() -> dict[str, Any]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM training_plan WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"error": "Kein aktiver Trainingsplan hinterlegt (set_training_plan)."}
        plan = dict(row)
        if plan.get("block_start") and plan.get("block_weeks"):
            try:
                start = date.fromisoformat(plan["block_start"])
                plan["block_week_number"] = (date.today() - start).days // 7 + 1
                plan["block_weeks_remaining"] = plan["block_weeks"] - plan["block_week_number"] + 1
            except ValueError:
                pass
        return plan
    finally:
        conn.close()


def set_training_plan(**fields: Any) -> dict[str, Any]:
    """Aktualisiert Felder des aktiven Plans (nur übergebene Felder)."""
    updates = {k: v for k, v in fields.items() if k in _PLAN_FIELDS and v is not None}
    if not updates:
        return {"error": f"Keine gültigen Felder. Erlaubt: {', '.join(_PLAN_FIELDS)}"}
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM training_plan WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        now = datetime.now(timezone.utc).isoformat()
        if row:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE training_plan SET {sets}, updated_at = ? WHERE id = ?",
                (*updates.values(), now, row["id"]),
            )
        else:
            cols = ", ".join(updates)
            marks = ", ".join("?" for _ in updates)
            conn.execute(
                f"INSERT INTO training_plan (active, {cols}, updated_at) VALUES (1, {marks}, ?)",
                (*updates.values(), now),
            )
        conn.commit()
        return {"status": "gespeichert", "updated_fields": sorted(updates)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Zielgewichte (+ Spiegelung in Hevy-Routine-Notiz)
# ---------------------------------------------------------------------------


def get_exercise_targets(exercise: str | None = None) -> dict[str, Any]:
    conn = get_connection()
    try:
        if exercise:
            canon = analytics.canonical_name(conn, exercise)
            t = analytics.get_target(conn, canon)
            return {"exercise": canon, "target": t}
        rows = conn.execute("SELECT * FROM exercise_targets ORDER BY updated_at DESC").fetchall()
        return {"target_count": len(rows), "targets": [dict(r) for r in rows]}
    finally:
        conn.close()


def _format_target_note(target_weight_kg: float, rep_min: int, rep_max: int, sets: int, reason: str) -> str:
    today = date.today().strftime("%d.%m.")
    core = f"{TARGET_NOTE_PREFIX}, {today}): {target_weight_kg:g} kg · {sets}×{rep_min}–{rep_max}"
    return f"{core} — {reason.strip()}" if reason and reason.strip() else core


def _merge_note(old: str | None, new_line: str) -> str:
    """Neue Zielzeile oben, darunter max. MAX_NOTE_HISTORY_LINES alte Zeilen
    (alte Isa-Zielzeilen werden ersetzt, damit die Notiz nicht wächst)."""
    old_lines = [ln.strip() for ln in (old or "").splitlines() if ln.strip()]
    kept = [ln for ln in old_lines if not ln.startswith(TARGET_NOTE_PREFIX)][:MAX_NOTE_HISTORY_LINES]
    return "\n".join([new_line, *kept])


def _fetch_routines() -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{hevy_ingest.API_BASE}/v1/routines",
        headers=_headers(),
        params={"page": 1, "pageSize": 10},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    routines = data.get("routines") or data.get("data") or []
    return routines if isinstance(routines, list) else []


def _headers() -> dict[str, str]:
    return {"api-key": config.hevy_api_key, "Content-Type": "application/json"}


def _routine_exercise_matches(ex: dict[str, Any], canon: str, template_id: str | None) -> bool:
    if template_id and ex.get("exercise_template_id") == template_id:
        return True
    return normalize_name(ex.get("title") or "") == normalize_name(canon)


def set_routine_exercise_note(canon: str, template_id: str | None, note_line: str) -> dict[str, Any]:
    """Schreibt `note_line` als oberste Zeile in die Notiz der Übung in allen
    Hevy-Routinen, die sie enthalten. Liefert, welche Routinen geändert wurden."""
    from trainer.agent.tools import (  # lokal wegen Import-Zyklus (tools importiert dieses Modul)
        _hevy_unwrap,
        _strip_hevy_readonly_exercise_fields,
    )

    if not config.hevy_api_key:
        return {"error": "Hevy ist nicht konfiguriert (HEVY_API_KEY fehlt)."}
    if not HEVY_WRITE_ENABLED:
        logger.info("Hevy-Schreiben deaktiviert — Notiz für %s wäre: %s", canon, note_line)
        return {"skipped": "Hevy-Schreiben deaktiviert (Dry-Run)", "note": note_line}
    try:
        routines = _fetch_routines()
    except httpx.HTTPError as exc:
        return {"error": f"Hevy-Routinen nicht abrufbar: {exc}"}

    updated: list[str] = []
    for rt in routines:
        exercises = rt.get("exercises") or []
        hit = False
        for ex in exercises:
            if _routine_exercise_matches(ex, canon, template_id):
                ex["notes"] = _merge_note(ex.get("notes"), note_line)
                hit = True
        if not hit:
            continue
        body = {
            "title": rt.get("title"),
            "exercises": _strip_hevy_readonly_exercise_fields(exercises),
        }
        try:
            resp = httpx.put(
                f"{hevy_ingest.API_BASE}/v1/routines/{rt['id']}",
                headers=_headers(),
                json={"routine": body},
                timeout=30,
            )
            resp.raise_for_status()
            _hevy_unwrap(resp.json(), "routine")
            updated.append(rt.get("title") or rt["id"])
        except httpx.HTTPError as exc:
            logger.warning("Hevy-Notiz-Update für %s in %s fehlgeschlagen: %s", canon, rt.get("title"), exc)
            return {"error": f"Hevy-Update in Routine '{rt.get('title')}' fehlgeschlagen: {exc}", "updated": updated}
    if not updated:
        return {"warning": f"'{canon}' steht in keiner Hevy-Routine — Notiz nicht gespiegelt.", "updated": []}
    return {"updated": updated}


def set_exercise_target(
    exercise: str,
    target_weight_kg: float,
    rep_min: int = 8,
    rep_max: int = 12,
    sets: int = 3,
    reason: str = "",
    mirror_to_hevy: bool = True,
) -> dict[str, Any]:
    """Setzt das Ziel fürs nächste Mal (DB) und spiegelt es in die Hevy-Routine-Notiz."""
    if rep_min > rep_max or rep_min < 1:
        return {"error": "rep_min muss >= 1 und <= rep_max sein."}
    conn = get_connection()
    try:
        canon = analytics.canonical_name(conn, exercise)
        meta = analytics.ensure_exercise_meta(conn, canon)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO exercise_targets (exercise, target_weight_kg, rep_min, rep_max, sets, reason, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'isa', ?) "
            "ON CONFLICT(exercise) DO UPDATE SET target_weight_kg = excluded.target_weight_kg, "
            "rep_min = excluded.rep_min, rep_max = excluded.rep_max, sets = excluded.sets, "
            "reason = excluded.reason, source = excluded.source, updated_at = excluded.updated_at",
            (canon, float(target_weight_kg), int(rep_min), int(rep_max), int(sets), reason, now),
        )
        conn.commit()
    finally:
        conn.close()

    result: dict[str, Any] = {
        "status": "gespeichert",
        "exercise": canon,
        "load_mode": meta["load_mode"],
        "target": {"target_weight_kg": target_weight_kg, "rep_min": rep_min, "rep_max": rep_max, "sets": sets},
        "effective_kg": analytics.effective_load(target_weight_kg, meta["load_mode"]),
    }
    if mirror_to_hevy:
        note = _format_target_note(target_weight_kg, rep_min, rep_max, sets, reason)
        result["hevy"] = set_routine_exercise_note(canon, meta.get("hevy_template_id"), note)
    return result


def set_exercise_load_mode(exercise: str, load_mode: str) -> dict[str, Any]:
    conn = get_connection()
    try:
        canon = analytics.canonical_name(conn, exercise)
        try:
            analytics.set_load_mode(conn, canon, load_mode)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"status": "gespeichert", "exercise": canon, "load_mode": load_mode}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hevy-Routinen lesen
# ---------------------------------------------------------------------------


def get_hevy_routines() -> dict[str, Any]:
    """Alle Hevy-Routinen mit Übungen, Notizen (= Zielvorgaben) und Satzschema."""
    if not config.hevy_api_key:
        return {"error": "Hevy ist nicht konfiguriert (HEVY_API_KEY fehlt)."}
    try:
        routines = _fetch_routines()
    except httpx.HTTPError as exc:
        return {"error": f"Hevy-Routinen nicht abrufbar: {exc}"}
    return {"routine_count": len(routines), "routines": [parse_routine(rt) for rt in routines]}


def parse_routine(rt: dict[str, Any]) -> dict[str, Any]:
    exercises = []
    for ex in rt.get("exercises") or []:
        sets = ex.get("sets") or []
        exercises.append(
            {
                "title": ex.get("title"),
                "template_id": ex.get("exercise_template_id"),
                "notes": ex.get("notes") or "",
                "set_count": len(sets),
                "reps_scheme": [s.get("reps") for s in sets],
                "rest_seconds": ex.get("rest_seconds"),
            }
        )
    return {"id": rt.get("id"), "title": rt.get("title"), "updated_at": rt.get("updated_at"), "exercises": exercises}


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


def schedule_checkin(due_date: str, text: str) -> dict[str, Any]:
    """Merkt einen Follow-up vor; der Post-Workout-Job schickt ihn am Fälligkeitstag."""
    try:
        due = date.fromisoformat(due_date)
    except ValueError:
        return {"error": "due_date muss YYYY-MM-DD sein."}
    if due < date.today():
        return {"error": "due_date liegt in der Vergangenheit."}
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO scheduled_checkins (due_date, text, created_ts) VALUES (?, ?, ?)",
            (due.isoformat(), text.strip(), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return {"status": "gespeichert", "checkin_id": cur.lastrowid, "due_date": due.isoformat()}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Körpergewicht
# ---------------------------------------------------------------------------


def log_body_weight(weight_kg: float, day: str | None = None, mirror_to_hevy: bool = True) -> dict[str, Any]:
    """Speichert das Körpergewicht (ein Wert pro Tag, überschreibt) und schreibt es
    best-effort auch als Hevy-Body-Measurement."""
    try:
        w = float(weight_kg)
    except (TypeError, ValueError):
        return {"error": "weight_kg muss eine Zahl sein."}
    if not 30 <= w <= 200:
        return {"error": f"{w} kg wirkt unplausibel — bitte prüfen."}
    d = date.today() if not day else date.fromisoformat(day)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO body_weight (date, weight_kg, source, ts) VALUES (?, ?, 'chat', ?) "
            "ON CONFLICT(date) DO UPDATE SET weight_kg = excluded.weight_kg, ts = excluded.ts",
            (d.isoformat(), w, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO profile (key, value) VALUES ('current_weight_kg', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"{w:g}",),
        )
        conn.commit()
    finally:
        conn.close()
    result: dict[str, Any] = {"status": "gespeichert", "date": d.isoformat(), "weight_kg": w}
    if mirror_to_hevy and config.hevy_api_key and HEVY_WRITE_ENABLED:
        from trainer.agent.tools import log_body_measurement, update_body_measurement

        r = log_body_measurement(d.isoformat(), weight_kg=w)
        if "error" in r and "existiert bereits" in r["error"]:
            r = update_body_measurement(d.isoformat(), weight_kg=w)
        result["hevy"] = r
    return result


def get_weight_trend(weeks: int = 8) -> dict[str, Any]:
    """Wochenschnitte des Körpergewichts, Tempo und Abgleich mit Ziel/Deadline aus dem Profil."""
    conn = get_connection()
    try:
        profile = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile")}
        goal = float(profile["goal_weight_kg"]) if profile.get("goal_weight_kg") else None
        deadline = date.fromisoformat(profile["goal_deadline"]) if profile.get("goal_deadline") else None
        return analytics.weight_trend(conn, weeks=max(2, min(weeks, 52)), goal_kg=goal, deadline=deadline)
    finally:
        conn.close()


def clear_memory_review() -> dict[str, Any]:
    """Markiert den offenen Memory-Review-Vorschlag als erledigt (nach Anwenden/Ablehnen)."""
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM sync_state WHERE key = 'memory_review_pending'")
        conn.commit()
        return {"status": "erledigt" if cur.rowcount else "kein offener Review"}
    finally:
        conn.close()


COACH_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "log_body_weight",
        "description": (
            "Speichert Manuels Körpergewicht (kg) für einen Tag — immer aufrufen, wenn er sein "
            "Gewicht nennt. Aktualisiert profile.current_weight_kg und Hevy-Body-Measurements."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "weight_kg": {"type": "number"},
                "day": {"type": "string", "description": "YYYY-MM-DD, Default heute."},
            },
            "required": ["weight_kg"],
        },
    },
    {
        "name": "get_weight_trend",
        "description": (
            "Körpergewicht: Wochenschnitte, Veränderung, Tempo (kg/Woche) und ob das Ziel bis zur "
            "Deadline erreichbar ist (needed_kg_per_week, on_track). days_since_last sagt, wie alt der letzte Wert ist."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"weeks": {"type": "integer", "description": "Zeitraum, Default 8."}},
        },
    },
    {
        "name": "clear_memory_review",
        "description": "Schließt den offenen Memory-Review-Vorschlag ab, nachdem du ihn angewendet oder Manuel ihn abgelehnt hat.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_exercise_progress",
        "description": (
            "Fortschritt EINER Übung: letzte Sessions (Sätze, Top-Satz, effektive Last, e1RM), "
            "Bestwert, Trend, sessions_since_pr, plateau-Flag, aktuelles Ziel und ein "
            "Double-Progression-Hinweis. IMMER vor einer Gewichtsempfehlung aufrufen. "
            "Rechnet mit der Gewichts-Konvention (load_mode) — effective_top_kg ist die reale Last."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {"type": "string", "description": "Übungsname (Schreibweise egal, wird kanonisiert)."},
                "sessions": {"type": "integer", "description": "Wie viele letzte Sessions (Default 8, max 30)."},
            },
            "required": ["exercise"],
        },
    },
    {
        "name": "get_muscle_frequency",
        "description": "Trainingstage pro Muskelgruppe in den letzten Wochen (Nippard-Check: 2×+/Woche).",
        "input_schema": {
            "type": "object",
            "properties": {"weeks": {"type": "integer", "description": "Zeitraum in Wochen (Default 4)."}},
        },
    },
    {
        "name": "get_training_plan",
        "description": "Aktiver Trainingsplan: Split, Tage/Woche, Block, Progressions- und Deload-Regel.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_training_plan",
        "description": "Ändert Felder des aktiven Trainingsplans (nur übergebene Felder).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "split": {"type": "string", "description": "z.B. 'PPL', 'Upper/Lower', 'Full Body'."},
                "days_per_week": {"type": "integer"},
                "block_start": {"type": "string", "description": "YYYY-MM-DD, Start des aktuellen Blocks."},
                "block_weeks": {"type": "integer"},
                "progression_rule": {"type": "string"},
                "deload_rule": {"type": "string"},
                "notes": {"type": "string"},
            },
        },
    },
    {
        "name": "get_exercise_targets",
        "description": "Zielvorgaben fürs nächste Mal (alle oder für eine Übung).",
        "input_schema": {
            "type": "object",
            "properties": {"exercise": {"type": "string", "description": "Optional: nur diese Übung."}},
        },
    },
    {
        "name": "set_exercise_target",
        "description": (
            "Setzt das Ziel fürs nächste Mal für eine Übung (Gewicht in der GELOGGTEN Konvention, "
            "Rep-Range, Sätze, Begründung) und spiegelt es als oberste Notizzeile in die "
            "Hevy-Routine, damit Manuel es im Training sieht. Zielgewichte IMMER hierüber setzen, "
            "nie nur im Text nennen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {"type": "string"},
                "target_weight_kg": {"type": "number", "description": "In Manuels Logging-Konvention (Langhantel: pro Seite)."},
                "rep_min": {"type": "integer", "description": "Default 8."},
                "rep_max": {"type": "integer", "description": "Default 12."},
                "sets": {"type": "integer", "description": "Default 3."},
                "reason": {"type": "string", "description": "Kurz: warum (z.B. '3×12 sauber → +2,5')."},
                "mirror_to_hevy": {"type": "boolean", "description": "Default true."},
            },
            "required": ["exercise", "target_weight_kg"],
        },
    },
    {
        "name": "set_exercise_load_mode",
        "description": (
            "Korrigiert, wie weight_kg einer Übung zu lesen ist: barbell_per_side (Scheiben einer "
            "Seite, +20 kg Stange), per_hand (pro Kurzhantel), total (Maschine/Kabel)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {"type": "string"},
                "load_mode": {"type": "string", "enum": list(analytics.LOAD_MODES)},
            },
            "required": ["exercise", "load_mode"],
        },
    },
    {
        "name": "get_hevy_routines",
        "description": "Alle Hevy-Routinen (Push/Pull/Legs …) mit Übungen, Notizen und Satzschema.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "schedule_checkin",
        "description": (
            "Setzt dir selbst einen Follow-up: am due_date schickst du Manuel eine Nachricht zu "
            "`text` (z.B. 'Nachfragen, ob Legs in Düsseldorf geklappt hat')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                "text": {"type": "string", "description": "Worum es beim Follow-up geht."},
            },
            "required": ["due_date", "text"],
        },
    },
]

COACH_TOOL_FUNCTIONS: dict[str, Any] = {
    "log_body_weight": log_body_weight,
    "get_weight_trend": get_weight_trend,
    "clear_memory_review": clear_memory_review,
    "get_exercise_progress": get_exercise_progress,
    "get_muscle_frequency": get_muscle_frequency,
    "get_training_plan": get_training_plan,
    "set_training_plan": set_training_plan,
    "get_exercise_targets": get_exercise_targets,
    "set_exercise_target": set_exercise_target,
    "set_exercise_load_mode": set_exercise_load_mode,
    "get_hevy_routines": get_hevy_routines,
    "schedule_checkin": schedule_checkin,
}
