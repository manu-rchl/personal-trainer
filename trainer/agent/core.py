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
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

from trainer.agent.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, get_profile
from trainer.agents import (
    DB_SCHEMA_OVERVIEW,
    DYNAMIC_CONTEXT_TEMPLATE,
    AgentDef,
    get_agent,
)
from trainer.config import config
from trainer.db import get_connection

logger = logging.getLogger(__name__)

MEMORY_INLINE_LIMIT = 100
MEMORY_INLINE_RECENT = 50

MAX_TOOL_ITERATIONS = 8
# War 1500 — zu knapp für Isas ausführlichere Antworten (Trainingspläne,
# Erklärungen): das Modell stoppte mitten im Satz (stop_reason="max_tokens"),
# und der abgeschnittene Text wurde unmarkiert als fertige Antwort behandelt.
# Telegram-seitiges Chunking (TELEGRAM_MAX_LEN in bot/main.py) ist davon
# unabhängig und bleibt unverändert nötig für sehr lange Antworten.
MAX_TOKENS = 4096

# Historien-Fenster für Prompt Caching: Caching ist ein Präfix-Match — ein
# strikt gleitendes Fenster (jeder Turn verliert vorne eine Nachricht) würde
# den Cache des gesamten Verlaufs bei jedem Turn invalidieren. Stattdessen
# wächst das Fenster von HISTORY_MIN bis knapp unter HISTORY_MIN+HISTORY_STEP
# und springt dann blockweise nach vorn: Der Fensteranfang bleibt über viele
# Turns identisch, nur der (einmalige) Sprung kostet einen Cache-Miss.
# MIN/STEP bewusst auf 30/10 statt vorher 20/20 — bei 20/20 flog beim Sprung
# die HÄLFTE des gesamten Fensters (20 von 20 Nachrichten) auf einen Schlag
# raus, das fühlte sich wie plötzliche Amnesie an ("versteht Kontext nicht
# mehr"). Bei 30/10 bleibt ein größeres Basis-Fenster erhalten und pro Sprung
# gehen nur 10 von 30 (ein Drittel) verloren — sanfter, seltener spürbar.
HISTORY_MIN = 30
HISTORY_STEP = 10


def _load_history(agent: str) -> list[dict[str, Any]]:
    """Lädt die Historie NUR dieses Agenten als cache-stabiles Fenster."""
    conn = get_connection()
    try:
        total = conn.execute(
            """
            SELECT COUNT(*) AS n FROM messages
            WHERE role IN ('user', 'assistant') AND agent = ?
            """,
            (agent,),
        ).fetchone()["n"]

        offset = 0
        if total > HISTORY_MIN:
            offset = ((total - HISTORY_MIN) // HISTORY_STEP) * HISTORY_STEP

        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE role IN ('user', 'assistant') AND agent = ?
            ORDER BY id ASC
            LIMIT -1 OFFSET ?
            """,
            (agent, offset),
        ).fetchall()
    finally:
        conn.close()

    msgs = [{"role": r["role"], "content": r["content"]} for r in rows]
    # Die API verlangt, dass die erste Nachricht ein User-Turn ist — falls das
    # Fenster mitten in einem Paar beginnt, führende assistant-Turns verwerfen.
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


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


def _build_system_blocks(agent_def: AgentDef) -> list[dict[str, Any]]:
    """Baut den System-Prompt als zwei Blöcke für Prompt Caching.

    Block 1 ist byte-stabil (Persona + statisches DB-Schema) und trägt den
    cache_control-Breakpoint — zusammen mit den davor gerenderten Tool-Schemas
    bildet er den festen, gecachten Präfix. Alles Dynamische (Datum, Profil,
    Memories) steht in Block 2 NACH dem Breakpoint, damit es den Cache des
    stabilen Teils nicht invalidiert.
    """
    static_text = agent_def.system_prompt_template.format(schema=DB_SCHEMA_OVERVIEW)

    profile = get_profile()["profile"]
    profile_text = (
        "\n".join(f"- {k}: {v}" for k, v in profile.items())
        if profile
        else "(noch kein Profil hinterlegt)"
    )
    # Lokale Zeit — der Agent soll Manuels Kalendertag kennen, nicht den UTC-Tag.
    today = datetime.now().date().isoformat()
    dynamic_text = DYNAMIC_CONTEXT_TEMPLATE.format(
        today=today, profile=profile_text, memories=_build_memories_text()
    )

    return [
        {
            "type": "text",
            "text": static_text,
            # 1h statt Standard-5min: Telegram-Nutzung ist sporadisch (Minuten bis
            # Stunden zwischen Nachrichten). Bei 5min TTL verfällt der Cache
            # zwischen den meisten Nachrichten, sodass praktisch jede Nachricht
            # einen vollpreisigen Cache-Write auslöst — und Cache-Writes zählen
            # (anders als Cache-Reads) voll gegen das ITPM-Rate-Limit.
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
        {"type": "text", "text": dynamic_text},
    ]


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

    agent_def = get_agent(agent)
    tool_schemas = [s for s in TOOL_SCHEMAS if s["name"] in agent_def.tool_names]
    tool_functions = {
        name: fn for name, fn in TOOL_FUNCTIONS.items() if name in agent_def.tool_names
    }

    # max_retries erhöht (SDK-Default: 2): Der Account läuft aktuell auf dem
    # Anthropic Free Tier (5 RPM/10K ITPM) — ein Tool-Loop mit mehreren
    # Iterationen kann das knapp bemessene RPM-Budget allein ausschöpfen.
    # Das SDK retried 429s automatisch mit Backoff und respektiert den
    # retry-after-Header; mehr Retries geben einzelnen Turns eine bessere
    # Chance, trotz des engen Limits durchzukommen statt hart zu failen.
    client = anthropic.Anthropic(api_key=config.anthropic_api_key, max_retries=5)
    system_blocks = _build_system_blocks(agent_def)

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
                # Auto-Caching: setzt den Breakpoint automatisch auf den letzten
                # cachebaren Block — er wandert so mit jedem Turn/jeder
                # Tool-Iteration ans Ende des wachsenden Verlaufs. 1h TTL aus
                # demselben Grund wie beim System-Block oben.
                cache_control={"type": "ephemeral", "ttl": "1h"},
                system=system_blocks,
                tools=tool_schemas,
                messages=messages,
            )
        except anthropic.RateLimitError as exc:
            logger.warning("Anthropic-Rate-Limit erreicht [%s]: %s", agent_def.name, exc)
            detail = str(exc)
            # ITPM-Fehler ("input tokens per minute") heißen: diese EINE Anfrage
            # ist für sich allein schon größer als das Free-Tier-Limit (10K/Min).
            # Warten behebt das NICHT — das braucht entweder ein Tier-Upgrade
            # (console.anthropic.com/settings/billing) oder einen kleineren Prompt
            # (kürzere Historie/Tool-Schemas). Reine RPM-Fehler ("requests per
            # minute") sind dagegen meist transient und lösen sich von selbst.
            if "input tokens per minute" in detail or "tokens per minute" in detail:
                return (
                    "Anthropic-Rate-Limit: Diese Anfrage ist allein schon größer als "
                    "das Free-Tier-Limit (10K Input-Tokens/Minute) — das behebt sich "
                    "NICHT durchs Warten. Manuel muss entweder auf "
                    "console.anthropic.com/settings/billing eine Zahlungsmethode "
                    "hinterlegen (höheres Limit) oder Claude Code bitten, den Prompt "
                    "zu verkleinern."
                )
            return (
                "Bin kurz am Anthropic-Rate-Limit (Free Tier) – probier's in ein, "
                "zwei Minuten nochmal."
            )
        except anthropic.APIError as exc:
            raise Exception(f"Anthropic-API-Fehler: {exc}") from exc

        usage = response.usage
        logger.info(
            "Anthropic-Usage [%s]: input=%d cache_read=%s cache_creation=%s output=%d",
            agent_def.name,
            usage.input_tokens,
            usage.cache_read_input_tokens,
            usage.cache_creation_input_tokens,
            usage.output_tokens,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            final_text = _extract_text(response.content)
            if response.stop_reason == "max_tokens":
                # Trotz MAX_TOKENS=4096 gekappt — sichtbar loggen statt die
                # abgeschnittene Antwort stillschweigend als vollständig zu
                # behandeln (genau das war die alte, unsichtbare Ursache für
                # "Nachrichten werden abgeschnitten").
                logger.warning(
                    "Antwort bei MAX_TOKENS gekappt [%s], %d Output-Tokens",
                    agent_def.name,
                    usage.output_tokens,
                )
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
