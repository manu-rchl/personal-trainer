"""Konsistenz zwischen Isa-Prompt, Tool-Subset und Tool-Registry.

Hintergrund: `query_notebooklm` stand monatelang im Prompt, war aber nie in
ISA_TOOL_NAMES — Isa hat ein Tool versprochen, das sie nicht hatte.
"""

from __future__ import annotations

import re

from trainer.agent.tools import TOOL_FUNCTIONS, TOOL_SCHEMAS
from trainer.agents import ATHLETE_PROFILE, DB_SCHEMA_OVERVIEW, ISA_TOOL_NAMES, get_agent

_SCHEMA_NAMES = {s["name"] for s in TOOL_SCHEMAS}
# Tool-Namen sind snake_case mit Unterstrich; das Regex fängt jede Erwähnung
# im Prompt-Text (auch "create_note/append_note/…"-Aufzählungen).
_IDENT_RE = re.compile(r"\b([a-z]+(?:_[a-z]+)+)\b")


def _rendered_isa_prompt() -> str:
    agent = get_agent("isa")
    return agent.system_prompt_template.format(
        athlete=ATHLETE_PROFILE, schema=DB_SCHEMA_OVERVIEW
    )


def test_isa_tool_names_exist_in_registry():
    missing_schema = set(ISA_TOOL_NAMES) - _SCHEMA_NAMES
    missing_fn = set(ISA_TOOL_NAMES) - set(TOOL_FUNCTIONS)
    assert not missing_schema, f"ohne Schema: {sorted(missing_schema)}"
    assert not missing_fn, f"ohne Implementierung: {sorted(missing_fn)}"


def test_every_tool_mentioned_in_prompt_is_available():
    prompt = _rendered_isa_prompt()
    mentioned = {m for m in _IDENT_RE.findall(prompt) if m in _SCHEMA_NAMES}
    assert "query_notebooklm" in mentioned  # Regressionsschutz für den Audit-Fund
    unavailable = mentioned - set(ISA_TOOL_NAMES)
    assert not unavailable, f"im Prompt erwähnt, aber nicht freigeschaltet: {sorted(unavailable)}"


def test_prompt_contains_weight_convention_and_honesty_rule():
    prompt = _rendered_isa_prompt()
    assert "Gewichts-Konvention" in prompt
    assert "EINER Seite" in prompt
    assert "Ehrlichkeit" in prompt
    assert "{athlete}" not in prompt and "{schema}" not in prompt
