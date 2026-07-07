"""Dev-Task-Workflow: Der Assistant kann das System selbst erweitern.

Ablauf (von Manuel explizit freigegeben, 2026-07-07):
  1. propose_dev_task()  — Assistant formuliert einen Auftrag, Status "proposed"
  2. Manuel sagt explizit Ja
  3. run_dev_task()      — Git-Worktree + Branch, Claude-Code-CLI läuft DETACHED
                           (--permission-mode acceptEdits: darf Dateien im
                           Worktree ändern, keine Einzelnachfragen)
  4. runner meldet sich per Telegram (Assistant-Bot), Status "done"/"failed"
  5. merge_dev_branch()  — nur auf Manuels Befehl; Bots danach manuell neustarten

Sicherheitsmodell: Der Live-Code (main, laufende launchd-Bots) wird nie
angefasst — Claude Code arbeitet ausschließlich in einem separaten Worktree
unter .worktrees/. Schlimmster Fall ist ein wegwerfbarer Branch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from trainer.config import BASE_DIR
from trainer.db import get_connection, init_db

PENDING_KEY = "pending_dev_task"
WORKTREES_DIR = BASE_DIR / ".worktrees"
LOG_DIR = Path.home() / "Library" / "Logs" / "trainer"


def _claude_bin() -> str:
    return (
        os.environ.get("CLAUDE_BIN")
        or shutil.which("claude")
        or str(Path.home() / ".local" / "bin" / "claude")
    )


def _get_task() -> dict[str, Any] | None:
    init_db()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (PENDING_KEY,)
        ).fetchone()
        return json.loads(row["value"]) if row else None
    finally:
        conn.close()


def _set_task(task: dict[str, Any] | None) -> None:
    conn = get_connection()
    try:
        if task is None:
            conn.execute("DELETE FROM sync_state WHERE key = ?", (PENDING_KEY,))
        else:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (PENDING_KEY, json.dumps(task, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


def _git(*args: str, cwd: Path = BASE_DIR) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )


# ---------------------------------------------------------------------------
# Tools (für den Assistant)
# ---------------------------------------------------------------------------


def propose_dev_task(title: str, description: str) -> dict[str, Any]:
    """Legt einen Entwicklungsauftrag als Vorschlag ab (Status 'proposed')."""
    existing = _get_task()
    if existing and existing.get("status") in ("proposed", "running"):
        return {
            "error": f"Es gibt bereits einen Dev-Task ({existing.get('status')}): "
            f"'{existing.get('title')}'. Erst abschließen oder mit run/merge/check weiterarbeiten."
        }
    _set_task(
        {
            "title": title,
            "description": description,
            "status": "proposed",
            "proposed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    return {
        "status": "proposed",
        "hint": "Vorschlag gespeichert. Zeige Manuel Titel + Beschreibung und warte "
        "auf sein explizites Ja, bevor du run_dev_task aufrufst.",
    }


def run_dev_task() -> dict[str, Any]:
    """Startet den freigegebenen Dev-Task detached in einem Git-Worktree."""
    task = _get_task()
    if not task or task.get("status") != "proposed":
        return {"error": "Kein freigegebener Vorschlag vorhanden (erst propose_dev_task)."}

    dirty = _git("status", "--porcelain")
    if dirty.stdout.strip():
        return {
            "error": "Das Repo hat uncommittete Änderungen — Dev-Tasks brauchen einen "
            "sauberen Stand. Manuel soll erst committen (oder Claude Code direkt fragen)."
        }

    ts = time.strftime("%Y%m%d-%H%M%S")
    branch = f"dev-task/{ts}"
    worktree = WORKTREES_DIR / ts
    WORKTREES_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"dev-task-{ts}.log"

    wt = _git("worktree", "add", str(worktree), "-b", branch)
    if wt.returncode != 0:
        return {"error": f"Worktree konnte nicht erstellt werden: {wt.stderr[-300:]}"}

    task.update(status="running", branch=branch, worktree=str(worktree), log=str(log_file))
    _set_task(task)

    # Detached starten: überlebt den Bot-Prozess, meldet sich selbst per Telegram.
    subprocess.Popen(
        [sys.executable, "-m", "trainer.dev.runner"],
        cwd=BASE_DIR,
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return {
        "status": "running",
        "branch": branch,
        "hint": "Claude Code arbeitet jetzt im Worktree. Sag Manuel, dass du dich "
        "per Nachricht meldest, sobald es fertig ist (dauert Minuten). "
        "Status jederzeit mit check_dev_task.",
    }


def check_dev_task() -> dict[str, Any]:
    """Aktueller Status des Dev-Tasks + letzte Log-Zeilen."""
    task = _get_task()
    if not task:
        return {"status": "none", "hint": "Kein Dev-Task vorhanden."}
    result = dict(task)
    log = task.get("log")
    if log and Path(log).exists():
        lines = Path(log).read_text(errors="replace").splitlines()
        result["log_tail"] = lines[-20:]
    return result


def merge_dev_branch() -> dict[str, Any]:
    """Merged den fertigen Dev-Branch nach main — nur auf Manuels expliziten Befehl."""
    task = _get_task()
    if not task or task.get("status") != "done":
        return {
            "error": f"Kein fertiger Dev-Task zum Mergen (Status: {task.get('status') if task else 'none'})."
        }
    branch = task["branch"]
    merge = _git("merge", branch, "--no-edit")
    if merge.returncode != 0:
        _git("merge", "--abort")
        return {"error": f"Merge fehlgeschlagen (abgebrochen): {merge.stderr[-300:] or merge.stdout[-300:]}"}

    worktree = task.get("worktree")
    if worktree:
        _git("worktree", "remove", worktree, "--force")
    task["status"] = "merged"
    _set_task(task)
    return {
        "status": "merged",
        "branch": branch,
        "hint": "Gemergt. WICHTIG für Manuel: Damit die Änderungen live gehen, Bots neu "
        "starten: launchctl kickstart -k gui/$(id -u)/com.manuel.trainer.bot "
        "(und .assistant analog). Danach kann der Task-Eintrag mit einem neuen "
        "propose_dev_task überschrieben werden.",
    }


DEV_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "propose_dev_task",
        "description": "Formuliert einen Entwicklungsauftrag für das Trainer-System als Vorschlag. "
        "description muss ein vollständiger, kontextfreier Auftrag für Claude Code sein "
        "(Kontext: Python-Projekt 'trainer/', Telegram-Multi-Agent-System, uv, SQLite). "
        "Danach IMMER auf Manuels explizites Ja warten.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Kurztitel des Auftrags"},
                "description": {"type": "string", "description": "Vollständiger Entwicklungsauftrag"},
            },
            "required": ["title", "description"],
        },
    },
    {
        "name": "run_dev_task",
        "description": "Startet den zuvor vorgeschlagenen Dev-Task (Claude Code auf eigenem Git-Branch). "
        "NUR aufrufen, nachdem Manuel dem Vorschlag EXPLIZIT zugestimmt hat.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_dev_task",
        "description": "Status des laufenden/letzten Dev-Tasks inkl. Log-Ausschnitt.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "merge_dev_branch",
        "description": "Übernimmt den fertigen Dev-Branch nach main. NUR auf Manuels expliziten Befehl.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

DEV_TOOL_FUNCTIONS = {
    "propose_dev_task": propose_dev_task,
    "run_dev_task": run_dev_task,
    "check_dev_task": check_dev_task,
    "merge_dev_branch": merge_dev_branch,
}
