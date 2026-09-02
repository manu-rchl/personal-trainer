"""Einheitliches Logging für alle Prozesse (Bot, Web, Jobs, Ingest).

launchd leitet stdout/stderr in Logdateien um — wir brauchen also keine
eigenen File-Handler, aber zwingend Zeitstempel: ohne die ist ein Logfile
nach Wochen nicht mehr auswertbar (Audit-Fund 2026-09).
"""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Idempotent: konfiguriert den Root-Logger einmal auf stderr mit Zeitstempel."""
    root = logging.getLogger()
    if getattr(root, "_trainer_configured", False):
        return
    logging.basicConfig(format=_FORMAT, level=level, stream=sys.stderr, force=True)
    # httpx loggt jeden Request auf INFO — das ist Rauschen in den Job-Logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root._trainer_configured = True  # type: ignore[attr-defined]
