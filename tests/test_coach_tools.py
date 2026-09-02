"""Coach-Tools: Zielgewicht -> DB + Hevy-Notiz, Routinen-Parsing, Follow-ups, Memory-Tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from trainer import analytics
from trainer.agent import coach_tools, tools as tools_module
from trainer.db import get_connection, init_db


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise coach_tools.httpx.HTTPStatusError("x", request=None, response=None)

    def json(self):
        return self._payload


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "c.db"
    init_db(path)
    connect = lambda: get_connection(path)  # noqa: E731
    monkeypatch.setattr(coach_tools, "get_connection", connect)
    monkeypatch.setattr(tools_module, "get_connection", connect)
    c = connect()
    c.execute(
        "INSERT INTO hevy_exercise_templates (id, title, primary_muscle, equipment) VALUES "
        "('BE640BA0', 'Face Pull', 'shoulders', 'cable')"
    )
    c.execute("INSERT INTO workouts (id, date, type, source) VALUES (1, '2026-08-30', 'Pull', 'hevy')")
    c.execute(
        "INSERT INTO workout_sets (workout_id, exercise, set_no, reps, weight_kg) VALUES (1, 'Face Pull', 1, 12, 23)"
    )
    c.commit()
    c.close()
    return path


def _routines():
    return [
        {
            "id": "R1",
            "title": "Pull",
            "exercises": [
                {
                    "index": 0,
                    "title": "Face Pull",
                    "exercise_template_id": "BE640BA0",
                    "notes": "alte Zeile 1\nalte Zeile 2",
                    "sets": [{"index": 0, "type": "normal", "reps": 12, "weight_kg": None}],
                }
            ],
        },
        {"id": "R2", "title": "Push", "exercises": [{"index": 0, "title": "Bench", "exercise_template_id": "X", "notes": "", "sets": []}]},
    ]


def test_set_exercise_target_writes_db_and_mirrors_note(db, monkeypatch):
    puts = []
    monkeypatch.setattr(coach_tools.httpx, "get", lambda url, **kw: _Resp({"routines": _routines()}))
    monkeypatch.setattr(coach_tools.httpx, "put", lambda url, **kw: puts.append((url, kw["json"])) or _Resp({"routine": {"id": "R1"}}))
    monkeypatch.setattr(coach_tools, "HEVY_WRITE_ENABLED", True)
    monkeypatch.setattr(type(coach_tools.config), "hevy_api_key", property(lambda self: "key"), raising=False)

    result = coach_tools.set_exercise_target("face pull", 25.5, 8, 12, 3, reason="3×12 sauber")
    assert result["status"] == "gespeichert" and result["exercise"] == "Face Pull"
    assert result["hevy"]["updated"] == ["Pull"]

    conn = get_connection(db)
    t = analytics.get_target(conn, "Face Pull")
    assert t["target_weight_kg"] == 25.5 and t["rep_max"] == 12

    assert len(puts) == 1  # nur die Routine mit der Übung
    url, body = puts[0]
    assert url.endswith("/v1/routines/R1")
    ex = body["routine"]["exercises"][0]
    assert "index" not in ex and "title" not in ex and "index" not in ex["sets"][0]
    lines = ex["notes"].splitlines()
    assert lines[0].startswith("Ziel (Isa") and "25.5 kg · 3×8–12 — 3×12 sauber" in lines[0]
    assert lines[1:] == ["alte Zeile 1"]  # nur eine alte Zeile bleibt


def test_merge_note_replaces_previous_target_line():
    old = "Ziel (Isa, 01.09.): 23 kg · 3×8–12\nBeobachtung: Form gut"
    merged = coach_tools._merge_note(old, "Ziel (Isa, 08.09.): 25 kg · 3×8–12")
    assert merged.splitlines() == ["Ziel (Isa, 08.09.): 25 kg · 3×8–12", "Beobachtung: Form gut"]


def test_hevy_write_disabled_skips(db, monkeypatch):
    monkeypatch.setattr(coach_tools, "HEVY_WRITE_ENABLED", False)
    monkeypatch.setattr(type(coach_tools.config), "hevy_api_key", property(lambda self: "key"), raising=False)
    monkeypatch.setattr(coach_tools.httpx, "get", lambda *a, **k: pytest.fail("kein GET im Dry-Run"))
    r = coach_tools.set_exercise_target("Face Pull", 25.5)
    assert "skipped" in r["hevy"]


def test_parse_routine_shape():
    parsed = coach_tools.parse_routine(_routines()[0])
    assert parsed["title"] == "Pull"
    assert parsed["exercises"][0] == {
        "title": "Face Pull",
        "template_id": "BE640BA0",
        "notes": "alte Zeile 1\nalte Zeile 2",
        "set_count": 1,
        "reps_scheme": [12],
        "rest_seconds": None,
    }


def test_schedule_checkin_validation(db):
    assert "error" in coach_tools.schedule_checkin("gestern", "x")
    assert "error" in coach_tools.schedule_checkin("2000-01-01", "x")
    r = coach_tools.schedule_checkin("2099-01-01", "Legs?")
    assert r["status"] == "gespeichert" and r["checkin_id"] == 1


def test_training_plan_roundtrip(db):
    plan = coach_tools.get_training_plan()
    assert plan["name"] == "PPL Basis" and plan["block_week_number"] >= 1
    assert coach_tools.set_training_plan(days_per_week=4, split="Upper/Lower")["updated_fields"] == ["days_per_week", "split"]
    assert coach_tools.get_training_plan()["days_per_week"] == 4
    assert "error" in coach_tools.set_training_plan(unknown="x")


def test_memory_update_delete_and_pinned(db):
    r = tools_module.save_memory("Fakt", "test", source="NotebookLM NB1", pinned=True)
    mid = r["memory_id"]
    assert tools_module.update_memory(mid, content="Fakt v2", pinned=False)["memory"]["content"] == "Fakt v2"
    found = tools_module.search_memories("v2")["memories"][0]
    assert found["source"] == "NotebookLM NB1" and found["pinned"] == 0
    assert tools_module.delete_memory(mid)["status"] == "gelöscht"
    assert "error" in tools_module.delete_memory(mid)
    assert coach_tools.clear_memory_review()["status"] == "kein offener Review"
