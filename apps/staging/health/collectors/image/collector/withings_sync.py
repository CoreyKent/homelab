"""Withings measurement ingestion.

This module keeps the vendor boundary strict: HTTP responses are validated before any field is
trusted, empty responses are treated as normal skips, and malformed individual records are dropped
without sacrificing the rest of the batch. Watermarks advance only after successful writes so the
mandated overlap window can safely re-cover any failed run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

import psycopg
import requests

from collector import db, logic, state, withings_auth
from collector.config import Config

WITHINGS_MEASURE_URL = "https://wbsapi.withings.net/measure"
HTTP_TIMEOUT_SECONDS = 30
MAX_PAGES = 100

logger = logging.getLogger(__name__)


class WithingsAPIError(Exception):
    """Non-zero Withings API status or malformed payload; carries the numeric status when known."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _coerce_api_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise WithingsAPIError(f"Withings getmeas payload field {field_name!r} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WithingsAPIError(
            f"Withings getmeas payload field {field_name!r} must be an integer",
        ) from exc


def _coerce_vendor_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid integer field")
    return int(value)


def _fetch_page(
    access_token: str,
    start_epoch: int,
    end_epoch: int,
    offset: int | None,
) -> tuple[list[dict], bool, int | None]:
    payload: dict[str, Any] = {
        "action": "getmeas",
        "category": "1",
        "startdate": start_epoch,
        "enddate": end_epoch,
    }
    if offset is not None:
        payload["offset"] = offset

    try:
        response = requests.post(
            WITHINGS_MEASURE_URL,
            data=payload,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Withings getmeas request failed: {exc}") from exc

    if response.status_code != 200:
        raise WithingsAPIError(f"Withings getmeas returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise WithingsAPIError("Withings getmeas returned non-JSON payload") from exc

    if not isinstance(data, dict):
        raise WithingsAPIError("Withings getmeas JSON payload must be an object")

    api_status = _coerce_api_int(data.get("status"), "status")
    if api_status != 0:
        raise WithingsAPIError(
            f"Withings getmeas returned non-zero status {api_status}",
            status=api_status,
        )

    body = data.get("body")
    if not isinstance(body, dict):
        raise WithingsAPIError("Withings getmeas payload body must be an object", status=api_status)

    measure_groups = body.get("measuregrps")
    if not isinstance(measure_groups, list):
        raise WithingsAPIError(
            "Withings getmeas payload body.measuregrps must be a list",
            status=api_status,
        )

    raw_more = body.get("more", 0)
    if isinstance(raw_more, bool):
        more = raw_more
    elif isinstance(raw_more, int):
        more = raw_more != 0
    else:
        raise WithingsAPIError(
            "Withings getmeas payload body.more must be a boolean or integer",
            status=api_status,
        )

    next_offset: int | None = None
    if more:
        next_offset = _coerce_api_int(body.get("offset"), "body.offset")
        if next_offset < 0:
            raise WithingsAPIError(
                "Withings getmeas payload body.offset must be non-negative",
                status=api_status,
            )

    return measure_groups, more, next_offset


def _deduplicate_rows(rows: list[tuple]) -> list[tuple]:
    deduped: dict[tuple[int, int], tuple] = {}
    for row in rows:
        deduped[(row[0], row[1])] = row

    if len(deduped) != len(rows):
        # A later duplicate is the safest winner because PostgreSQL refuses a single upsert batch
        # that tries to affect the same conflict key twice.
        logger.debug(
            "Collapsed %d duplicate Withings measurement rows before upsert",
            len(rows) - len(deduped),
        )

    return list(deduped.values())


def _upsert_measurements(conn: psycopg.Connection, rows: list[tuple]) -> int:
    if not rows:
        return 0

    with conn.cursor() as cur:
        return db.upsert(
            cur,
            "withings.measurement",
            ("group_id", "measure_type"),
            ("group_id", "measure_type", "metric", "ts", "local_date", "value"),
            rows,
            set_ingested_at=True,
        )


def _close_run(
    conn: psycopg.Connection,
    run_id: int,
    status_text: str,
    *,
    days_attempted: int,
    days_failed: int,
    rows_upserted: int,
    first_error: str | None,
    details: dict[str, str] | None,
) -> None:
    state.close_run(
        conn,
        run_id,
        status_text,
        days_attempted=days_attempted,
        days_failed=days_failed,
        rows_upserted=rows_upserted,
        first_error=first_error,
        details=details,
    )


def fetch_measure_groups(access_token: str, start: datetime, end: datetime) -> list[dict]:
    """POST action=getmeas, category=1, startdate/enddate as int epoch seconds,
    Authorization: Bearer header. Validate: HTTP 200, JSON dict, status == 0, body dict,
    measuregrps list. Pagination: while body.get('more'): repeat with offset=body['offset']
    (validated int), accumulating groups; more than MAX_PAGES pages -> WithingsAPIError
    (a runaway loop is a bug, not a workload). requests.post timeout=HTTP_TIMEOUT_SECONDS.
    'No data' (empty measuregrps) returns [] — a valid, loggable outcome, never an error.
    """
    if not isinstance(access_token, str) or access_token == "":
        raise ValueError("access_token must be a non-empty string")
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware datetimes")
    if start > end:
        raise ValueError("start must be less than or equal to end")

    start_epoch = int(start.timestamp())
    end_epoch = int(end.timestamp())

    all_groups: list[dict] = []
    offset: int | None = None
    page_count = 0

    while True:
        page_count += 1
        if page_count > MAX_PAGES:
            raise WithingsAPIError(
                f"Withings getmeas exceeded MAX_PAGES={MAX_PAGES}; pagination loop is runaway",
            )

        groups, more, offset = _fetch_page(access_token, start_epoch, end_epoch, offset)
        all_groups.extend(groups)

        if more:
            continue
        return all_groups


def rows_from_groups(groups: list[dict], tz_name: str) -> list[tuple]:
    """Flatten to withings.measurement rows
    (group_id, measure_type, metric, ts, local_date, value):
    group_id = int(grp['grpid']); ts = logic.parse_epoch_ms(grp['date'] * 1000) (the API gives
    epoch SECONDS); local_date = logic.to_local_date(ts, tz_name); per measure in
    grp.get('measures') or []: measure_type = int(m['type']), metric =
    logic.metric_for_type(measure_type), value = logic.normalize_measure(int(m['value']),
    int(m['unit'])). Entries with missing/non-numeric fields are skipped with a debug log
    (one malformed vendor record must not kill the batch); groups without grpid/date likewise.
    """
    rows: list[tuple] = []

    for group_index, group in enumerate(groups):
        if not isinstance(group, dict):
            logger.debug("Skipping malformed Withings group at index %d: not an object", group_index)
            continue

        try:
            group_id = _coerce_vendor_int(group["grpid"])
            group_epoch_s = _coerce_vendor_int(group["date"])
            ts = logic.parse_epoch_ms(group_epoch_s * 1000)
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
            logger.debug(
                "Skipping malformed Withings group at index %d: %s",
                group_index,
                exc,
            )
            continue

        local_date = logic.to_local_date(ts, tz_name)

        measures = group.get("measures") or []
        if not isinstance(measures, list):
            logger.debug(
                "Skipping malformed Withings group %s: measures is not a list",
                group_id,
            )
            continue

        for measure_index, measure in enumerate(measures):
            if not isinstance(measure, dict):
                logger.debug(
                    "Skipping malformed Withings measure in group %s at index %d: not an object",
                    group_id,
                    measure_index,
                )
                continue

            try:
                measure_type = _coerce_vendor_int(measure["type"])
                metric = logic.metric_for_type(measure_type)
                if not isinstance(metric, str) or metric == "":
                    raise ValueError("metric mapping returned an empty metric")

                value = logic.normalize_measure(
                    _coerce_vendor_int(measure["value"]),
                    _coerce_vendor_int(measure["unit"]),
                )
                if value is None:
                    raise ValueError("normalized value is null")
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                logger.debug(
                    "Skipping malformed Withings measure in group %s at index %d: %s",
                    group_id,
                    measure_index,
                    exc,
                )
                continue

            rows.append((group_id, measure_type, metric, ts, local_date, value))

    return rows


def sync_range(conn: psycopg.Connection, cfg: Config, start: datetime, end: datetime) -> int:
    """Token via withings_auth.get_access_token (its commit-before-use contract already holds),
    fetch, flatten, one upsert into withings.measurement — keys (group_id, measure_type), cols
    (group_id, measure_type, metric, ts, local_date, value), set_ingested_at=True — then
    conn.commit(). Returns rows upserted.
    """
    access_token = withings_auth.get_access_token(conn, cfg)
    if not isinstance(access_token, str) or access_token == "":
        raise RuntimeError("withings_auth.get_access_token returned an empty token")

    try:
        groups = fetch_measure_groups(access_token, start, end)
    except WithingsAPIError as exc:
        if getattr(exc, "status", None) != 401:
            raise
        # Withings keeps one active access token per (user, app): another chain's refresh
        # (e.g. HealthData's sync) invalidates ours mid-lifetime. Reactive refresh, ONE retry;
        # a second 401 is a real auth failure and raises.
        access_token = withings_auth.get_access_token(
            conn, cfg, force_refresh=True, stale_token=access_token
        )
        groups = fetch_measure_groups(access_token, start, end)
    if not groups:
        logger.info(
            "Withings measurement sync skipped: no data for %s to %s",
            start.isoformat(),
            end.isoformat(),
        )
        conn.commit()
        return 0

    rows = rows_from_groups(groups, cfg.tz_local)
    if not rows:
        logger.info(
            "Withings measurement sync skipped: %d groups yielded no valid rows",
            len(groups),
        )
        conn.commit()
        return 0

    rows = _deduplicate_rows(rows)
    upserted = _upsert_measurements(conn, rows)
    conn.commit()
    return upserted


def run_cron(conn: psycopg.Connection, cfg: Config) -> bool:
    """One withings cron run; True when status == 'ok'."""
    run_started = datetime.now(timezone.utc)
    run_id = state.open_run(conn, "withings", "measurement", "cron")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise RuntimeError("state.open_run returned a non-integer run_id")

    try:
        start = logic.overlap_start(state.get_watermark(conn, "withings", "measurement"))
        if start is None:
            start = run_started - timedelta(days=logic.DEFAULT_LOOKBACK_DAYS)

        end = run_started
        rows = sync_range(conn, cfg, start, end)

        state.set_watermark(conn, "withings", "measurement", run_started)
        _close_run(
            conn,
            run_id,
            "ok",
            days_attempted=1,
            days_failed=0,
            rows_upserted=rows,
            first_error=None,
            details=None,
        )
        return True
    except Exception as exc:
        conn.rollback()
        logger.exception("Withings measurement cron run failed")
        _close_run(
            conn,
            run_id,
            "failed",
            days_attempted=1,
            days_failed=1,
            rows_upserted=0,
            first_error=str(exc),
            details={"error_type": type(exc).__name__},
        )
        return False
