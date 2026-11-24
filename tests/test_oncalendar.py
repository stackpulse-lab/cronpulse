"""Tests for systemd ``OnCalendar=`` parsing and preview."""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cronpulse.cli import EXIT_INVALID, EXIT_NEVER, EXIT_OK
from cronpulse.oncalendar import CalendarError, parse_calendar
from test_cli import run_cli

SH = ZoneInfo("Asia/Shanghai")
NY = ZoneInfo("America/New_York")


def after_sh(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=SH)


class ParseTests(unittest.TestCase):
    def test_shortcuts(self):
        cases = {
            "daily": "*-*-* 00:00:00",
            "midnight": "*-*-* 00:00:00",
            "hourly": "*-*-* *:00:00",
            "weekly": "Mon *-*-* 00:00:00",
            "monthly": "*-*-01 00:00:00",
            "quarterly": "*-01,04,07,10-01 00:00:00",
            "semi-annually": "*-01,07-01 00:00:00",
            "yearly": "*-01-01 00:00:00",
            "minutely": "*-*-* *:*:00",
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(parse_calendar(spec).normalized, expected)

    def test_weekday_range_and_list(self):
        self.assertEqual(
            parse_calendar("mon..fri 09:00").normalized, "Mon..Fri *-*-* 09:00:00"
        )
        self.assertEqual(
            parse_calendar("Sat,Sun 10:30").normalized, "Sat,Sun *-*-* 10:30:00"
        )
        self.assertEqual(
            parse_calendar("Friday 18:00").normalized, "Fri *-*-* 18:00:00"
        )

    def test_date_and_time_defaults(self):
        self.assertEqual(parse_calendar("09:00").normalized, "*-*-* 09:00:00")
        self.assertEqual(parse_calendar("2026-11-25").normalized, "2026-11-25 00:00:00")
        self.assertEqual(parse_calendar("Mon").normalized, "Mon *-*-* 00:00:00")

    def test_steps(self):
        spec = parse_calendar("*-*-* 09/2:00:00")
        self.assertEqual(spec.normalized, "*-*-* 09..23/2:00:00")
        self.assertEqual(sorted(spec.hours), [9, 11, 13, 15, 17, 19, 21, 23])
        spec = parse_calendar("*:0/15")
        self.assertEqual(sorted(spec.minutes), [0, 15, 30, 45])
        self.assertEqual(sorted(spec.seconds), [0])

    def test_month_names_and_ranges(self):
        spec = parse_calendar("*-Jan-01 00:00:00")
        self.assertEqual(sorted(spec.months), [1])
        spec = parse_calendar("*-03..04-* 00:00:00")
        self.assertEqual(sorted(spec.months), [3, 4])


class ErrorTests(unittest.TestCase):
    def assert_invalid(self, spec: str, needle: str):
        with self.assertRaises(CalendarError) as ctx:
            parse_calendar(spec)
        self.assertIn(needle, str(ctx.exception))

    def test_quartz_wildcard_rejected(self):
        self.assert_invalid("*-*-? 00:00", "?")
        self.assert_invalid("* *-*-? 00:00", "?")

    def test_last_day_marker_rejected(self):
        self.assert_invalid("*-*-~ 00:00", "~")

    def test_fractional_seconds_rejected(self):
        self.assert_invalid("*-*-* 00:00:00.500", "fractional")

    def test_unknown_shortcut(self):
        self.assert_invalid("fortnightly", "unknown shortcut")

    def test_numeric_weekday_rejected(self):
        self.assert_invalid("1 *-*-* 09:00", "numeric weekday")

    def test_ranges_and_values(self):
        self.assert_invalid("*-13-01 00:00", "out of range")
        self.assert_invalid("*-*-* 24:00", "out of range")
        self.assert_invalid("*-*-* 00:60", "out of range")
        self.assert_invalid("*-05..03-01 00:00", "descending")
        self.assert_invalid("*-*-* 23..09:00", "descending")

    def test_shape_errors(self):
        self.assert_invalid("", "empty")
        self.assert_invalid("Mon Tue *-*-* 09:00", "too many")
        self.assert_invalid("09:00 10:00", "expected")
        self.assert_invalid("daily UTC", "timezone suffix")

    def test_bad_step(self):
        self.assert_invalid("*-*-* 00:00/0", "step must be positive")


class ScheduleTests(unittest.TestCase):
    def test_weekly_from_wednesday(self):
        runs = parse_calendar("weekly").next_runs(after_sh(2026, 11, 25, 6), 2)
        self.assertEqual(
            [run.isoformat() for run in runs],
            ["2026-11-30T00:00:00+08:00", "2026-12-07T00:00:00+08:00"],
        )

    def test_weekdays_only(self):
        runs = parse_calendar("Mon..Fri 09:00").next_runs(after_sh(2026, 11, 25, 6), 3)
        self.assertEqual([run.strftime("%Y-%m-%d %a") for run in runs],
                         ["2026-11-25 Wed", "2026-11-26 Thu", "2026-11-27 Fri"])

    def test_weekend_only(self):
        runs = parse_calendar("Sat,Sun 10:30").next_runs(after_sh(2026, 11, 25, 6), 2)
        self.assertEqual([run.strftime("%Y-%m-%d %a") for run in runs],
                         ["2026-11-28 Sat", "2026-11-29 Sun"])

    def test_strictly_after(self):
        spec = parse_calendar("daily")
        runs = spec.next_runs(after_sh(2026, 11, 25, 0, 0), 1)
        self.assertEqual(runs[0].isoformat(), "2026-11-26T00:00:00+08:00")

    def test_seconds_schedule(self):
        runs = parse_calendar("*:*:30").next_runs(after_sh(2026, 11, 25, 6, 0), 2)
        self.assertEqual([run.strftime("%H:%M:%S") for run in runs],
                         ["06:00:30", "06:01:30"])

    def test_fixed_past_timestamp_never_fires(self):
        spec = parse_calendar("2026-11-25 07:00:00")
        self.assertEqual(spec.next_runs(after_sh(2026, 11, 25, 8), 1), [])
        self.assertTrue(spec.never_fires(after_sh(2026, 11, 25, 8)))

    def test_fixed_future_timestamp(self):
        runs = parse_calendar("2026-11-25 07:00:00").next_runs(after_sh(2026, 11, 25, 6), 1)
        self.assertEqual(runs[0].isoformat(), "2026-11-25T07:00:00+08:00")

    def test_leap_day(self):
        self.assertTrue(parse_calendar("*-02-30 00:00").never_fires(after_sh(2026, 1, 1)))
        runs = parse_calendar("*-02-29 00:00").next_runs(after_sh(2026, 1, 1), 2)
        self.assertEqual([run.date().isoformat() for run in runs],
                         ["2028-02-29", "2032-02-29"])

    def test_month_with_31_days(self):
        runs = parse_calendar("*-*-31 00:00").next_runs(after_sh(2026, 4, 1), 3)
        self.assertEqual([run.date().isoformat() for run in runs],
                         ["2026-05-31", "2026-07-31", "2026-08-31"])

    def test_year_range(self):
        runs = parse_calendar("2026..2027-12-31 23:00").next_runs(after_sh(2026, 1, 1), 2)
        self.assertEqual([run.isoformat() for run in runs],
                         ["2026-12-31T23:00:00+08:00", "2027-12-31T23:00:00+08:00"])

    def test_weekday_and_day_are_anded(self):
        # Unlike cron, systemd requires both the weekday and the day to match.
        runs = parse_calendar("Mon *-*-13 00:00").next_runs(after_sh(2026, 11, 25), 4)
        self.assertTrue(runs)
        for run in runs:
            self.assertEqual(run.weekday(), 0)
            self.assertEqual(run.day, 13)

    def test_dst_gap_is_reported(self):
        after = datetime(2026, 3, 7, 12, 0, tzinfo=NY)
        result = parse_calendar("*-*-* 02:30:00").scan(after, count=2)
        self.assertEqual(
            [run.isoformat() for run in result.runs],
            ["2026-03-09T02:30:00-04:00", "2026-03-10T02:30:00-04:00"],
        )
        self.assertEqual([dt.isoformat(sep=" ") for dt in result.skipped],
                         ["2026-03-08 02:30:00"])

    def test_dst_overlap_listed_once(self):
        after = datetime(2026, 10, 31, 12, 0, tzinfo=NY)
        result = parse_calendar("*-*-* 01:30:00").scan(after, count=2)
        self.assertEqual(result.runs[0].isoformat(), "2026-11-01T01:30:00-04:00")
        self.assertEqual(
            [dt.isoformat(sep=" ") for dt in result.ambiguous],
            ["2026-11-01 01:30:00"],
        )


class CalendarCliTests(unittest.TestCase):
    def test_calendar_text(self):
        code, out, _ = run_cli(
            "calendar", "Mon..Fri 09:00", "--after", "2026-11-25T06:00", "-n", "2"
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Mon..Fri *-*-* 09:00:00", out)
        self.assertIn("2026-11-25 09:00:00", out)
        self.assertIn("2026-11-26 09:00:00", out)

    def test_calendar_json(self):
        code, out, _ = run_cli(
            "calendar", "weekly", "--after", "2026-11-25T06:00", "--json"
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["normalized"], "Mon *-*-* 00:00:00")
        self.assertEqual(payload["runs"][0], "2026-11-30T00:00:00")

    def test_calendar_never(self):
        code, _, err = run_cli(
            "calendar", "*-02-30 00:00", "--after", "2026-01-01T00:00"
        )
        self.assertEqual(code, EXIT_NEVER)
        self.assertIn("NEVER", err)

    def test_calendar_invalid(self):
        code, _, err = run_cli("calendar", "*-*-~ 00:00")
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("invalid OnCalendar expression", err)


if __name__ == "__main__":
    unittest.main()
