"""Wöchentliche Memory-Konsolidierung (So 18:30) — Vorschlag, Manuel bestätigt.

Usage:
    uv run python -m trainer.jobs.memory_review [--dry-run]

Ein Anthropic-Call OHNE Tools über alle Memories + Athleten-Steckbrief:
Duplikate, Widersprüche (auch zum Steckbrief/Profil), Veraltetes, Kandidaten
fürs Pinnen. Ergebnis ist ein JSON-Vorschlag, der als lesbarer Text per
Telegram geht und in `sync_state["memory_review_pending"]` liegt. Bestätigt
Manuel im Chat, wendet Isa ihn mit update/delete/save_memory an und ruft
clear_memory_review auf (Prompt-Regel in agents.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import date
from typing import Any

import anthropic

from trainer.agent.core import persist_exchange
from trainer.agents import ATHLETE_PROFILE
from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.jobs.notify import run_job, send_telegram

logger = logging.getLogger(__name__)

PENDING_KEY = "memory_review_pending"
SYNTHETIC_USER_TURN = "[System: wöchentlicher Memory-Review]"
# Großzügig: das Modell denkt vor der JSON-Antwort mit (Thinking-Blöcke zählen
# gegen max_tokens) — mit 1500 kam nur Thinking und kein Text zurück.
MAX_TOKENS = 8000

SYSTEM_PROMPT = """Du bist die Gedächtnis-Pflege für "Isa", einen persönlichen Fitness-Coach.
Du bekommst den festen Athleten-Steckbrief, das Profil und ALLE Memories (mit #id).
Finde: (a) Duplikate/Überschneidungen → zusammenfassen, (b) Widersprüche zum Steckbrief,
Profil oder untereinander → korrigieren oder löschen, (c) Veraltetes (überholt, einmalige
Ereignisse ohne Dauerrelevanz, Test-Einträge) → löschen, (d) Kernfakten, die IMMER im Prompt
stehen sollten (Verletzungen, Konventionen, feste Ziele) → pinnen.
Sei konservativ: lieber wenige, klar begründete Änderungen als viele. Was schon im
Steckbrief steht, muss NICHT zusätzlich gepinnt werden.

Antworte NUR mit JSON in dieser Form (keine Erklärung außerhalb):
{"delete": [{"id": 12, "reason": "…"}],
 "merge": [{"ids": [3, 7], "category": "gesundheit", "content": "…zusammengefasst…"}],
 "update": [{"id": 5, "content": "…korrigiert…", "reason": "…"}],
 "pin": [{"id": 9, "reason": "…"}],
 "summary": "1-2 Sätze, was du insgesamt vorschlägst"}
Leere Listen sind erlaubt. Wenn nichts zu tun ist: alle Listen leer, summary "Alles konsistent"."""


def load_memories(conn) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, ts, category, content, source, pinned FROM memories ORDER BY id"
        ).fetchall()
    ]


def build_user_message(memories: list[dict[str, Any]], profile: dict[str, str]) -> str:
    lines = [
        "ATHLETEN-STECKBRIEF (fix im Prompt):",
        ATHLETE_PROFILE,
        "",
        "PROFIL:",
        *(f"- {k}: {v}" for k, v in profile.items()),
        "",
        f"MEMORIES ({len(memories)}):",
    ]
    for m in memories:
        flags = " [gepinnt]" if m.get("pinned") else ""
        src = f" (Quelle: {m['source']})" if m.get("source") else ""
        lines.append(f"#{m['id']} [{m['category']}] {m['content']}{src}{flags}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Kein JSON in der Antwort: {text[:200]}")
    return json.loads(text[start : end + 1])


def propose(memories: list[dict[str, Any]], profile: dict[str, str]) -> dict[str, Any]:
    client = anthropic.Anthropic(api_key=config.anthropic_api_key, max_retries=3)
    resp = client.messages.create(
        model=config.trainer_model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(memories, profile)}],
    )
    text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    proposal = _extract_json(text)
    for key in ("delete", "merge", "update", "pin"):
        proposal.setdefault(key, [])
    return proposal


def render_proposal(proposal: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> str:
    def snippet(mid: int) -> str:
        m = by_id.get(mid)
        return (m["content"][:70] + "…") if m and len(m["content"]) > 70 else (m["content"] if m else "?")

    lines = ["🧠 Memory-Review (Vorschlag):", proposal.get("summary", "").strip(), ""]
    for d in proposal["delete"]:
        lines.append(f"🗑 #{d['id']} löschen — {d.get('reason', '')}\n   „{snippet(d['id'])}“")
    for m in proposal["merge"]:
        ids = ", ".join(f"#{i}" for i in m.get("ids", []))
        lines.append(f"🔗 {ids} zusammenfassen zu:\n   „{m.get('content', '')[:160]}“")
    for u in proposal["update"]:
        lines.append(f"✏️ #{u['id']} korrigieren — {u.get('reason', '')}\n   neu: „{u.get('content', '')[:160]}“")
    for p in proposal["pin"]:
        lines.append(f"📌 #{p['id']} pinnen — {p.get('reason', '')}")
    if not any(proposal[k] for k in ("delete", "merge", "update", "pin")):
        return ""
    lines += ["", "Antworte mit „ok Memories“ (alles übernehmen) oder sag, was anders soll."]
    return "\n".join(lines)


def run(dry_run: bool = False) -> None:
    conn = get_connection()
    try:
        memories = load_memories(conn)
        profile = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM profile")}
    finally:
        conn.close()
    if not memories:
        logger.info("Keine Memories — nichts zu reviewen.")
        return

    proposal = propose(memories, profile)
    text = render_proposal(proposal, {m["id"]: m for m in memories})
    if not text:
        logger.info("Memory-Review: nichts zu tun (%s).", proposal.get("summary"))
        return
    if dry_run:
        print(json.dumps(proposal, ensure_ascii=False, indent=1))
        print(text)
        return

    send_telegram(text)
    persist_exchange(SYNTHETIC_USER_TURN, text, agent="isa")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PENDING_KEY, json.dumps({"date": date.today().isoformat(), "proposal": proposal}, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Memory-Review-Vorschlag gesendet.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trainer.jobs.memory_review")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    init_db()
    run_job("memory-review", lambda: run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
