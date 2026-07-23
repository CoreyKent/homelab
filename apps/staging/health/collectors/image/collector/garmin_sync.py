"""Garmin stream ingestion and cron orchestration.

Design rules: every Garmin HTTP call is funneled through one guarded path so pacing and
account-level 429 blocking are enforced uniformly, while top-level payloads are shape-checked
before use and malformed leaf samples are skipped so one drifting unofficial-API record doesn't
discard an otherwise valid day.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from numbers import Real
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from garminconnect import Garmin, GarminConnectTooManyRequestsError
from psycopg.types.json import Json
from requests import HTTPError

from collector import logic, state
from collector.config import Config
from collector.db import upsert

logger = logging.getLogger(__name__)


def _parse_gmt(value: str) -> datetime:
    """Parse Garmin's naive-UTC GMT strings and attach UTC explicitly."""
    if not isinstance(value, str):
        raise ValueError(f"expected Garmin GMT timestamp string, got {type(value).__name__}")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unsupported Garmin GMT timestamp shape: {value!r}")


def _guarded_get(mapping: Any, *keys: str) -> Any | None:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def _sample_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Real):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any, field: str) -> float | None:
    if value is None:
        return None
    number = _sample_float(value)
    if number is None:
        raise ValueError(f"invalid {field}: {value!r}")
    return number


def _coerce_int(value: Any, field: str) -> int | None:
    number = _coerce_float(value, field)
    if number is None:
        return None
    return int(number)


def _round_int(value: Any, field: str) -> int | None:
    number = _coerce_float(value, field)
    if number is None:
        return None
    return round(number)


def _extract_retry_after(exc: Exception) -> str | None:
    for headers in (
        getattr(getattr(exc, "response", None), "headers", None),
        getattr(exc, "headers", None),
    ):
        if hasattr(headers, "get"):
            retry_after = headers.get("Retry-After")
            if retry_after is not None:
                return str(retry_after)
    return None


def _blocked_until_iso(exc: state.AccountBlockedError) -> str | None:
    blocked_until = getattr(exc, "blocked_until", None)
    if isinstance(blocked_until, datetime):
        return blocked_until.isoformat()
    if exc.args:
        first = exc.args[0]
        if isinstance(first, datetime):
            return first.isoformat()
        if first is not None:
            return str(first)
    return None


def _failure(stream: str, unit: str, error: str) -> dict[str, str]:
    return {"stream": stream, "unit": unit, "error": error}


class GarminStreams:
    """One Garmin sync worker bound to a single run and connection."""

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        client: Garmin,
        cfg: Config,
        budget: object | None = None,
    ) -> None:
        self.conn = conn
        self.client = client
        self.cfg = cfg
        self.budget = budget

    def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Route every Garmin request through the account block and pacing guard."""
        state.check_blocked(self.conn)
        if self.budget is not None:
            spend = getattr(self.budget, "spend", None)
            if not callable(spend):
                raise TypeError("budget must expose a callable spend()")
            spend()
        time.sleep(logic.jitter_seconds(self.cfg.garmin_pacing_min))
        try:
            return fn(*args, **kwargs)
        except GarminConnectTooManyRequestsError as exc:
            blocked_until = state.set_blocked(self.conn, _extract_retry_after(exc))
            raise state.AccountBlockedError(blocked_until) from exc
        except HTTPError as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 429:
                blocked_until = state.set_blocked(self.conn, _extract_retry_after(exc))
                raise state.AccountBlockedError(blocked_until) from exc
            raise

    def _upsert_rows(
        self,
        table: str,
        key_cols: tuple[str, ...],
        cols: tuple[str, ...],
        rows: list[tuple[Any, ...]],
        *,
        set_ingested_at: bool,
    ) -> int:
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            return upsert(
                cur,
                table,
                key_cols,
                cols,
                rows,
                set_ingested_at=set_ingested_at,
            )

    def sync_daily_summary(self, day: date) -> int:
        iso = day.isoformat()
        payload = self._call(self.client.get_user_summary, iso)
        if payload is None or payload == {}:
            logger.info("Garmin daily_summary skipped for %s: no data", iso)
            return 0
        if not isinstance(payload, dict):
            raise ValueError(f"daily summary response for {iso} was not a dict")

        stress_avg = _coerce_int(payload.get("averageStressLevel"), "averageStressLevel")
        if stress_avg is not None and stress_avg < 0:
            stress_avg = None

        cols = (
            "calendar_date",
            "steps",
            "resting_hr",
            "stress_avg",
            "calories_active",
            "calories_total",
            "body_battery_high",
            "body_battery_low",
            "raw",
        )
        rows = [
            (
                day,
                _coerce_int(payload.get("totalSteps"), "totalSteps"),
                _coerce_int(payload.get("restingHeartRate"), "restingHeartRate"),
                stress_avg,
                _coerce_int(payload.get("activeKilocalories"), "activeKilocalories"),
                _coerce_int(payload.get("totalKilocalories"), "totalKilocalories"),
                _coerce_int(payload.get("bodyBatteryHighestValue"), "bodyBatteryHighestValue"),
                _coerce_int(payload.get("bodyBatteryLowestValue"), "bodyBatteryLowestValue"),
                Json(payload),
            )
        ]
        return self._upsert_rows(
            "garmin.daily_summary",
            ("calendar_date",),
            cols,
            rows,
            set_ingested_at=True,
        )

    def sync_intraday(self, day: date) -> int:
        iso = day.isoformat()
        rows: list[tuple[Any, ...]] = []
        cols = ("metric", "ts", "value")

        heart_payload = self._call(self.client.get_heart_rates, iso)
        if heart_payload is None or heart_payload == {}:
            logger.info("Garmin intraday heart_rate skipped for %s: no data", iso)
        else:
            if not isinstance(heart_payload, dict):
                raise ValueError(f"heart rate response for {iso} was not a dict")
            heart_values = heart_payload.get("heartRateValues")
            if heart_values in (None, []):
                logger.info("Garmin intraday heart_rate skipped for %s: no data", iso)
            else:
                if not isinstance(heart_values, list):
                    raise ValueError(f"heartRateValues for {iso} was not a list")
                for entry in heart_values:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    ts = None
                    try:
                        ts = logic.parse_epoch_ms(entry[0])
                    except Exception:
                        continue
                    hr = _sample_float(entry[1])
                    if hr is None or hr <= 0:
                        continue
                    rows.append(("heart_rate", ts, hr))

        stress_payload = self._call(self.client.get_stress_data, iso)
        if stress_payload is None or stress_payload == {}:
            logger.info("Garmin intraday stress skipped for %s: no data", iso)
            logger.info("Garmin intraday body_battery skipped for %s: no data", iso)
        else:
            if not isinstance(stress_payload, dict):
                raise ValueError(f"stress response for {iso} was not a dict")

            stress_values = stress_payload.get("stressValuesArray")
            if stress_values in (None, []):
                logger.info("Garmin intraday stress skipped for %s: no data", iso)
            else:
                if not isinstance(stress_values, list):
                    raise ValueError(f"stressValuesArray for {iso} was not a list")
                for entry in stress_values:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    try:
                        ts = logic.parse_epoch_ms(entry[0])
                    except Exception:
                        continue
                    level = _sample_float(entry[1])
                    if level is None or level < 0:
                        continue
                    rows.append(("stress", ts, level))

            body_battery_values = stress_payload.get("bodyBatteryValuesArray")
            if body_battery_values in (None, []):
                logger.info("Garmin intraday body_battery skipped for %s: no data", iso)
            else:
                if not isinstance(body_battery_values, list):
                    raise ValueError(f"bodyBatteryValuesArray for {iso} was not a list")
                for entry in body_battery_values:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    try:
                        ts = logic.parse_epoch_ms(entry[0])
                    except Exception:
                        continue
                    raw_level = entry[2] if len(entry) >= 3 else entry[1]
                    level = _sample_float(raw_level)
                    if level is None:
                        continue
                    rows.append(("body_battery", ts, level))

        spo2_payload = self._call(self.client.get_spo2_data, iso)
        if spo2_payload is None or spo2_payload == {}:
            logger.info("Garmin intraday spo2 skipped for %s: no data", iso)
        else:
            if not isinstance(spo2_payload, dict):
                raise ValueError(f"spo2 response for {iso} was not a dict")
            spo2_values: Any = None
            for key in ("spO2SingleValues", "spO2HourlyAverages"):
                candidate = spo2_payload.get(key)
                if candidate in (None, []):
                    continue
                if not isinstance(candidate, list):
                    raise ValueError(f"{key} for {iso} was not a list")
                spo2_values = candidate
                break
            if spo2_values is None:
                logger.info("Garmin intraday spo2 skipped for %s: no data", iso)
            else:
                for entry in spo2_values:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    try:
                        ts = logic.parse_epoch_ms(entry[0])
                    except Exception:
                        continue
                    value = _sample_float(entry[1])
                    if value is None or value <= 0:
                        continue
                    rows.append(("spo2", ts, value))

        respiration_payload = self._call(self.client.get_respiration_data, iso)
        if respiration_payload is None or respiration_payload == {}:
            logger.info("Garmin intraday respiration skipped for %s: no data", iso)
        else:
            if not isinstance(respiration_payload, dict):
                raise ValueError(f"respiration response for {iso} was not a dict")
            respiration_values = respiration_payload.get("respirationValuesArray")
            if respiration_values in (None, []):
                logger.info("Garmin intraday respiration skipped for %s: no data", iso)
            else:
                if not isinstance(respiration_values, list):
                    raise ValueError(f"respirationValuesArray for {iso} was not a list")
                for entry in respiration_values:
                    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                        continue
                    try:
                        ts = logic.parse_epoch_ms(entry[0])
                    except Exception:
                        continue
                    value = _sample_float(entry[1])
                    if value is None or value <= 0:
                        continue
                    rows.append(("respiration", ts, value))

        return self._upsert_rows(
            "garmin.intraday",
            ("metric", "ts"),
            cols,
            rows,
            set_ingested_at=False,
        )

    def sync_steps_epoch(self, day: date) -> int:
        iso = day.isoformat()
        payload = self._call(self.client.get_steps_data, iso)
        if payload is None or payload == [] or payload == {}:
            logger.info("Garmin steps_epoch skipped for %s: no data", iso)
            return 0
        if not isinstance(payload, list):
            raise ValueError(f"steps epoch response for {iso} was not a list")

        cols = ("start_ts", "duration_s", "steps", "activity_level")
        rows: list[tuple[Any, ...]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue

            start_raw = entry.get("startGMT")
            if start_raw is None:
                continue
            steps_number = _sample_float(entry.get("steps"))
            if steps_number is None:
                continue

            try:
                start_ts = _parse_gmt(start_raw)
            except Exception:
                continue

            end_raw = entry.get("endGMT")
            if end_raw is None:
                duration_s = 900
            else:
                try:
                    duration_s = int((_parse_gmt(end_raw) - start_ts).total_seconds())
                except Exception:
                    duration_s = 900
                if duration_s <= 0:
                    # Garmin epochs are fixed 15-minute buckets; bad end bounds should not create nonsense widths.
                    duration_s = 900

            activity_level_value = entry.get("primaryActivityLevel")
            activity_level = None if activity_level_value is None else str(activity_level_value)
            rows.append((start_ts, duration_s, int(steps_number), activity_level))

        return self._upsert_rows(
            "garmin.steps_epoch",
            ("start_ts",),
            cols,
            rows,
            set_ingested_at=False,
        )

    def sync_sleep(self, day: date) -> int:
        iso = day.isoformat()
        payload = self._call(self.client.get_sleep_data, iso)
        if payload is None or payload == [] or payload == {}:
            logger.info("Garmin sleep skipped for %s: no data", iso)
            return 0
        if not isinstance(payload, dict):
            raise ValueError(f"sleep response for {iso} was not a dict")

        dto = payload.get("dailySleepDTO")
        if dto is None or dto == {}:
            logger.info("Garmin sleep skipped for %s: no data", iso)
            return 0
        if not isinstance(dto, dict):
            raise ValueError(f"dailySleepDTO for {iso} was not a dict")

        duration_s = _coerce_int(dto.get("sleepTimeSeconds"), "sleepTimeSeconds")
        start_ms = dto.get("sleepStartTimestampGMT")
        if start_ms is None and (duration_s is None or duration_s <= 0):
            logger.info("Garmin sleep skipped for %s: no data", iso)
            return 0

        calendar_date = date.fromisoformat(dto["calendarDate"])
        sleep_start = logic.parse_epoch_ms(start_ms) if start_ms is not None else None
        end_ms = dto.get("sleepEndTimestampGMT")
        sleep_end = logic.parse_epoch_ms(end_ms) if end_ms is not None else None

        sleep_cols = (
            "calendar_date",
            "sleep_start",
            "sleep_end",
            "duration_s",
            "deep_s",
            "light_s",
            "rem_s",
            "awake_s",
            "score",
            "avg_spo2",
            "avg_respiration",
            "raw",
        )
        sleep_rows = [
            (
                calendar_date,
                sleep_start,
                sleep_end,
                duration_s,
                _coerce_int(dto.get("deepSleepSeconds"), "deepSleepSeconds"),
                _coerce_int(dto.get("lightSleepSeconds"), "lightSleepSeconds"),
                _coerce_int(dto.get("remSleepSeconds"), "remSleepSeconds"),
                _coerce_int(dto.get("awakeSleepSeconds"), "awakeSleepSeconds"),
                _coerce_int(
                    _guarded_get(dto, "sleepScores", "overall", "value"),
                    "sleepScores.overall.value",
                ),
                _coerce_float(dto.get("averageSpO2Value"), "averageSpO2Value"),
                _coerce_float(
                    dto.get("averageRespirationValue"),
                    "averageRespirationValue",
                ),
                Json(payload),
            )
        ]
        rows_upserted = self._upsert_rows(
            "garmin.sleep",
            ("calendar_date",),
            sleep_cols,
            sleep_rows,
            set_ingested_at=True,
        )

        sleep_levels = payload.get("sleepLevels") or []
        if not isinstance(sleep_levels, list):
            raise ValueError(f"sleepLevels for {iso} was not a list")

        stage_map = {0.0: "deep", 1.0: "light", 2.0: "rem", 3.0: "awake"}
        stage_cols = ("calendar_date", "start_ts", "end_ts", "stage")
        stage_rows: list[tuple[Any, ...]] = []
        for entry in sleep_levels:
            if not isinstance(entry, dict):
                continue
            start_raw = entry.get("startGMT")
            end_raw = entry.get("endGMT")
            level_number = _sample_float(entry.get("activityLevel"))
            if start_raw is None or end_raw is None or level_number is None:
                continue
            stage = stage_map.get(float(level_number))
            if stage is None:
                continue
            try:
                start_ts = _parse_gmt(start_raw)
                end_ts = _parse_gmt(end_raw)
            except Exception:
                continue
            stage_rows.append((calendar_date, start_ts, end_ts, stage))

        rows_upserted += self._upsert_rows(
            "garmin.sleep_stage",
            ("calendar_date", "start_ts"),
            stage_cols,
            stage_rows,
            set_ingested_at=False,
        )
        return rows_upserted

    def sync_hrv(self, day: date) -> int:
        iso = day.isoformat()
        payload = self._call(self.client.get_hrv_data, iso)
        if payload is None or payload == [] or payload == {}:
            logger.info("Garmin hrv skipped for %s: no data", iso)
            return 0
        if not isinstance(payload, dict):
            raise ValueError(f"hrv response for {iso} was not a dict")

        summary = payload.get("hrvSummary")
        if summary is None or summary == {}:
            logger.info("Garmin hrv skipped for %s: no data", iso)
            return 0
        if not isinstance(summary, dict):
            raise ValueError(f"hrvSummary for {iso} was not a dict")

        status_value = summary.get("status")
        cols = (
            "calendar_date",
            "last_night_avg_ms",
            "weekly_avg_ms",
            "status",
            "baseline_low_ms",
            "baseline_high_ms",
        )
        rows = [
            (
                day,
                _coerce_int(summary.get("lastNightAvg"), "lastNightAvg"),
                _coerce_int(summary.get("weeklyAvg"), "weeklyAvg"),
                None if status_value is None else str(status_value),
                _coerce_int(
                    _guarded_get(summary, "baseline", "balancedLow"),
                    "baseline.balancedLow",
                ),
                _coerce_int(
                    _guarded_get(summary, "baseline", "balancedUpper"),
                    "baseline.balancedUpper",
                ),
            )
        ]
        return self._upsert_rows(
            "garmin.hrv",
            ("calendar_date",),
            cols,
            rows,
            set_ingested_at=True,
        )

    def sync_training_status(self, day: date) -> int:
        iso = day.isoformat()
        payload = self._call(self.client.get_training_status, iso)
        if payload is None or payload == [] or payload == {}:
            logger.info("Garmin training_status skipped for %s: no data", iso)
            return 0
        if not isinstance(payload, dict):
            raise ValueError(f"training status response for {iso} was not a dict")

        latest_training_status = _guarded_get(
            payload,
            "mostRecentTrainingStatus",
            "latestTrainingStatusData",
        )
        if latest_training_status is not None and not isinstance(latest_training_status, dict):
            raise ValueError(f"latestTrainingStatusData for {iso} was not a dict")

        first_status_payload = None
        if isinstance(latest_training_status, dict) and latest_training_status:
            first_status_payload = next(iter(latest_training_status.values()))

        status_value = _guarded_get(first_status_payload, "trainingStatus")
        # Garmin's training-load payload drifts too often to map safely here; raw preserves it for reprocessing.
        load_7d = None

        cols = ("calendar_date", "status", "vo2max", "load_7d", "raw")
        rows = [
            (
                day,
                None if status_value is None else str(status_value),
                _coerce_float(
                    _guarded_get(
                        payload,
                        "mostRecentVO2Max",
                        "generic",
                        "vo2MaxValue",
                    ),
                    "mostRecentVO2Max.generic.vo2MaxValue",
                ),
                load_7d,
                Json(payload),
            )
        ]
        return self._upsert_rows(
            "garmin.training_status",
            ("calendar_date",),
            cols,
            rows,
            set_ingested_at=True,
        )

    def sync_activities(self, start_day: date, end_day: date) -> tuple[int, list[dict[str, str]]]:
        start_iso = start_day.isoformat()
        end_iso = end_day.isoformat()
        rows_upserted = 0
        failures: list[dict[str, str]] = []

        try:
            payload = self._call(self.client.get_activities_by_date, start_iso, end_iso)
        except state.AccountBlockedError as exc:
            setattr(exc, "rows_upserted", rows_upserted)
            setattr(exc, "failures", failures)
            raise

        if payload is None or payload == [] or payload == {}:
            logger.info("Garmin activity skipped for %s..%s: no data", start_iso, end_iso)
            return 0, failures
        if not isinstance(payload, list):
            raise ValueError(f"activity listing response for {start_iso}..{end_iso} was not a list")

        cols = (
            "activity_id",
            "start_ts",
            "local_date",
            "activity_type",
            "name",
            "duration_s",
            "distance_m",
            "calories",
            "avg_hr",
            "max_hr",
            "elevation_gain_m",
            "avg_pace_s_km",
            "hr_zones",
            "raw",
        )

        for activity in payload:
            unit = "unknown"
            try:
                if not isinstance(activity, dict):
                    raise ValueError("activity entry was not a dict")

                unit = str(activity.get("activityId", "unknown"))
                activity_id = int(activity["activityId"])
                start_ts = _parse_gmt(activity["startTimeGMT"])
                local_date = logic.to_local_date(start_ts, self.cfg.tz_local)

                activity_type_value = _guarded_get(activity, "activityType", "typeKey")
                activity_type = "unknown" if activity_type_value is None else str(activity_type_value)

                name_value = activity.get("activityName")
                name = None if name_value is None else str(name_value)

                duration_s = _round_int(activity.get("duration"), "duration")
                distance_m = _coerce_float(activity.get("distance"), "distance")
                calories = _round_int(activity.get("calories"), "calories")
                avg_hr = _coerce_int(activity.get("averageHR"), "averageHR")
                max_hr = _coerce_int(activity.get("maxHR"), "maxHR")
                elevation_gain_m = _coerce_float(
                    activity.get("elevationGain"),
                    "elevationGain",
                )

                average_speed = _coerce_float(activity.get("averageSpeed"), "averageSpeed")
                if average_speed is None or average_speed <= 0:
                    avg_pace_s_km = None
                else:
                    avg_pace_s_km = 1000.0 / average_speed

                try:
                    zones = self._call(self.client.get_activity_hr_in_timezones, activity_id)
                except state.AccountBlockedError as exc:
                    setattr(exc, "rows_upserted", rows_upserted)
                    setattr(exc, "failures", failures)
                    raise

                if zones is None or zones == [] or zones == {}:
                    hr_zones = None
                else:
                    if not isinstance(zones, (dict, list)):
                        raise ValueError(
                            f"activity hr zones response for {activity_id} had unexpected shape"
                        )
                    hr_zones = Json(zones)

                rows_upserted += self._upsert_rows(
                    "garmin.activity",
                    ("activity_id",),
                    cols,
                    [
                        (
                            activity_id,
                            start_ts,
                            local_date,
                            activity_type,
                            name,
                            duration_s,
                            distance_m,
                            calories,
                            avg_hr,
                            max_hr,
                            elevation_gain_m,
                            avg_pace_s_km,
                            hr_zones,
                            Json(activity),
                        )
                    ],
                    set_ingested_at=True,
                )
            except state.AccountBlockedError:
                raise
            except Exception as exc:
                failures.append(_failure("activity", unit, str(exc)))

        return rows_upserted, failures


DAY_STREAMS: tuple[tuple[str, Callable[[GarminStreams, date], int]], ...] = (
    ("daily_summary", GarminStreams.sync_daily_summary),
    ("intraday", GarminStreams.sync_intraday),
    ("steps_epoch", GarminStreams.sync_steps_epoch),
    ("sleep", GarminStreams.sync_sleep),
    ("hrv", GarminStreams.sync_hrv),
    ("training_status", GarminStreams.sync_training_status),
)


def run_cron(conn: psycopg.Connection[Any], client: Garmin, cfg: Config) -> bool:
    """One garmin cron run. Returns True when status == 'ok'."""
    run_id = state.open_run(conn, "garmin", "_all", "cron")
    run_started_utc = datetime.now(timezone.utc)
    streams = GarminStreams(conn, client, cfg)
    today_local = datetime.now(ZoneInfo(cfg.tz_local)).date()

    days_attempted = 0
    days_failed = 0
    rows_upserted = 0
    failures: list[dict[str, str]] = []

    try:
        for stream_name, method in DAY_STREAMS:
            stream_failures = 0
            dates = logic.sync_dates(
                state.get_watermark(conn, "garmin", stream_name),
                today_local,
                cfg.tz_local,
            )

            if not dates:
                state.set_watermark(conn, "garmin", stream_name, run_started_utc)
                conn.commit()
            else:
                for day in dates:
                    days_attempted += 1
                    try:
                        rows_upserted += method(streams, day)
                    except state.AccountBlockedError:
                        days_failed += 1
                        raise
                    except Exception as exc:
                        days_failed += 1
                        stream_failures += 1
                        failures.append(_failure(stream_name, day.isoformat(), str(exc)))
                        conn.rollback()
                    else:
                        conn.commit()

                if stream_failures == 0:
                    state.set_watermark(conn, "garmin", stream_name, run_started_utc)
                    conn.commit()

        activity_dates = logic.sync_dates(
            state.get_watermark(conn, "garmin", "activity"),
            today_local,
            cfg.tz_local,
        )
        if not activity_dates:
            state.set_watermark(conn, "garmin", "activity", run_started_utc)
            conn.commit()
        else:
            activity_unit = (
                f"{activity_dates[0].isoformat()}..{activity_dates[-1].isoformat()}"
            )
            try:
                activity_rows, activity_failures = streams.sync_activities(
                    activity_dates[0],
                    activity_dates[-1],
                )
            except state.AccountBlockedError:
                days_attempted += 1
                days_failed += 1
                raise
            except Exception as exc:
                days_attempted += 1
                days_failed += 1
                failures.append(_failure("activity", activity_unit, str(exc)))
                conn.rollback()
            else:
                rows_upserted += activity_rows
                days_attempted += activity_rows + len(activity_failures)
                days_failed += len(activity_failures)
                failures.extend(activity_failures)
                if not activity_failures:
                    state.set_watermark(conn, "garmin", "activity", run_started_utc)
                conn.commit()

        if not failures:
            status = "ok"
        elif days_attempted > 0 and days_failed == days_attempted:
            status = "failed"
        else:
            status = "partial"

        state.close_run(
            conn,
            run_id,
            status=status,
            days_attempted=days_attempted,
            days_failed=days_failed,
            rows_upserted=rows_upserted,
            first_error=failures[0]["error"] if failures else None,
            details={"failures": failures[:50]},
        )
        conn.commit()
        return status == "ok"

    except state.AccountBlockedError as exc:
        partial_rows = getattr(exc, "rows_upserted", None)
        if isinstance(partial_rows, int) and partial_rows >= 0:
            rows_upserted += partial_rows
            days_attempted += partial_rows

        partial_failures = getattr(exc, "failures", None)
        if isinstance(partial_failures, list):
            clean_failures: list[dict[str, str]] = []
            for entry in partial_failures:
                if not isinstance(entry, dict):
                    continue
                stream = entry.get("stream")
                unit = entry.get("unit")
                error = entry.get("error")
                if isinstance(stream, str) and isinstance(unit, str) and isinstance(error, str):
                    clean_failures.append({"stream": stream, "unit": unit, "error": error})
            failures.extend(clean_failures)
            days_attempted += len(clean_failures)
            days_failed += len(clean_failures)

        blocked_until = _blocked_until_iso(exc)
        blocked_message = "Garmin account blocked"
        if blocked_until is not None:
            blocked_message = f"{blocked_message} until {blocked_until}"

        state.close_run(
            conn,
            run_id,
            status="failed",
            days_attempted=days_attempted,
            days_failed=days_failed,
            rows_upserted=rows_upserted,
            first_error=blocked_message,
            details={"failures": failures[:50], "blocked_until": blocked_until},
        )
        conn.commit()
        return False

    except Exception as exc:
        fatal_error = str(exc)
        if not failures:
            failures.append(_failure("_all", "fatal", fatal_error))

        try:
            conn.rollback()
        except Exception:
            pass

        try:
            state.close_run(
                conn,
                run_id,
                status="failed",
                days_attempted=days_attempted,
                days_failed=days_failed,
                rows_upserted=rows_upserted,
                first_error=failures[0]["error"] if failures else fatal_error,
                details={"failures": failures[:50]},
            )
            conn.commit()
        except Exception:
            logger.exception("Failed to close Garmin run %s after fatal error", run_id)
        raise
