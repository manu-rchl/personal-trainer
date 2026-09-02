"""query_db: nur lesen, nie aus secrets/sync_state — auch nicht über Subqueries/CTEs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from trainer.agent import tools as tools_module
from trainer.db import get_connection, init_db


@pytest.fixture
def db_with_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "q.db"
    init_db(db)
    conn = get_connection(db)
    try:
        conn.execute("INSERT INTO secrets VALUES ('oura_access_token', 'GEHEIM')")
        conn.execute("INSERT INTO sync_state VALUES ('oura_last_sync', '1')")
        conn.execute("INSERT INTO memories (ts, category, content) VALUES ('t', 'x', 'm1')")
        conn.commit()
    finally:
        conn.close()
    # Config ist ein frozen dataclass — Modul-Attribut ersetzen statt Feld setzen.
    monkeypatch.setattr(tools_module, "config", SimpleNamespace(db_path=db))
    return db


def test_select_on_normal_table_works(db_with_secret):
    result = tools_module.query_db("SELECT content FROM memories")
    assert result["rows"] == [{"content": "m1"}]


def test_cte_is_allowed(db_with_secret):
    result = tools_module.query_db("WITH x AS (SELECT COUNT(*) AS n FROM memories) SELECT n FROM x")
    assert result["rows"] == [{"n": 1}]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secrets",
        "SELECT value FROM sync_state",
        "SELECT (SELECT value FROM secrets LIMIT 1) AS v",
        "WITH s AS (SELECT value FROM secrets) SELECT * FROM s",
        "SELECT m.content, s.value FROM memories m, secrets s",
    ],
)
def test_hidden_tables_are_denied_everywhere(db_with_secret, sql):
    result = tools_module.query_db(sql)
    assert "error" in result
    assert "GEHEIM" not in str(result)


@pytest.mark.parametrize("sql", ["DELETE FROM memories", "UPDATE memories SET content='x'", "DROP TABLE memories"])
def test_writes_are_rejected_by_prefix_check(db_with_secret, sql):
    result = tools_module.query_db(sql)
    assert result["error"].startswith("Nur SELECT")
