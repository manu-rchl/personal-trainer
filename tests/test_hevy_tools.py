"""Tests für den Hevy-Routine-Payload-Builder (trainer.agent.tools).

Reproduziert einen echten 400-Fehler der Hevy-API: POST /v1/routines lehnt
Payloads ohne `folder_id` mit "Invalid routine folder id: undefined" ab.
Laut Hevy-OpenAPI-Doku muss `folder_id` explizit als `null` mitgeschickt
werden, um die Routine in den Standard-Ordner "My Routines" zu legen.
"""

import sqlite3

import pytest

import trainer.agent.tools as tools_module
from trainer.agent.tools import _build_hevy_routine_payload, create_hevy_routine


def _conn_with_templates() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE hevy_exercise_templates "
        "(id TEXT, title TEXT, primary_muscle TEXT, equipment TEXT)"
    )
    conn.execute(
        "INSERT INTO hevy_exercise_templates (id, title, primary_muscle) VALUES (?, ?, ?)",
        ("3BC06AD3", "Bicep Curl", "biceps"),
    )
    conn.commit()
    return conn


def test_routine_payload_includes_null_folder_id():
    conn = _conn_with_templates()
    try:
        payload, _matched, _unmatched = _build_hevy_routine_payload(
            conn, "Test Routine", [{"name": "Bicep Curl", "sets": 2, "reps": 10}]
        )
    finally:
        conn.close()

    assert "folder_id" in payload["routine"]
    assert payload["routine"]["folder_id"] is None


def test_routine_payload_passes_through_rest_seconds_and_notes():
    """Hevys Schema unterstützt rest_seconds/notes pro Übung; das Tool-Schema
    nimmt rest_seconds vom Modell entgegen — der Payload-Builder darf es nicht
    stillschweigend verwerfen.
    """
    conn = _conn_with_templates()
    try:
        payload, _matched, _unmatched = _build_hevy_routine_payload(
            conn,
            "Test Routine",
            [{"name": "Bicep Curl", "sets": 1, "reps": 10, "rest_seconds": 90}],
        )
    finally:
        conn.close()

    exercise = payload["routine"]["exercises"][0]
    assert exercise["rest_seconds"] == 90


def test_routine_payload_matches_exercise_and_builds_sets():
    conn = _conn_with_templates()
    try:
        payload, matched, unmatched = _build_hevy_routine_payload(
            conn, "Test Routine", [{"name": "Bicep Curl", "sets": 2, "reps": 10}]
        )
    finally:
        conn.close()

    assert unmatched == []
    assert matched == [
        {"name": "Bicep Curl", "matched_title": "Bicep Curl", "template_id": "3BC06AD3"}
    ]
    exercise = payload["routine"]["exercises"][0]
    assert exercise["exercise_template_id"] == "3BC06AD3"
    assert len(exercise["sets"]) == 2
    assert exercise["sets"][0] == {"type": "normal", "reps": 10, "weight_kg": None}


class _FakeResponse:
    """Minimaler httpx.Response-Stand-in mit der echten, live beobachteten
    Hevy-Response-Form: `routine` ist ein Array mit einem Element, nicht ein
    einzelnes Objekt (weicht von Hevys eigener OpenAPI-Doku ab)."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_create_hevy_routine_parses_real_response_shape(monkeypatch):
    """Live gegen die echte Hevy-API beobachtete 201-Response:
    {"routine": [{"id": "...", ...}]} — `routine` als Liste, nicht als Objekt.
    """
    monkeypatch.setattr(tools_module, "init_db", lambda: None)
    monkeypatch.setattr(tools_module, "get_connection", lambda: _conn_with_templates())
    if not tools_module.config.hevy_api_key:
        pytest.skip("HEVY_API_KEY nicht gesetzt — Config ist frozen, kann in Tests nicht gepatcht werden")

    fake_response = _FakeResponse(
        {
            "routine": [
                {
                    "id": "970690f1-575b-4f2f-8b56-fcec73c11658",
                    "title": "Test Routine",
                    "folder_id": None,
                    "exercises": [],
                }
            ]
        }
    )
    monkeypatch.setattr(
        tools_module.httpx, "post", lambda *a, **kw: fake_response
    )

    result = create_hevy_routine(
        "Test Routine", [{"name": "Bicep Curl", "sets": 1, "reps": 10}]
    )

    assert result["routine_id"] == "970690f1-575b-4f2f-8b56-fcec73c11658"
