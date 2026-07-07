"""Tool-Use-Loop für Isa, den persönlichen Trainer-Agenten.

Nutzt das offizielle `anthropic`-Python-SDK direkt (kein claude-agent-sdk,
Architektur-Entscheidung wg. Portabilität). `run_agent()` ist die einzige
öffentliche Schnittstelle: nimmt eine Nutzer-Nachricht entgegen, lädt Kontext
(letzte Nachrichten + Profil) aus der DB, führt den Tool-Use-Loop aus und
persistiert User- und Assistant-Turn.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

import anthropic

from trainer.agent.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS, get_profile
from trainer.config import config
from trainer.db import get_connection, init_db

MEMORY_INLINE_LIMIT = 100
MEMORY_INLINE_RECENT = 50

MAX_TOOL_ITERATIONS = 8
MAX_TOKENS = 1500
HISTORY_LIMIT = 30

DB_SCHEMA_OVERVIEW = """
- oura_daily(date, kind, payload_json, sleep_score, readiness_score, activity_score,
  hrv_avg, resting_hr, sleep_duration_min, steps) — PRIMARY KEY (date, kind)
- health_metrics(source, metric, ts, value, unit) — generische Apple-Health-Datenpunkte
- workouts(id, date, type, source, notes) — source ist 'strong_csv', 'chat' oder 'apple_health'
- workout_sets(workout_id, exercise, set_no, reps, weight_kg)
- profile(key, value) — Ziele, Gewicht, Präferenzen
- messages(id, ts, role, content) — Chat-Historie
- sync_state(key, value) — interne Sync-Metadaten (nicht relevant für Trainer-Fragen)
- memories(id, ts, category, content) — Langzeit-Gedächtnis über Manuel (siehe save_memory/search_memories)
""".strip()

SYSTEM_PROMPT_TEMPLATE = """Du bist "Isa", Manuels persönlicher Fitness-Trainer & Health-Coach.

Ton: Direkt, motivierend, Kumpel-Ton (du duzt Manuel). Wissenschaftlich fundiert
(Hypertrophie-Training, Recovery, Ernährung) – aber keine Vorlesung, sondern
knackige, actionable Antworten. Du schreibst für Telegram: kurze Absätze, KEINE
Markdown-Tabellen, sparsame Emojis (höchstens vereinzelt, nicht in jeder Zeile).

Nutze deine Tools statt zu raten – wenn dir Daten fehlen oder ein Tool nichts
liefert, sag das ehrlich statt zu erfinden. Für Standardfragen (Health-Überblick,
Workouts, Profil, Logging) nutze die spezialisierten Tools. Nur wenn die nicht
reichen, greif mit query_db (nur SELECT) direkt auf die DB zu.

Wenn Manuel ein Essens-Foto schickt: Analysiere das Gericht, schätze Portionsgröße
und Makros (kcal, Protein, Carbs, Fett) mit realistischen Zahlen, logge die
Mahlzeit über log_meal und gib eine kurze Einschätzung, ob sie zu seinen Zielen
passt. Bei Fotos, die kein Essen zeigen: beschreib kurz, was zu sehen ist, frag
nach, was er damit will, und logge nichts.

Du lernst Manuel aktiv kennen: Wenn im Gespräch dauerhaft relevante Fakten über
ihn auftauchen (Job/Alltag, Verletzungen, Vorlieben, Gewohnheiten, Ziele,
wichtige Lebensumstände), speichere sie unaufgefordert mit save_memory – kurz
und faktisch, keine Duplikate zu bereits bekannten Memories. Isa ist nicht nur
Fitness-Trainer, sondern kennt Manuel als Person.

Mit get_calendar siehst du Manuels Termine (Google Kalender, read-only) und
kannst Gym-Slots passend um Arbeit/Termine herum vorschlagen. Mit search_notes
und read_note kannst du in Manuels persönlichen Notizen (Obsidian) suchen,
wenn es hilft, ihn zu verstehen oder Fragen zu beantworten.

Heutiges Datum: {today}

DB-Schema (SQLite):
{schema}

Nutzerprofil (aktueller Stand):
{profile}

Was du bereits über Manuel weißt (Langzeit-Gedächtnis):
{memories}
"""


def _load_history(limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    # Neueste zuerst geladen -> für die API-Reihenfolge umdrehen (chronologisch).
    ordered = list(reversed(rows))
    return [{"role": r["role"], "content": r["content"]} for r in ordered]


def _persist_message(role: str, content: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO messages (ts, role, content) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), role, content),
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


def _build_system_prompt() -> str:
    profile = get_profile()["profile"]
    profile_text = (
        "\n".join(f"- {k}: {v}" for k, v in profile.items())
        if profile
        else "(noch kein Profil hinterlegt)"
    )
    # Lokale Zeit — Isa soll Manuels Kalendertag kennen, nicht den UTC-Tag.
    today = datetime.now().date().isoformat()
    memories_text = _build_memories_text()
    return SYSTEM_PROMPT_TEMPLATE.format(
        today=today, schema=DB_SCHEMA_OVERVIEW, profile=profile_text, memories=memories_text
    )


def _extract_text(content_blocks: list[Any]) -> str:
    parts = []
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def run_agent(user_message: str, image: tuple[str, bytes] | None = None) -> str:
    """Führt eine Runde des Trainer-Agenten aus und liefert die finale Antwort.

    Lädt Kontext aus der DB, ruft die Anthropic-API mit Tool-Use auf, führt
    angeforderte Tools lokal aus (max. MAX_TOOL_ITERATIONS Runden) und
    persistiert sowohl die Nutzer-Nachricht als auch die finale Antwort.

    `image`, falls gesetzt, ist ein (media_type, raw_bytes)-Tupel (z.B. ein
    Essens-Foto). Das Bild wird nur für den aktuellen API-Call verwendet — in
    der messages-Tabelle landet nie Base64-Bilddaten, nur Text plus ein
    Platzhalter ("[Foto gesendet]"), damit die geladene Historie immer aus
    reinen Strings besteht.
    """
    init_db()

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    system_prompt = _build_system_prompt()

    messages: list[dict[str, Any]] = _load_history()

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
                tools=TOOL_SCHEMAS,
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
            tool_fn = TOOL_FUNCTIONS.get(block.name)
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

    _persist_message("user", persisted_user_text)
    _persist_message("assistant", final_text)

    return final_text
