"""Telegram-Versand für geplante Jobs (weekly_report, reminder_check).

Nutzt plain httpx statt python-telegram-bot: die Jobs laufen als eigenständige
CLI-Skripte ohne Application/Polling-Kontext, ein einfacher POST an die
Bot-API reicht für den reinen Versand.
"""

from __future__ import annotations

import httpx

from trainer.agents import get_agent
from trainer.config import config

TELEGRAM_MAX_LEN = 4096
API_BASE = "https://api.telegram.org"


def send_telegram(text: str, agent: str = "isa") -> None:
    """Schickt `text` an die konfigurierte Chat-ID, gesplittet bei 4096 Zeichen.

    `agent` wählt den Bot-Token aus der Agent-Registry (default "isa", das
    bisherige Verhalten für weekly_report/reminder_check bleibt unverändert).

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
