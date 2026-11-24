"""Tests for next-run calculation and never-fires detection."""

from __future__ import annotations

import unittest
from datetime import datetime

from cronpulse import parse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


class NextRunTests(unittest.TestCase):
    def test_every_fifteen_minutes(self):
        runs = parse("*/15 * * * *").next_runs(datetime(2026, 1, 1, 0, 7), count=3)
        self.assertEqual(
            runs,
            [
                datetime(2026, 1, 1, 0, 15),
                datetime(2026, 1, 1, 0, 30),
                datetime(2026, 1, 1, 0, 45),
            ],
        )

    def test_daily_rolls_to_next_day(self):
        runs = parse("0 9 * * *").next_runs(datetime(2026, 1, 1, 10, 0), count=1)
        self.assertEqual(runs, [datetime(2026, 1, 2, 9, 0)])

    def test_strictly_after(self):
        runs = parse("0 9 * * *").next_runs(datetime(2026, 1, 1, 9, 0), count=1)
        self.assertEqual(runs, [datetime(2026, 1, 2, 9, 0)])

    def test_weekdays_only(self):
        # 2026-01-02 is a Friday; next weekday run is Monday 2026-01-05.
        runs = parse("30 9 * * 1-5").next_runs(datetime(2026, 1, 2, 12, 0), count=2)
        self.assertEqual(
            runs, [datetime(2026, 1, 5, 9, 30), datetime(2026, 1, 6, 9, 30)]
        )

    def test_dom_and_dow_are_or_ed(self):
        # First of the month OR Monday.
        expr = parse("0 0 1 * 1")
        runs = expr.next_runs(datetime(2026, 1, 2, 0, 0), count=2)
        self.assertEqual(runs, [datetime(2026, 1, 5, 0, 0), datetime(2026, 1, 12, 0, 0)])

    def test_leap_day(self):
        runs = parse("0 0 29 2 *").next_runs(datetime(2026, 1, 1), count=1)
        self.assertEqual(runs, [datetime(2028, 2, 29, 0, 0)])

    def test_count_must_be_positive(self):
        with self.assertRaises(ValueError):
            parse("* * * * *").next_runs(datetime(2026, 1, 1), count=0)

    def test_timezone_is_preserved(self):
        if ZoneInfo is None:
            self.skipTest("zoneinfo unavailable")
        tz = ZoneInfo("Asia/Shanghai")
        after = datetime(2026, 11, 24, 17, 49, tzinfo=tz)
        runs = parse("0 * * * *").next_runs(after, count=1)
        self.assertEqual(runs[0].tzinfo, tz)
        self.assertEqual(runs[0], datetime(2026, 11, 24, 18, 0, tzinfo=tz))

    def test_midnight_boundary(self):
        runs = parse("0 0 * * *").next_runs(datetime(2026, 12, 31, 23, 59), count=1)
        self.assertEqual(runs, [datetime(2027, 1, 1, 0, 0)])


class NeverFiresTests(unittest.TestCase):
    def test_february_30_never_fires(self):
        self.assertTrue(parse("0 0 30 2 *").never_fires(datetime(2026, 1, 1)))

    def test_april_31_never_fires(self):
        self.assertTrue(parse("0 0 31 4 *").never_fires(datetime(2026, 1, 1)))

    def test_leap_day_does_fire(self):
        self.assertFalse(parse("0 0 29 2 *").never_fires(datetime(2026, 1, 1)))

    def test_normal_schedule_fires(self):
        self.assertFalse(parse("*/5 * * * *").never_fires(datetime(2026, 1, 1)))

    def test_empty_result_for_impossible(self):
        self.assertEqual(parse("0 0 30 2 *").next_runs(datetime(2026, 1, 1), count=5), [])

    def test_combined_impossible_fields(self):
        self.assertTrue(parse("0 0 31 2,4,6,9,11 *").never_fires(datetime(2026, 1, 1)))


if __name__ == "__main__":
    unittest.main()
