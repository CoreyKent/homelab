"""Call-site tests: the sync modules really use the travel-aware date helpers.

test_logic.py proves the helpers; these prove the Withings call sites use them, so
reverting a call site to to_local_date() goes red. They import withings_sync, which
needs the image's runtime dependencies (requests, psycopg); run them inside the image.
Regression context (03/11/2026): activities and weigh-ins recorded abroad were dated in
Australia/Sydney.
"""

from __future__ import annotations

from datetime import date
import unittest
from unittest import mock

try:
    from collector import withings_sync
except ImportError as exc:  # pragma: no cover - only outside the image
    withings_sync = None
    _IMPORT_ERROR = exc

# 29/08/2025 15:04:45 UTC = 08:04 in Arizona (29/08), 01:04 in Sydney (30/08).
EPOCH_TUCSON = 1756479885
# East of Sydney: 03/11/2026 11:30 UTC = 00:30 on 04/11 in Auckland (NZDT), but still
# 22:30 on 03/11 in Sydney (AEDT). Here the local date is a day AHEAD of Sydney's.
EPOCH_AUCKLAND = 1793705400


def _group(grpid, epoch, tz=None):
    g = {"grpid": grpid, "date": epoch, "measures": [{"type": 1, "value": 71427, "unit": -3}]}
    if tz is not None:
        g["timezone"] = tz
    return g


@unittest.skipIf(withings_sync is None, "runtime deps not installed (run inside the image)")
class WithingsCallSiteDates(unittest.TestCase):
    def test_group_timezone_dates_the_weigh_in_where_it_happened(self):
        rows = withings_sync.rows_from_groups([_group(1, EPOCH_TUCSON, "America/Phoenix")], "Australia/Sydney")
        self.assertEqual(date(2025, 8, 29), rows[0][4])

    def test_group_without_timezone_uses_home(self):
        rows = withings_sync.rows_from_groups([_group(2, EPOCH_TUCSON)], "Australia/Sydney")
        self.assertEqual(date(2025, 8, 30), rows[0][4])

    def test_east_of_sydney_is_a_day_ahead(self):
        rows = withings_sync.rows_from_groups([_group(3, EPOCH_AUCKLAND, "Pacific/Auckland")], "Australia/Sydney")
        self.assertEqual(date(2026, 11, 4), rows[0][4])

    def test_body_timezone_is_carried_onto_groups_without_one(self):
        payload = {"status": 0, "body": {"timezone": "America/Phoenix", "more": 0,
                                         "measuregrps": [_group(4, EPOCH_TUCSON),
                                                         _group(5, EPOCH_TUCSON, "Australia/Sydney")]}}
        resp = mock.Mock(status_code=200, **{"json.return_value": payload})
        with mock.patch.object(withings_sync.requests, "post", return_value=resp):
            page = withings_sync._fetch_page("token", 0, 1, None)
        groups = page[0] if isinstance(page, tuple) else page
        rows = withings_sync.rows_from_groups(groups, "Australia/Sydney")
        self.assertEqual({4: date(2025, 8, 29), 5: date(2025, 8, 30)}, {r[0]: r[4] for r in rows})


if __name__ == "__main__":
    unittest.main()
