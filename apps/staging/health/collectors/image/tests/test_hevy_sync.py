"""hevy_sync.run_cron against a real PostgreSQL with migration 0009 applied, connected AS
qs_writer (so the grants are exercised too). Set HEVY_TEST_PGDSN to run; without it every test
is skipped, and the skip is loud. NEVER point it at the live cluster: each test deletes all
hevy.* rows and the hevy run_log rows.

Pins: a full snapshot rewrites edits and removes stale workouts; each deletion guard blocks the
delete and records `partial`; exhausted retries record `failed` and delete nothing; a held
advisory lock makes a run exit without work; the template TTL; the key never reaches run_log.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import os
import unittest

import psycopg

from collector import hevy_sync
from collector.config import Config
from collector.hevy_api import HevyAPIError, HevyClient, HevyNotFound, Response, WorkoutSnapshot

DSN = os.environ.get("HEVY_TEST_PGDSN")
KEY = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc)
CFG = Config("h", 5432, "health", "qs_writer", "x", None, None, "/x", "Australia/Sydney", 0.5, None)


def wk(wid, n_sets=2, updated="2026-11-20T10:00:00+00:00", template="T1"):
    return {
        "id": wid, "title": f"W {wid}", "routine_id": None, "description": None,
        "start_time": "2026-11-20T09:00:00+00:00", "end_time": "2026-11-20T10:00:00+00:00",
        "updated_at": updated, "created_at": None, "media": [],
        "exercises": [{"index": 0, "title": f"Ex {template}", "notes": None, "exercise_template_id": template,
                       "superset_id": None,
                       "sets": [{"index": i, "type": "normal", "weight_kg": 40 + i, "reps": 10, "distance_meters": None,
                                 "duration_seconds": None, "rpe": None, "custom_metric": None} for i in range(n_sets)]}],
    }


class Fake:
    """Stands in for HevyClient: a fixed account, with knobs for each failure mode."""

    def __init__(self, workouts, count=None, complete=True, fail=None, templates=None):
        self.workouts = workouts
        self.count = len(workouts) if count is None else count
        self.complete = complete
        self.fail = fail
        self.templates = templates if templates is not None else {}
        self.template_calls: list[str] = []

    def scrub(self, text):
        return text.replace(KEY, "<hevy-key>")

    def workout_count(self):
        return self.count

    def all_workouts(self):
        if self.fail:
            raise self.fail
        return WorkoutSnapshot(copy.deepcopy(self.workouts), 1, 1, self.complete)

    def exercise_template(self, tid):
        self.template_calls.append(tid)
        t = self.templates.get(tid, {"id": tid, "title": f"Template {tid}", "type": "weight_reps",
                                     "primary_muscle_group": "quadriceps", "secondary_muscle_groups": [],
                                     "equipment": "barbell", "is_custom": False})
        if t is None:
            raise HevyNotFound("404")
        return t


@unittest.skipUnless(DSN, "HEVY_TEST_PGDSN not set: PostgreSQL sync tests SKIPPED")
class Sync(unittest.TestCase):
    def setUp(self):
        self.conn = psycopg.connect(DSN, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM hevy.workout")
            cur.execute("DELETE FROM hevy.exercise_template")
            cur.execute("DELETE FROM ops.run_log WHERE source = 'hevy'")
            cur.execute("DELETE FROM ops.sync_state WHERE source = 'hevy'")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def q(self, sql, *args):
        with self.conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def run_(self, fake):
        return hevy_sync.run_cron(self.conn, CFG, fake, now=NOW)

    def last_run(self):
        return self.q("SELECT status, rows_upserted, first_error, details FROM ops.run_log "
                      "WHERE source='hevy' ORDER BY run_id DESC LIMIT 1")[0]

    def ids(self):
        return [r[0] for r in self.q("SELECT workout_id FROM hevy.workout ORDER BY 1")]

    def test_first_run_loads_everything_and_a_second_changes_nothing(self):
        fake = Fake([wk("a"), wk("b", 3)])
        self.assertTrue(self.run_(fake))
        self.assertEqual(self.ids(), ["a", "b"])
        self.assertEqual(self.q("SELECT count(*) FROM hevy.set")[0][0], 5)
        self.assertEqual(self.q("SELECT count(*) FROM hevy.set_v WHERE template_type = 'weight_reps'")[0][0], 5)
        self.assertEqual(self.last_run()[:2], ("ok", 2))
        self.assertTrue(self.run_(fake))
        self.assertEqual(self.last_run()[:2], ("ok", 0))
        self.assertEqual(self.q("SELECT count(*) FROM hevy.set")[0][0], 5)

    def test_an_edit_that_removes_a_set_removes_it_here(self):
        self.run_(Fake([wk("a", 3)]))
        self.assertTrue(self.run_(Fake([wk("a", 1, updated="2026-11-21T10:00:00+00:00")])))
        self.assertEqual(self.q("SELECT set_idx FROM hevy.set ORDER BY 1"), [(0,)])
        self.assertEqual(self.last_run()[1], 1)

    def test_a_workout_deleted_in_hevy_is_deleted_here(self):
        self.run_(Fake([wk(x) for x in "abcd"]))
        self.assertTrue(self.run_(Fake([wk(x) for x in "abc"])))
        self.assertEqual(self.ids(), ["a", "b", "c"])
        self.assertEqual(self.q("SELECT count(*) FROM hevy.set WHERE workout_id = 'd'")[0][0], 0)
        self.assertEqual(self.last_run()[3]["deleted"], 1)

    def test_guard_incomplete_snapshot_deletes_nothing(self):
        self.run_(Fake([wk(x) for x in "abcd"]))
        self.assertFalse(self.run_(Fake([wk(x) for x in "abc"], complete=False)))
        self.assertEqual(self.ids(), list("abcd"))
        status, _, err, details = self.last_run()
        self.assertEqual(status, "partial")
        self.assertIn("incomplete", details["deletion_blocked"])

    def test_guard_count_mismatch_deletes_nothing(self):
        self.run_(Fake([wk(x) for x in "abcd"]))
        self.assertFalse(self.run_(Fake([wk(x) for x in "abc"], count=4)))
        self.assertEqual(self.ids(), list("abcd"))
        self.assertEqual(self.last_run()[0], "partial")

    def test_guard_too_many_deletions_deletes_nothing(self):
        self.run_(Fake([wk(x) for x in "abcdefghij"]))
        self.assertFalse(self.run_(Fake([wk(x) for x in "abcdef"])))      # 4 of 10 > max(3, 1)
        self.assertEqual(len(self.ids()), 10)
        self.assertIn("limit 3", self.last_run()[3]["deletion_blocked"])

    def test_an_empty_reply_never_wipes_the_table(self):
        self.run_(Fake([wk(x) for x in "abcdefghij"]))
        self.assertFalse(self.run_(Fake([], count=0)))
        self.assertEqual(len(self.ids()), 10)

    def test_exhausted_retries_record_failed_and_change_nothing(self):
        self.run_(Fake([wk(x) for x in "abcd"]))
        self.assertFalse(self.run_(Fake([], fail=HevyAPIError(502, "/v1/workouts"))))
        self.assertEqual(self.ids(), list("abcd"))
        status, _, err, _ = self.last_run()
        self.assertEqual(status, "failed")
        self.assertIn("502", err)

    def test_a_held_lock_means_no_run_and_no_writes(self):
        other = psycopg.connect(DSN, autocommit=True)
        try:
            other.execute("SELECT pg_advisory_lock(hashtext('hevy.workouts'))")
            self.assertTrue(self.run_(Fake([wk("a")])))
            self.assertEqual(self.ids(), [])
            self.assertEqual(self.q("SELECT count(*) FROM ops.run_log WHERE source='hevy'")[0][0], 0)
        finally:
            other.close()
        self.assertTrue(self.run_(Fake([wk("a")])))   # the lock was ours to take once released
        self.assertEqual(self.ids(), ["a"])

    def test_the_lock_is_released_after_a_run(self):
        self.run_(Fake([wk("a")]))
        other = psycopg.connect(DSN, autocommit=True)
        try:
            self.assertTrue(other.execute("SELECT pg_try_advisory_lock(hashtext('hevy.workouts'))").fetchone()[0])
        finally:
            other.close()

    def test_template_ttl_and_404(self):
        fake = Fake([wk("a", template="T1"), wk("b", template="CUSTOM")], templates={"CUSTOM": None})
        self.assertTrue(self.run_(fake))
        self.assertEqual(sorted(fake.template_calls), ["CUSTOM", "T1"])
        self.assertEqual(self.q("SELECT title, fetch_ok FROM hevy.exercise_template WHERE template_id='CUSTOM'"),
                         [("Ex CUSTOM", False)])
        fake.template_calls.clear()
        self.run_(fake)
        self.assertEqual(fake.template_calls, [])                         # both fresh: no calls
        self.q("UPDATE hevy.exercise_template SET fetched_at = %s RETURNING 1", NOW - timedelta(days=8))
        self.run_(fake)
        self.assertEqual(sorted(fake.template_calls), ["CUSTOM", "T1"])   # stale and 404 both retried

    def test_the_key_never_reaches_run_log(self):
        self.assertFalse(self.run_(Fake([], fail=RuntimeError(f"exploded near {KEY}"))))
        err = self.last_run()[2]
        self.assertNotIn(KEY, err)
        self.assertIn("<hevy-key>", err)

    def test_real_client_through_a_stub_transport(self):
        responses = [Response(200, {"workout_count": 1}),
                     Response(200, {"page": 1, "page_count": 1, "workouts": [wk("a")]}),
                     Response(200, {"id": "T1", "title": "Squat", "type": "weight_reps",
                                    "primary_muscle_group": "quadriceps", "secondary_muscle_groups": ["glutes"],
                                    "equipment": "barbell", "is_custom": False})]

        def transport(url, params, headers, timeout):
            return responses.pop(0)

        self.assertTrue(hevy_sync.run_cron(self.conn, CFG, HevyClient(KEY, transport=transport), now=NOW))
        self.assertEqual(self.q("SELECT secondary_muscle_groups FROM hevy.exercise_template"), [(["glutes"],)])


if __name__ == "__main__":
    unittest.main()
