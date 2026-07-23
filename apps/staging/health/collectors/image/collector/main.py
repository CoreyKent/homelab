"""Command-line entrypoint for collector CronJobs and operator workflows.

This module keeps runtime behavior explicit and boring: configuration failures map to a
distinct exit code, operational failures surface as non-zero exits, and unexpected problems
are allowed to traceback for diagnosis. The interactive Garmin bootstrap is isolated behind
a TTY guard so the credential path cannot run inside a cluster pod.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from collections.abc import Sequence
from typing import Any

import psycopg

from collector import backfill, db, garmin_auth, garmin_sync, state, withings_sync
from collector.config import ConfigError, load_config
from collector.withings_auth import WithingsAuthError

_GARMIN_SEED_DEFAULT = "/garmin-seed/garmin_tokens.json"


def _positive_int_arg(value: str) -> int:
    """Parse a strictly positive integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _run_garmin() -> int:
    cfg = load_config()
    conn: Any | None = None
    client: Any | None = None
    try:
        conn = db.connect(cfg)
        client = garmin_auth.login_client(conn, cfg)
        ok = garmin_sync.run_cron(conn, client, cfg)
        return 0 if ok else 1
    finally:
        try:
            # Mid-run token refreshes must survive pod exit, but there is nothing to save if
            # authentication never produced a client.
            if conn is not None and client is not None:
                garmin_auth.persist_current(conn, client)
        finally:
            if conn is not None:
                conn.close()


def _run_withings() -> int:
    cfg = load_config(require_withings=True)
    conn: Any | None = None
    try:
        conn = db.connect(cfg)
        ok = withings_sync.run_cron(conn, cfg)
        return 0 if ok else 1
    finally:
        if conn is not None:
            conn.close()


def _run_backfill(args: argparse.Namespace) -> int:
    source = args.source
    chunk_days = args.chunk_days
    budget_limit = args.budget

    cfg = load_config(require_withings=(source == "withings"))
    conn: Any | None = None
    client: Any | None = None
    try:
        conn = db.connect(cfg)
        if source == "garmin":
            client = garmin_auth.login_client(conn, cfg)
        status = backfill.run_backfill(
            conn,
            cfg,
            source,
            chunk_days,
            budget_limit,
            client=client,
        )
        return 0 if status in ("ok", "partial") else 1
    finally:
        try:
            if conn is not None and client is not None:
                garmin_auth.persist_current(conn, client)
        finally:
            if conn is not None:
                conn.close()


def _run_bootstrap_garmin() -> int:
    # CronJob pods have no TTY, so this guard physically blocks the credential path in-cluster.
    if not sys.stdin.isatty():
        print(
            "bootstrap-garmin is interactive and must never run in-cluster; use `docker run -it` on the workstation",
            file=sys.stderr,
        )
        return 2

    seed_path = os.getenv("GARMIN_SEED_PATH", _GARMIN_SEED_DEFAULT)
    email = input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password: ")

    if not email:
        print("Garmin bootstrap failed: email is required.", file=sys.stderr)
        return 1
    if not password:
        print("Garmin bootstrap failed: password is required.", file=sys.stderr)
        return 1

    try:
        from garminconnect import Garmin

        client = Garmin(email=email, password=password, return_on_mfa=True)
        login_result = client.login()
        if not isinstance(login_result, (tuple, list)) or len(login_result) != 2:
            print("Garmin bootstrap failed: unexpected login response.", file=sys.stderr)
            return 1

        mfa_status, client_state = login_result
        if mfa_status == "needs_mfa":
            code = input("MFA code: ").strip()
            if not code:
                print("Garmin bootstrap failed: MFA code is required.", file=sys.stderr)
                return 1
            client.resume_login(client_state, code)
        elif mfa_status is not None:
            print("Garmin bootstrap failed: unexpected login response.", file=sys.stderr)
            return 1

        seed_dir = os.path.dirname(seed_path) or "."
        os.makedirs(seed_dir, exist_ok=True)

        low_level_client = getattr(client, "client", None)
        dump = getattr(low_level_client, "dump", None)
        if not callable(dump):
            print("Garmin bootstrap failed: token writer is unavailable.", file=sys.stderr)
            return 1

        dump(seed_dir)

        written_path = os.path.join(seed_dir, "garmin_tokens.json")
        if not os.path.isfile(written_path):
            print("Garmin bootstrap failed: tokenstore was not written.", file=sys.stderr)
            return 1
    except Exception:
        # The workstation bootstrap must never risk printing secrets back to the terminal.
        print("Garmin bootstrap failed during interactive login.", file=sys.stderr)
        return 1

    print(
        f"{written_path}: tokenstore written; tell your assistant it is ready to be sealed into the cluster secret"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync quantified-self data into PostgreSQL.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("garmin", help="Run the Garmin cron sync.")
    subparsers.add_parser("withings", help="Run the Withings cron sync.")

    backfill_parser = subparsers.add_parser("backfill", help="Run bounded historical backfill.")
    backfill_parser.add_argument(
        "--source",
        required=True,
        choices=("garmin", "withings"),
        help="Data source to backfill.",
    )
    backfill_parser.add_argument(
        "--chunk-days",
        type=_positive_int_arg,
        default=30,
        help="Days per backfill chunk.",
    )
    backfill_parser.add_argument(
        "--budget",
        type=_positive_int_arg,
        default=500,
        help="Maximum chunk budget for this invocation.",
    )

    subparsers.add_parser(
        "bootstrap-garmin",
        help="Interactive workstation-only Garmin token bootstrap.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        if args.command == "garmin":
            return _run_garmin()
        if args.command == "withings":
            return _run_withings()
        if args.command == "backfill":
            return _run_backfill(args)
        if args.command == "bootstrap-garmin":
            return _run_bootstrap_garmin()
        raise RuntimeError(f"unknown command: {args.command}")
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (garmin_auth.GarminAuthError, WithingsAuthError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except state.AccountBlockedError:
        return 1
    except psycopg.OperationalError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
