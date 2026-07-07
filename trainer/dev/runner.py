"""Detached Runner für Dev-Tasks: führt Claude Code im Worktree aus.

Wird von trainer.agent.dev_tasks.run_dev_task() als eigenständiger Prozess
gestartet (start_new_session) und überlebt damit Bot-Neustarts. Meldet das
Ergebnis per Telegram über den Assistant-Bot.

Sicherheitsmodell: arbeitet NUR im Git-Worktree des Tasks (nie im Live-Repo),
Claude Code läuft mit --permission-mode acceptEdits (Datei-Edits erlaubt,
alles andere bleibt gesperrt). Von Manuel explizit freigegeben (2026-07-07).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from trainer.agent.dev_tasks import PENDING_KEY, _claude_bin, _get_task, _set_task

TIMEOUT_SECONDS = 20 * 60


def _notify(text: str) -> None:
    try:
        from trainer.jobs.notify import send_telegram

        try:
            send_telegram(text, agent="assistant")
        except TypeError:
            # Fallback, falls notify noch keinen agent-Parameter kennt
            send_telegram(text)
    except Exception as exc:  # Benachrichtigung darf das Ergebnis nie zerstören
        print(f"Telegram-Benachrichtigung fehlgeschlagen: {exc}")


def main() -> None:
    task = _get_task()
    if not task or task.get("status") != "running":
        print("Kein laufender Dev-Task gefunden — Abbruch.")
        return

    worktree = Path(task["worktree"])
    branch = task["branch"]
    description = task["description"]
    title = task.get("title", "Dev-Task")

    print(f"[runner] Starte Claude Code für '{title}' in {worktree}")
    started = time.time()

    try:
        proc = subprocess.run(
            [
                _claude_bin(),
                "-p",
                description,
                "--permission-mode",
                "acceptEdits",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        claude_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        print(claude_output)
        failed = proc.returncode != 0
    except subprocess.TimeoutExpired:
        claude_output = f"TIMEOUT nach {TIMEOUT_SECONDS // 60} Minuten"
        print(claude_output)
        failed = True

    # Änderungen im Worktree committen (falls Claude nicht selbst committet hat)
    subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"Dev-Task: {title}\n\nCo-Authored-By: Claude <noreply@anthropic.com>"],
        cwd=worktree,
        capture_output=True,
    )

    diffstat = subprocess.run(
        ["git", "diff", "main...HEAD", "--stat"],
        cwd=worktree,
        capture_output=True,
        text=True,
    ).stdout.strip()
    diff_summary = "\n".join(diffstat.splitlines()[-6:]) if diffstat else "(keine Änderungen)"

    minutes = round((time.time() - started) / 60, 1)
    task["status"] = "failed" if failed else "done"
    task["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _set_task(task)

    if failed:
        _notify(
            f"❌ Dev-Task '{title}' fehlgeschlagen ({minutes} Min).\n"
            f"Letzte Ausgabe:\n{claude_output[-500:]}\n\n"
            f"Branch {branch} bleibt zum Anschauen bestehen."
        )
    else:
        _notify(
            f"✅ Dev-Task '{title}' fertig ({minutes} Min).\n\n"
            f"Änderungen auf Branch {branch}:\n{diff_summary}\n\n"
            f"Sag mir 'merge es', wenn ich es übernehmen soll — oder schau erst rein."
        )
    print(f"[runner] Fertig, Status: {task['status']}")


if __name__ == "__main__":
    main()
