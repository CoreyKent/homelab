"""Garmin token-only authentication helpers.

This module enforces the collector's account-safety rule: Garmin login is always driven by a
single JSON tokenstore and never by interactive credentials at runtime. The same token dict
shape is used everywhere (seed file, database blob, temporary login file) so refresh handling
stays predictable and mid-run token rotations can be persisted back to the database.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from garminconnect import Garmin
from psycopg.types.json import Jsonb

from collector.config import Config

TOKEN_FILENAME = "garmin_tokens.json"

logger = logging.getLogger(__name__)

_BOOTSTRAP_GUIDANCE = "re-run the workstation bootstrap to mint a fresh tokenstore"


class GarminAuthError(Exception):
    """Message must direct the operator to re-run the workstation bootstrap; never mention
    or suggest a credentials path."""


def _validate_token_blob(blob: object, *, checked: str) -> dict:
    if not isinstance(blob, dict):
        raise GarminAuthError(
            f"Garmin tokenstore in {checked} is not a JSON object; {_BOOTSTRAP_GUIDANCE}"
        )

    di_token = blob.get("di_token")
    di_refresh_token = blob.get("di_refresh_token")
    if not isinstance(di_token, str) or not di_token.strip():
        raise GarminAuthError(
            f"Garmin tokenstore in {checked} is missing a non-empty di_token; {_BOOTSTRAP_GUIDANCE}"
        )
    if not isinstance(di_refresh_token, str) or not di_refresh_token.strip():
        raise GarminAuthError(
            f"Garmin tokenstore in {checked} is missing a non-empty di_refresh_token; {_BOOTSTRAP_GUIDANCE}"
        )

    # A shallow copy prevents accidental mutation of the caller's object after validation.
    return dict(blob)


def _read_token_file(path: Path, *, checked: str) -> dict:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GarminAuthError(
            f"Garmin tokenstore file not found at {checked}; {_BOOTSTRAP_GUIDANCE}"
        ) from exc
    except OSError as exc:
        raise GarminAuthError(
            f"Unable to read Garmin tokenstore file at {checked}: {exc}; {_BOOTSTRAP_GUIDANCE}"
        ) from exc

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise GarminAuthError(
            f"Garmin tokenstore file at {checked} is not valid JSON: {exc}; {_BOOTSTRAP_GUIDANCE}"
        ) from exc

    return _validate_token_blob(parsed, checked=checked)


def _write_token_file(path: Path, blob: dict) -> None:
    validated = _validate_token_blob(blob, checked="the in-memory tokenstore blob")
    payload = json.dumps(validated)

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)

    # The file may already exist, so chmod keeps the on-disk mode strict regardless of umask.
    os.chmod(path, 0o600)


def load_token_blob(conn: psycopg.Connection, seed_path: str) -> tuple[dict, str]:
    """ops.garmin_session.token_blob when the row exists (authoritative), else the seed file.
    Returns (blob, origin), origin in ('db','seed'). Validate: dict with non-empty str
    di_token AND di_refresh_token (di_client_id passes through untouched). Malformed or
    missing both sources -> GarminAuthError naming what was checked."""
    row = conn.execute(
        """
        SELECT token_blob
        FROM ops.garmin_session
        WHERE singleton = true
        """
    ).fetchone()

    if row is not None:
        if len(row) != 1:
            raise RuntimeError(
                "Unexpected row shape from ops.garmin_session while loading Garmin session"
            )
        # The database row is authoritative so a bad row fails loudly instead of silently
        # regressing to an older mounted seed.
        return _validate_token_blob(row[0], checked="ops.garmin_session.token_blob"), "db"

    seed = Path(seed_path)
    try:
        return _read_token_file(seed, checked=f"seed file {seed}"), "seed"
    except GarminAuthError as exc:
        raise GarminAuthError(
            f"No usable Garmin tokenstore found after checking ops.garmin_session and seed file {seed}: {exc}"
        ) from exc


def persist_token_blob(conn: psycopg.Connection, blob: dict) -> None:
    """Upsert singleton ops.garmin_session (token_blob=Jsonb(blob), refreshed_at=now())
    ON CONFLICT (singleton) DO UPDATE. Commits."""
    validated = _validate_token_blob(blob, checked="the token blob being persisted")
    conn.execute(
        """
        INSERT INTO ops.garmin_session (singleton, token_blob, refreshed_at)
        VALUES (true, %s, now())
        ON CONFLICT (singleton) DO UPDATE
        SET token_blob = EXCLUDED.token_blob,
            refreshed_at = now()
        """,
        (Jsonb(validated),),
    )
    conn.commit()


def login_client(conn: psycopg.Connection, cfg: Config) -> Garmin:
    """Token-only login:
    1. blob, origin = load_token_blob(conn, cfg.garmin_seed_path)
    2. tmp = tempfile.TemporaryDirectory(); write json.dumps(blob) to tmp/garmin_tokens.json
       with 0600 (the dir is private but umask-safety costs one line)
    3. client = Garmin()  # NO credentials, ever — the account-lockout firewall
       mfa_status, _ = client.login(<path to the temp token file>)
       - any exception -> GarminAuthError('Garmin token login failed; re-run the workstation
         bootstrap to mint a fresh tokenstore') chained from the cause
       - mfa_status == 'needs_mfa' -> GarminAuthError (cannot happen on a pure token path;
         if it does, the store is unusable)
    4. login() may have proactively refreshed and re-dumped the file: re-read it; persist to
       ops.garmin_session when origin == 'seed' (first use promotes the seed to DB authority)
       or the content changed.
    5. Attach the TemporaryDirectory to the client (client._collector_tokendir = tmp) so
       mid-run auto-refresh dumps still have a live directory; return the client."""
    blob, origin = load_token_blob(conn, cfg.garmin_seed_path)

    tmp = TemporaryDirectory()
    token_path = Path(tmp.name) / TOKEN_FILENAME
    _write_token_file(token_path, blob)

    client = Garmin()
    try:
        login_result = client.login(str(token_path))
    except Exception as exc:
        tmp.cleanup()
        raise GarminAuthError(
            f"Garmin token login failed; {_BOOTSTRAP_GUIDANCE}"
        ) from exc

    if not isinstance(login_result, tuple) or len(login_result) != 2:
        tmp.cleanup()
        raise GarminAuthError(
            f"Garmin token login returned an unexpected result; {_BOOTSTRAP_GUIDANCE}"
        )

    mfa_status, _ = login_result
    if mfa_status == "needs_mfa":
        tmp.cleanup()
        raise GarminAuthError(
            f"Garmin token login unexpectedly requested MFA for a token-only login; {_BOOTSTRAP_GUIDANCE}"
        )
    if mfa_status is not None:
        tmp.cleanup()
        raise GarminAuthError(
            f"Garmin token login returned unexpected status {mfa_status!r}; {_BOOTSTRAP_GUIDANCE}"
        )

    current_blob = _read_token_file(
        token_path, checked=f"temporary Garmin tokenstore {token_path}"
    )
    if origin == "seed" or current_blob != blob:
        persist_token_blob(conn, current_blob)

    setattr(client, "_collector_tokendir", tmp)
    setattr(client, "_collector_persisted_token_blob", current_blob)
    return client


def persist_current(conn: psycopg.Connection, client: Garmin) -> None:
    """Called by main after a run completes (finally-style): re-read the client's attached
    temp token file and persist_token_blob when it still parses to a valid blob and differs
    from what login persisted; a refresh that happened mid-run must survive the pod. Missing
    attribute/file or invalid content -> log a warning, never raise (the run's data already
    landed; token persistence is best-effort here because the next login re-syncs)."""
    try:
        tokendir = getattr(client, "_collector_tokendir", None)
        if tokendir is None or not hasattr(tokendir, "name"):
            logger.warning(
                "Garmin client has no attached token directory; skipping token persistence"
            )
            return

        token_path = Path(tokendir.name) / TOKEN_FILENAME
        if not token_path.exists():
            logger.warning(
                "Garmin token file %s is missing after the run; skipping token persistence",
                token_path,
            )
            return

        current_blob = _read_token_file(
            token_path, checked=f"temporary Garmin tokenstore {token_path}"
        )
        persisted_blob = getattr(client, "_collector_persisted_token_blob", None)
        if persisted_blob is not None and current_blob == persisted_blob:
            return

        persist_token_blob(conn, current_blob)
        setattr(client, "_collector_persisted_token_blob", current_blob)
    except Exception as exc:
        logger.warning(
            "Failed to persist refreshed Garmin tokenstore after the run: %s",
            exc,
        )
