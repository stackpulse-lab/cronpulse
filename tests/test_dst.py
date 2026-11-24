"""DST boundary tests: gaps are skipped, overlaps yield two instants."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

from cronpulse import parse
from cronpulse.cli import EXIT_OK, main

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

NY = "America/New_York"


def run_cli(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


@unittest.skipIf(ZoneInfo is None, "zoneinfo unavailable")
class DstGapTests(unittest.TestCase):
    """2026-03-08 02:00 -> 03:00 in America/New_York."""

    def setUp(self):
        self.tz = ZoneInfo(NY)

    def test_gap_time_is_skipped_and_reported(self):
        after = datetime(2026, 3, 7, 12, 0, tzinfo=self.tz)
        result = parse("30 2 * * *").scan(after, count=2)
        self.assertEqual(
            [run.date().isoformat() for run in result.runs],
            ["2026-03-09", "2026-03-10"],
        )
        self.assertEqual(result.skipped, [datetime(2026, 3, 8, 2, 30)])
        self.assertEqual(result.ambiguous, [])

    def test_only_gap_run_on_that_day_means_empty_before_next_day(self):
        after = datetime(2026, 3, 8, 0, 0, tzinfo=self.tz)
        result = parse("30 2 * * *").scan(after, count=1)
        self.assertEqual(result.runs[0], datetime(2026, 3, 9, 2, 30, tzinfo=self.tz))

    def test_gap_does_not_affect_naive_input(self):
        result = parse("30 2 * * *").scan(datetime(2026, 3, 7, 12, 0), count=1)
        self.assertEqual(result.runs, [datetime(2026, 3, 8, 2, 30)])
        self.assertEqual(result.skipped, [])

    def test_fixed_offset_has_no_notes(self):
        tz = timezone(timedelta(hours=8))
        result = parse("30 2 * * *").scan(datetime(2026, 3, 7, 12, 0, tzinfo=tz), count=1)
        self.assertEqual(result.runs, [datetime(2026, 3, 8, 2, 30, tzinfo=tz)])
        self.assertEqual((result.skipped, result.ambiguous), ([], []))


@unittest.skipIf(ZoneInfo is None, "zoneinfo unavailable")
class DstOverlapTests(unittest.TestCase):
    """2026-11-01 02:00 -> 01:00 in America/New_York: 01:xx happens twice."""

    def setUp(self):
        self.tz = ZoneInfo(NY)

    def test_overlap_returns_two_instants(self):
        after = datetime(2026, 10, 31, 12, 0, tzinfo=self.tz)
        result = parse("30 1 * * *").scan(after, count=2)
        self.assertEqual(len(result.runs), 2)
        first, second = result.runs
        self.assertEqual(first.replace(tzinfo=None), datetime(2026, 11, 1, 1, 30))
        self.assertEqual(second.replace(tzinfo=None), datetime(2026, 11, 1, 1, 30))
        self.assertEqual(first.utcoffset(), timedelta(hours=-4))
        self.assertEqual(second.utcoffset(), timedelta(hours=-5))
        self.assertLess(
            first.astimezone(timezone.utc), second.astimezone(timezone.utc)
        )
        self.assertEqual(result.ambiguous, [datetime(2026, 11, 1, 1, 30)])
        self.assertEqual(result.skipped, [])

    def test_runs_stay_chronological(self):
        after = datetime(2026, 10, 31, 12, 0, tzinfo=self.tz)
        runs = parse("*/30 1 * * *").next_runs(after, count=4)
        utc = [run.astimezone(timezone.utc) for run in runs]
        self.assertEqual(utc, sorted(utc))


@unittest.skipIf(ZoneInfo is None, "zoneinfo unavailable")
class DstCliTests(unittest.TestCase):
    def test_next_text_reports_gap(self):
        code, out, _ = run_cli(
            "next", "30 2 * * *", "--tz", NY, "--after", "2026-03-07T12:00", "-n", "1"
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("2026-03-09 02:30:00", out)
        self.assertIn("DST gap", out)
        self.assertIn("2026-03-08 02:30", out)

    def test_next_json_reports_overlap(self):
        code, out, _ = run_cli(
            "next", "30 1 * * *", "--tz", NY, "--after", "2026-10-31T12:00", "-n", "2", "--json"
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["dst"]["ambiguous"], ["2026-11-01 01:30:00"])
        self.assertEqual(payload["dst"]["skipped"], [])
        self.assertEqual(len(payload["runs"]), 2)
        self.assertNotEqual(payload["runs"][0], payload["runs"][1])

    def test_explain_shows_timezone_hint(self):
        code, out, _ = run_cli("explain", "0 9 * * *")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("CRON_TZ", out)
        code, out, _ = run_cli("explain", "0 9 * * *", "--json")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("timezone_hint", json.loads(out))


if __name__ == "__main__":
    unittest.main()
