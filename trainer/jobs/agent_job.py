"""Gemeinsamer Rahmen für agent-getriebene Jobs (Morgen-Check, Post-Workout,
Wochenreport, Reminder-Text).

Ablauf: Instruction (beginnt mit `[System: …]`) → `run_agent(persist=False)`
→ Antwort prüfen → wenn nicht `NO_MESSAGE`/Fehlertext: per Telegram senden und
als User/Assistant-Paar persistieren, damit Isa im Chat weiß, was sie von
sich aus geschickt hat.
"""

from __future__ import annotations

import logging
import re

from trainer.agent.core import ABORT_TEXT, persist_exchange, run_agent
from trainer.jobs.notify import send_telegram

logger = logging.getLogger(__name__)

NO_MESSAGE = "NO_MESSAGE"
_NO_MESSAGE_RE = re.compile(r"^\W*NO_MESSAGE\W*$", re.IGNORECASE)
_FAILURE_PREFIXES = (
    ABORT_TEXT,
    "Bin kurz am Anthropic-Rate-Limit",
    "Ich bin gerade noch mit einer anderen Nachricht",
    "Dazu ist mir gerade keine Antwort eingefallen",
)


class AgentJobFailed(RuntimeError):
    """Der Agent hat keine brauchbare Antwort geliefert (Rate-Limit, Abbruch)."""


def is_no_message(reply: str) -> bool:
    return bool(_NO_MESSAGE_RE.match(reply or ""))


def run_agent_job(name: str, instruction: str, *, dry_run: bool = False) -> str | None:
    """Führt einen Agent-Turn aus und verschickt die Antwort.

    Liefert den gesendeten Text, oder None bei `NO_MESSAGE`. Wirft
    AgentJobFailed bei Rate-Limit/Abbruch-Antworten — run_job macht daraus
    eine ⚠️-Nachricht.
    """
    reply = run_agent(instruction, agent="isa", persist=False)
    if is_no_message(reply):
        logger.info("[%s] Agent: NO_MESSAGE — nichts gesendet.", name)
        return None
    if any(reply.startswith(p) for p in _FAILURE_PREFIXES):
        raise AgentJobFailed(f"{name}: Agent-Turn nicht erfolgreich: {reply[:160]}")

    if dry_run:
        print(f"--- [{name}] DRY-RUN, würde senden:\n{reply}\n---")
        return reply

    send_telegram(reply)
    persist_exchange(instruction, reply, agent="isa")
    logger.info("[%s] gesendet (%d Zeichen).", name, len(reply))
    return reply
