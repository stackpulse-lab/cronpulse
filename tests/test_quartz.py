"""Quartz-only syntax must fail loudly, not silently mean something else."""

from __future__ import annotations

import unittest

from cronpulse import CronError, UnsupportedQuartzError, parse


class QuartzRejectionTests(unittest.TestCase):
    def assertQuartz(self, expression, needle):
        with self.assertRaises(UnsupportedQuartzError) as ctx:
            parse(expression)
        self.assertIsInstance(ctx.exception, CronError)
        self.assertIn(needle, str(ctx.exception))

    def test_last_day_of_month(self):
        self.assertQuartz("0 0 L * *", "`L` (last day of month)")
        self.assertQuartz("0 0 L * *", "Quartz extension")

    def test_last_weekday_of_month(self):
        self.assertQuartz("0 0 LW * *", "`LW` (last weekday of month)")

    def test_days_before_month_end(self):
        self.assertQuartz("0 0 L-3 * *", "`L-n`")

    def test_nearest_weekday(self):
        self.assertQuartz("0 0 15W * *", "`nW`")

    def test_last_named_weekday(self):
        self.assertQuartz("0 0 * * 6L", "`nL`")
        self.assertQuartz("0 0 * * FRIL", "`nL`")

    def test_nth_weekday(self):
        self.assertQuartz("0 0 * * MON#2", "`n#k`")

    def test_rejected_inside_list(self):
        self.assertQuartz("0 0 1,15W * *", "`nW`")

    def test_message_mentions_field(self):
        with self.assertRaises(UnsupportedQuartzError) as ctx:
            parse("0 0 * * MON#2")
        self.assertIn("day-of-week", str(ctx.exception))
        self.assertIn("Vixie", str(ctx.exception))


class DialectBoundaryTests(unittest.TestCase):
    def test_month_name_containing_l_still_parses(self):
        expr = parse("0 0 1 jul *")
        self.assertEqual(expr.month, frozenset({7}))

    def test_weekday_name_containing_l_still_parses(self):
        # No current weekday abbreviation ends in "l"; this guards the regex.
        expr = parse("0 0 * * sat")
        self.assertEqual(expr.day_of_week, frozenset({6}))

    def test_question_mark_still_wildcard(self):
        expr = parse("0 0 ? * 1")
        self.assertEqual(expr.day_of_month, frozenset(range(1, 32)))

    def test_plain_expressions_unaffected(self):
        self.assertEqual(parse("*/5 * * * *").minute, frozenset(range(0, 60, 5)))

    def test_quartz_rejection_is_a_cron_error(self):
        self.assertTrue(issubclass(UnsupportedQuartzError, CronError))


if __name__ == "__main__":
    unittest.main()
