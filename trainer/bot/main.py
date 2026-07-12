"""Telegram-Bot-Einstiegspunkt für die Trainer-Agenten (Isa, Assistant).

Start: `uv run python -m trainer.bot.main [--agent isa|assistant]` (default: isa)

Long Polling, nur die in TELEGRAM_ALLOWED_CHAT_ID konfigurierte Chat-ID wird
bedient. Textnachrichten gehen an den Tool-Use-Agenten (trainer.agent.core),
.csv-Uploads werden über den bestehenden Strong-CSV-Importer (Phase 1)
eingelesen (nur beim Isa-Bot).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import logging
import re
import sys
from pathlib import Path

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
from trainer.ingest.strong_csv import import_csv

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TMP_DIR = Path("/private/tmp")
# Telegrams HARTE Obergrenze pro Nachricht — wird nie überschritten, dient nur
# als allerletzter Not-Schnitt, falls im ganzen Chunk keine gute Trennstelle
# (Absatz/Zeile/Wort) gefunden wird.
TELEGRAM_MAX_LEN = 4096
# Weicher Ziel-Schnittpunkt, deutlich unter TELEGRAM_MAX_LEN: der Bot schneidet
# lieber etwas früher an einer sauberen Stelle (Absatz > Zeile > Wortgrenze),
# statt das harte Limit zu riskieren und mitten im Wort oder mitten in einer
# **fett**-Markierung zu landen (das hätte mit aktiviertem Markdown-Parsing
# sonst "Can't parse entities" zur Folge und der Chunk würde verschluckt).
TELEGRAM_SPLIT_TARGET = 3500

# Isa/Assistant schreiben **bold** (CommonMark-Stil, siehe app.js
# renderInlineMarkdown fürs Web-UI — gleiche Konvention überall). Telegrams
# klassischer Markdown-Modus erwartet dagegen *bold* mit EINEM Stern.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)

START_MESSAGES: dict[str, str] = {
    "isa": (
        "Hey, ich bin Isa – dein Trainer & Health-Coach.\n\n"
        "Ich kann:\n"
        "- Trainingsfragen beantworten (Übungen, Hypertrophie, Recovery, Ernährung)\n"
        "- deine Oura- & Health-Daten auswerten (\"Wie war mein Schlaf diese Woche?\")\n"
        "- Workouts loggen (\"Bankdrücken 3x8 80kg\")\n"
        "- Strong-CSV-Exporte importieren (Datei einfach hier hochladen)\n\n"
        "Leg los, schreib mir einfach."
    ),
    "assistant": (
        "Hey, ich bin dein persönlicher Assistent.\n\n"
        "Ich kann:\n"
        "- Fragen zu deinem Kalender, Notizen und Health-Daten beantworten\n"
        "- proaktiv mitdenken (Termine, offene Punkte, nächste Schritte)\n"
        "- mir dauerhaft relevante Fakten über dich merken\n\n"
        "Für Training & Ernährung schreib Isa – ich bin der Rest.\n"
        "Leg los, schreib mir einfach."
    ),
}

UNAUTHORIZED_MESSAGE = "Dieser Bot ist privat eingerichtet und nicht für dich freigeschaltet."


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


def _split_for_telegram(text: str) -> list[str]:
    """Teilt `text` in Telegram-sichere Chunks — bevorzugt an einer sauberen
    Stelle (Absatz, sonst Zeile, sonst Wortgrenze) NAHE TELEGRAM_SPLIT_TARGET,
    nie später als TELEGRAM_MAX_LEN. So wird proaktiv vor dem harten Limit
    geschnitten, statt es zu erreichen — und nie mitten im Wort oder mitten in
    einer **fett**-Markierung, die sonst beim Markdown-Parsing brechen würde.
    """
    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_MAX_LEN:
        window = remaining[:TELEGRAM_SPLIT_TARGET]
        split_at = window.rfind("\n\n")
        if split_at < TELEGRAM_SPLIT_TARGET * 0.5:
            split_at = window.rfind("\n")
        if split_at < TELEGRAM_SPLIT_TARGET * 0.5:
            split_at = window.rfind(" ")
        if split_at < TELEGRAM_SPLIT_TARGET * 0.5:
            split_at = TELEGRAM_MAX_LEN  # keine brauchbare Trennstelle -> harter Schnitt
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _to_telegram_markdown(text: str) -> str:
    return _BOLD_RE.sub(r"*\1*", text)


async def _send_long_message(update: Update, text: str) -> None:
    if update.message is None:
        return
    if not text:
        return
    for chunk in _split_for_telegram(text):
        try:
            await update.message.reply_text(
                _to_telegram_markdown(chunk), parse_mode=ParseMode.MARKDOWN
            )
        except BadRequest:
            # Unbalancierte Markdown-Zeichen (z.B. ein einzelnes "*" in Isas
            # Text, das nicht als Formatierung gemeint war) dürfen die
            # Nachricht nicht verschlucken — lieber unformatiert zustellen
            # als den Chunk stillschweigend zu verlieren.
            logger.warning("Telegram-Markdown ungültig, sende Chunk unformatiert nach")
            await update.message.reply_text(chunk)


def _agent_name(context: ContextTypes.DEFAULT_TYPE) -> str:
    agent_def: AgentDef = context.bot_data["agent_def"]
    return agent_def.name


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    if update.message is not None:
        await update.message.reply_text(START_MESSAGES[_agent_name(context)])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    message = update.message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        reply = await asyncio.to_thread(run_agent, message.text, None, _agent_name(context))
    except Exception as exc:  # niemals crashen, immer eine Antwort schicken
        logger.exception("run_agent fehlgeschlagen")
        await message.reply_text(f"Da ist was schiefgelaufen: {exc}")
        return

    await _send_long_message(update, reply)


DEFAULT_PHOTO_CAPTION = "Hier ist ein Foto von meinem Essen."


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    message = update.message
    if message is None or not message.photo:
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    photo = message.photo[-1]  # größte verfügbare Auflösung
    text = message.caption or DEFAULT_PHOTO_CAPTION

    try:
        tg_file = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        image_bytes = buf.getvalue()

        reply = await asyncio.to_thread(
            run_agent, text, ("image/jpeg", image_bytes), _agent_name(context)
        )
    except Exception as exc:  # niemals crashen, immer eine Antwort schicken
        logger.exception("run_agent (Foto) fehlgeschlagen")
        await message.reply_text(f"Da ist was schiefgelaufen: {exc}")
        return

    await _send_long_message(update, reply)


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

    try:
        reply = await asyncio.to_thread(
            run_agent,
            f"[Sprachnachricht transkribiert] {transcript}",
            None,
            _agent_name(context),
        )
    except Exception as exc:  # niemals crashen, immer eine Antwort schicken
        logger.exception("run_agent (Voice) fehlgeschlagen")
        await message.reply_text(f"Da ist was schiefgelaufen: {exc}")
        return

    await _send_long_message(update, reply)


def _parse_import_summary(stdout_text: str) -> str:
    workouts_m = re.search(r"Workouts importiert:\s*(\d+)", stdout_text)
    sets_m = re.search(r"S(?:ä|ae)tze importiert:\s*(\d+)", stdout_text)
    skipped_m = re.search(r"Zeilen übersprungen.*?:\s*(\d+)", stdout_text)

    if workouts_m and sets_m and skipped_m:
        return (
            f"{workouts_m.group(1)} Workouts, {sets_m.group(1)} Sätze importiert, "
            f"{skipped_m.group(1)} Zeilen übersprungen."
        )
    # Fallback: rohe Ausgabe des Importers, falls das Format sich mal ändert.
    return stdout_text.strip() or "Import abgeschlossen (keine Zusammenfassung verfügbar)."


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_if_unauthorized(update):
        return
    message = update.message
    document = message.document if message is not None else None
    if document is None or not (document.file_name or "").lower().endswith(".csv"):
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    tmp_path = TMP_DIR / f"strong_import_{document.file_unique_id}.csv"
    try:
        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(custom_path=tmp_path)

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                import_csv(tmp_path)
        except SystemExit:
            # import_csv beendet sich per sys.exit(1) bei fehlenden Pflichtspalten.
            pass

        summary = _parse_import_summary(buf.getvalue())
        await message.reply_text(summary)
    except Exception as exc:
        logger.exception("CSV-Import fehlgeschlagen")
        await message.reply_text(f"Da ist was schiefgelaufen beim Import: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)


def build_application(agent_def: AgentDef) -> Application:
    if not agent_def.token:
        raise RuntimeError(
            f"Kein Bot-Token für Agent '{agent_def.name}' gesetzt "
            f"(config.{agent_def.token_config_attr}, siehe .env)."
        )

    app = Application.builder().token(agent_def.token).build()
    app.bot_data["agent_def"] = agent_def
    app.add_handler(CommandHandler("start", start_command))
    if agent_def.name == "isa":
        # CSV-Import (Strong-Export) ist ausschließlich Isas Job.
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Startet einen Trainer-Telegram-Bot.")
    parser.add_argument(
        "--agent",
        choices=["isa", "assistant"],
        default="isa",
        help="Welcher Agent bedient diesen Bot (default: isa).",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    agent_def = get_agent(args.agent)

    try:
        app = build_application(agent_def)
    except RuntimeError as exc:
        logger.error(str(exc))
        print(f"Fehler: {exc}", file=sys.stderr)
        sys.exit(1)

    logger.info("%s-Bot startet (Long Polling)...", agent_def.display_name)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
