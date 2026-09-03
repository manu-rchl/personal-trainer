"""trainer.analytics auf synthetischen Daten: Load-Modus, e1RM, Fortschritt, Plateau, Buckets."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from trainer import analytics as a
from trainer.db import get_connection, init_db


@pytest.fixture
def conn(tmp_path: Path):
    db = tmp_path / "a.db"
    init_db(db)
    c = get_connection(db)
    c.execute(
        "INSERT INTO hevy_exercise_templates (id, title, primary_muscle, equipment) VALUES "
        "('B1', 'Bench Press (Barbell)', 'chest', 'barbell'), ('L1', 'Lat Pulldown (Machine)', 'lats', 'machine')"
    )
    c.commit()
    yield c
    c.close()


def _add_session(conn, day: date, exercise: str, sets: list[tuple[int, float]], type_="Push") -> int:
    cur = conn.execute(
        "INSERT INTO workouts (date, type, source) VALUES (?, ?, 'hevy')", (day.isoformat(), type_)
    )
    wid = cur.lastrowid
    for i, (reps, w) in enumerate(sets, start=1):
        conn.execute(
            "INSERT INTO workout_sets (workout_id, exercise, set_no, reps, weight_kg) VALUES (?, ?, ?, ?, ?)",
            (wid, exercise, i, reps, w),
        )
    conn.commit()
    return wid


@pytest.mark.parametrize(
    "name, equipment, expected",
    [
        ("Bench Press (Barbell)", None, "barbell_per_side"),
        ("Bench Press (Smith Machine)", None, "barbell_per_side"),
        ("Incline Bench Press (Dumbbell)", None, "per_hand"),
        ("Lat Pulldown (Machine)", "machine", "total"),
        ("Cable Fly", None, "total"),
    ],
)
def test_guess_load_mode(name, equipment, expected):
    assert a.guess_load_mode(name, equipment) == expected


def test_effective_load_and_e1rm():
    assert a.effective_load(12.5, "barbell_per_side") == 45.0
    assert a.effective_load(15, "per_hand") == 15
    assert a.effective_load(50, "total") == 50
    assert a.est_1rm(45.0, 10) == 60.0
    assert a.volume_multiplier("per_hand") == 2


def test_week_buckets_end_with_current_week():
    today = date(2026, 9, 2)  # Mittwoch
    buckets = a.week_buckets(3, today)
    assert buckets == [date(2026, 8, 17), date(2026, 8, 24), date(2026, 8, 31)]


def test_exercise_progress_plateau_and_double_progression(conn):
    ex = "Bench Press (Barbell)"
    d0 = date(2026, 6, 1)
    # Aufbau bis PR, danach 4 Sessions ohne neues e1RM -> Plateau
    _add_session(conn, d0, ex, [(10, 10.0), (10, 10.0), (10, 10.0)])
    _add_session(conn, d0 + timedelta(days=7), ex, [(10, 12.5), (10, 12.5), (10, 12.5)])
    _add_session(conn, d0 + timedelta(days=14), ex, [(8, 15.0), (8, 15.0), (6, 15.0)])  # PR
    for k in range(3, 7):
        _add_session(conn, d0 + timedelta(days=7 * k), ex, [(8, 12.5), (8, 12.5), (6, 12.5)])

    p = a.exercise_progress(conn, "bench press barbell", sessions=3)
    assert p["exercise"] == ex
    assert p["load_mode"] == "barbell_per_side"
    assert p["primary_muscle"] == "chest"
    assert p["session_count_total"] == 7
    assert len(p["sessions"]) == 3
    assert p["best"]["top_weight_kg"] == 15.0 and p["best"]["est_1rm"] == a.est_1rm(50.0, 8)
    assert p["sessions_since_pr"] == 4
    assert p["plateau"] is True
    assert "halten" in p["progression_hint"]  # 8/8/6 < rep_max 12
    assert p["sessions"][-1]["effective_top_kg"] == 45.0


def test_progression_hint_increase_when_all_sets_at_top(conn):
    ex = "Lat Pulldown (Machine)"
    for k in range(3):
        _add_session(conn, date(2026, 8, 1) + timedelta(days=7 * k), ex, [(12, 45.0), (12, 45.0), (12, 45.0)], "Pull")
    p = a.exercise_progress(conn, ex)
    assert p["load_mode"] == "total"
    assert p["plateau"] is False
    assert "steigern auf 47.5" in p["progression_hint"]


def test_targets_and_summaries(conn):
    ex = "Lat Pulldown (Machine)"
    _add_session(conn, date(2026, 8, 20), ex, [(10, 45.0), (10, 45.0)], "Pull")
    conn.execute(
        "INSERT INTO exercise_targets (exercise, target_weight_kg, rep_min, rep_max, sets, reason, source, updated_at) "
        "VALUES (?, 47.5, 8, 12, 3, 'test', 'isa', 'now')",
        (ex,),
    )
    conn.commit()
    items = a.exercise_summaries(conn)
    row = next(i for i in items if i["name"] == ex)
    assert row["target_weight_kg"] == 47.5
    assert row["pr_effective_kg"] == 45.0
    assert row["load_mode"] == "total"
    assert a.exercise_progress(conn, ex)["target"]["target_weight_kg"] == 47.5


def test_weekly_volume_uses_effective_load(conn):
    today = date(2026, 9, 2)
    _add_session(conn, date(2026, 8, 31), "Bench Press (Barbell)", [(10, 10.0)])  # 40 kg effektiv x 10
    _add_session(conn, date(2026, 8, 31), "Incline Bench Press (Dumbbell)", [(10, 15.0)])  # 15 x 10 x 2
    vol = a.weekly_volume(conn, weeks=2, today=today)
    assert vol[-1]["week"] == "2026-08-31"
    assert vol[-1]["volume_kg"] == 400 + 300
    assert vol[-1]["workout_count"] == 2
    assert a.workouts_per_week(conn, 2, today)[-1]["count"] == 2


def test_muscle_frequency_groups_by_template_muscle(conn):
    today = date(2026, 9, 2)
    for k in range(4):
        _add_session(conn, date(2026, 8, 10) + timedelta(days=5 * k), "Bench Press (Barbell)", [(10, 10.0)])
    mf = a.muscle_frequency(conn, weeks=4, today=today)
    assert mf["muscles"]["chest"]["sessions"] == 4
    assert mf["muscles"]["chest"]["per_week"] == 1.0


def test_set_load_mode_override(conn):
    _add_session(conn, date(2026, 8, 1), "Kitten", [(10, 23.0)])
    assert a.ensure_exercise_meta(conn, "Kitten")["load_mode"] == "total"
    a.set_load_mode(conn, "Kitten", "per_hand")
    assert a.exercise_progress(conn, "Kitten")["load_mode"] == "per_hand"
    with pytest.raises(ValueError):
        a.set_load_mode(conn, "Kitten", "nonsense")


def test_weight_trend_pace_and_goal(conn):
    today = date(2026, 9, 2)
    for k, w in enumerate([66.0, 66.2, 66.5, 66.9]):
        conn.execute(
            "INSERT INTO body_weight (date, weight_kg, source) VALUES (?, ?, 'chat')",
            ((date(2026, 8, 10) + timedelta(days=7 * k)).isoformat(), w),
        )
    conn.commit()
    t = a.weight_trend(conn, weeks=6, goal_kg=70.0, deadline=date(2026, 12, 31), today=today)
    assert t["latest"]["weight_kg"] == 66.9
    assert t["change_kg"] == 0.9
    assert t["pace_kg_per_week"] == 0.3
    assert t["needed_kg_per_week"] > 0 and t["on_track"] is True
    assert t["days_since_last"] == 2
    empty = a.weight_trend(conn, weeks=2, today=date(2020, 1, 1))
    assert empty["latest"]["weight_kg"] == 66.9 and empty["change_kg"] is None
