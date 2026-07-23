"""Pure-logic tests for collector.logic.

These tests intentionally exercise only stdlib-only decision helpers so they can run on
machines without database drivers, HTTP clients, or vendor SDKs installed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import random
import unittest

from collector import logic


class TestWatermarkOverlapMath(unittest.TestCase):
    """Watermark overlap and sync-date window calculations."""

    def test_overlap_start_subtracts_exactly_72_hours_and_passes_through_none(self) -> None:
        watermark = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
        expected = datetime(2026, 1, 12, 14, 30, tzinfo=timezone.utc)

        self.assertEqual(expected, logic.overlap_start(watermark))
        self.assertIsNone(logic.overlap_start(None))

    def test_overlap_start_honors_custom_overlap_hours(self) -> None:
        watermark = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
        expected = datetime(2026, 1, 14, 14, 30, tzinfo=timezone.utc)

        self.assertEqual(expected, logic.overlap_start(watermark, overlap_hours=24))

    def test_sync_dates_with_watermark_starts_from_local_date_of_overlap_and_ends_today(self) -> None:
        watermark = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
        today_local = date(2026, 1, 16)

        result = logic.sync_dates(
            watermark=watermark,
            today_local=today_local,
            tz_name="Australia/Sydney",
        )

        self.assertEqual(
            [
                date(2026, 1, 13),
                date(2026, 1, 14),
                date(2026, 1, 15),
                date(2026, 1, 16),
            ],
            result,
        )

    def test_sync_dates_without_watermark_returns_default_lookback_ending_today(self) -> None:
        today_local = date(2026, 1, 16)

        result = logic.sync_dates(
            watermark=None,
            today_local=today_local,
            tz_name="Australia/Sydney",
        )

        self.assertEqual(logic.DEFAULT_LOOKBACK_DAYS, len(result))
        self.assertEqual(date(2026, 1, 10), result[0])
        self.assertEqual(today_local, result[-1])
        self.assertEqual(
            [
                date(2026, 1, 10),
                date(2026, 1, 11),
                date(2026, 1, 12),
                date(2026, 1, 13),
                date(2026, 1, 14),
                date(2026, 1, 15),
                date(2026, 1, 16),
            ],
            result,
        )

    def test_sync_dates_rejects_lookback_days_less_than_one(self) -> None:
        with self.assertRaises(ValueError):
            logic.sync_dates(
                watermark=None,
                today_local=date(2026, 1, 16),
                tz_name="Australia/Sydney",
                lookback_days=0,
            )


class TestGarmin429SentinelLogic(unittest.TestCase):
    """Account-level 429 sentinel decisions."""

    def test_is_blocked_is_false_for_none_now_equal_and_past(self) -> None:
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        self.assertFalse(logic.is_blocked(None, now))
        self.assertFalse(logic.is_blocked(now, now))
        self.assertFalse(logic.is_blocked(now - timedelta(seconds=1), now))

    def test_is_blocked_is_true_when_blocked_until_is_in_the_future(self) -> None:
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        self.assertTrue(logic.is_blocked(now + timedelta(seconds=1), now))

    def test_compute_blocked_until_honors_int_and_numeric_string_retry_after(self) -> None:
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            now + timedelta(seconds=120),
            logic.compute_blocked_until(now, 120),
        )
        self.assertEqual(
            now + timedelta(seconds=45),
            logic.compute_blocked_until(now, "45"),
        )

    def test_compute_blocked_until_falls_back_to_default_for_invalid_negative_or_zero_values(self) -> None:
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        expected = now + timedelta(seconds=logic.DEFAULT_BLOCK_SECONDS)

        fallback_inputs: list[object] = [None, "garbage", -1, 0, "-10", "0", "  "]
        for retry_after in fallback_inputs:
            with self.subTest(retry_after=retry_after):
                self.assertEqual(expected, logic.compute_blocked_until(now, retry_after))  # type: ignore[arg-type]

    def test_compute_blocked_until_honors_custom_default_seconds(self) -> None:
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            now + timedelta(seconds=5),
            logic.compute_blocked_until(now, None, default_seconds=5),
        )


class TestWithingsNormalizationLogic(unittest.TestCase):
    """Withings measure normalization and metric naming."""

    def test_normalize_measure_handles_negative_positive_and_zero_exponents_with_exact_scale(self) -> None:
        negative = logic.normalize_measure(72500, -3)
        positive = logic.normalize_measure(5, 1)
        zero = logic.normalize_measure(42, 0)

        self.assertEqual(Decimal("72.500"), negative)
        self.assertEqual("72.500", str(negative))

        self.assertEqual(Decimal("50.000"), positive)
        self.assertEqual("50.000", str(positive))

        self.assertEqual(Decimal("42.000"), zero)
        self.assertEqual("42.000", str(zero))

    def test_metric_for_type_maps_all_known_types_and_passthroughs_unknown(self) -> None:
        expected = {
            1: "weight",
            5: "fat_free_mass",
            6: "fat_ratio",
            8: "fat_mass",
            76: "muscle_mass",
            77: "hydration",
            88: "bone_mass",
        }

        for measure_type, metric in expected.items():
            with self.subTest(measure_type=measure_type):
                self.assertEqual(metric, logic.metric_for_type(measure_type))

        self.assertEqual("type_999", logic.metric_for_type(999))


class TestLocalDateConversion(unittest.TestCase):
    """UTC-to-local calendar conversion, including Sydney DST behavior."""

    def test_to_local_date_rejects_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            logic.to_local_date(datetime(2026, 1, 15, 14, 30), "Australia/Sydney")

    def test_to_local_date_converts_aedt_timestamp_past_midnight(self) -> None:
        ts = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)

        self.assertEqual(date(2026, 1, 16), logic.to_local_date(ts, "Australia/Sydney"))

    def test_to_local_date_keeps_aedt_timestamp_on_same_local_day(self) -> None:
        ts = datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc)

        self.assertEqual(date(2026, 1, 15), logic.to_local_date(ts, "Australia/Sydney"))

    def test_to_local_date_converts_aest_timestamp_past_midnight(self) -> None:
        ts = datetime(2026, 6, 15, 14, 30, tzinfo=timezone.utc)

        self.assertEqual(date(2026, 6, 16), logic.to_local_date(ts, "Australia/Sydney"))

    def test_to_local_date_keeps_same_utc_hour_on_same_day_in_winter_after_dst_ends(self) -> None:
        ts = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)

        self.assertEqual(date(2026, 6, 15), logic.to_local_date(ts, "Australia/Sydney"))


class TestBackfillRangeLogic(unittest.TestCase):
    """Date spans and chunking for backfills."""

    def _assert_gapless_exact_cover(
        self,
        start: date,
        end: date,
        chunks: list[tuple[date, date]],
    ) -> None:
        self.assertEqual(start, chunks[0][0])
        self.assertEqual(end, chunks[-1][1])

        flattened: list[date] = []
        for index, (chunk_start, chunk_end) in enumerate(chunks):
            self.assertLessEqual(chunk_start, chunk_end)
            flattened.extend(logic.date_range(chunk_start, chunk_end))

            if index > 0:
                prev_end = chunks[index - 1][1]
                self.assertEqual(prev_end + timedelta(days=1), chunk_start)

        self.assertEqual(logic.date_range(start, end), flattened)

    def test_date_range_is_inclusive(self) -> None:
        self.assertEqual(
            [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)],
            logic.date_range(date(2026, 1, 1), date(2026, 1, 3)),
        )

    def test_date_range_singleton_and_inverted_range(self) -> None:
        same_day = date(2026, 1, 1)

        self.assertEqual([same_day], logic.date_range(same_day, same_day))
        self.assertEqual([], logic.date_range(date(2026, 1, 3), date(2026, 1, 1)))

    def test_chunk_ranges_single_day_range_returns_one_singleton_chunk(self) -> None:
        same_day = date(2026, 1, 1)

        self.assertEqual([(same_day, same_day)], logic.chunk_ranges(same_day, same_day, 3))

    def test_chunk_ranges_chunk_days_larger_than_span_returns_one_chunk(self) -> None:
        start = date(2026, 1, 1)
        end = date(2026, 1, 3)

        chunks = logic.chunk_ranges(start, end, 10)

        self.assertEqual([(start, end)], chunks)
        self._assert_gapless_exact_cover(start, end, chunks)

    def test_chunk_ranges_exact_multiple_case_covers_range_exactly(self) -> None:
        start = date(2026, 1, 1)
        end = date(2026, 1, 6)

        chunks = logic.chunk_ranges(start, end, 3)

        self.assertEqual(
            [
                (date(2026, 1, 1), date(2026, 1, 3)),
                (date(2026, 1, 4), date(2026, 1, 6)),
            ],
            chunks,
        )
        self._assert_gapless_exact_cover(start, end, chunks)

    def test_chunk_ranges_remainder_case_covers_range_exactly(self) -> None:
        start = date(2026, 1, 1)
        end = date(2026, 1, 7)

        chunks = logic.chunk_ranges(start, end, 3)

        self.assertEqual(
            [
                (date(2026, 1, 1), date(2026, 1, 3)),
                (date(2026, 1, 4), date(2026, 1, 6)),
                (date(2026, 1, 7), date(2026, 1, 7)),
            ],
            chunks,
        )
        self._assert_gapless_exact_cover(start, end, chunks)

    def test_chunk_ranges_rejects_chunk_days_less_than_one(self) -> None:
        with self.assertRaises(ValueError):
            logic.chunk_ranges(date(2026, 1, 1), date(2026, 1, 2), 0)

    def test_chunk_ranges_returns_empty_for_inverted_range(self) -> None:
        self.assertEqual(
            [],
            logic.chunk_ranges(date(2026, 1, 3), date(2026, 1, 1), 3),
        )


class TestJitterAndEpochParsing(unittest.TestCase):
    """Random pacing jitter and epoch-millisecond parsing."""

    def test_jitter_seconds_stays_within_expected_bounds(self) -> None:
        pacing_min = 0.5
        random.seed(0)

        for _ in range(500):
            value = logic.jitter_seconds(pacing_min)
            self.assertGreaterEqual(value, pacing_min)
            self.assertLessEqual(value, pacing_min + 0.5)

    def test_parse_epoch_ms_parses_epoch_zero_as_aware_utc(self) -> None:
        parsed = logic.parse_epoch_ms(0)

        self.assertEqual(datetime(1970, 1, 1, tzinfo=timezone.utc), parsed)
        self.assertIs(parsed.tzinfo, timezone.utc)

    def test_parse_epoch_ms_round_trips_a_known_millisecond_value(self) -> None:
        expected = datetime(2024, 2, 29, 12, 34, 56, 789000, tzinfo=timezone.utc)
        ms = 1709210096789

        parsed = logic.parse_epoch_ms(ms)

        self.assertEqual(expected, parsed)
        self.assertIs(parsed.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
