"""Oura API v2 Ingestion — OAuth2 Server-Side Flow + Daily-Sync.

Usage:
    uv run python -m trainer.ingest.oura auth
    uv run python -m trainer.ingest.oura sync [--days N]

Auth-Flow:
    1. Lokaler HTTP-Server auf http://localhost:8484/oura/callback
    2. Browser öffnet https://cloud.ouraring.com/oauth/authorize
    3. Nutzer bestätigt, Oura redirected mit ?code=... zurück
    4. Code wird gegen access_token/refresh_token getauscht (POST /oauth/token)
    5. Tokens landen in der Tabelle `secrets` (Keys: oura_access_token,
       oura_refresh_token, oura_token_expires_at) — NICHT in sync_state, damit
       query_db sie dem Modell nie zeigt.

WICHTIG: Oura-Refresh-Tokens sind SINGLE-USE. Bei jedem Refresh liefert die API
einen neuen refresh_token zurück — dieser wird SOFORT und in EINER Transaktion
persistiert, bevor der Rest der Response weiterverarbeitet wird (siehe
refresh_access_token()). Schlägt der Refresh fehl (Token von Oura invalidiert),
geht eine Telegram-Nachricht raus, statt still im Log zu sterben.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs

import httpx

from trainer.config import config
from trainer.db import get_connection, init_db

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/"

SCOPES = "daily heartrate workout session personal"

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 8484
# Muss exakt der in der Oura-App registrierten Redirect-URI entsprechen.
CALLBACK_PATH = "/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"

SK_ACCESS_TOKEN = "oura_access_token"
SK_REFRESH_TOKEN = "oura_refresh_token"
SK_EXPIRES_AT = "oura_token_expires_at"


# --------------------------------------------------------------------------
# sync_state / secrets Helfer
# --------------------------------------------------------------------------


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def _get_secret(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM secrets WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _save_tokens(conn: sqlite3.Connection, token_response: dict[str, Any]) -> None:
    """Persistiert access_token, refresh_token und expiry SOFORT — atomar.

    Muss aufgerufen werden, bevor der Aufrufer irgendetwas anderes mit der
    Response macht — Oura-Refresh-Tokens sind single-use, ein Absturz danach
    darf den neuen Token nicht verlieren. Alle drei Keys in EINER Transaktion,
    sonst könnte ein Crash mittendrin neuen Access- mit altem Refresh-Token
    kombinieren.
    """
    access_token = token_response["access_token"]
    refresh_token = token_response["refresh_token"]
    expires_in = int(token_response.get("expires_in", 86400))
    expires_at = time.time() + expires_in

    conn.executemany(
        "INSERT INTO secrets (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [
            (SK_ACCESS_TOKEN, access_token),
            (SK_REFRESH_TOKEN, refresh_token),
            (SK_EXPIRES_AT, str(expires_at)),
        ],
    )
    conn.commit()


# --------------------------------------------------------------------------
# OAuth2 Auth-Flow
# --------------------------------------------------------------------------


class _CallbackResult:
    code: str | None = None
    state: str | None = None
    error: str | None = None


def _make_handler(expected_state: str, result: _CallbackResult):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib API)
            parsed = urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return

            params = parse_qs(parsed.query)
            error = params.get("error", [None])[0]
            code = params.get("code", [None])[0]
            state = params.get("state", [None])[0]

            if error:
                result.error = error
            elif state != expected_state:
                result.error = "state_mismatch"
            else:
                result.code = code
                result.state = state

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result.error:
                body = f"<html><body><h1>Oura-Autorisierung fehlgeschlagen</h1><p>{result.error}</p></body></html>"
            else:
                body = "<html><body><h1>Oura-Autorisierung erfolgreich</h1><p>Du kannst dieses Fenster schließen.</p></body></html>"
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # stdout ruhig halten

    return Handler


def auth() -> None:
    if not config.oura_client_id or not config.oura_client_secret:
        print(
            "FEHLER: OURA_CLIENT_ID / OURA_CLIENT_SECRET fehlen in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = secrets.token_urlsafe(24)
    result = _CallbackResult()

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _make_handler(state, result))

    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.oura_client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
        }
    )
    authorize_url = f"{AUTHORIZE_URL}?{query}"

    print(f"Öffne Browser für Oura-Autorisierung:\n{authorize_url}\n")
    webbrowser.open(authorize_url)

    print(f"Warte auf Callback auf {REDIRECT_URI} ...")
    server.timeout = 300
    while result.code is None and result.error is None:
        server.handle_request()

    if result.error:
        print(f"FEHLER bei der Autorisierung: {result.error}", file=sys.stderr)
        sys.exit(1)

    print("Code erhalten, tausche gegen Tokens...")
    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": result.code,
            "redirect_uri": REDIRECT_URI,
            "client_id": config.oura_client_id,
            "client_secret": config.oura_client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    token_response = resp.json()

    conn = get_connection()
    try:
        _save_tokens(conn, token_response)
    finally:
        conn.close()

    print("Oura-Autorisierung abgeschlossen. Tokens in `secrets` gespeichert.")


# --------------------------------------------------------------------------
# Token-Refresh
# --------------------------------------------------------------------------


REAUTH_HINT = "Bitte `uv run python -m trainer.ingest.oura auth` ausführen."


class OuraAuthExpired(RuntimeError):
    """Refresh-Token ungültig/abgelaufen — nur ein neuer `auth`-Lauf hilft."""


def refresh_access_token(conn: sqlite3.Connection) -> str:
    """Tauscht den aktuellen (single-use) refresh_token gegen ein neues Paar.

    Persistiert das Ergebnis SOFORT (siehe _save_tokens), bevor der neue
    access_token an den Aufrufer zurückgegeben wird. Ein 400/401 von Oura
    heißt: Refresh-Token ist verbraucht oder widerrufen → OuraAuthExpired.
    """
    refresh_token = _get_secret(conn, SK_REFRESH_TOKEN)
    if not refresh_token:
        raise OuraAuthExpired(f"Kein Oura-Refresh-Token vorhanden. {REAUTH_HINT}")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.oura_client_id,
            "client_secret": config.oura_client_secret,
        },
        timeout=30,
    )
    if resp.status_code in (400, 401):
        raise OuraAuthExpired(
            f"Oura-Token-Refresh abgelehnt (HTTP {resp.status_code}). {REAUTH_HINT}"
        )
    resp.raise_for_status()
    token_response = resp.json()

    # Single-use refresh_token: sofort persistieren, bevor irgendetwas anderes passiert.
    _save_tokens(conn, token_response)

    return token_response["access_token"]


def ensure_access_token(conn: sqlite3.Connection) -> str:
    access_token = _get_secret(conn, SK_ACCESS_TOKEN)
    expires_at_raw = _get_secret(conn, SK_EXPIRES_AT)

    if not access_token:
        raise OuraAuthExpired(f"Kein Oura-Access-Token vorhanden. {REAUTH_HINT}")

    expires_at = float(expires_at_raw) if expires_at_raw else 0.0
    if time.time() >= expires_at - 60:  # kleiner Sicherheitspuffer
        access_token = refresh_access_token(conn)

    return access_token


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

ENDPOINTS = {
    "sleep": "usercollection/daily_sleep",
    "readiness": "usercollection/daily_readiness",
    "activity": "usercollection/daily_activity",
    # Detaillierte Schlafphasen: liefert die echten Rohwerte (average_hrv,
    # lowest_heart_rate, total_sleep_duration), die die daily_*-Endpoints
    # nicht enthalten. Naps werden übersprungen (siehe _extract_fields).
    "sleep_detail": "usercollection/sleep",
}


def _fetch_all(client: httpx.Client, endpoint: str, start_date: str, end_date: str) -> list[dict]:
    records: list[dict] = []
    params = {"start_date": start_date, "end_date": end_date}
    next_token: str | None = None

    while True:
        req_params = dict(params)
        if next_token:
            req_params["next_token"] = next_token
        resp = client.get(API_BASE + endpoint, params=req_params)
        if resp.status_code == 401:
            raise _Unauthorized()
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("data", []))
        next_token = data.get("next_token")
        if not next_token:
            break

    return records


class _Unauthorized(Exception):
    pass


def _extract_fields(kind: str, record: dict) -> dict:
    """Extrahiert die für den jeweiligen kind relevanten Spalten; Rest bleibt NULL."""
    fields: dict[str, Any] = {
        "sleep_score": None,
        "readiness_score": None,
        "activity_score": None,
        "hrv_avg": None,
        "resting_hr": None,
        "sleep_duration_min": None,
        "steps": None,
    }

    score = record.get("score")
    if kind == "sleep":
        fields["sleep_score"] = score
    elif kind == "readiness":
        fields["readiness_score"] = score
        # temperature_deviation/contributors sind Score-Werte (0-100), keine
        # Rohdaten für HRV/RHR — daher hier bewusst NULL gelassen.
    elif kind == "activity":
        fields["activity_score"] = score
        fields["steps"] = record.get("steps")
    elif kind == "sleep_detail":
        fields["hrv_avg"] = record.get("average_hrv")
        fields["resting_hr"] = record.get("lowest_heart_rate")
        duration_s = record.get("total_sleep_duration")
        if duration_s is not None:
            fields["sleep_duration_min"] = round(duration_s / 60.0, 1)

    return fields


def upsert_oura_daily(conn: sqlite3.Connection, kind: str, record: dict) -> None:
    date = record.get("day")
    if not date:
        return

    # usercollection/sleep liefert mehrere Perioden pro Tag (Naps etc.) —
    # nur der Haupt-Schlaf ("long_sleep") zählt; PK (date, kind) hält 1 Zeile/Tag.
    if kind == "sleep_detail" and record.get("type") not in (None, "long_sleep"):
        return

    fields = _extract_fields(kind, record)

    conn.execute(
        """
        INSERT OR REPLACE INTO oura_daily (
            date, kind, payload_json,
            sleep_score, readiness_score, activity_score,
            hrv_avg, resting_hr, sleep_duration_min, steps
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            kind,
            json.dumps(record),
            fields["sleep_score"],
            fields["readiness_score"],
            fields["activity_score"],
            fields["hrv_avg"],
            fields["resting_hr"],
            fields["sleep_duration_min"],
            fields["steps"],
        ),
    )


def sync(days: int = 7) -> None:
    """Holt die letzten `days` Tage aller ENDPOINTS und upsertet sie.

    Wirft OuraAuthExpired, wenn die Tokens nicht mehr zu retten sind — der
    Aufrufer (main → run_job) macht daraus eine Telegram-Nachricht.
    """
    conn = get_connection()
    try:
        access_token = ensure_access_token(conn)

        end_date = time.strftime("%Y-%m-%d")
        start_date = time.strftime(
            "%Y-%m-%d", time.localtime(time.time() - days * 86400)
        )

        headers = {"Authorization": f"Bearer {access_token}"}
        total = 0
        refreshed = False  # höchstens EIN Refresh pro Lauf (Refresh-Tokens sind single-use)

        with httpx.Client(headers=headers, timeout=30) as client:
            for kind, endpoint in ENDPOINTS.items():
                try:
                    records = _fetch_all(client, endpoint, start_date, end_date)
                except _Unauthorized:
                    if refreshed:
                        raise OuraAuthExpired(
                            f"Weiterhin 401 nach Token-Refresh. {REAUTH_HINT}"
                        )
                    print("401 erhalten, versuche Token-Refresh...")
                    access_token = refresh_access_token(conn)
                    refreshed = True
                    client.headers["Authorization"] = f"Bearer {access_token}"
                    records = _fetch_all(client, endpoint, start_date, end_date)

                for record in records:
                    upsert_oura_daily(conn, kind, record)
                    total += 1

                print(f"{kind}: {len(records)} Datensätze ({start_date} .. {end_date})")

        conn.commit()
        _set_state(conn, "oura_last_sync", str(time.time()))
        print(f"Fertig. {total} Datensätze upserted.")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m trainer.ingest.oura")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="OAuth2-Autorisierung durchführen")

    sync_parser = sub.add_parser("sync", help="Daily-Daten synchronisieren")
    sync_parser.add_argument(
        "--days", type=int, default=7, help="Anzahl Tage rückwirkend (default 7)"
    )

    args = parser.parse_args()
    init_db()

    if args.command == "auth":
        auth()
    elif args.command == "sync":
        sync(days=args.days)


if __name__ == "__main__":
    main()
