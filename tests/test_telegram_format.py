"""Telegram-Formatierung: HTML-Escaping + Chunking ohne zerrissene Fett-Markierung."""

from __future__ import annotations

from trainer.bot.main import (
    TELEGRAM_MAX_LEN,
    _split_for_telegram,
    _to_telegram_html,
)


def test_bold_and_escaping():
    assert _to_telegram_html("Leg_Press **3x8 <45kg>** & fertig") == (
        "Leg_Press <b>3x8 &lt;45kg&gt;</b> &amp; fertig"
    )


def test_unbalanced_stars_are_plain_text():
    assert _to_telegram_html("5*8 und **halb") == "5*8 und **halb"


def test_short_text_is_single_chunk():
    assert _split_for_telegram("kurz") == ["kurz"]


def test_split_never_cuts_inside_bold_and_respects_max_len():
    text = ("Absatz " * 400) + "**fett bleibt zusammen** " + ("x " * 2000)
    chunks = _split_for_telegram(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MAX_LEN
        assert chunk.count("**") % 2 == 0, "Fett-Markierung wurde zerrissen"
    assert "".join(c.strip() for c in chunks).replace(" ", "") == text.replace(" ", "")
