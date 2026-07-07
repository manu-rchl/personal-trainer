"""Namens-Normalisierung für Übungen (App-Wechsel Strong -> Hevy etc.).

Reine, testbare Funktionen ohne DB-Zugriff — DB-Werte (alle Rohnamen,
manuelle Alias-Overrides) werden von außen reingereicht. Ziel: Varianten
wie "Sitting Leg Extensions" und "leg extensions" einer gemeinsamen Gruppe
zuordnen, ohne echt unterschiedliche Übungen ("seated calf raise" vs.
"standing calf raise") fälschlich zu mergen.
"""

from __future__ import annotations

import re
from collections import Counter

_PUNCTUATION_RE = re.compile(r"[.,()/\-]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """lowercase, trim, Satzzeichen -> Space, Mehrfach-Whitespace -> ein Space."""
    s = (name or "").strip().lower()
    s = _PUNCTUATION_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def _display_name(raw_names: list[str]) -> str:
    """Häufigste Original-Schreibweise einer Gruppe (bei Gleichstand: erstes Vorkommen)."""
    counts = Counter(raw_names)
    best_count = max(counts.values())
    for raw in raw_names:
        if counts[raw] == best_count:
            return raw
    return raw_names[0]


def canonicalize(name: str, all_names: list[str], alias_map: dict[str, str]) -> str:
    """Liefert den kanonischen ANZEIGE-Namen für `name`.

    (1) Wenn der normalisierte Name in `alias_map` steht -> dessen Ziel
        (manueller Override, z.B. via merge_exercises).
    (2) Sonst Teilmengen-Heuristik: unter allen ANDEREN in `all_names`
        vorkommenden (normalisierten) Übungsnamen den kürzesten finden,
        dessen Token-Menge eine ECHTE Teilmenge der eigenen Token-Menge ist
        -> dieser kürzere Name ist kanonisch. Nur echte Superset-Beziehungen
        zählen, keine bloße Wort-Überlappung (sonst würden z.B. "seated calf
        raise" und "standing calf raise" fälschlich zusammenfallen).
        Bei mehreren Kandidaten: kürzeste Token-Zahl, dann häufigster,
        dann kürzeste Zeichenlänge.
    (3) Sonst der normalisierte Name selbst (als eigene Gruppe).

    Der Rückgabewert ist die häufigste Original-Schreibweise der gewählten
    Gruppe, nicht die lowercase-Form.
    """
    normalized = normalize_name(name)
    if not normalized:
        return name.strip()

    if normalized in alias_map:
        return alias_map[normalized]

    groups: dict[str, list[str]] = {}
    for raw in all_names:
        n = normalize_name(raw)
        if not n:
            continue
        groups.setdefault(n, []).append(raw)

    own_tokens = frozenset(normalized.split())

    candidates: list[str] = []
    for other_norm in groups:
        if other_norm == normalized:
            continue
        other_tokens = frozenset(other_norm.split())
        if other_tokens and other_tokens < own_tokens:  # echte Teilmenge
            candidates.append(other_norm)

    if candidates:
        candidates.sort(key=lambda c: (len(c.split()), -len(groups[c]), len(c)))
        chosen = candidates[0]
        return _display_name(groups[chosen])

    if normalized in groups:
        return _display_name(groups[normalized])

    return normalized
