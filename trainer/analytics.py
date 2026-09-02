"""Trainings-Analytik: Übungsfortschritt, e1RM, Volumen, Wochen-Buckets.

EINE Quelle für Web-Dashboard, Agent-Tools und Jobs — vorher lag dieselbe
Logik dreifach in web/app.py, agent/tools.py und jobs/weekly_report.py, mit
leicht unterschiedlichen Ergebnissen (Übungsfilter fand Übungen nicht, e1RM
rechnete mit Roh-Per-Seite-Werten).

Zentrales Konzept: **Load-Modus.** `workout_sets.weight_kg` ist bei Manuel
NIE das Gesamtgewicht:
- `barbell_per_side`: Scheiben auf EINER Seite ohne Stange -> real = 20 kg + 2w
- `per_hand`: Kurzhantel pro Hand -> real pro Hand = w, Volumen zählt x2
- `total`: Maschine/Kabel -> eingestellter Wert

Alle Funktionen sind reine Funktionen über eine `sqlite3.Connection`
(row_factory=Row) — kein Netzwerk, keine Seiteneffekte außer dem lazy
Befüllen von `exercise_meta`.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from trainer.exercise_norm import canonicalize, normalize_name

LoadMode = Literal["barbell_per_side", "per_hand", "total"]
LOAD_MODES: tuple[str, ...] = ("barbell_per_side", "per_hand", "total")

BAR_KG = 20.0
# Kleinste sinnvolle Steigerung pro Modus (auf dem GELOGGTEN Wert):
# Langhantel +1,25 kg pro Seite (= +2,5 kg real), Kurzhantel +2,5 kg,
# Maschine/Kabel +2,5 kg (eine Platte; Isa darf runden).
INCREMENT_KG: dict[str, float] = {"barbell_per_side": 1.25, "per_hand": 2.5, "total": 2.5}
DEFAULT_REP_RANGE = (8, 12)
PLATEAU_SESSIONS = 3  # Sessions ohne neues e1RM-Hoch -> Plateau


def _round(value: Any, ndigits: int = 1) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# Load-Modus
# ---------------------------------------------------------------------------


def guess_load_mode(name: str, equipment: str | None = None) -> str:
    """Heuristik aus Übungsname (+ Hevy-Equipment, falls bekannt)."""
    n = normalize_name(name)
    eq = (equipment or "").lower()
    if "barbell" in n or "smith" in n or eq in ("barbell", "smith_machine", "smith machine"):
        return "barbell_per_side"
    if "dumbbell" in n or eq == "dumbbell":
        return "per_hand"
    return "total"


def effective_load(weight_kg: float | None, load_mode: str) -> float | None:
    """Reale Last eines Satzes aus dem geloggten Wert."""
    if weight_kg is None:
        return None
    w = float(weight_kg)
    if load_mode == "barbell_per_side":
        return BAR_KG + 2 * w
    return w


def volume_multiplier(load_mode: str) -> int:
    return 2 if load_mode == "per_hand" else 1


def est_1rm(effective_kg: float | None, reps: int | None) -> float | None:
    """Epley auf der EFFEKTIVEN Last."""
    if effective_kg is None:
        return None
    return round(effective_kg * (1 + (reps or 0) / 30), 1)


# ---------------------------------------------------------------------------
# Wochen-Helfer
# ---------------------------------------------------------------------------


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_buckets(n: int, today: date | None = None) -> list[date]:
    """Montage der letzten `n` Kalenderwochen, älteste zuerst, aktuelle Woche zuletzt."""
    today = today or date.today()
    current = week_start(today)
    return [current - timedelta(weeks=k) for k in range(n - 1, -1, -1)]


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Kanonische Namen + Meta
# ---------------------------------------------------------------------------


def load_alias_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["alias"]: r["canonical"]
        for r in conn.execute("SELECT alias, canonical FROM exercise_aliases").fetchall()
    }


def build_canon_map(conn: sqlite3.Connection) -> dict[str, str]:
    """Rohname -> kanonischer Anzeigename, über ALLE Satz-Zeilen gewichtet.

    Gewichtung nach Satzanzahl (nicht DISTINCT), damit Web und Tools denselben
    Anzeigenamen wählen — die Divergenz war der Übungsfilter-Bug.
    """
    rows = conn.execute(
        "SELECT exercise FROM workout_sets WHERE exercise IS NOT NULL AND exercise != ''"
    ).fetchall()
    all_names = [r["exercise"] for r in rows]
    alias_map = load_alias_map(conn)
    canon: dict[str, str] = {}
    for raw in set(all_names):
        canon[raw] = canonicalize(raw, all_names, alias_map)
    return canon


def canonical_name(conn: sqlite3.Connection, name: str) -> str:
    """Kanonischer Name für eine (evtl. anders geschriebene) Übung."""
    cmap = build_canon_map(conn)
    if name in cmap:
        return cmap[name]
    by_norm = {normalize_name(k): v for k, v in cmap.items()}
    n = normalize_name(name)
    if n in by_norm:
        return by_norm[n]
    # kanonische Namen selbst sind gültige Eingaben
    if name in set(cmap.values()):
        return name
    for canon in set(cmap.values()):
        if normalize_name(canon) == n:
            return canon
    return name


def _template_info(conn: sqlite3.Connection, canon: str) -> dict[str, Any]:
    """Hevy-Template zu einem kanonischen Namen (exakter normalisierter Titel)."""
    n = normalize_name(canon)
    for r in conn.execute(
        "SELECT id, title, primary_muscle, equipment FROM hevy_exercise_templates"
    ).fetchall():
        if normalize_name(r["title"] or "") == n:
            return dict(r)
    return {}


def ensure_exercise_meta(conn: sqlite3.Connection, canon: str) -> dict[str, Any]:
    """Liefert die Meta-Zeile einer Übung; legt sie bei Bedarf per Heuristik an."""
    row = conn.execute(
        "SELECT exercise, load_mode, primary_muscle, hevy_template_id FROM exercise_meta "
        "WHERE exercise = ?",
        (canon,),
    ).fetchone()
    if row:
        return dict(row)
    tmpl = _template_info(conn, canon)
    meta = {
        "exercise": canon,
        "load_mode": guess_load_mode(canon, tmpl.get("equipment")),
        "primary_muscle": tmpl.get("primary_muscle"),
        "hevy_template_id": tmpl.get("id"),
    }
    conn.execute(
        "INSERT OR IGNORE INTO exercise_meta (exercise, load_mode, primary_muscle, "
        "hevy_template_id, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            canon,
            meta["load_mode"],
            meta["primary_muscle"],
            meta["hevy_template_id"],
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return meta


def set_load_mode(conn: sqlite3.Connection, canon: str, load_mode: str) -> None:
    if load_mode not in LOAD_MODES:
        raise ValueError(f"Unbekannter load_mode {load_mode!r}, erlaubt: {LOAD_MODES}")
    ensure_exercise_meta(conn, canon)
    conn.execute(
        "UPDATE exercise_meta SET load_mode = ?, updated_at = ? WHERE exercise = ?",
        (load_mode, datetime.now(timezone.utc).isoformat(), canon),
    )
    conn.commit()


def get_target(conn: sqlite3.Connection, canon: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM exercise_targets WHERE exercise = ?", (canon,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Sessions pro Übung
# ---------------------------------------------------------------------------


def _load_sessions(
    conn: sqlite3.Connection, canon_map: dict[str, str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """canon -> chronologische Sessions (ein Eintrag pro Workout) mit allen Sätzen."""
    canon_map = canon_map or build_canon_map(conn)
    rows = conn.execute(
        """
        SELECT w.id AS workout_id, w.date AS date, w.type AS type,
               s.exercise AS exercise, s.set_no AS set_no, s.reps AS reps, s.weight_kg AS weight_kg
        FROM workout_sets s
        JOIN workouts w ON s.workout_id = w.id
        WHERE s.exercise IS NOT NULL AND w.date IS NOT NULL
        ORDER BY w.date, w.id, s.set_no
        """
    ).fetchall()

    per_canon: dict[str, dict[int, dict[str, Any]]] = {}
    for r in rows:
        canon = canon_map.get(r["exercise"], r["exercise"])
        sessions = per_canon.setdefault(canon, {})
        sess = sessions.get(r["workout_id"])
        if sess is None:
            sess = {
                "workout_id": r["workout_id"],
                "date": str(r["date"])[:10],
                "type": (r["type"] or "").strip() or "Sonstige",
                "sets": [],
            }
            sessions[r["workout_id"]] = sess
        sess["sets"].append({"reps": r["reps"], "weight_kg": r["weight_kg"]})

    return {
        canon: sorted(sessions.values(), key=lambda s: (s["date"], s["workout_id"]))
        for canon, sessions in per_canon.items()
    }


def _enrich_session(sess: dict[str, Any], load_mode: str) -> dict[str, Any]:
    """Top-Satz, effektive Last, e1RM, Volumen für eine Session."""
    weighted = [s for s in sess["sets"] if s["weight_kg"] is not None]
    top = None
    for s in weighted:
        if top is None or s["weight_kg"] > top["weight_kg"] or (
            s["weight_kg"] == top["weight_kg"] and (s["reps"] or 0) > (top["reps"] or 0)
        ):
            top = s
    mult = volume_multiplier(load_mode)
    volume = sum(
        (effective_load(s["weight_kg"], load_mode) or 0) * (s["reps"] or 0) * mult
        for s in weighted
    )
    top_w = top["weight_kg"] if top else None
    top_reps = top["reps"] if top else None
    eff = effective_load(top_w, load_mode)
    return {
        **sess,
        "set_count": len(sess["sets"]),
        "top_weight_kg": _round(top_w, 2),
        "top_reps": top_reps,
        "effective_top_kg": _round(eff, 2),
        "est_1rm": est_1rm(eff, top_reps),
        "volume_kg": _round(volume, 0),
        "reps": [s["reps"] for s in sess["sets"]],
    }


def exercise_points(
    conn: sqlite3.Connection,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], dict[str, dict[str, Any]]]:
    """Alle Übungen: (points_by_canon, category_by_canon, meta_by_canon).

    Ein Punkt pro Session — Ersatz für web/app._grouped_exercise_points,
    jetzt mit effektiver Last. Kategorie = Modus von workouts.type über die
    Sessions der Übung.
    """
    sessions_by_canon = _load_sessions(conn)
    points: dict[str, list[dict[str, Any]]] = {}
    categories: dict[str, str] = {}
    metas: dict[str, dict[str, Any]] = {}
    for canon, sessions in sessions_by_canon.items():
        meta = ensure_exercise_meta(conn, canon)
        metas[canon] = meta
        enriched = [_enrich_session(s, meta["load_mode"]) for s in sessions]
        points[canon] = [
            {
                "date": e["date"],
                "workout_id": e["workout_id"],
                "top_weight_kg": e["top_weight_kg"],
                "top_reps": e["top_reps"],
                "effective_top_kg": e["effective_top_kg"],
                "est_1rm": e["est_1rm"],
                "set_count": e["set_count"],
                "volume_kg": e["volume_kg"],
            }
            for e in enriched
            if e["top_weight_kg"] is not None
        ]
        categories[canon] = Counter(s["type"] for s in sessions).most_common(1)[0][0]
    return points, categories, metas


def _pr_info(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Erster Punkt, der das laufende Maximum (effektiv) überschreitet."""
    best: dict[str, Any] | None = None
    running = float("-inf")
    for p in points:
        w = p["effective_top_kg"]
        if w is not None and w > running:
            running = w
            best = p
    return best or {}


def exercise_summaries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Zeilen für /api/exercises, inkl. load_mode, Plateau und Ziel."""
    points, categories, metas = exercise_points(conn)
    items = []
    for canon, pts in points.items():
        pr = _pr_info(pts)
        target = get_target(conn, canon)
        items.append(
            {
                "name": canon,
                "sessions": len(pts),
                "category": categories.get(canon, "Sonstige"),
                "load_mode": metas[canon]["load_mode"],
                "primary_muscle": metas[canon].get("primary_muscle"),
                "last_weight_kg": pts[-1]["top_weight_kg"] if pts else None,
                "last_date": pts[-1]["date"] if pts else None,
                "pr_weight_kg": pr.get("top_weight_kg"),
                "pr_effective_kg": pr.get("effective_top_kg"),
                "pr_date": pr.get("date"),
                "pr_est_1rm": pr.get("est_1rm"),
                "plateau": _sessions_since_pr(pts) >= PLATEAU_SESSIONS if len(pts) > PLATEAU_SESSIONS else False,
                "target_weight_kg": target["target_weight_kg"] if target else None,
            }
        )
    items.sort(key=lambda x: x["sessions"], reverse=True)
    return items


def _sessions_since_pr(points: list[dict[str, Any]]) -> int:
    """Sessions seit dem letzten neuen e1RM-Hoch (0 = letzte Session war ein Hoch)."""
    best = float("-inf")
    since = 0
    for p in points:
        e = p.get("est_1rm")
        if e is not None and e > best:
            best = e
            since = 0
        else:
            since += 1
    return since


def progression_hint(
    last: dict[str, Any] | None, target: dict[str, Any] | None, load_mode: str
) -> str:
    """Double-Progression-Empfehlung auf Basis der letzten Session."""
    if not last:
        return "Keine Daten — erste Session konservativ ansetzen."
    rep_min = (target or {}).get("rep_min") or DEFAULT_REP_RANGE[0]
    rep_max = (target or {}).get("rep_max") or DEFAULT_REP_RANGE[1]
    reps = [r for r in last["reps"] if r is not None]
    if not reps or last["top_weight_kg"] is None:
        return "Letzte Session ohne verwertbare Reps/Gewichte."
    inc = INCREMENT_KG.get(load_mode, 2.5)
    w = float(last["top_weight_kg"])
    work_sets = reps[1:] if len(reps) > 3 else reps  # erster Satz oft Aufwärmsatz
    if all(r >= rep_max for r in work_sets):
        return (
            f"Alle Arbeitssätze ≥ {rep_max} Reps bei {w:g} kg → steigern auf "
            f"{w + inc:g} kg (Ziel {rep_min}–{rep_max})."
        )
    if min(work_sets) < rep_min:
        return (
            f"Reps unter {rep_min} bei {w:g} kg ({'/'.join(map(str, reps))}) → halten oder "
            f"leicht reduzieren, Technik/Erholung prüfen."
        )
    return f"{w:g} kg halten, Reps Richtung {rep_max} ausbauen (zuletzt {'/'.join(map(str, reps))})."


def exercise_progress(conn: sqlite3.Connection, exercise: str, sessions: int = 8) -> dict[str, Any]:
    """Fortschritt einer Übung: letzte Sessions, Bestwert, Trend, Plateau, Hinweis."""
    canon = canonical_name(conn, exercise)
    all_sessions = _load_sessions(conn).get(canon, [])
    if not all_sessions:
        return {"exercise": exercise, "error": f"Keine Sessions für '{exercise}' gefunden."}
    meta = ensure_exercise_meta(conn, canon)
    enriched = [_enrich_session(s, meta["load_mode"]) for s in all_sessions]
    pts = [e for e in enriched if e["top_weight_kg"] is not None]
    target = get_target(conn, canon)

    best = max((p for p in pts if p["est_1rm"] is not None), key=lambda p: p["est_1rm"], default=None)
    recent = pts[-sessions:]
    trend_pct = None
    if len(pts) >= 4 and pts[-1]["est_1rm"]:
        prev = [p["est_1rm"] for p in pts[-4:-1] if p["est_1rm"]]
        if prev:
            avg_prev = sum(prev) / len(prev)
            trend_pct = round((pts[-1]["est_1rm"] - avg_prev) / avg_prev * 100, 1) if avg_prev else None
    since_pr = _sessions_since_pr(pts)
    last_date = _parse_date(pts[-1]["date"]) if pts else None

    return {
        "exercise": canon,
        "load_mode": meta["load_mode"],
        "primary_muscle": meta.get("primary_muscle"),
        "session_count_total": len(pts),
        "sessions": [
            {
                "date": p["date"],
                "workout_id": p["workout_id"],
                "type": p["type"],
                "top_weight_kg": p["top_weight_kg"],
                "top_reps": p["top_reps"],
                "effective_top_kg": p["effective_top_kg"],
                "est_1rm": p["est_1rm"],
                "sets": [f"{s['reps']}x{s['weight_kg']:g}" if s["weight_kg"] is not None else f"{s['reps']}xBW" for s in p["sets"]],
                "volume_kg": p["volume_kg"],
            }
            for p in recent
        ],
        "best": {
            "est_1rm": best["est_1rm"],
            "date": best["date"],
            "top_weight_kg": best["top_weight_kg"],
            "top_reps": best["top_reps"],
        }
        if best
        else None,
        "trend_pct_vs_prev3": trend_pct,
        "sessions_since_pr": since_pr,
        "plateau": len(pts) > PLATEAU_SESSIONS and since_pr >= PLATEAU_SESSIONS,
        "days_since_last": (date.today() - last_date).days if last_date else None,
        "target": target,
        "progression_hint": progression_hint(pts[-1] if pts else None, target, meta["load_mode"]),
        "weight_note": {
            "barbell_per_side": "weight_kg = Scheiben einer Seite; effective = 20 kg Stange + 2×",
            "per_hand": "weight_kg = pro Kurzhantel; Volumen zählt beide",
            "total": "weight_kg = eingestellter Wert",
        }[meta["load_mode"]],
    }


# ---------------------------------------------------------------------------
# Wochen-Aggregate
# ---------------------------------------------------------------------------


def workouts_per_week(conn: sqlite3.Connection, weeks: int = 8, today: date | None = None) -> list[dict[str, Any]]:
    buckets = week_buckets(weeks, today)
    counts = {b.isoformat(): 0 for b in buckets}
    rows = conn.execute(
        "SELECT date FROM workouts WHERE date IS NOT NULL AND date >= ?",
        (buckets[0].isoformat(),),
    ).fetchall()
    for r in rows:
        d = _parse_date(r["date"])
        if d is None:
            continue
        key = week_start(d).isoformat()
        if key in counts:
            counts[key] += 1
    return [{"week": b.isoformat(), "count": counts[b.isoformat()]} for b in buckets]


def weekly_volume(conn: sqlite3.Connection, weeks: int = 12, today: date | None = None) -> list[dict[str, Any]]:
    """Effektives Volumen (kg) pro Kalenderwoche, Zero-Fill."""
    buckets = week_buckets(weeks, today)
    agg: dict[str, dict[str, Any]] = {
        b.isoformat(): {"volume_kg": 0.0, "set_count": 0, "workout_ids": set()} for b in buckets
    }
    canon_map = build_canon_map(conn)
    meta_cache: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT w.date AS date, w.id AS workout_id, s.exercise AS exercise,
               s.weight_kg AS weight_kg, s.reps AS reps
        FROM workout_sets s JOIN workouts w ON s.workout_id = w.id
        WHERE w.date IS NOT NULL AND w.date >= ? AND s.weight_kg IS NOT NULL AND s.reps IS NOT NULL
        """,
        (buckets[0].isoformat(),),
    ).fetchall()
    for r in rows:
        d = _parse_date(r["date"])
        if d is None:
            continue
        key = week_start(d).isoformat()
        bucket = agg.get(key)
        if bucket is None:
            continue
        canon = canon_map.get(r["exercise"], r["exercise"])
        mode = meta_cache.get(canon)
        if mode is None:
            mode = ensure_exercise_meta(conn, canon)["load_mode"]
            meta_cache[canon] = mode
        bucket["volume_kg"] += (effective_load(r["weight_kg"], mode) or 0) * float(r["reps"]) * volume_multiplier(mode)
        bucket["set_count"] += 1
        bucket["workout_ids"].add(r["workout_id"])
    return [
        {
            "week": b.isoformat(),
            "volume_kg": _round(agg[b.isoformat()]["volume_kg"], 0),
            "set_count": agg[b.isoformat()]["set_count"],
            "workout_count": len(agg[b.isoformat()]["workout_ids"]),
        }
        for b in buckets
    ]


def muscle_frequency(conn: sqlite3.Connection, weeks: int = 4, today: date | None = None) -> dict[str, Any]:
    """Wie oft pro Woche wurde jede Muskelgruppe (Hevy primary_muscle) trainiert?

    Zählt Trainingstage pro Muskel im Zeitraum; Nippard-Empfehlung: 2×+/Woche.
    Übungen ohne bekannte Muskelgruppe landen unter 'unbekannt'.
    """
    today = today or date.today()
    start = week_start(today) - timedelta(weeks=weeks - 1)
    canon_map = build_canon_map(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT w.date AS date, s.exercise AS exercise
        FROM workout_sets s JOIN workouts w ON s.workout_id = w.id
        WHERE w.date IS NOT NULL AND w.date >= ?
        """,
        (start.isoformat(),),
    ).fetchall()
    days_by_muscle: dict[str, set[str]] = {}
    exercises_by_muscle: dict[str, set[str]] = {}
    for r in rows:
        canon = canon_map.get(r["exercise"], r["exercise"])
        muscle = ensure_exercise_meta(conn, canon).get("primary_muscle") or "unbekannt"
        days_by_muscle.setdefault(muscle, set()).add(str(r["date"])[:10])
        exercises_by_muscle.setdefault(muscle, set()).add(canon)
    result = {
        muscle: {
            "sessions": len(days),
            "per_week": round(len(days) / weeks, 1),
            "exercises": sorted(exercises_by_muscle[muscle]),
        }
        for muscle, days in days_by_muscle.items()
    }
    return {
        "weeks": weeks,
        "since": start.isoformat(),
        "recommendation": "2-4 Sessions/Woche pro Muskelgruppe (Jeff Nippard)",
        "muscles": dict(sorted(result.items(), key=lambda kv: -kv[1]["sessions"])),
    }
