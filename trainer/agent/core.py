"""Tool-Use-Loop für die Trainer-Agenten (Isa, Assistant).

Nutzt das offizielle `anthropic`-Python-SDK direkt (kein claude-agent-sdk,
Architektur-Entscheidung wg. Portabilität). `run_agent()` ist die einzige
öffentliche Schnittstelle: nimmt eine Nutzer-Nachricht entgegen, lädt Kontext
(letzte Nachrichten des jeweiligen Agenten + Profil) aus der DB, führt den
Tool-Use-Loop mit dem Tool-Subset des Agenten aus und persistiert User- und
Assistant-Turn (mit Agent-Zuordnung).

Agent-Definitionen (System-Prompt, Tool-Subset, Bot-Token) leben in
`trainer.agents`, nicht hier.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import anthropic

from trainer.agent.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, get_profile
from trainer.agents import DB_SCHEMA_OVERVIEW, AgentDef, get_agent
from trainer.config import config
from trainer.db import get_connection, init_db

MEMORY_INLINE_LIMIT = 100
MEMORY_INLINE_RECENT = 50

MAX_TOOL_ITERATIONS = 8
MAX_TOKENS = 1500
HISTORY_LIMIT = 30


def _load_history(agent: str, limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    """Lädt die letzten `limit` Nachrichten NUR dieses Agenten (getrennte Historie)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE role IN ('user', 'assistant') AND agent = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (agent, limit),
        ).fetchall()
    finally:
        conn.close()

    # Neueste zuerst geladen -> für die API-Reihenfolge umdrehen (chronologisch).
    ordered = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in ordered]


def _persist_message(role: str, content: str, agent: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO messages (ts, role, content, agent) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), role, content, agent),
        )
        conn.commit()
    finally:
        conn.close()


def _load_memories_for_prompt() -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        limit = total if total < MEMORY_INLINE_LIMIT else MEMORY_INLINE_RECENT
        rows = conn.execute(
            "SELECT category, content FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {"total": total, "rows": [dict(r) for r in rows]}
    finally:
        conn.close()


def _build_memories_text() -> str:
    data = _load_memories_for_prompt()
    total = data["total"]
    rows = data["rows"]

    if total == 0:
        return "(noch keine Memories gespeichert)"

    lines = [f"- [{r['category']}] {r['content']}" for r in rows]
    text = "\n".join(lines)

    if total >= MEMORY_INLINE_LIMIT:
        text += (
            f"\n\n({total} Memories insgesamt — hier nur die {MEMORY_INLINE_RECENT} "
            "neuesten. Nutze search_memories für ältere oder gezielte Suche.)"
        )
    return text


def _build_system_prompt(agent_def: AgentDef) -> str:
    profile = get_profile()["profile"]
    profile_text = (
        "\n".join(f"- {k}: {v}" for k, v in profile.items())
        if profile
        else "(noch kein Profil hinterlegt)"
    )
    # Lokale Zeit — der Agent soll Manuels Kalendertag kennen, nicht den UTC-Tag.
    today = datetime.now().date().isoformat()
    memories_text = _build_memories_text()
    return agent_def.system_prompt_template.format(
        today=today, schema=DB_SCHEMA_OVERVIEW, profile=profile_text, memories=memories_text
    )


def _extract_text(content_blocks: list[Any]) -> str:
    parts = []
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def run_agent(
    user_message: str, image: tuple[str, bytes] | None = None, agent: str = "isa"
) -> str:
    """Führt eine Runde des angegebenen Agenten aus und liefert die finale Antwort.

    `agent` wählt die AgentDef (System-Prompt + Tool-Subset) aus der Registry
    in `trainer.agents` (default "isa"). Lädt Kontext NUR aus der Historie
    dieses Agenten aus der DB, ruft die Anthropic-API mit dessen Tool-Subset
    auf, führt angeforderte Tools lokal aus (max. MAX_TOOL_ITERATIONS Runden)
    und persistiert sowohl die Nutzer-Nachricht als auch die finale Antwort
    mit Agent-Zuordnung. Das Langzeit-Gedächtnis (memories) bleibt zwischen
    allen Agenten geteilt (siehe `_build_memories_text`).

    `image`, falls gesetzt, ist ein (media_type, raw_bytes)-Tupel (z.B. ein
    Essens-Foto). Das Bild wird nur für den aktuellen API-Call verwendet — in
    der messages-Tabelle landet nie Base64-Bilddaten, nur Text plus ein
    Platzhalter ("[Foto gesendet]"), damit die geladene Historie immer aus
    reinen Strings besteht.
    """
    init_db()

    agent_def = get_agent(agent)
    tool_schemas = [s for s in TOOL_SCHEMAS if s["name"] in agent_def.tool_names]
    tool_functions = {
        name: fn for name, fn in TOOL_FUNCTIONS.items() if name in agent_def.tool_names
    }

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    system_prompt = _build_system_prompt(agent_def)

    messages: list[dict[str, Any]] = _load_history(agent_def.name)

    if image is not None:
        media_type, raw_bytes = image
        content_blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                },
            },
            {"type": "text", "text": user_message},
        ]
        messages.append({"role": "user", "content": content_blocks})
    else:
        messages.append({"role": "user", "content": user_message})

    final_text = ""

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            response = client.messages.create(
                model=config.trainer_model,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=tool_schemas,
                messages=messages,
            )
        except anthropic.APIError as exc:
            raise Exception(f"Anthropic-API-Fehler: {exc}") from exc

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            final_text = _extract_text(response.content)
            break

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_fn = tool_functions.get(block.name)
            if tool_fn is None:
                result: Any = {"error": f"Unbekanntes Tool: {block.name}"}
            else:
                try:
                    result = tool_fn(**(block.input or {}))
                except Exception as exc:  # Tool-Fehler an das Modell zurückgeben, nicht crashen
                    result = {"error": f"Tool-Fehler in {block.name}: {exc}"}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )

        messages.append({"role": "user", "content": tool_results})
    else:
        # Iterationslimit erreicht, ohne finale Text-Antwort
        final_text = (
            "Ich brauche dafür zu viele Schritte und breche ab – frag mich gern "
            "nochmal konkreter."
        )

    if not final_text:
        final_text = "Dazu ist mir gerade keine Antwort eingefallen – frag nochmal anders."

    persisted_user_text = user_message
    if image is not None:
        persisted_user_text = (
            f"{user_message}\n\n[Foto gesendet]" if user_message else "[Foto gesendet]"
        )

    _persist_message("user", persisted_user_text, agent_def.name)
    _persist_message("assistant", final_text, agent_def.name)

    return final_text
