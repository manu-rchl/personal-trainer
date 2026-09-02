"""Health-Check-Bewertung über einem Zustands-Dict (ohne DB)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from trainer.jobs.health_check import evaluate

NOW = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)


def _epoch(dt: datetime) -> str:
    return str(dt.timestamp())


def _fresh_state() -> dict:
    return {
        "oura_last_sync": _epoch(NOW - timedelta(hours=3)),
        "hevy_last_sync": _epoch(NOW - timedelta(hours=10)),
        "oura_token_expires_at": _epoch(NOW + timedelta(days=20)),
        "bot_heartbeat": (NOW - timedelta(minutes=2)).isoformat(),
        "post_workout_last_run": (NOW - timedelta(minutes=45)).isoformat(),
        "last_workout_date": (NOW - timedelta(days=2)).date().isoformat(),
    }


def test_all_fresh_means_no_problems():
    assert evaluate(_fresh_state(), NOW) == []


def test_stale_syncs_and_missing_heartbeat():
    state = _fresh_state()
    state["oura_last_sync"] = _epoch(NOW - timedelta(days=3))
    state["hevy_last_sync"] = _epoch(NOW - timedelta(hours=40))
    del state["bot_heartbeat"]
    problems = evaluate(state, NOW)
    assert any(p.startswith("Oura-Sync") for p in problems)
    assert any(p.startswith("Hevy-Sync") for p in problems)
    assert any(p.startswith("Bot: kein Heartbeat") for p in problems)


def test_token_expiry_warning_and_workout_gap():
    state = _fresh_state()
    state["oura_token_expires_at"] = _epoch(NOW + timedelta(days=1))
    state["last_workout_date"] = (NOW - timedelta(days=12)).date().isoformat()
    problems = evaluate(state, NOW)
    assert any("Oura-Token" in p and "läuft" in p for p in problems)
    assert any("letztes Workout vor 12 Tagen" in p for p in problems)


def test_empty_state_reports_never_ran():
    problems = evaluate({}, NOW)
    assert "Oura-Sync: noch nie gelaufen" in problems
    assert "Hevy-Sync: noch nie gelaufen" in problems
    assert "Post-Workout-Job: noch nie gelaufen" in problems


def test_stale_post_workout_job():
    state = _fresh_state()
    state["post_workout_last_run"] = (NOW - timedelta(hours=5)).isoformat()
    assert any(p.startswith("Post-Workout-Job: letzter Lauf") for p in evaluate(state, NOW))
