"""Tests für die Obsidian-Vault-Schreib-Tools (trainer.agent.tools)."""

from pathlib import Path

import pytest

import trainer.agent.tools as tools_module
from trainer.agent.tools import append_note, create_note, delete_note, edit_note


@pytest.fixture
def vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr(tools_module, "_obsidian_vault_root", lambda: vault_dir)
    return vault_dir


def test_create_note_writes_new_file(vault):
    result = create_note("Trainer/Notiz.md", "# Titel\n\nInhalt")

    assert result == {"file": "Trainer/Notiz.md", "created": True}
    assert (vault / "Trainer" / "Notiz.md").read_text() == "# Titel\n\nInhalt"


def test_create_note_refuses_to_overwrite_existing(vault):
    (vault / "Notiz.md").write_text("Original")

    result = create_note("Notiz.md", "Neuer Inhalt")

    assert "error" in result
    assert (vault / "Notiz.md").read_text() == "Original"


def test_create_note_rejects_path_traversal(vault):
    result = create_note("../outside.md", "böse")

    assert "error" in result
    assert not (vault.parent / "outside.md").exists()


def test_create_note_rejects_non_markdown(vault):
    result = create_note("secrets.txt", "nope")

    assert "error" in result
    assert not (vault / "secrets.txt").exists()


def test_create_note_rejects_obsidian_internal_dir(vault):
    result = create_note(".obsidian/config.md", "nope")

    assert "error" in result


def test_append_note_creates_when_missing(vault):
    result = append_note("Log.md", "Erster Eintrag")

    assert result == {"file": "Log.md", "appended": True}
    assert (vault / "Log.md").read_text() == "Erster Eintrag"


def test_append_note_adds_newline_separator(vault):
    (vault / "Log.md").write_text("Erster Eintrag")

    append_note("Log.md", "Zweiter Eintrag")

    assert (vault / "Log.md").read_text() == "Erster Eintrag\nZweiter Eintrag"


def test_edit_note_replaces_unique_match(vault):
    (vault / "Notiz.md").write_text("Zeile A\nZeile B\nZeile C")

    result = edit_note("Notiz.md", "Zeile B", "Zeile B (korrigiert)")

    assert result == {"file": "Notiz.md", "edited": True}
    assert (vault / "Notiz.md").read_text() == "Zeile A\nZeile B (korrigiert)\nZeile C"


def test_edit_note_fails_on_zero_matches(vault):
    (vault / "Notiz.md").write_text("Zeile A")

    result = edit_note("Notiz.md", "Zeile X", "neu")

    assert "error" in result
    assert (vault / "Notiz.md").read_text() == "Zeile A"


def test_edit_note_fails_on_ambiguous_match(vault):
    (vault / "Notiz.md").write_text("dup\ndup")

    result = edit_note("Notiz.md", "dup", "neu")

    assert "error" in result
    assert (vault / "Notiz.md").read_text() == "dup\ndup"


def test_delete_note_removes_file(vault):
    (vault / "Notiz.md").write_text("weg damit")

    result = delete_note("Notiz.md")

    assert result == {"file": "Notiz.md", "deleted": True}
    assert not (vault / "Notiz.md").exists()


def test_delete_note_missing_file_returns_error(vault):
    result = delete_note("nicht-da.md")

    assert "error" in result


def test_no_vault_configured_returns_error(monkeypatch):
    monkeypatch.setattr(tools_module, "_obsidian_vault_root", lambda: None)

    assert "error" in create_note("x.md", "y")
    assert "error" in append_note("x.md", "y")
    assert "error" in edit_note("x.md", "a", "b")
    assert "error" in delete_note("x.md")
