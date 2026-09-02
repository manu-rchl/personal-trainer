"""Telegram-Bot-Einstiegspunkt für den Trainer-Agenten Isa.

Start: `uv run python -m trainer.bot.main`

Long Polling, nur die in TELEGRAM_ALLOWED_CHAT_ID konfigurierte Chat-ID wird
bedient. Text-, Foto- und Sprachnachrichten gehen an den Tool-Use-Agenten
(trainer.agent.core).

Betriebs-Details:
- Antworten werden als Telegram-HTML gesendet (`**fett**` -> <b>, Rest
  escaped) — der alte Legacy-Markdown-Modus brach bei jedem `_` in
  Übungsnamen.
- Ein Agent-Turn hat ein hartes Timeout (AGENT_TIMEOUT_S); solange läuft ein
  Typing-Indikator, damit der Bot nicht "tot" wirkt.
- Alle HEARTBEAT_INTERVAL_S schreibt der Bot sync_state["bot_heartbeat"];
  der Health-Check-Job meldet, wenn der Heartbeat ausbleibt.
- Konfigurationsfehler beim Start beenden mit Exit-Code 0 (launchd startet
  dann NICHT neu — ein fehlender Token löst keinen Restart-Storm mehr aus).
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, TypeVar

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from trainer.agent.core import run_agent
from trainer.agents import AgentDef, get_agent
from trainer.bot.transcribe import transcribe_ogg
from trainer.config import config
from trainer.db import get_connection, init_db
from trainer.logging_setup import configure_logging

logger = logging.getLogger(__name__)

T = TypeVar("T")

TMP_DIR = Path(tempfile.gettempdir())
# Telegrams HARTE Obergrenze pro Nachricht — wird nie überschritten, dient nur
# als allerletzter Not-Schnitt, falls im ganzen Chunk keine gute Trennstelle
# (Absatz/Zeile/Wort) gefunden wird.
TELEGRAM_MAX_LEN = 4096
# Weicher Ziel-Schnittpunkt, deutlich unter TELEGRAM_MAX_LEN: HTML-Escaping
# (&amp; &lt; <b>) verlängert den Text noch etwas, und wir wollen an einer
# sauberen Stelle schneiden (Absatz > Zeile > Wortgrenze).
TELEGRAM_SPLIT_TARGET = 3500

AGENT_TIMEOUT_S = 240
TYPING_REFRESH_S = 4  # Telegram zeigt "schreibt…" nur ~5 s pro send_chat_action
HEARTBEAT_INTERVAL_S = 300

# Isa schreibt **bold** (CommonMark-Stil, gleiche Konvention wie app.js
# renderInlineMarkdown im Web-UI).
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

START_MESSAGE = (
    "Hey, ich bin Isa – dein Trainer & Health-Coach.\n\n"
    "Ich kann:\n"
    "- Trainingsfragen beantworten (Übungen, Hypertrophie, Recovery, Ernährung)\n"
    "- deine Oura- & Hevy-Daten auswerten (\"Wie war mein Schlaf diese Woche?\")\n"
    "- Workouts und Mahlzeiten loggen (Text, Foto oder Sprachnachricht)\n\n"
    "Leg los, schreib mir einfach."
)

UNAUTHORIZED_MESSAGE = "Dieser Bot ist privat eingerichtet und nicht für dich freigeschaltet."
TIMEOUT_MESSAGE = (
    "Das hat zu lange gedauert und ich hab abgebrochen – frag mich gern nochmal, "
    "am besten etwas konkreter."
)


# ---------------------------------------------------------------------------
# Zugriff
# ---------------------------------------------------------------------------


def _is_allowed(chat_id: int) -> bool:
    allowed = config.telegram_allowed_chat_id
    return bool(allowed) and str(chat_id) == str(allowed)


async def _reject_if_unauthorized(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None or _is_allowed(chat.id):
        return False
    logger.warning("Unauthorized chat_id=%s versucht Zugriff", chat.id if chat else None)
    if update.message is not None:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)
    return True


# ---------------------------------------------------------------------------
# Formatierung / Chunking
# ---------------------------------------------------------------------------


def _split_for_telegram(text: str) -> list[str]:
    """Teilt `text` in Telegram-sichere Chunks — bevorzugt an einer sauberen
    Stelle (Absatz, sonst Zeile, sonst Wortgrenze) NAHE TELEGRAM_SPLIT_TARGET,
    nie später als TELEGRAM_MAX_LEN, und nie innerhalb einer **fett**-Markierung
    (sonst bricht das HTML-Parsing des Chunks).
    """
    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_SPLIT_TARGET:
        window = remaining[:TELEGRAM_SPLIT_TARGET]
        split_at = window.rfind("\n\n")
        if split_at < TELEGRAM_SPLIT_TARGET * 0.5:
            split_at = window.rfind("\n")
        if split_at < TELEGRAM_SPLIT_TARGET * 0.5:
            split_at = window.rfind(" ")
        if split_at < TELEGRAM_SPLIT_TARGET * 0.5:
            split_at = TELEGRAM_SPLIT_TARGET  # keine brauchbare Trennstelle -> harter Schnitt

        # Ungerade Anzahl "**" im Chunk = wir würden mitten in einer Fett-
        # Markierung schneiden -> vor deren Anfang schneiden.
        head = remaining[:split_at]
        if head.count("**") % 2 == 1:
            last_open = head.rfind("**")
            if last_open > 0:
                split_at = last_open

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _to_telegram_html(text: str) -> str:
    """`**fett**` -> <b>fett</b>, alles andere HTML-escaped.

    _BOLD_RE.split liefert abwechselnd [normal, fett, normal, fett, …] — die
    ungeraden Indizes sind die Capture-Gruppen.
    """
    parts = _BOLD_RE.split(text)
    out: list[str] = []
    for i, part in enumerate(parts):
        escaped = html.escape(part, quote=False)
        out.append(f"<b>{escaped}</b>" if i % 2 == 1 else escaped)
    return "".join(out)


async def _send_long_message(update: Update, text: str) -> None:
    if update.message is None or not text:
        return
    for chunk in _split_for_telegram(text):
        try:
            await update.message.reply_text(_to_telegram_html(chunk), parse_mode=ParseMode.HTML)
        except BadRequest:
            # Sollte mit Escaping nicht mehr vorkommen — aber lieber unformatiert
            # zustellen als den Chunk stillschweigend zu verlieren.
            logger.warning("Telegram-HTML ungültig, sende Chunk unformatiert nach")
            await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Agent-Aufruf mit Timeout + Typing-Indikator
# ---------------------------------------------------------------------------


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:  # Typing ist Kosmetik — nie den Turn abbrechen
            pass
        await asyncio.sleep(TYPING_REFRESH_S)


async def _run_with_typing(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, coro: Awaitable[T]
) -> T:
    typing_task = asyncio.create_task(_keep_typing(context, chat_id))
    try:
        return await asyncio.wait_for(coro, timeout=AGENT_TIMEOUT_S)
    finally:
        typing_task.cancel()


async def _agent_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    image: tuple[str, bytes] | None = None,
) -> None:
    message = update.message
    if message is None:
        return
    chat_id = update.effective_chat.id
    try:
        reply = await _run_with_typing(
            context, chat_id, asyncio.to_thread(run_agent, text, image, _agent_name(context))
        )
    except asyncio.TimeoutError:
        logger.error("run_agent Timeout nach %ss", AGENT_TIMEOUT_S)
        await message.reply_text(TIMEOUT_MESSAGE)
        return
    except Exception as exc:  # niemals crashen, immer eine Antwort schicken
        logger.exception("run_agent fehlgeschlagen")
        await message.reply_text(f"Da ist was schiefgelaufen: {exc}")
        return
    await _send_long_message(update, reply)


def _agent_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    agent_def: AgentDef = context.bot_data["agent_def"]
    return agent_def.name


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    if update.message is not None:
        await update.message.reply_text(START_MESSAGE)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    message = update.message
    if message is None or not message.text:
        return
    await _agent_reply(update, context, message.text)


DEFAULT_PHOTO_CAPTION = "Hier ist ein Foto von meinem Essen."


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    message = update.message
    if message is None or not message.photo:
        return

    photo = message.photo[-1]  # größte verfügbare Auflösung
    text = message.caption or DEFAULT_PHOTO_CAPTION
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        image_bytes = buf.getvalue()
    except Exception as exc:
        logger.exception("Foto-Download fehlgeschlagen")
        await message.reply_text(f"Da ist was schiefgelaufen beim Foto: {exc}")
        return

    await _agent_reply(update, context, text, ("image/jpeg", image_bytes))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    message = update.message
    voice_or_audio = (message.voice or message.audio) if message is not None else None
    if message is None or voice_or_audio is None:
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    tmp_path = TMP_DIR / f"voice_{voice_or_audio.file_unique_id}.ogg"
    try:
        tg_file = await context.bot.get_file(voice_or_audio.file_id)
        await tg_file.download_to_drive(custom_path=tmp_path)
        transcript = await asyncio.to_thread(transcribe_ogg, tmp_path)
    except Exception as exc:  # niemals crashen, immer eine Antwort schicken
        logger.exception("Transkription fehlgeschlagen")
        await message.reply_text(f"Da ist was schiefgelaufen bei der Transkription: {exc}")
        return
    finally:
        tmp_path.unlink(missing_ok=True)

    if not transcript:
        await message.reply_text(
            "Ich konnte leider nichts verstehen – kannst du das nochmal sagen oder tippen?"
        )
        return

    await message.reply_text(f'🎙 Verstanden: "{transcript}"')
    await _agent_reply(update, context, f"[Sprachnachricht transkribiert] {transcript}")


# ---------------------------------------------------------------------------
# Heartbeat + Application
# ---------------------------------------------------------------------------


def _write_heartbeat() -> None:
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES ('bot_heartbeat', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    finally:
        conn.close()


async def _heartbeat_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_write_heartbeat)
        except Exception:
            logger.exception("Heartbeat konnte nicht geschrieben werden")
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def _post_init(app: Application) -> None:
    # Referenz im bot_data halten, sonst wird der Task vom GC eingesammelt.
    app.bot_data["heartbeat_task"] = asyncio.create_task(_heartbeat_loop())


def build_application(agent_def: AgentDef) -> Application:
    if not agent_def.token:
        raise RuntimeError(
            f"Kein Bot-Token für Agent '{agent_def.name}' gesetzt "
            f"(config.{agent_def.token_config_attr}, siehe .env)."
        )
    if not config.telegram_allowed_chat_id:
        raise RuntimeError("TELEGRAM_ALLOWED_CHAT_ID ist nicht gesetzt (siehe .env).")

    app = Application.builder().token(agent_def.token).post_init(_post_init).build()
    app.bot_data["agent_def"] = agent_def
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main() -> None:
    configure_logging()
    init_db()
    agent_def = get_agent("isa")

    try:
        app = build_application(agent_def)
    except RuntimeError as exc:
        # Exit 0 mit Absicht: launchd (KeepAlive SuccessfulExit=false) startet
        # bei Exit 0 NICHT neu — ein Konfigurationsfehler ist kein Crash, den
        # ein Neustart beheben könnte.
        logger.error("Bot startet nicht: %s", exc)
        return

    logger.info("%s-Bot startet (Long Polling)...", agent_def.display_name)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
