"""Zentrale Konfiguration: lädt .env und stellt typed Zugriff auf alle Variablen bereit.

Variablennamen orientieren sich an .env.example. Fehlende, optionale Werte sind
leere Strings/None — Module, die sie brauchen (z.B. Oura-OAuth), validieren
selbst und geben klare Fehlermeldungen aus statt hier hart zu failen, damit
z.B. `init_db()` auch ohne vollständige .env funktioniert.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Projekt-Root = Verzeichnis, das dieses Paket enthält (eine Ebene über trainer/)
BASE_DIR = Path(__file__).resolve().parent.parent

# .env aus dem Projekt-Root laden (falls vorhanden). override=False lässt bereits
# gesetzte echte Umgebungsvariablen (z.B. in CI) Vorrang haben.
load_dotenv(BASE_DIR / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # --- Anthropic / Agent (Phase 2) ---
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    trainer_model: str = field(
        default_factory=lambda: _env("TRAINER_MODEL", "claude-sonnet-5")
    )

    # --- Telegram (Phase 2) ---
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_allowed_chat_id: str = field(
        default_factory=lambda: _env("TELEGRAM_ALLOWED_CHAT_ID")
    )

    # --- Telegram: zweiter Bot für den "assistant"-Agenten (Multi-Agent) ---
    assistant_bot_token: str = field(default_factory=lambda: _env("ASSISTANT_BOT_TOKEN"))

    # --- Oura OAuth2 ---
    oura_client_id: str = field(default_factory=lambda: _env("OURA_CLIENT_ID"))
    oura_client_secret: str = field(default_factory=lambda: _env("OURA_CLIENT_SECRET"))

    # --- Health Auto Export Webhook ---
    health_webhook_secret: str = field(
        default_factory=lambda: _env("HEALTH_WEBHOOK_SECRET")
    )

    # --- Datenbank ---
    db_path: Path = field(
        default_factory=lambda: Path(_env("DB_PATH", "data/trainer.db"))
    )

    # --- Google Kalender (read-only via geheime iCal-Links) ---
    # Kommaseparierte Liste von iCal-URLs; Whitespace um Kommas wird gestrippt,
    # leere Einträge werden ignoriert. Leer = kein Kalender konfiguriert.
    calendar_ics_urls: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            u.strip() for u in _env("CALENDAR_ICS_URLS").split(",") if u.strip()
        )
    )

    # --- Obsidian-Vault (read-only) ---
    obsidian_vault_path: str = field(
        default_factory=lambda: _env("OBSIDIAN_VAULT_PATH")
    )

    def __post_init__(self) -> None:
        # DB_PATH relativ zum Projekt-Root auflösen, falls nicht absolut
        db_path = self.db_path
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        object.__setattr__(self, "db_path", db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)


config = Config()
