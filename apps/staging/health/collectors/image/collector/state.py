"""State-table helpers for collector sync runs.

This module owns the small amount of mutable operational state in PostgreSQL:
watermarks, Garmin's account-level 429 sentinel, backfill cursors, and run-log rows.
Design rules here are strict because these rows coordinate whole CronJob runs:
validate fetched row shapes before trusting them, and commit state transitions that
must survive a crash immediately after the write.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from collector import logic


GARMIN_SENTINEL = ("garmin", "_account")

_VALID_RUN_KINDS = {"cron", "backfill"}
_VALID_RUN_STATUSES = {"ok", "partial", "failed"}


class AccountBlockedError(Exception):
    """Garmin account-level 429 backoff is active; carries blocked_until."""

    def __init__(self, blocked_until: datetime):
        _require_aware_datetime_input(blocked_until, "blocked_until")
        self.blocked_until = blocked_until
        super().__init__(
            f"Garmin account is blocked until {blocked_until.isoformat()}"
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware_datetime_input(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _require_optional_aware_datetime_db(
    value: Any,
    field_name: str,
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise RuntimeError(
            f"expected {field_name} to be a datetime or None, got {type(value).__name__}"
        )
    if value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError(f"expected {field_name} to be timezone-aware")
    return value


def _require_aware_datetime_db(value: Any, field_name: str) -> datetime:
    parsed = _require_optional_aware_datetime_db(value, field_name)
    if parsed is None:
        raise RuntimeError(f"expected {field_name} to be a datetime, got NULL")
    return parsed


def _single_value_or_none(row: Any, field_name: str) -> Any | None:
    if row is None:
        return None
    try:
        row_len = len(row)
    except TypeError as exc:
        raise RuntimeError(
            f"expected one-column row for {field_name}, got non-sized {type(row).__name__}"
        ) from exc
    if row_len != 1:
        raise RuntimeError(f"expected one-column row for {field_name}, got {row_len} columns")
    return row[0]


def _require_json_dict_input(value: dict, name: str) -> dict:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a dict")
    return value


def _require_optional_json_dict_db(value: Any, field_name: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeError(
            f"expected {field_name} to be a JSON object or NULL, got {type(value).__name__}"
        )
    return value


def _require_run_id(value: Any) -> int:
    if not isinstance(value, int):
        raise RuntimeError(f"expected run_id to be an int, got {type(value).__name__}")
    if value < 1:
        raise RuntimeError(f"expected run_id to be positive, got {value}")
    return value


def get_watermark(conn: psycopg.Connection, source: str, stream: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT watermark
            FROM ops.sync_state
            WHERE source = %s AND stream = %s
            """,
            (source, stream),
        )
        value = _single_value_or_none(cur.fetchone(), "ops.sync_state.watermark")

    if value is None:
        return None
    return _require_aware_datetime_db(value, "ops.sync_state.watermark")


def set_watermark(conn: psycopg.Connection, source: str, stream: str, ts: datetime) -> None:
    """Upsert (source,stream): watermark=ts, updated_at=now(). Commits."""
    _require_aware_datetime_input(ts, "ts")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.sync_state (source, stream, watermark)
            VALUES (%s, %s, %s)
            ON CONFLICT (source, stream) DO UPDATE
            SET watermark = EXCLUDED.watermark,
                updated_at = now()
            """,
            (source, stream, ts),
        )
    conn.commit()


def check_blocked(conn: psycopg.Connection, now: datetime | None = None) -> None:
    """Read GARMIN_SENTINEL.blocked_until; raise AccountBlockedError while it is in the
    future (logic.is_blocked decides). Called before EVERY Garmin API call."""
    current_now = _utc_now() if now is None else _require_aware_datetime_input(now, "now")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT blocked_until
            FROM ops.sync_state
            WHERE source = %s AND stream = %s
            """,
            GARMIN_SENTINEL,
        )
        value = _single_value_or_none(cur.fetchone(), "ops.sync_state.blocked_until")

    blocked_until = _require_optional_aware_datetime_db(
        value, "ops.sync_state.blocked_until"
    )
    if logic.is_blocked(blocked_until, current_now):
        raise AccountBlockedError(blocked_until)


def set_blocked(
    conn: psycopg.Connection,
    retry_after: str | int | None,
    now: datetime | None = None,
) -> datetime:
    """Persist blocked_until = logic.compute_blocked_until(...) on the sentinel row and
    COMMIT IMMEDIATELY — the block must survive the run aborting right after. sync_state.watermark
    is NOT NULL, so the sentinel upsert supplies watermark = now() on first insert and must NOT
    clobber an existing watermark on conflict (COALESCE/keep-existing). Returns blocked_until."""
    current_now = _utc_now() if now is None else _require_aware_datetime_input(now, "now")
    blocked_until = logic.compute_blocked_until(current_now, retry_after)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.sync_state AS sync_state
                (source, stream, watermark, blocked_until)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source, stream) DO UPDATE
            SET watermark = COALESCE(sync_state.watermark, EXCLUDED.watermark),
                blocked_until = EXCLUDED.blocked_until,
                updated_at = now()
            """,
            (GARMIN_SENTINEL[0], GARMIN_SENTINEL[1], current_now, blocked_until),
        )

    # Garmin 429s are account-wide, so the sentinel must outlive an abrupt abort immediately after.
    conn.commit()
    return blocked_until


def get_extra(conn: psycopg.Connection, source: str, stream: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT extra
            FROM ops.sync_state
            WHERE source = %s AND stream = %s
            """,
            (source, stream),
        )
        value = _single_value_or_none(cur.fetchone(), "ops.sync_state.extra")

    extra = _require_optional_json_dict_db(value, "ops.sync_state.extra")
    if extra is None:
        # Missing cursor state is equivalent to "no cursor yet", which composes cleanly for callers.
        return {}
    return extra


def set_extra(conn: psycopg.Connection, source: str, stream: str, extra: dict) -> None:
    """Backfill cursor storage in sync_state.extra jsonb; same NOT-NULL watermark care as
    set_blocked; merge-not-replace is NOT wanted — store the dict as given. Commits."""
    _require_json_dict_input(extra, "extra")
    current_now = _utc_now()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.sync_state AS sync_state
                (source, stream, watermark, extra)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source, stream) DO UPDATE
            SET watermark = COALESCE(sync_state.watermark, EXCLUDED.watermark),
                extra = EXCLUDED.extra,
                updated_at = now()
            """,
            (source, stream, current_now, Jsonb(extra)),
        )
    conn.commit()


def open_run(conn: psycopg.Connection, source: str, stream: str, kind: str) -> int:
    """INSERT ops.run_log row, RETURNING run_id. Commits (a crashed run must still be visible
    as an opened-never-finished row on the dashboard)."""
    if kind not in _VALID_RUN_KINDS:
        raise ValueError(
            f"kind must be one of {sorted(_VALID_RUN_KINDS)}, got {kind!r}"
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.run_log (source, stream, kind)
            VALUES (%s, %s, %s)
            RETURNING run_id
            """,
            (source, stream, kind),
        )
        run_id = _require_run_id(_single_value_or_none(cur.fetchone(), "ops.run_log.run_id"))

    conn.commit()
    return run_id


def close_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    days_attempted: int,
    days_failed: int,
    rows_upserted: int,
    first_error: str | None,
    details: dict | None,
) -> None:
    """UPDATE the row: finished_at=now() + all counters. status must be one of
    ok|partial|failed (assert before writing — the CHECK constraint firing at the DB is a
    worse error message). Commits."""
    if status not in _VALID_RUN_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_RUN_STATUSES)}, got {status!r}"
        )
    if first_error is not None and not isinstance(first_error, str):
        raise TypeError("first_error must be a str or None")
    if details is not None:
        _require_json_dict_input(details, "details")

    details_param = Jsonb(details) if details is not None else None

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.run_log
            SET finished_at = now(),
                status = %s,
                days_attempted = %s,
                days_failed = %s,
                rows_upserted = %s,
                first_error = %s,
                details = %s
            WHERE run_id = %s
            """,
            (
                status,
                days_attempted,
                days_failed,
                rows_upserted,
                first_error,
                details_param,
                run_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError(
                f"expected to update exactly one ops.run_log row for run_id={run_id}, "
                f"updated {cur.rowcount}"
            )

    conn.commit()
