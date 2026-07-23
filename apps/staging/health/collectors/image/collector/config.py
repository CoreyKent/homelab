"""Environment-backed configuration for collector.

Design rules: fail fast with one complete missing-variable error, keep Garmin-only runs
independent from Withings secrets, and validate numeric settings at startup so CronJobs
die loudly instead of drifting into partial work.
"""

from __future__ import annotations

from dataclasses import dataclass
import os


class ConfigError(Exception):
    """Raised when process environment cannot produce a usable runtime configuration."""


@dataclass(frozen=True)
class Config:
    pghost: str
    pgport: int
    pgdatabase: str
    pguser: str
    pgpassword: str
    withings_client_id: str | None
    withings_client_secret: str | None
    garmin_seed_path: str
    tz_local: str
    garmin_pacing_min: float


def _read_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value == "":
        # Empty strings are treated as absent because they cannot satisfy connection or auth settings.
        return None
    return value


def _parse_int(name: str, raw_value: str) -> int:
    try:
        return int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid {name}: expected integer, got {raw_value!r}") from exc


def _parse_float(name: str, raw_value: str) -> float:
    try:
        return float(raw_value)
    except ValueError as exc:
        raise ConfigError(f"Invalid {name}: expected float, got {raw_value!r}") from exc


def load_config(require_withings: bool = False) -> Config:
    pguser = _read_env("PGUSER")
    pgpassword = _read_env("PGPASSWORD")
    withings_client_id = _read_env("WITHINGS_CLIENT_ID")
    withings_client_secret = _read_env("WITHINGS_CLIENT_SECRET")

    missing: list[str] = []
    if pguser is None:
        missing.append("PGUSER")
    if pgpassword is None:
        missing.append("PGPASSWORD")
    if require_withings:
        if withings_client_id is None:
            missing.append("WITHINGS_CLIENT_ID")
        if withings_client_secret is None:
            missing.append("WITHINGS_CLIENT_SECRET")

    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    pghost = _read_env("PGHOST") or "health-pg-rw.health.svc"
    pgport = _parse_int("PGPORT", _read_env("PGPORT") or "5432")
    pgdatabase = _read_env("PGDATABASE") or "health"
    garmin_seed_path = _read_env("GARMIN_SEED_PATH") or "/garmin-seed/garmin_tokens.json"
    tz_local = _read_env("TZ_LOCAL") or "Australia/Sydney"
    garmin_pacing_min = _parse_float(
        "GARMIN_PACING_MIN",
        _read_env("GARMIN_PACING_MIN") or "0.5",
    )

    assert pguser is not None
    assert pgpassword is not None

    return Config(
        pghost=pghost,
        pgport=pgport,
        pgdatabase=pgdatabase,
        pguser=pguser,
        pgpassword=pgpassword,
        withings_client_id=withings_client_id,
        withings_client_secret=withings_client_secret,
        garmin_seed_path=garmin_seed_path,
        tz_local=tz_local,
        garmin_pacing_min=garmin_pacing_min,
    )
