"""Withings OAuth2 token-chain management.

This module treats the database singleton row as the only source of truth for the shared
Withings token chain. Refreshes are serialized with a transaction-scoped advisory lock so
hourly CronJobs and manual backfills cannot race, and refreshed token pairs are committed
before the new access token is ever returned because Withings rotates refresh tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import requests

from collector.config import Config

WITHINGS_TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
EXPIRY_MARGIN_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 30


class WithingsAuthError(Exception):
    """Chain problems. Messages may include Withings' numeric status and HTTP codes but must
    NEVER include token values."""


@dataclass(frozen=True, slots=True)
class _TokenChain:
    access_token: str
    refresh_token: str
    access_expires_at: datetime


def get_access_token(conn: psycopg.Connection, cfg: Config) -> str:
    """Return a currently usable Withings access token, refreshing the shared chain if needed."""
    try:
        with conn.cursor() as cur:
            chain = _load_token_chain(cur)

        if _is_fresh(chain.access_expires_at):
            conn.commit()
            return chain.access_token

        # Advisory locks are transaction-scoped, so the stale-read transaction must be closed
        # before starting the serialized refresh transaction.
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('withings_oauth'))")

            # Another pod may have refreshed while this pod was waiting on the lock.
            chain = _load_token_chain(cur)
            if _is_fresh(chain.access_expires_at):
                conn.commit()
                return chain.access_token

            client_id, client_secret = _require_client_credentials(cfg)
            access_token, refresh_token, expires_in = _refresh_token_pair(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=chain.refresh_token,
            )
            access_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            cur.execute(
                """
                UPDATE ops.withings_oauth_token
                SET access_token = %s,
                    refresh_token = %s,
                    access_expires_at = %s,
                    refreshed_at = now(),
                    refresh_count = refresh_count + 1
                WHERE singleton = true
                """,
                (access_token, refresh_token, access_expires_at),
            )
            if cur.rowcount != 1:
                raise WithingsAuthError(
                    "Withings token chain row disappeared during refresh update"
                )

            # Withings keeps the old refresh token valid until the new access token is first
            # used, so persisting the rotated pair first preserves crash safety.
            conn.commit()
            return access_token
    except Exception:
        _safe_rollback(conn)
        raise


def _load_token_chain(cur: psycopg.Cursor[Any]) -> _TokenChain:
    cur.execute(
        """
        SELECT access_token, refresh_token, access_expires_at
        FROM ops.withings_oauth_token
        WHERE singleton = true
        """
    )
    row = cur.fetchone()
    if row is None:
        raise WithingsAuthError(
            "no Withings token chain in ops.withings_oauth_token; run the workstation bootstrap"
        )

    if not isinstance(row, (tuple, list)) or len(row) != 3:
        raise WithingsAuthError(
            "invalid Withings token chain row in ops.withings_oauth_token"
        )

    access_token, refresh_token, access_expires_at = row
    if not isinstance(access_token, str) or access_token == "":
        raise WithingsAuthError(
            "invalid Withings token chain row in ops.withings_oauth_token"
        )
    if not isinstance(refresh_token, str) or refresh_token == "":
        raise WithingsAuthError(
            "invalid Withings token chain row in ops.withings_oauth_token"
        )
    if not _is_aware_datetime(access_expires_at):
        raise WithingsAuthError(
            "invalid Withings token chain row in ops.withings_oauth_token"
        )

    return _TokenChain(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=access_expires_at,
    )


def _is_fresh(access_expires_at: datetime) -> bool:
    return access_expires_at - datetime.now(timezone.utc) > timedelta(
        seconds=EXPIRY_MARGIN_SECONDS
    )


def _is_aware_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _require_client_credentials(cfg: Config) -> tuple[str, str]:
    client_id = cfg.withings_client_id
    client_secret = cfg.withings_client_secret
    if not isinstance(client_id, str) or client_id == "":
        raise WithingsAuthError(
            "missing Withings client credentials in config (WITHINGS_CLIENT_ID/WITHINGS_CLIENT_SECRET)"
        )
    if not isinstance(client_secret, str) or client_secret == "":
        raise WithingsAuthError(
            "missing Withings client credentials in config (WITHINGS_CLIENT_ID/WITHINGS_CLIENT_SECRET)"
        )
    return client_id, client_secret


def _refresh_token_pair(
    *, client_id: str, client_secret: str, refresh_token: str
) -> tuple[str, str, int]:
    try:
        response = requests.post(
            WITHINGS_TOKEN_URL,
            data={
                "action": "requesttoken",
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise WithingsAuthError("Withings token refresh request failed") from exc

    try:
        payload_obj: object = response.json()
    except ValueError:
        payload_obj = None

    status_label = _status_label(payload_obj)

    if response.status_code != 200:
        raise WithingsAuthError(
            f"Withings token refresh failed: http_status={response.status_code} "
            f"withings_status={status_label}"
        )
    if not isinstance(payload_obj, dict):
        raise WithingsAuthError(
            f"Withings token refresh failed: http_status={response.status_code} "
            f"withings_status={status_label}"
        )

    status_value = payload_obj.get("status")
    if not isinstance(status_value, int) or isinstance(status_value, bool) or status_value != 0:
        raise WithingsAuthError(
            f"Withings token refresh failed: http_status={response.status_code} "
            f"withings_status={status_label}"
        )

    body = payload_obj.get("body")
    if not isinstance(body, dict):
        raise WithingsAuthError(
            f"Withings token refresh failed: http_status={response.status_code} "
            f"withings_status={status_label}"
        )

    access_token = body.get("access_token")
    new_refresh_token = body.get("refresh_token")
    expires_in = body.get("expires_in")

    if not isinstance(access_token, str) or access_token == "":
        raise WithingsAuthError(
            f"Withings token refresh failed: http_status={response.status_code} "
            f"withings_status={status_label}"
        )
    if not isinstance(new_refresh_token, str) or new_refresh_token == "":
        raise WithingsAuthError(
            f"Withings token refresh failed: http_status={response.status_code} "
            f"withings_status={status_label}"
        )
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
        raise WithingsAuthError(
            f"Withings token refresh failed: http_status={response.status_code} "
            f"withings_status={status_label}"
        )

    return access_token, new_refresh_token, expires_in


def _status_label(payload_obj: object) -> str:
    if not isinstance(payload_obj, dict):
        return "unavailable"

    status_value = payload_obj.get("status")
    if isinstance(status_value, int) and not isinstance(status_value, bool):
        return str(status_value)
    if status_value is None:
        return "missing"
    return repr(status_value)


def _safe_rollback(conn: psycopg.Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        # Rollback cleanup must not hide the original failure.
        pass
