"""Lokale Sprachnachrichten-Transkription über faster-whisper.

Kein ffmpeg-Zwang: faster-whisper dekodiert Audio (inkl. OGG/Opus, wie
Telegram-Sprachnachrichten) direkt über PyAV, das FFmpeg-Bibliotheken bündelt
statt einen `ffmpeg`-Systembefehl vorauszusetzen.

Das Whisper-Modell wird lazy als Modul-Singleton geladen — der erste Aufruf
lädt bzw. lädt beim allerersten Mal das Modell herunter, das darf dauern.
Danach bleibt es für die Prozesslaufzeit im Speicher (ein Bot-Prozess läuft
dauerhaft via launchd).
"""

from __future__ import annotations

from pathlib import Path

from faster_whisper import WhisperModel

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    return _model


def transcribe_ogg(path: Path) -> str:
    """Transkribiert eine Audiodatei (z.B. Telegram-Sprachnachricht, OGG/Opus).

    Nutzt das deutsche Sprachmodell explizit (language="de"). Gibt den
    zusammengefügten, getrimmten Text aller erkannten Segmente zurück.
    """
    model = _get_model()
    segments, _info = model.transcribe(str(path), language="de")
    return " ".join(segment.text.strip() for segment in segments).strip()
