"""Tests for cron expression parsing."""

from __future__ import annotations

import unittest

from cronpulse import CronError, parse


class ParseFieldTests(unittest.TestCase):
    def test_star(self):
        expr = parse("* * * * *")
        self.assertEqual(expr.minute, frozenset(range(60)))
        self.assertEqual(expr.hour, frozenset(range(24)))
        self.assertEqual(expr.day_of_month, frozenset(range(1, 32)))
        self.assertEqual(expr.month, frozenset(range(1, 13)))
        self.assertEqual(expr.day_of_week, frozenset(range(7)))

    def test_step(self):
        expr = parse("*/15 * * * *")
        self.assertEqual(expr.minute, frozenset({0, 15, 30, 45}))

    def test_step_from_value(self):
        expr = parse("5/20 * * * *")
        self.assertEqual(expr.minute, frozenset({5, 25, 45}))

    def test_range_and_step(self):
        expr = parse("0 9-17/4 * * *")
        self.assertEqual(expr.hour, frozenset({9, 13, 17}))

    def test_list(self):
        expr = parse("0,30 8,20 * * *")
        self.assertEqual(expr.minute, frozenset({0, 30}))
        self.assertEqual(expr.hour, frozenset({8, 20}))

    def test_month_and_weekday_names(self):
        expr = parse("0 0 1 jan MON")
        self.assertEqual(expr.month, frozenset({1}))
        self.assertEqual(expr.day_of_week, frozenset({1}))

    def test_names_case_insensitive(self):
        self.assertEqual(parse("0 0 * Mar *").month, frozenset({3}))
        self.assertEqual(parse("0 0 * * sUn").day_of_week, frozenset({0}))

    def test_sunday_as_seven(self):
        self.assertEqual(parse("0 0 * * 7").day_of_week, frozenset({0}))
        self.assertEqual(parse("0 0 * * 5-7").day_of_week, frozenset({0, 5, 6}))

    def test_question_mark_is_star(self):
        expr = parse("0 0 ? * ?")
        self.assertFalse(expr.dom_restricted)
        self.assertFalse(expr.dow_restricted)

    def test_restricted_flags(self):
        self.assertFalse(parse("* * * * *").dom_restricted)
        self.assertFalse(parse("* * * * *").dow_restricted)
        expr = parse("0 0 * * 1")
        self.assertFalse(expr.dom_restricted)
        self.assertTrue(expr.dow_restricted)
        expr = parse("0 0 15 * *")
        self.assertTrue(expr.dom_restricted)
        self.assertFalse(expr.dow_restricted)

    def test_macros(self):
        self.assertEqual(parse("@hourly").expression, "0 * * * *")
        self.assertEqual(parse("@daily").expression, "0 0 * * *")
        self.assertEqual(parse("@WEEKLY").expression, "0 0 * * 0")
        self.assertEqual(parse("@monthly").expression, "0 0 1 * *")
        self.assertEqual(parse("@yearly").expression, "0 0 1 1 *")
        self.assertEqual(parse("@annually").expression, "0 0 1 1 *")
        self.assertEqual(parse("@midnight").expression, "0 0 * * *")

    def test_whitespace_normalised(self):
        self.assertEqual(parse("  0   9  *  *  1-5 ").expression, "0 9 * * 1-5")


class ParseErrorTests(unittest.TestCase):
    def assert_invalid(self, expression: str):
        with self.assertRaises(CronError):
            parse(expression)

    def test_wrong_field_count(self):
        self.assert_invalid("* * * *")
        self.assert_invalid("* * * * * *")

    def test_empty(self):
        self.assert_invalid("")
        self.assert_invalid("   ")

    def test_out_of_range(self):
        self.assert_invalid("60 * * * *")
        self.assert_invalid("* 24 * * *")
        self.assert_invalid("* * 0 * *")
        self.assert_invalid("* * 32 * *")
        self.assert_invalid("* * * 13 *")
        self.assert_invalid("* * * * 8")

    def test_bad_numbers(self):
        self.assert_invalid("a * * * *")
        self.assert_invalid("*/x * * * *")
        self.assert_invalid("*/0 * * * *")
        self.assert_invalid("*-5 * * * *")

    def test_descending_range(self):
        self.assert_invalid("0 17-9 * * *")

    def test_unknown_macro(self):
        self.assert_invalid("@every5m")
        self.assert_invalid("@reboot")

    def test_invalid_characters(self):
        self.assert_invalid("0 0 * * MON#2")


class DescribeTests(unittest.TestCase):
    def test_describe_star(self):
        self.assertEqual(
            parse("* * * * *").describe(),
            "minute=*; hour=*; day-of-month=*; month=*; day-of-week=*",
        )

    def test_describe_values(self):
        text = parse("*/15 9 * * 1-5").describe()
        self.assertIn("minute=0,15,30,45", text)
        self.assertIn("hour=9", text)
        self.assertIn("day-of-week=1,2,3,4,5", text)


if __name__ == "__main__":
    unittest.main()
