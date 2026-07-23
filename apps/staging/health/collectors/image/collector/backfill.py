"""Budgeted, resumable helpers for oldest-to-newest historical backfill.

Design rules:
- Persist the backfill cursor in ``ops.sync_state.extra`` so each invocation can resume exactly
  where the previous one stopped.
- Bound probe traffic per invocation because Garmin's account-level 429 handling makes unbounded
  historical scans reckless.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from collector import logic, state, withings_auth, withings_sync
from collector.config import Config
from collector.garmin_sync import GarminStreams

logger = logging.getLogger(__name__)

BACKFILL_STREAM = "_backfill"
MAX_PROBE_YEARS = 25


class BudgetExhausted(Exception):
    """Per-run API request budget used up; the run stops cleanly and resumes next invocation."""


class RequestBudget:
    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("request budget limit must be at least 1")
        self._limit = limit
        self._spent = 0

    @property
    def spent(self) -> int:
        return self._spent

    def spend(self) -> None:
        if self._spent >= self._limit:
            raise BudgetExhausted(f"request budget of {self._limit} exhausted")
        self._spent += 1


def _iso_or_none(value: object) -> date | None:
    """Parse an ISO date string or pass through null for cursor fields."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"cursor value must be an ISO date string or null, got {type(value).__name__}"
        )
    return date.fromisoformat(value)


def _load_cursor(
    conn: psycopg.Connection[Any], source: str
) -> tuple[date | None, date | None]:
    """Load ``(account_start, cursor_date)`` from backfill sync-state extra."""
    extra = state.get_extra(conn, source, BACKFILL_STREAM)
    if extra is None:
        return None, None
    if not isinstance(extra, dict):
        raise ValueError(
            f"invalid {source} backfill cursor: expected object, got {type(extra).__name__}"
        )

    account_start = _iso_or_none(extra.get("account_start"))
    cursor_date = _iso_or_none(extra.get("cursor_date"))

    # Cursor writes persist the pair together, so a half-populated pair is corruption.
    if (account_start is None) != (cursor_date is None):
        raise ValueError(
            f"invalid {source} backfill cursor: account_start and cursor_date must both be set or both be null"
        )

    return account_start, cursor_date


def _save_cursor(
    conn: psycopg.Connection[Any], source: str, account_start: date, cursor_date: date
) -> None:
    """Persist the backfill cursor after every completed chunk and every clean stop."""
    state.set_extra(
        conn,
        source,
        BACKFILL_STREAM,
        {
            "account_start": account_start.isoformat(),
            "cursor_date": cursor_date.isoformat(),
        },
    )


def _garmin_day_has_data(streams: GarminStreams, day: date) -> bool:
    """Return whether Garmin summary data exists for the probed day."""
    payload = streams._call(streams.client.get_user_summary, day.isoformat())

    if payload is None:
        logger.debug("garmin probe found no data for %s", day.isoformat())
        return False
    if not isinstance(payload, dict):
        raise ValueError(
            f"invalid Garmin summary probe payload for {day.isoformat()}: "
            f"expected dict or null, got {type(payload).__name__}"
        )
    if not payload:
        logger.debug("garmin probe found no data for %s", day.isoformat())
        return False

    has_data = any(
        payload.get(key) is not None
        for key in ("totalSteps", "restingHeartRate", "totalKilocalories")
    )
    if not has_data:
        logger.debug(
            "garmin probe summary had no qualifying metrics for %s", day.isoformat()
        )
    return has_data


def discover_account_start_garmin(streams: GarminStreams, today: date) -> date:
    """Discover the oldest Garmin day with data using yearly probes and day bisection."""
    last_with_data = today
    low: date | None = None

    for k in range(1, MAX_PROBE_YEARS + 1):
        candidate = today - timedelta(days=365 * k)
        if _garmin_day_has_data(streams, candidate):
            last_with_data = candidate
            continue
        low = candidate
        break

    if low is None:
        low = today - timedelta(days=365 * (MAX_PROBE_YEARS + 1))

    high = last_with_data
    while (high - low).days > 1:
        mid = low + timedelta(days=(high - low).days // 2)
        if _garmin_day_has_data(streams, mid):
            high = mid
        else:
            low = mid

    return high


def discover_account_start_withings(
    conn: psycopg.Connection[Any],
    cfg: Config,
    budget: RequestBudget,
    today: date,
) -> date:
    """Discover the oldest non-empty Withings year window within the probe horizon."""
    token = withings_auth.get_access_token(conn, cfg)
    if not isinstance(token, str) or not token:
        raise ValueError("withings access token must be a non-empty string")

    oldest_nonempty = today - timedelta(days=365)

    # Withings measurements are sparse, so a few empty yearly windows cost less than more probes.
    for k in range(1, MAX_PROBE_YEARS + 1):
        window_start_date = today - timedelta(days=365 * k)
        window_end_date = today - timedelta(days=365 * (k - 1))

        budget.spend()
        groups = withings_sync.fetch_measure_groups(
            token,
            datetime.combine(window_start_date, time.min, tzinfo=timezone.utc),
            datetime.combine(window_end_date, time.min, tzinfo=timezone.utc),
        )

        if groups is None:
            logger.debug(
                "withings probe found no data for [%s, %s)",
                window_start_date.isoformat(),
                window_end_date.isoformat(),
            )
            break
        if not isinstance(groups, list):
            raise ValueError(
                "invalid Withings measure group payload for "
                f"[{window_start_date.isoformat()}, {window_end_date.isoformat()}): "
                f"expected list or null, got {type(groups).__name__}"
            )
        if not groups:
            logger.debug(
                "withings probe found no data for [%s, %s)",
                window_start_date.isoformat(),
                window_end_date.isoformat(),
            )
            break
        if any(not isinstance(group, dict) for group in groups):
            raise ValueError(
                "invalid Withings measure group payload for "
                f"[{window_start_date.isoformat()}, {window_end_date.isoformat()}): "
                "list items must be objects"
            )

        oldest_nonempty = window_start_date

    return oldest_nonempty


_GARMIN_DAY_STREAMS: tuple[tuple[str, Any], ...] = (
    ("daily_summary", GarminStreams.sync_daily_summary),
    ("intraday", GarminStreams.sync_intraday),
    ("steps_epoch", GarminStreams.sync_steps_epoch),
    ("sleep", GarminStreams.sync_sleep),
    ("hrv", GarminStreams.sync_hrv),
    ("training_status", GarminStreams.sync_training_status),
)


def _run_garmin_chunk(
    conn: psycopg.Connection[Any],
    streams: GarminStreams,
    chunk_start: date,
    chunk_end: date,
    failures: list[dict[str, str]],
    counters: dict[str, int],
) -> None:
    """Run one Garmin backfill chunk, isolating unit failures so later units still progress."""
    for day in logic.date_range(chunk_start, chunk_end):
        for name, method in _GARMIN_DAY_STREAMS:
            counters["attempted"] += 1
            try:
                rows = method(streams, day)
                if type(rows) is not int or rows < 0:
                    raise ValueError(
                        f"{name} returned invalid row count for {day.isoformat()}: {rows!r}"
                    )
                counters["rows"] += rows
            except (state.AccountBlockedError, BudgetExhausted):
                raise
            except Exception as exc:
                counters["failed"] += 1
                failures.append(
                    {
                        "stream": name,
                        "unit": day.isoformat(),
                        "error": str(exc),
                    }
                )
                # A failed statement can poison the transaction; clearing it keeps later units runnable.
                conn.rollback()
                continue
        conn.commit()

    counters["attempted"] += 1
    try:
        activity_result = streams.sync_activities(chunk_start, chunk_end)
        if not isinstance(activity_result, tuple) or len(activity_result) != 2:
            raise ValueError(
                "activity sync returned invalid result: expected (rows, failures)"
            )

        rows, act_failures = activity_result
        if type(rows) is not int or rows < 0:
            raise ValueError(
                f"activity sync returned invalid row count for {chunk_start}..{chunk_end}: {rows!r}"
            )
        if not isinstance(act_failures, list):
            raise ValueError(
                "activity sync returned invalid failures payload: expected list"
            )

        for failure in act_failures:
            if not isinstance(failure, dict):
                raise ValueError(
                    "activity sync returned invalid failure item: expected object"
                )
            stream = failure.get("stream")
            unit = failure.get("unit")
            error = failure.get("error")
            if (
                not isinstance(stream, str)
                or not isinstance(unit, str)
                or not isinstance(error, str)
            ):
                raise ValueError(
                    "activity sync returned invalid failure item: "
                    "stream, unit, and error must be strings"
                )

        counters["rows"] += rows
        counters["failed"] += len(act_failures)
        failures.extend(act_failures)
        conn.commit()
    except (state.AccountBlockedError, BudgetExhausted):
        raise
    except Exception as exc:
        counters["failed"] += 1
        failures.append(
            {
                "stream": "activity",
                "unit": f"{chunk_start}..{chunk_end}",
                "error": str(exc),
            }
        )
        conn.rollback()


def _run_withings_chunk(
    conn: psycopg.Connection[Any],
    cfg: Config,
    budget: RequestBudget,
    chunk_start: date,
    chunk_end: date,
    failures: list[dict[str, str]],
    counters: dict[str, int],
) -> None:
    """Run one Withings backfill chunk as a single budgeted unit."""
    counters["attempted"] += 1
    budget.spend()

    try:
        rows = withings_sync.sync_range(
            conn,
            cfg,
            datetime.combine(chunk_start, time.min, tzinfo=timezone.utc),
            datetime.combine(chunk_end, time(23, 59, 59), tzinfo=timezone.utc),
        )
        if type(rows) is not int or rows < 0:
            raise ValueError(
                f"withings sync returned invalid row count for {chunk_start}..{chunk_end}: {rows!r}"
            )
        counters["rows"] += rows
    except BudgetExhausted:
        raise
    except Exception as exc:
        counters["failed"] += 1
        failures.append(
            {
                "stream": "measurement",
                "unit": f"{chunk_start}..{chunk_end}",
                "error": str(exc),
            }
        )
        conn.rollback()


def run_backfill(
    conn: psycopg.Connection[Any],
    cfg: Config,
    source: str,
    chunk_days: int,
    budget_limit: int,
    client: Any = None,
) -> str:
    """Run a resumable oldest-to-newest backfill without touching cron watermarks."""
    if source not in ("garmin", "withings"):
        raise ValueError(f"unsupported backfill source: {source}")
    if chunk_days < 1:
        raise ValueError("chunk_days must be at least 1")
    if source == "garmin" and client is None:
        raise ValueError("garmin backfill requires an authenticated token-based client")

    budget = RequestBudget(budget_limit)
    run_id = state.open_run(conn, source, BACKFILL_STREAM, "backfill")
    today_local = datetime.now(ZoneInfo(cfg.tz_local)).date()

    account_start: date | None = None
    cursor_date: date | None = None
    failures: list[dict[str, str]] = []
    counters = {"attempted": 0, "failed": 0, "rows": 0}
    status = "failed"
    first_error: str | None = None
    stop_details: dict[str, object] = {}
    reraised_exc: Exception | None = None
    reraised_tb: Any = None

    try:
        account_start, cursor_date = _load_cursor(conn, source)

        if account_start is None:
            if source == "garmin":
                streams_for_probe = GarminStreams(conn, client, cfg, budget=budget)
                account_start = discover_account_start_garmin(
                    streams_for_probe, today_local
                )
            else:
                account_start = discover_account_start_withings(
                    conn, cfg, budget, today_local
                )

            cursor_date = account_start
            # Discovery can be expensive enough that losing it would waste a later run's budget.
            _save_cursor(conn, source, account_start, cursor_date)

        if cursor_date is None:
            cursor_date = account_start
        if account_start is None or cursor_date is None:
            raise ValueError(f"{source} backfill cursor discovery produced no cursor")

        chunks = logic.chunk_ranges(cursor_date, today_local, chunk_days)
        streams: GarminStreams | None = (
            GarminStreams(conn, client, cfg, budget=budget)
            if source == "garmin"
            else None
        )

        for chunk_start, chunk_end in chunks:
            if source == "garmin":
                if streams is None:
                    raise RuntimeError("garmin streams were not initialized")
                _run_garmin_chunk(
                    conn, streams, chunk_start, chunk_end, failures, counters
                )
            else:
                _run_withings_chunk(
                    conn, cfg, budget, chunk_start, chunk_end, failures, counters
                )

            cursor_date = chunk_end + timedelta(days=1)
            _save_cursor(conn, source, account_start, cursor_date)

        status = "ok" if counters["failed"] == 0 else "partial"
        stop_details = {"stopped": "complete"}
    except BudgetExhausted:
        # If discovery stopped before establishing a cursor, the next run repeats the probe.
        conn.rollback()
        if account_start is not None and cursor_date is not None:
            _save_cursor(conn, source, account_start, cursor_date)
        status = "partial"
        stop_details = {"stopped": "budget", "spent": budget.spent}
    except state.AccountBlockedError as exc:
        # A pre-cursor block during discovery leaves nothing resumable to persist yet.
        conn.rollback()
        if account_start is not None and cursor_date is not None:
            _save_cursor(conn, source, account_start, cursor_date)
        status = "failed"
        first_error = str(exc)
        stop_details = {
            "stopped": "blocked",
            "blocked_until": exc.blocked_until.isoformat(),
        }
    except Exception as exc:
        conn.rollback()
        status = "failed"
        first_error = str(exc)
        stop_details = {"stopped": "fatal"}
        reraised_exc = exc
        reraised_tb = exc.__traceback__

    if first_error is None and failures:
        first_error = failures[0]["error"]

    details: dict[str, object] = {
        "failures": failures[:50],
        "cursor_date": cursor_date.isoformat() if cursor_date is not None else None,
        "account_start": (
            account_start.isoformat() if account_start is not None else None
        ),
        **stop_details,
    }
    state.close_run(
        conn,
        run_id,
        status,
        counters["attempted"],
        counters["failed"],
        counters["rows"],
        first_error,
        details,
    )

    if reraised_exc is not None:
        raise reraised_exc.with_traceback(reraised_tb)

    return status
    # NOTE: watermarks deliberately untouched — backfill must never fast-forward the
    # cron's incremental state.
