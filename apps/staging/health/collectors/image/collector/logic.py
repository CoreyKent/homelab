"""Pure decision logic for collector sync runs.

This module is intentionally stdlib-only so tests can validate watermark overlap, Garmin
account blocking, local-date conversion, and Withings normalization without network clients
or database drivers installed. Helpers reject ambiguous inputs such as naive datetimes
because silent assumptions in ingestion code hide real data-loss bugs.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import random
from zoneinfo import ZoneInfo

OVERLAP_HOURS: int = 72
DEFAULT_BLOCK_SECONDS: int = 3600
DEFAULT_LOOKBACK_DAYS: int = 7

WITHINGS_METRIC_BY_TYPE: dict[int, str] = {
    1: "weight",
    5: "fat_free_mass",
    6: "fat_ratio",
    8: "fat_mass",
    76: "muscle_mass",
    77: "hydration",
    88: "bone_mass",
}


def overlap_start(
    watermark: datetime | None,
    overlap_hours: int = OVERLAP_HOURS,
) -> datetime | None:
    """Watermark minus overlap; None when no watermark exists yet."""
    if watermark is None:
        return None
    return watermark - timedelta(hours=overlap_hours)


def is_blocked(blocked_until: datetime | None, now: datetime) -> bool:
    """True while the account-level 429 sentinel is in the future."""
    if blocked_until is None:
        return False
    return blocked_until > now


def compute_blocked_until(
    now: datetime,
    retry_after: str | int | None,
    default_seconds: int = DEFAULT_BLOCK_SECONDS,
) -> datetime:
    """now + Retry-After seconds when parseable and positive, else now + default.
    Accepts int, delta-seconds string; anything unparseable (incl. HTTP-date form,
    None, negative) falls back to the default — a wrong guess must err long, not short.
    """
    seconds = default_seconds

    if isinstance(retry_after, int):
        if retry_after > 0:
            seconds = retry_after
    elif isinstance(retry_after, str):
        candidate = retry_after.strip()
        if candidate:
            try:
                parsed = int(candidate, 10)
            except ValueError:
                parsed = None
            if parsed is not None and parsed > 0:
                seconds = parsed

    return now + timedelta(seconds=seconds)


def normalize_measure(value: int, unit: int) -> Decimal:
    """Withings getmeas normalization: value * 10^unit, quantized to 3 dp
    (matches numeric(8,3)). E.g. value=72500, unit=-3 -> Decimal('72.500').
    """
    normalized = Decimal(value).scaleb(unit)
    return normalized.quantize(Decimal("0.001"))


def metric_for_type(measure_type: int) -> str:
    """Known-type name from WITHINGS_METRIC_BY_TYPE, else 'type_<N>' passthrough."""
    return WITHINGS_METRIC_BY_TYPE.get(measure_type, f"type_{measure_type}")


def to_local_date(ts: datetime, tz_name: str) -> date:
    """Aware UTC timestamp -> local calendar date. Raises ValueError on naive input:
    a naive timestamp is a bug upstream, never silently assumed UTC.
    """
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("naive datetime is invalid for local-date conversion")
    return ts.astimezone(ZoneInfo(tz_name)).date()


def date_range(start: date, end: date) -> list[date]:
    """Inclusive ascending list of dates."""
    if start > end:
        # Inverted ranges mean there is no work to do, which composes more cleanly than raising.
        return []

    span_days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span_days + 1)]


def chunk_ranges(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    """Oldest->newest inclusive (start,end) chunks of at most chunk_days days each,
    covering [start, end] exactly, no overlap, no gap. chunk_days >= 1 else ValueError.
    """
    if chunk_days < 1:
        raise ValueError("chunk_days must be >= 1")
    if start > end:
        return []

    chunks: list[tuple[date, date]] = []
    chunk_start = start

    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)

    return chunks


def sync_dates(
    watermark: datetime | None,
    today_local: date,
    tz_name: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[date]:
    """Which local calendar dates a cron run must (re)fetch: from the local date of
    (watermark - 72h) through today_local inclusive; without a watermark, the last
    lookback_days days through today_local.
    """
    if lookback_days < 1:
        raise ValueError("lookback_days must be >= 1")

    if watermark is None:
        start_date = today_local - timedelta(days=lookback_days - 1)
    else:
        start_dt = overlap_start(watermark)
        if start_dt is None:
            # The branch is unreachable today, but keeping it explicit avoids hidden assumptions.
            start_date = today_local
        else:
            start_date = to_local_date(start_dt, tz_name)

    return date_range(start_date, today_local)


def jitter_seconds(pacing_min: float) -> float:
    """pacing_min + random.uniform(0, 0.5) — Garmin inter-request pacing with jitter."""
    return pacing_min + random.uniform(0.0, 0.5)


def parse_epoch_ms(ms: int | float) -> datetime:
    """Milliseconds since epoch -> aware UTC datetime (Garmin intraday arrays)."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
