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
import fcntl
import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import anthropic

from trainer.agent.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, get_profile
from trainer.agents import (
    ATHLETE_PROFILE,
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
# Tool-Results werden VOR dem Rücksenden ans Modell gekappt: get_workouts(days=365)
# oder eine große Notiz können sonst allein den Kontext sprengen — und bei bis
# zu 8 Iterationen addieren sich die Results.
MAX_TOOL_RESULT_CHARS = 12_000
# Im tool_log reicht ein Ausschnitt — es geht um "wurde es aufgerufen, kam ein
# Fehler", nicht um eine zweite Kopie der Daten.
TOOL_LOG_RESULT_CHARS = 4_000
# Bot (Telegram) und Web-Chat laufen als getrennte Prozesse auf derselben
# Historie — ohne Lock laden beide dieselbe Historie ohne die jeweils andere
# User-Nachricht und persistieren dann user,user,assistant,assistant.
AGENT_LOCK_TIMEOUT_S = 300

# Text, den run_agent liefert, wenn der Turn nicht sauber zu Ende kam. Wird
# bewusst NICHT persistiert — sonst steht "ich breche ab" dauerhaft als echte
# Isa-Antwort in der Historie.
ABORT_TEXT = (
    "Ich brauche dafür zu viele Schritte und breche ab – frag mich gern "
    "nochmal konkreter."
)
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


def persist_exchange(user_text: str, assistant_text: str, agent: str = "isa") -> None:
    """Schreibt ein User/Assistant-Paar in die Historie — für Jobs (Report,
    Reminder), die ohne echte User-Nachricht senden. Der synthetische
    User-Turn hält die Historie strikt alternierend (API-Anforderung) und
    macht im Verlauf sichtbar, dass die Nachricht vom System kam."""
    _persist_message("user", user_text, agent)
    _persist_message("assistant", assistant_text, agent)


def _log_tool_call(
    agent: str, tool: str, tool_input: dict[str, Any], result: Any, ok: bool
) -> None:
    """Schreibt einen Tool-Aufruf ins tool_log — Fehler hier dürfen den Turn nie killen."""
    try:
        conn = get_connection()
        try:
            conn.execute(
                "INSERT INTO tool_log (ts, agent, tool, input_json, result_json, ok) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    agent,
                    tool,
                    json.dumps(tool_input, ensure_ascii=False, default=str),
                    json.dumps(result, ensure_ascii=False, default=str)[:TOOL_LOG_RESULT_CHARS],
                    1 if ok else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # Logging ist Beiwerk — niemals den Agent-Turn abbrechen
        logger.exception("tool_log-Insert fehlgeschlagen (%s)", tool)


def _serialize_tool_result(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return (
        text[:MAX_TOOL_RESULT_CHARS]
        + f'\n… [gekürzt: Ergebnis hatte {len(text)} Zeichen, Limit {MAX_TOOL_RESULT_CHARS}. '
        "Frag mit engerem Zeitraum/Filter nochmal, falls du den Rest brauchst.]"
    )


@contextmanager
def _agent_lock(agent: str) -> Iterator[None]:
    """Prozessübergreifender Lock pro Agent (fcntl.flock auf data/.lock-<agent>)."""
    lock_path = config.db_path.parent / f".lock-{agent}"
    fd = open(lock_path, "w")
    deadline = time.monotonic() + AGENT_LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Agent '{agent}' ist seit {AGENT_LOCK_TIMEOUT_S}s in einem anderen "
                        "Prozess beschäftigt."
                    )
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()


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
    static_text = agent_def.system_prompt_template.format(
        athlete=ATHLETE_PROFILE, schema=DB_SCHEMA_OVERVIEW
    )

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

    Hält währenddessen den prozessübergreifenden Agent-Lock (Bot und Web-Chat
    dürfen nicht gleichzeitig auf derselben Historie arbeiten). Bei Lock-Timeout
    kommt eine freundliche Meldung statt einer Exception.
    """
    try:
        with _agent_lock(agent):
            return _run_agent_unlocked(user_message, image, agent)
    except TimeoutError as exc:
        logger.warning("Agent-Lock-Timeout [%s]: %s", agent, exc)
        return (
            "Ich bin gerade noch mit einer anderen Nachricht von dir beschäftigt – "
            "gib mir einen Moment und schick's dann nochmal."
        )


def _run_agent_unlocked(
    user_message: str, image: tuple[str, bytes] | None, agent: str
) -> str:
    """Eigentlicher Agent-Turn (ohne Lock — nur über run_agent aufrufen).

    `agent` wählt die AgentDef (System-Prompt + Tool-Subset) aus der Registry
    in `trainer.agents`. Lädt Kontext NUR aus der Historie dieses Agenten aus
    der DB, ruft die Anthropic-API mit dessen Tool-Subset auf, führt
    angeforderte Tools lokal aus (max. MAX_TOOL_ITERATIONS Runden, jeder Call
    landet im tool_log) und persistiert sowohl die Nutzer-Nachricht als auch
    die finale Antwort mit Agent-Zuordnung.

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

    # max_retries 3 (SDK-Default 2): 429/5xx werden vom SDK mit Backoff
    # wiederholt; ein Wert darüber lässt einen einzelnen Turn minutenlang hängen.
    client = anthropic.Anthropic(api_key=config.anthropic_api_key, max_retries=3)
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
            # Nach den SDK-Retries immer noch 429 — Nachricht nicht persistieren,
            # Manuel kann sie gleich nochmal schicken.
            logger.warning("Anthropic-Rate-Limit erreicht [%s]: %s", agent_def.name, exc)
            return "Bin kurz am Anthropic-Rate-Limit – probier's in ein, zwei Minuten nochmal."
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
            tool_input = block.input or {}
            if tool_fn is None:
                result: Any = {"error": f"Unbekanntes Tool: {block.name}"}
            else:
                try:
                    result = tool_fn(**tool_input)
                except Exception as exc:  # Tool-Fehler an das Modell zurückgeben, nicht crashen
                    logger.exception("Tool-Fehler in %s", block.name)
                    result = {"error": f"Tool-Fehler in {block.name}: {exc}"}
            ok = not (isinstance(result, dict) and "error" in result)
            _log_tool_call(agent_def.name, block.name, tool_input, result, ok)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": _serialize_tool_result(result),
                }
            )

        messages.append({"role": "user", "content": tool_results})
    else:
        # Iterationslimit erreicht, ohne finale Text-Antwort — nicht persistieren.
        logger.warning("Iterationslimit erreicht [%s]", agent_def.name)
        return ABORT_TEXT

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
