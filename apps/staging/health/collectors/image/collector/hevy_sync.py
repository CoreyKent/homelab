"""Hevy workouts -> hevy.* tables, as a full authoritative snapshot every run.

Design (23/11/2026, `20261123-hevy-collector`, revised after a cross-vendor audit):
- Every run re-reads ALL workouts from /v1/workouts and rewrites each one (workout row, then its
  exercises and sets deleted and reinserted), so an edit made in the app, including a removed
  set, always lands. There is no events feed and no watermark: the audit showed an events feed
  plus a count check can miss an edit for good. At ~60 workouts a year this is a few requests.
- Workouts stored here but absent from Hevy are deleted, and ONLY when all three guards pass:
  every page was fetched; the fetched ids equal /v1/workouts/count taken in the same run; and the
  deletion removes at most max(3, 10%) of the stored workouts. An empty or short reply from Hevy
  must never wipe the table. A failed guard deletes nothing and records the run as `partial`.
- A session advisory lock makes a second concurrent run (cron or manual) exit 0 without work.
- Exercise templates (name, type, muscle group) are fetched when missing or older than 7 days.
  A template Hevy 404s on (a deleted custom exercise) is stored with the exercise's own title and
  fetch_ok=false, and retried on the same TTL.
- Times arrive in UTC; local_date is the start in TZ_LOCAL (logic.to_local_date).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from collector import db, logic, state
from collector.config import Config
from collector.hevy_api import HevyClient, HevyError, HevyNotFound

logger = logging.getLogger(__name__)

SOURCE, STREAM = "hevy", "workouts"
LOCK_KEY = "hevy.workouts"
TEMPLATE_TTL = timedelta(days=7)
DELETE_FLOOR = 3
DELETE_FRACTION = 0.10

WORKOUT_COLS = (
    "workout_id", "title", "routine_id", "description", "start_ts", "end_ts",
    "local_date", "updated_at", "created_at", "raw",
)
EXERCISE_COLS = ("workout_id", "exercise_idx", "exercise_template_id", "title", "notes", "superset_id")
SET_COLS = (
    "workout_id", "exercise_idx", "set_idx", "set_type", "weight_kg", "reps",
    "distance_m", "duration_s", "rpe", "custom_metric",
)


class HevyDataError(ValueError):
    """A workout from Hevy does not have the shape this module relies on."""


def _ts(value: Any, field: str, required: bool = True) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise HevyDataError(f"{field}: expected an ISO timestamp, got {type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HevyDataError(f"{field}: not ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HevyDataError(f"{field}: timestamp has no offset")
    return parsed


def _num(value: Any, field: str) -> Decimal | None:
    """Exact decimal (42.5 stays 42.5, not 42.49999...). None stays None; 0 stays 0 (bodyweight)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HevyDataError(f"{field}: expected a number, got {type(value).__name__}")
    return Decimal(str(value))


def _int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise HevyDataError(f"{field}: expected an integer, got {value!r}")
    return int(value)


def _str(value: Any, field: str, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise HevyDataError(f"{field}: expected a non-empty string")
    return value


def parse_workout(w: dict, tz_name: str) -> tuple[tuple, list[tuple], list[tuple]]:
    """One API workout -> (workout row, exercise rows, set rows), in *_COLS order."""
    if not isinstance(w, dict):
        raise HevyDataError("workout: expected an object")
    wid = _str(w.get("id"), "workout.id")
    start = _ts(w.get("start_time"), "start_time")
    assert start is not None
    workout = (
        wid,
        _str(w.get("title"), "title"),
        _str(w.get("routine_id"), "routine_id", required=False),
        _str(w.get("description"), "description", required=False),
        start,
        _ts(w.get("end_time"), "end_time", required=False),
        logic.to_local_date(start, tz_name),
        _ts(w.get("updated_at"), "updated_at"),
        _ts(w.get("created_at"), "created_at", required=False),
        Jsonb(w),
    )
    exercises: list[tuple] = []
    sets: list[tuple] = []
    seen_ex: set[int] = set()
    for e in w.get("exercises") or []:
        ex_idx = _int(e.get("index"), "exercise.index")
        if ex_idx is None or ex_idx in seen_ex:
            raise HevyDataError(f"workout {wid}: missing or duplicate exercise index")
        seen_ex.add(ex_idx)
        exercises.append((
            wid, ex_idx,
            _str(e.get("exercise_template_id"), "exercise_template_id"),
            _str(e.get("title"), "exercise.title"),
            _str(e.get("notes"), "exercise.notes", required=False),
            _int(e.get("superset_id"), "superset_id"),
        ))
        seen_set: set[int] = set()
        for s in e.get("sets") or []:
            set_idx = _int(s.get("index"), "set.index")
            if set_idx is None or set_idx in seen_set:
                raise HevyDataError(f"workout {wid}: missing or duplicate set index")
            seen_set.add(set_idx)
            sets.append((
                wid, ex_idx, set_idx,
                _str(s.get("type"), "set.type"),
                _num(s.get("weight_kg"), "weight_kg"),
                _int(s.get("reps"), "reps"),
                _num(s.get("distance_meters"), "distance_meters"),
                _int(s.get("duration_seconds"), "duration_seconds"),
                _num(s.get("rpe"), "rpe"),
                _num(s.get("custom_metric"), "custom_metric"),
            ))
    return workout, exercises, sets


def deletion_allowed(stored: int, stale: int, snapshot_complete: bool, fetched: int, api_count: int) -> str | None:
    """None when stale rows may be deleted, else the reason they may not."""
    if stale == 0:
        return None
    if not snapshot_complete:
        return "snapshot incomplete (page cap reached)"
    if fetched != api_count:
        return f"fetched {fetched} workouts but Hevy counts {api_count}"
    limit = max(DELETE_FLOOR, int(stored * DELETE_FRACTION))
    if stale > limit:
        return f"would delete {stale} of {stored} stored workouts (limit {limit})"
    return None


def _write_snapshot(conn: psycopg.Connection, parsed: list[tuple[tuple, list[tuple], list[tuple]]]) -> tuple[int, int]:
    """Rewrite every fetched workout. Returns (new or changed by Hevy's updated_at, stored before the run)."""
    changed = 0
    with conn.cursor() as cur:
        cur.execute("SELECT workout_id, updated_at FROM hevy.workout")
        before = dict(cur.fetchall())
        for workout, exercises, sets in parsed:
            wid, updated = workout[0], workout[7]
            if before.get(wid) != updated:
                changed += 1
            db.upsert(cur, "hevy.workout", ("workout_id",), WORKOUT_COLS, [workout])
            cur.execute("UPDATE hevy.workout SET fetched_at = now() WHERE workout_id = %s", (wid,))
            # Cascades to sets, so a set or exercise removed in the app disappears here too.
            cur.execute("DELETE FROM hevy.exercise WHERE workout_id = %s", (wid,))
            if exercises:
                cur.executemany(
                    f"INSERT INTO hevy.exercise ({', '.join(EXERCISE_COLS)}) VALUES ({', '.join(['%s'] * len(EXERCISE_COLS))})",
                    exercises,
                )
            if sets:
                cur.executemany(
                    f"INSERT INTO hevy.set ({', '.join(SET_COLS)}) VALUES ({', '.join(['%s'] * len(SET_COLS))})",
                    sets,
                )
    return changed, len(before)


def _refresh_templates(conn: psycopg.Connection, client: HevyClient, titles: dict[str, str], now: datetime) -> tuple[int, list[str]]:
    """Fetch templates that are missing or stale. Returns (fetched, errors). Commits per template."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT template_id FROM hevy.exercise_template WHERE template_id = ANY(%s) AND fetched_at >= %s",
            (list(titles), now - TEMPLATE_TTL),
        )
        fresh = {r[0] for r in cur.fetchall()}
    fetched, errors = 0, []
    for tid in sorted(set(titles) - fresh):
        try:
            t = client.exercise_template(tid)
            row = (tid, _str(t.get("title"), "template.title"), t.get("type"), t.get("primary_muscle_group"),
                   t.get("secondary_muscle_groups"), t.get("equipment"), t.get("is_custom"), True, Jsonb(t))
        except HevyNotFound:
            row = (tid, titles[tid], None, None, None, None, None, False, None)
        except (HevyError, HevyDataError) as exc:
            errors.append(f"{tid}: {client.scrub(str(exc))}")
            continue
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hevy.exercise_template (template_id, title, type, primary_muscle_group,
                    secondary_muscle_groups, equipment, is_custom, fetch_ok, raw, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (template_id) DO UPDATE SET title = EXCLUDED.title, type = EXCLUDED.type,
                    primary_muscle_group = EXCLUDED.primary_muscle_group,
                    secondary_muscle_groups = EXCLUDED.secondary_muscle_groups, equipment = EXCLUDED.equipment,
                    is_custom = EXCLUDED.is_custom, fetch_ok = EXCLUDED.fetch_ok, raw = EXCLUDED.raw,
                    fetched_at = EXCLUDED.fetched_at
                """,
                (*row, now),
            )
        conn.commit()
        fetched += 1
    return fetched, errors


def _try_lock(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (LOCK_KEY,))
        got = bool(cur.fetchone()[0])
    conn.commit()
    return got


def _unlock(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_KEY,))
    conn.commit()


def run_cron(conn: psycopg.Connection, cfg: Config, client: HevyClient, now: datetime | None = None) -> bool:
    """One snapshot run; True when status is ok (or another run holds the lock)."""
    now = now or datetime.now(timezone.utc)
    if not _try_lock(conn):
        logger.info("another hevy run holds the lock; exiting without work")
        return True
    try:
        return _run_locked(conn, cfg, client, now)
    finally:
        try:
            conn.rollback()
            _unlock(conn)
        except psycopg.Error:
            logger.warning("could not release the hevy advisory lock (the session end releases it)")


def _run_locked(conn: psycopg.Connection, cfg: Config, client: HevyClient, now: datetime) -> bool:
    run_id = state.open_run(conn, SOURCE, STREAM, "cron")
    try:
        api_count = client.workout_count()
        snap = client.all_workouts()
        parsed = [parse_workout(w, cfg.tz_local) for w in snap.workouts]
        by_id = {p[0][0]: p for p in parsed}  # a workout can repeat if pages shift mid-walk
        changed, stored_before = _write_snapshot(conn, list(by_id.values()))

        with conn.cursor() as cur:
            cur.execute("SELECT workout_id FROM hevy.workout")
            stored_ids = {r[0] for r in cur.fetchall()}
        stale = sorted(stored_ids - set(by_id))
        blocked = deletion_allowed(stored_before, len(stale), snap.complete, len(by_id), api_count)
        deleted = 0
        if stale and blocked is None:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM hevy.workout WHERE workout_id = ANY(%s)", (stale,))
                deleted = cur.rowcount
        conn.commit()

        titles: dict[str, str] = {}
        for _, exercises, _ in by_id.values():
            for ex in exercises:
                titles.setdefault(ex[2], ex[3])
        templates_fetched, template_errors = _refresh_templates(conn, client, titles, now)

        details: dict[str, Any] = {
            "api_count": api_count, "fetched": len(by_id), "pages": snap.pages_fetched,
            "page_count": snap.page_count, "changed": changed, "deleted": deleted,
            "templates_fetched": templates_fetched,
        }
        problems = []
        if blocked is not None:
            details["deletion_blocked"] = blocked
            details["stale"] = len(stale)
            problems.append(blocked)
        elif not snap.complete or len(by_id) != api_count:
            # Nothing to delete, but the snapshot still disagrees with Hevy's own count.
            problems.append(f"fetched {len(by_id)} workouts, Hevy counts {api_count}, complete={snap.complete}")
        if template_errors:
            details["template_errors"] = template_errors[:5]
            problems.append(f"{len(template_errors)} template fetch(es) failed")
        status = "partial" if problems else "ok"
        if status == "ok":
            state.set_extra(conn, SOURCE, STREAM, {"last_full_sync": now.isoformat()})
        state.close_run(conn, run_id, status, days_attempted=1, days_failed=0 if status == "ok" else 1,
                        rows_upserted=changed, first_error=(problems[0] if problems else None), details=details)
        logger.info("hevy run %s: %s", status, details)
        return status == "ok"
    except Exception as exc:
        conn.rollback()
        msg = client.scrub(str(exc))
        logger.error("hevy run failed: %s: %s", type(exc).__name__, msg)
        state.close_run(conn, run_id, "failed", days_attempted=1, days_failed=1, rows_upserted=0,
                        first_error=msg, details={"error_type": type(exc).__name__})
        return False
