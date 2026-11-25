"""Tests for systemd timer unit parsing and calendar drift detection."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from cronpulse.cli import EXIT_DRIFT, EXIT_INVALID, EXIT_OK, main
from cronpulse.oncalendar import parse_calendar
from cronpulse.timers import (
    TimerError,
    canonical_calendar,
    compare_timers,
    load_timer_expectations,
    load_timer_units,
    parse_timer_unit,
)


BACKUP_TIMER = """\
[Unit]
Description=Nightly backup

[Timer]
OnCalendar=daily
Unit=backup.service
Persistent=true

[Install]
WantedBy=timers.target
"""


def run_cli(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class CanonicalCalendarTests(unittest.TestCase):
    def test_shortcuts_and_explicit_forms_are_equal(self):
        self.assertEqual(
            canonical_calendar(parse_calendar("weekly")),
            canonical_calendar(parse_calendar("Mon *-*-* 00:00:00")),
        )
        self.assertEqual(
            canonical_calendar(parse_calendar("Mon 00:00")),
            canonical_calendar(parse_calendar("weekly")),
        )

    def test_all_weekdays_equal_no_prefix(self):
        self.assertEqual(
            canonical_calendar(parse_calendar("Mon..Sun 00:00")),
            canonical_calendar(parse_calendar("daily")),
        )

    def test_weekday_list_equals_range(self):
        self.assertEqual(
            canonical_calendar(parse_calendar("Mon,Tue 09:00")),
            canonical_calendar(parse_calendar("Mon..Tue 09:00")),
        )

    def test_month_name_equals_number(self):
        self.assertEqual(
            canonical_calendar(parse_calendar("*-Jan-01 00:00")),
            canonical_calendar(parse_calendar("*-01-01 00:00")),
        )

    def test_different_schedules_differ(self):
        self.assertNotEqual(
            canonical_calendar(parse_calendar("daily")),
            canonical_calendar(parse_calendar("Mon 09:00")),
        )


class ParseTimerUnitTests(unittest.TestCase):
    def test_reads_calendar_and_service(self):
        entries = parse_timer_unit(BACKUP_TIMER, "backup.timer", source="/etc/systemd/system/backup.timer")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "backup.timer")
        self.assertEqual(entries[0].calendar, "daily")
        self.assertEqual(entries[0].service, "backup.service")
        self.assertEqual(entries[0].source, "/etc/systemd/system/backup.timer")
        self.assertEqual(entries[0].line, 5)

    def test_ignores_comments_and_keys_outside_timer(self):
        text = """\
# top comment
[Unit]
OnCalendar=not-a-timer-key
[Timer]
; semicolon comment
AccuracySec=1min
OnCalendar=*-*-* 03:30:00
"""
        entries = parse_timer_unit(text, "x.timer")
        self.assertEqual([entry.calendar for entry in entries], ["*-*-* 03:30:00"])

    def test_multiple_calendar_lines_are_kept_in_order(self):
        text = """\
[Timer]
OnCalendar=Mon 09:00
OnCalendar=Fri 18:00
"""
        entries = parse_timer_unit(text, "twice.timer")
        self.assertEqual([entry.line for entry in entries], [2, 3])
        self.assertEqual([entry.calendar for entry in entries], ["Mon 09:00", "Fri 18:00"])

    def test_service_can_follow_calendar_lines(self):
        text = """\
[Timer]
OnCalendar=daily
Unit=cleanup.service
"""
        entries = parse_timer_unit(text, "cleanup.timer")
        self.assertEqual(entries[0].service, "cleanup.service")

    def test_continuation_lines_are_unfolded(self):
        text = """\
[Timer]
OnCalendar=Mon..Fri \\
    09:00
"""
        entries = parse_timer_unit(text, "cont.timer")
        self.assertEqual(entries[0].calendar, "Mon..Fri 09:00")
        self.assertEqual(entries[0].line, 2)

    def test_section_names_are_case_insensitive(self):
        entries = parse_timer_unit("[timer]\nOnCalendar=daily\n", "x.timer")
        self.assertEqual(entries[0].calendar, "daily")

    def test_invalid_calendar_reports_source_and_line(self):
        text = "[Timer]\nOnCalendar=Mon 25:00\n"
        with self.assertRaises(TimerError) as ctx:
            parse_timer_unit(text, "bad.timer", source="bad.timer")
        self.assertIn("bad.timer:2", str(ctx.exception))
        self.assertIn("invalid OnCalendar", str(ctx.exception))

    def test_empty_oncalendar_reports_line(self):
        with self.assertRaises(TimerError) as ctx:
            parse_timer_unit("[Timer]\nOnCalendar=\n", "x.timer", source="x.timer")
        self.assertIn("x.timer:2", str(ctx.exception))
        self.assertIn("must not be empty", str(ctx.exception))

    def test_missing_oncalendar(self):
        with self.assertRaises(TimerError) as ctx:
            parse_timer_unit("[Timer]\nPersistent=true\n", "idle.timer")
        self.assertIn("no OnCalendar", str(ctx.exception))

    def test_relative_only_timer_is_rejected(self):
        text = "[Timer]\nOnBootSec=15min\n"
        with self.assertRaises(TimerError) as ctx:
            parse_timer_unit(text, "boot.timer", source="boot.timer")
        self.assertIn("OnBootSec", str(ctx.exception))
        self.assertIn("--allow-relative", str(ctx.exception))

    def test_relative_only_timer_can_be_skipped(self):
        text = "[Timer]\nOnBootSec=15min\nOnUnitActiveSec=1h\n"
        entries = parse_timer_unit(text, "boot.timer", allow_relative=True)
        self.assertEqual(entries, [])

    def test_relative_trigger_next_to_calendar_is_allowed(self):
        text = "[Timer]\nOnCalendar=daily\nOnBootSec=5min\n"
        entries = parse_timer_unit(text, "mixed.timer")
        self.assertEqual([entry.calendar for entry in entries], ["daily"])

    def test_malformed_section_header(self):
        with self.assertRaises(TimerError) as ctx:
            parse_timer_unit("[Timer\nOnCalendar=daily\n", "x.timer", source="x.timer")
        self.assertIn("malformed section", str(ctx.exception))

    def test_line_without_equals(self):
        with self.assertRaises(TimerError) as ctx:
            parse_timer_unit("[Timer]\nnonsense\n", "x.timer", source="x.timer")
        self.assertIn("expected 'Key=value'", str(ctx.exception))

    def test_empty_name_is_rejected(self):
        with self.assertRaises(TimerError):
            parse_timer_unit("[Timer]\nOnCalendar=daily\n", "  ")


class CompareTimersTests(unittest.TestCase):
    def test_identical_timers_have_no_drift(self):
        actual = parse_timer_unit(BACKUP_TIMER, "backup.timer")
        expected = parse_timer_unit(BACKUP_TIMER, "backup.timer")
        report = compare_timers(expected, actual)
        self.assertEqual(report.matched, 1)
        self.assertFalse(report.has_drift)
        self.assertFalse(report.has_issues)

    def test_renamed_timer_is_missing_and_unexpected(self):
        actual = parse_timer_unit("[Timer]\nOnCalendar=daily\n", "new.timer")
        expected = parse_timer_unit("[Timer]\nOnCalendar=daily\n", "old.timer")
        report = compare_timers(expected, actual)
        self.assertEqual([entry.name for entry in report.missing], ["old.timer"])
        self.assertEqual([entry.name for entry in report.unexpected], ["new.timer"])
        self.assertTrue(report.has_drift)

    def test_schedule_change_is_reported_once(self):
        actual = parse_timer_unit("[Timer]\nOnCalendar=Mon 09:00\n", "backup.timer")
        expected = parse_timer_unit("[Timer]\nOnCalendar=daily\n", "backup.timer")
        report = compare_timers(expected, actual)
        self.assertEqual(len(report.changed), 1)
        self.assertEqual(report.changed[0].expected.calendar, "daily")
        self.assertEqual(report.changed[0].actual.calendar, "Mon 09:00")
        self.assertEqual(report.missing, [])
        self.assertEqual(report.unexpected, [])

    def test_service_change_is_reported_once(self):
        actual = parse_timer_unit(
            "[Timer]\nOnCalendar=daily\nUnit=new.service\n", "backup.timer"
        )
        expected = parse_timer_unit(
            "[Timer]\nOnCalendar=daily\nUnit=old.service\n", "backup.timer"
        )
        report = compare_timers(expected, actual)
        self.assertEqual(len(report.changed), 1)
        self.assertEqual(report.changed[0].expected.service, "old.service")
        self.assertEqual(report.changed[0].actual.service, "new.service")

    def test_semantically_equal_formatting_is_not_drift(self):
        actual = parse_timer_unit("[Timer]\nOnCalendar=Mon 00:00\n", "weekly.timer")
        expected = parse_timer_unit("[Timer]\nOnCalendar=weekly\n", "weekly.timer")
        report = compare_timers(expected, actual)
        self.assertFalse(report.has_issues)

    def test_duplicate_calendar_lines_are_warned(self):
        text = "[Timer]\nOnCalendar=daily\nOnCalendar=*-*-* 00:00:00\n"
        actual = parse_timer_unit(text, "dup.timer")
        report = compare_timers([], actual)
        self.assertEqual(report.actual_count, 2)
        self.assertEqual(len(report.duplicates), 1)
        self.assertTrue(report.has_issues)

    def test_never_fires_is_warned(self):
        actual = parse_timer_unit("[Timer]\nOnCalendar=*-02-30 00:00\n", "never.timer")
        report = compare_timers([], actual)
        self.assertTrue(report.never_fires)
        self.assertTrue(report.has_issues)

    def test_report_payload(self):
        actual = parse_timer_unit("[Timer]\nOnCalendar=Mon 09:00\n", "backup.timer")
        expected = parse_timer_unit("[Timer]\nOnCalendar=daily\n", "backup.timer")
        payload = compare_timers(expected, actual).as_dict()
        self.assertEqual(payload["expected_count"], 1)
        self.assertEqual(payload["actual_count"], 1)
        self.assertEqual(payload["matched"], 0)
        self.assertEqual(payload["changed"][0]["name"], "backup.timer")
        self.assertTrue(payload["drift"])


class LoadTimerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)

    def test_directory_scan_only_reads_timer_files(self):
        write(self.path("b.timer"), "[Timer]\nOnCalendar=daily\n")
        write(self.path("a.timer"), "[Timer]\nOnCalendar=hourly\n")
        write(self.path("c.service"), "[Service]\nExecStart=/bin/true\n")
        write(self.path("notes.txt"), "ignore me")
        entries = load_timer_units([self.tmp.name])
        self.assertEqual([entry.name for entry in entries], ["a.timer", "b.timer"])

    def test_explicit_file_is_loaded(self):
        path = self.path("one.timer")
        write(path, "[Timer]\nOnCalendar=daily\n")
        entries = load_timer_units([path])
        self.assertEqual(entries[0].source, path)

    def test_unreadable_path(self):
        with self.assertRaises(TimerError) as ctx:
            load_timer_units([self.path("missing.timer")])
        self.assertIn("cannot read timer unit", str(ctx.exception))

    def test_load_expectations_from_list(self):
        path = self.path("expect.json")
        write(
            path,
            json.dumps(
                [
                    {"name": "backup.timer", "calendar": "daily", "service": "backup.service"},
                    {"name": "report.timer", "calendars": ["Mon 09:00", "Fri 18:00"]},
                ]
            ),
        )
        entries = load_timer_expectations(path)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].service, "backup.service")
        self.assertEqual([entry.calendar for entry in entries[1:]], ["Mon 09:00", "Fri 18:00"])

    def test_load_expectations_from_timers_object(self):
        path = self.path("expect.json")
        write(path, json.dumps({"timers": [{"name": "x.timer", "calendar": "daily"}]}))
        self.assertEqual(len(load_timer_expectations(path)), 1)

    def test_expectation_requires_name_and_calendar(self):
        path = self.path("expect.json")
        write(path, json.dumps([{"calendar": "daily"}]))
        with self.assertRaises(TimerError) as ctx:
            load_timer_expectations(path)
        self.assertIn("'name'", str(ctx.exception))

        write(path, json.dumps([{"name": "x.timer"}]))
        with self.assertRaises(TimerError) as ctx:
            load_timer_expectations(path)
        self.assertIn("'calendar' or 'calendars'", str(ctx.exception))

    def test_expectation_rejects_both_shapes_at_once(self):
        path = self.path("expect.json")
        write(
            path,
            json.dumps(
                [{"name": "x.timer", "calendar": "daily", "calendars": ["hourly"]}]
            ),
        )
        with self.assertRaises(TimerError) as ctx:
            load_timer_expectations(path)
        self.assertIn("either 'calendar' or 'calendars'", str(ctx.exception))

    def test_expectation_invalid_json(self):
        path = self.path("expect.json")
        write(path, "{not json")
        with self.assertRaises(TimerError) as ctx:
            load_timer_expectations(path)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_expectation_invalid_calendar(self):
        path = self.path("expect.json")
        write(path, json.dumps([{"name": "x.timer", "calendar": "Mon 25:00"}]))
        with self.assertRaises(TimerError) as ctx:
            load_timer_expectations(path)
        self.assertIn("invalid OnCalendar", str(ctx.exception))


class TimerDriftCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)

    def manifest(self, payload) -> str:
        path = self.path("expected.json")
        write(path, json.dumps(payload))
        return path

    def test_clean_run_exits_zero(self):
        write(self.path("backup.timer"), "[Timer]\nOnCalendar=daily\nUnit=backup.service\n")
        expect = self.manifest(
            [{"name": "backup.timer", "calendar": "daily", "service": "backup.service"}]
        )
        code, out, _ = run_cli("timer-drift", self.tmp.name, "--expect", expect)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no drift", out)

    def test_drift_exits_four_and_prints_change(self):
        write(self.path("backup.timer"), "[Timer]\nOnCalendar=Mon 09:00\n")
        expect = self.manifest([{"name": "backup.timer", "calendar": "daily"}])
        code, out, _ = run_cli("timer-drift", self.path("backup.timer"), "--expect", expect)
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("changed (1):", out)
        self.assertIn("backup.timer: daily -> Mon 09:00", out)

    def test_json_output(self):
        write(self.path("backup.timer"), "[Timer]\nOnCalendar=Mon 09:00\n")
        expect = self.manifest([{"name": "backup.timer", "calendar": "daily"}])
        code, out, _ = run_cli(
            "timer-drift", self.tmp.name, "--expect", expect, "--json"
        )
        self.assertEqual(code, EXIT_DRIFT)
        payload = json.loads(out)
        self.assertTrue(payload["drift"])
        self.assertEqual(payload["changed"][0]["actual_calendar"], "Mon 09:00")

    def test_invalid_calendar_exits_two(self):
        write(self.path("bad.timer"), "[Timer]\nOnCalendar=Mon 25:00\n")
        expect = self.manifest([])
        code, _, err = run_cli("timer-drift", self.tmp.name, "--expect", expect)
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("invalid OnCalendar", err)

    def test_relative_timer_needs_opt_in(self):
        write(self.path("boot.timer"), "[Timer]\nOnBootSec=15min\n")
        expect = self.manifest([])
        code, _, err = run_cli("timer-drift", self.tmp.name, "--expect", expect)
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("--allow-relative", err)

        code, out, _ = run_cli(
            "timer-drift", self.tmp.name, "--expect", expect, "--allow-relative"
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no drift", out)

    def test_never_fires_is_a_warning(self):
        write(self.path("never.timer"), "[Timer]\nOnCalendar=*-02-30 00:00\n")
        expect = self.manifest([{"name": "never.timer", "calendar": "*-02-30 00:00"}])
        code, out, _ = run_cli("timer-drift", self.tmp.name, "--expect", expect)
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("never fire", out)


if __name__ == "__main__":
    unittest.main()
