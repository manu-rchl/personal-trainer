"""Agent-Jobs: NO_MESSAGE-Konvention, Fehlertexte, Post-Workout-Dedupe, Follow-ups."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from trainer.agent import coach_tools
from trainer.agent.core import ABORT_TEXT
from trainer.db import get_connection, init_db
from trainer.jobs import agent_job, post_workout


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "j.db"
    init_db(path)
    # Module, die get_connection direkt importiert haben, auf die Test-DB umbiegen.
    monkeypatch.setattr(post_workout, "get_connection", lambda: _connect(path))
    monkeypatch.setattr(coach_tools, "get_connection", lambda: _connect(path))
    return path


def _connect(path: Path):
    return get_connection(path)


def test_no_message_sends_nothing(monkeypatch):
    sent, persisted = [], []
    monkeypatch.setattr(agent_job, "run_agent", lambda *a, **k: "NO_MESSAGE")
    monkeypatch.setattr(agent_job, "send_telegram", lambda t: sent.append(t))
    monkeypatch.setattr(agent_job, "persist_exchange", lambda *a, **k: persisted.append(a))
    assert agent_job.run_agent_job("t", "[System: x]") is None
    assert not sent and not persisted


@pytest.mark.parametrize("reply", ["no_message", "`NO_MESSAGE`", " NO_MESSAGE. "])
def test_no_message_variants(reply):
    assert agent_job.is_no_message(reply)


def test_reply_is_sent_and_persisted(monkeypatch):
    sent, persisted = [], []
    monkeypatch.setattr(agent_job, "run_agent", lambda *a, **k: "Hey Manuel, heute Push!")
    monkeypatch.setattr(agent_job, "send_telegram", lambda t: sent.append(t))
    monkeypatch.setattr(agent_job, "persist_exchange", lambda u, a_, agent: persisted.append((u, a_)))
    assert agent_job.run_agent_job("t", "[System: x]") == "Hey Manuel, heute Push!"
    assert sent == ["Hey Manuel, heute Push!"]
    assert persisted[0][0] == "[System: x]"


def test_failure_replies_raise(monkeypatch):
    monkeypatch.setattr(agent_job, "run_agent", lambda *a, **k: ABORT_TEXT)
    monkeypatch.setattr(agent_job, "send_telegram", lambda t: pytest.fail("darf nicht senden"))
    with pytest.raises(agent_job.AgentJobFailed):
        agent_job.run_agent_job("t", "[System: x]")


def test_dry_run_does_not_send(monkeypatch, capsys):
    monkeypatch.setattr(agent_job, "run_agent", lambda *a, **k: "Antwort")
    monkeypatch.setattr(agent_job, "send_telegram", lambda t: pytest.fail("darf nicht senden"))
    assert agent_job.run_agent_job("t", "[System: x]", dry_run=True) == "Antwort"
    assert "DRY-RUN" in capsys.readouterr().out


def test_post_workout_only_handles_pending_and_marks_them(db, monkeypatch):
    conn = _connect(db)
    conn.execute("INSERT INTO workouts (date, type, source) VALUES (?, 'Push', 'hevy')", (date.today().isoformat(),))
    wid_new = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO workouts (date, type, source, checkin_sent_at) VALUES (?, 'Pull', 'hevy', 'done')",
        (date.today().isoformat(),),
    )
    conn.execute(
        "INSERT INTO workout_sets (workout_id, exercise, set_no, reps, weight_kg) VALUES (?, 'Bench Press (Barbell)', 1, 10, 12.5)",
        (wid_new,),
    )
    conn.execute(
        "INSERT INTO scheduled_checkins (due_date, text, created_ts) VALUES (?, 'Legs geklappt?', 'x')",
        (date.today().isoformat(),),
    )
    conn.commit()

    calls: list[str] = []
    monkeypatch.setattr(post_workout, "run_agent_job", lambda name, instr, dry_run=False: calls.append(name) or "ok")
    monkeypatch.setattr(post_workout, "_sync_hevy_best_effort", lambda: None)

    assert [w["id"] for w in post_workout.pending_workouts(conn, date.today())] == [wid_new]
    post_workout.run()
    assert calls == [f"post-workout #{wid_new}", "checkin #1"]

    conn = _connect(db)
    assert conn.execute("SELECT checkin_sent_at FROM workouts WHERE id = ?", (wid_new,)).fetchone()[0]
    assert conn.execute("SELECT sent_at FROM scheduled_checkins WHERE id = 1").fetchone()[0]
    assert conn.execute("SELECT value FROM sync_state WHERE key = 'post_workout_last_run'").fetchone()

    # zweiter Lauf: nichts mehr zu tun
    calls.clear()
    post_workout.run()
    assert calls == []


def test_post_workout_instruction_contains_sets(db):
    conn = _connect(db)
    conn.execute("INSERT INTO workouts (date, type, source) VALUES ('2026-09-01', 'Push', 'hevy')")
    wid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO workout_sets (workout_id, exercise, set_no, reps, weight_kg) VALUES (?, 'Face Pull', 1, 12, 23)",
        (wid,),
    )
    conn.commit()
    block, n = post_workout._sets_block(conn, wid)
    assert n == 1 and "Face Pull: 12×23" in block
