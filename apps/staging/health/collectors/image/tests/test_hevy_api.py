"""hevy_api.py and hevy_sync.parse_workout: no database needed. Run inside the image
(requests and psycopg are imported). Each test pins a rule whose failure would either write to
the user's Hevy account, leak the key, lose a run to one transient error, or misdate a workout.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import re
import unittest

import requests

from collector import hevy_api
from collector.hevy_api import HevyAPIError, HevyAuthError, HevyClient, HevyNotFound, Response
from collector.hevy_sync import HevyDataError, deletion_allowed, parse_workout

KEY = "11111111-2222-3333-4444-555555555555"


class Stub:
    """Transport returning queued responses (or raising queued exceptions); records calls."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, params, headers, timeout):
        self.calls.append((url, dict(params), dict(headers)))
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(stub, sleeps=None):
    return HevyClient(KEY, transport=stub, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def workout(wid="w1", start="2026-11-20T09:00:00+00:00", exercises=None):
    return {
        "id": wid, "title": "Day A", "routine_id": None, "description": None,
        "start_time": start, "end_time": None, "updated_at": "2026-11-20T10:00:00+00:00",
        "created_at": None, "media": [],
        "exercises": exercises if exercises is not None else [
            {"index": 0, "title": "Squat", "notes": None, "exercise_template_id": "T1", "superset_id": None,
             "sets": [{"index": 0, "type": "normal", "weight_kg": 42.5, "reps": 10, "distance_meters": None,
                       "duration_seconds": None, "rpe": None, "custom_metric": None}]},
        ],
    }


class GetOnly(unittest.TestCase):
    def test_module_issues_no_other_http_verb(self):
        src = Path(hevy_api.__file__).read_text()
        code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
        code = re.sub(r'"""[\s\S]*?"""', "", code)  # docstrings may name verbs in prose
        bad = re.findall(r"\.(post|put|patch|delete|request|head|options)\s*\(", code, re.I)
        self.assertEqual(bad, [], f"non-GET call sites: {bad}")
        self.assertEqual(len(re.findall(r"\.get\(\s*url", code)), 1, "exactly one HTTP call site")

    def test_key_is_sent_only_as_the_api_key_header(self):
        stub = Stub(Response(200, {"workout_count": 3}))
        client(stub).workout_count()
        url, params, headers = stub.calls[0]
        self.assertEqual(headers["api-key"], KEY)
        self.assertNotIn(KEY, url)
        self.assertNotIn(KEY, repr(params))


class Errors(unittest.TestCase):
    def test_401_is_auth_error_without_the_key_and_without_retry(self):
        stub = Stub(Response(401, None))
        with self.assertRaises(HevyAuthError) as cm:
            client(stub).workout_count()
        self.assertNotIn(KEY, str(cm.exception))
        self.assertEqual(len(stub.calls), 1)

    def test_404_is_not_found_without_retry(self):
        stub = Stub(Response(404, None))
        with self.assertRaises(HevyNotFound):
            client(stub).exercise_template("T9")
        self.assertEqual(len(stub.calls), 1)

    def test_400_fails_at_once(self):
        stub = Stub(Response(400, None))
        with self.assertRaises(HevyAPIError) as cm:
            client(stub).workout_count()
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(len(stub.calls), 1)

    def test_scrub_removes_the_key(self):
        self.assertNotIn(KEY, client(Stub()).scrub(f"boom {KEY} boom"))


class Retries(unittest.TestCase):
    def test_429_then_200_succeeds_after_one_backoff(self):
        sleeps: list[float] = []
        stub = Stub(Response(429, None), Response(200, {"workout_count": 5}))
        self.assertEqual(client(stub, sleeps).workout_count(), 5)
        self.assertEqual(sleeps, [2])

    def test_timeout_is_retried(self):
        stub = Stub(requests.Timeout("slow"), Response(200, {"workout_count": 1}))
        self.assertEqual(client(stub).workout_count(), 1)

    def test_persistent_502_gives_up_after_backoff_2_4_8(self):
        sleeps: list[float] = []
        stub = Stub(*[Response(502, None)] * 4)
        with self.assertRaises(HevyAPIError) as cm:
            client(stub, sleeps).workout_count()
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(sleeps, [2, 4, 8])
        self.assertEqual(len(stub.calls), 4)


class Paging(unittest.TestCase):
    def test_walks_every_page_named_by_page_1(self):
        stub = Stub(
            Response(200, {"page": 1, "page_count": 2, "workouts": [workout("a")]}),
            Response(200, {"page": 2, "page_count": 2, "workouts": [workout("b")]}),
        )
        snap = client(stub).all_workouts()
        self.assertTrue(snap.complete)
        self.assertEqual([w["id"] for w in snap.workouts], ["a", "b"])
        self.assertEqual([c[1]["page"] for c in stub.calls], [1, 2])
        self.assertTrue(all(c[1]["pageSize"] == 10 for c in stub.calls))

    def test_cap_stops_a_runaway_page_count_and_marks_incomplete(self):
        stub = Stub(*[Response(200, {"page_count": 10**6, "workouts": []})] * 5)
        snap = client(stub).all_workouts(page_cap=3)
        self.assertFalse(snap.complete)
        self.assertEqual(len(stub.calls), 3)

    def test_empty_account_is_complete(self):
        snap = client(Stub(Response(200, {"page": 1, "page_count": 0, "workouts": []}))).all_workouts()
        self.assertTrue(snap.complete)
        self.assertEqual(snap.workouts, [])


class Parsing(unittest.TestCase):
    def test_utc_start_after_13_00_is_the_next_sydney_day(self):
        w, _, _ = parse_workout(workout(start="2026-11-20T14:00:00+00:00"), "Australia/Sydney")
        self.assertEqual(w[6], date(2026, 11, 21))   # AEDT is UTC+11
        w, _, _ = parse_workout(workout(start="2026-11-20T12:59:00+00:00"), "Australia/Sydney")
        self.assertEqual(w[6], date(2026, 11, 20))

    def test_weights_are_exact_and_zero_stays_zero(self):
        ex = workout()["exercises"]
        ex[0]["sets"].append({"index": 1, "type": "normal", "weight_kg": 0, "reps": 12, "distance_meters": None,
                              "duration_seconds": None, "rpe": None, "custom_metric": None})
        _, _, sets = parse_workout(workout(exercises=ex), "Australia/Sydney")
        self.assertEqual(sets[0][4], Decimal("42.5"))
        self.assertEqual(sets[1][4], Decimal("0"))      # bodyweight, not NULL
        self.assertIsNone(sets[0][7])

    def test_duplicate_set_index_is_refused(self):
        ex = workout()["exercises"]
        ex[0]["sets"].append(dict(ex[0]["sets"][0]))
        with self.assertRaises(HevyDataError):
            parse_workout(workout(exercises=ex), "Australia/Sydney")

    def test_naive_timestamp_is_refused(self):
        with self.assertRaises(HevyDataError):
            parse_workout(workout(start="2026-11-20T09:00:00"), "Australia/Sydney")


class DeletionGuards(unittest.TestCase):
    def test_guards(self):
        self.assertIsNone(deletion_allowed(14, 1, True, 13, 13))
        self.assertIsNone(deletion_allowed(14, 0, False, 0, 99))                 # nothing to delete
        self.assertIn("incomplete", deletion_allowed(14, 1, False, 13, 13))
        self.assertIn("Hevy counts", deletion_allowed(14, 1, True, 13, 14))
        self.assertIsNone(deletion_allowed(14, 3, True, 11, 11))                 # floor of 3
        self.assertIn("limit 3", deletion_allowed(14, 4, True, 10, 10))
        self.assertIsNone(deletion_allowed(100, 10, True, 90, 90))               # 10%
        self.assertIn("limit 10", deletion_allowed(100, 11, True, 89, 89))
        self.assertIn("limit 3", deletion_allowed(14, 14, True, 0, 0))           # an empty reply never wipes


if __name__ == "__main__":
    unittest.main()
