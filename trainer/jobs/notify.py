"""Telegram-Versand + Fehler-Wrapper für geplante Jobs.

`send_telegram` nutzt plain httpx statt python-telegram-bot: die Jobs laufen
als eigenständige CLI-Skripte ohne Application/Polling-Kontext, ein einfacher
POST an die Bot-API reicht für den reinen Versand.

`run_job` ist der Rahmen für jeden Job-/Ingest-Einstieg: Fehler landen als
⚠️-Nachricht bei Manuel statt nur in einem Logfile, das niemand liest
(Audit 2026-09: Pipeline stand drei Wochen still, ohne dass es jemand merkte).
"""

from __future__ import annotations

import logging
import sys
from typing import Callable

import httpx

from trainer.agents import get_agent
from trainer.config import config
from trainer.logging_setup import configure_logging

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096
API_BASE = "https://api.telegram.org"


def send_telegram(text: str, agent: str = "isa") -> None:
    """Schickt `text` an die konfigurierte Chat-ID, gesplittet bei 4096 Zeichen.

    Wirft bei fehlender Konfiguration (Token/Chat-ID) einen RuntimeError statt
    still zu tun, als sei alles gesendet worden — Jobs sollen sichtbar
    fehlschlagen statt Nachrichten stillschweigend zu verschlucken.
    """
    if not text:
        return

    agent_def = get_agent(agent)
    token = agent_def.token
    if not token:
        raise RuntimeError(
            f"Kein Bot-Token für Agent '{agent_def.name}' gesetzt "
            f"(config.{agent_def.token_config_attr}, siehe .env)."
        )
    if not config.telegram_allowed_chat_id:
        raise RuntimeError("TELEGRAM_ALLOWED_CHAT_ID ist nicht gesetzt (siehe .env).")

    url = f"{API_BASE}/bot{token}/sendMessage"
    with httpx.Client(timeout=30) as client:
        for i in range(0, len(text), TELEGRAM_MAX_LEN):
            chunk = text[i : i + TELEGRAM_MAX_LEN]
            resp = client.post(
                url,
                json={"chat_id": config.telegram_allowed_chat_id, "text": chunk},
            )
            resp.raise_for_status()


def run_job(name: str, fn: Callable[[], object]) -> None:
    """Führt `fn` aus; bei Exception: Log + ⚠️-Telegram + Exit-Code 1.

    Kann der Telegram-Versand selbst nicht (z.B. kein Netz), bleibt es beim
    Log — der Health-Check-Job meldet den Ausfall dann über die Sync-
    Zeitstempel nach.
    """
    configure_logging()
    logger.info("Job %s startet", name)
    try:
        fn()
    except SystemExit:
        raise
    except Exception as exc:
        logger.exception("Job %s fehlgeschlagen", name)
        try:
            send_telegram(f"⚠️ Job {name} fehlgeschlagen:\n{type(exc).__name__}: {exc}")
        except Exception:
            logger.exception("Fehler-Benachrichtigung für Job %s konnte nicht gesendet werden", name)
        sys.exit(1)
    logger.info("Job %s fertig", name)
