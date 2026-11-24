"""Tests for crontab manifest parsing and drift detection."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from cronpulse.cli import EXIT_DRIFT, EXIT_INVALID, EXIT_OK, main
from cronpulse.manifest import (
    ManifestError,
    canonical_schedule,
    compare_manifests,
    load_expectations,
    parse_crontab,
)

from cronpulse.expr import parse


CRONTAB = """\
# managed by ops
SHELL=/bin/bash
CRON_TZ=Asia/Shanghai

0 9 * * 1-5   /usr/local/bin/backup.sh --full
*/5 * * * *   /usr/local/bin/poll.sh
@daily        /usr/local/bin/cleanup.sh
"""


def run_cli(*argv: str, stdin_text: str = ""):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        with mock.patch("sys.stdin", io.StringIO(stdin_text)):
            code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ParseCrontabTests(unittest.TestCase):
    def test_skips_comments_env_and_blanks(self):
        entries = parse_crontab(CRONTAB, source="test.cron")
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].line, 5)
        # Multi-space alignment is normalised in the command.
        self.assertEqual(entries[0].command, "/usr/local/bin/backup.sh --full")
        self.assertEqual(entries[0].schedule, "0 9 * * 1-5")

    def test_macro_is_expanded(self):
        entries = parse_crontab("@daily /usr/local/bin/cleanup.sh")
        self.assertEqual(entries[0].schedule, "0 0 * * *")
        self.assertEqual(entries[0].canonical, canonical_schedule(parse("@daily")))

    def test_invalid_schedule_reports_line(self):
        text = "0 9 * * * /ok.sh\n0 0 32 * * /bad.sh\n"
        with self.assertRaises(ManifestError) as ctx:
            parse_crontab(text, source="cron.d/app")
        self.assertIn("cron.d/app:2", str(ctx.exception))
        self.assertIn("invalid schedule", str(ctx.exception))

    def test_missing_command(self):
        with self.assertRaises(ManifestError) as ctx:
            parse_crontab("0 9 * * *\n")
        self.assertIn("no command", str(ctx.exception))

    def test_too_few_fields(self):
        with self.assertRaises(ManifestError) as ctx:
            parse_crontab("0 9 *\n")
        self.assertIn("expected 5 schedule fields", str(ctx.exception))

    def test_system_crontab_user_column(self):
        text = "0 3 * * *  root  /usr/sbin/logrotate\n@daily backup /opt/snap.sh\n"
        entries = parse_crontab(text, system=True, source="/etc/crontab")
        self.assertEqual(entries[0].user, "root")
        self.assertEqual(entries[0].command, "/usr/sbin/logrotate")
        self.assertEqual(entries[1].user, "backup")

    def test_system_crontab_requires_user_and_command(self):
        with self.assertRaises(ManifestError) as ctx:
            parse_crontab("0 3 * * * root\n", system=True)
        self.assertIn("no command", str(ctx.exception))

    def test_user_crontab_does_not_consume_user(self):
        entries = parse_crontab("0 3 * * * backup /opt/snap.sh\n")
        self.assertIsNone(entries[0].user)
        self.assertEqual(entries[0].command, "backup /opt/snap.sh")


class CanonicalScheduleTests(unittest.TestCase):
    def test_macro_equals_five_fields(self):
        self.assertEqual(
            canonical_schedule(parse("@daily")),
            canonical_schedule(parse("0 0 * * *")),
        )

    def test_list_equals_range(self):
        self.assertEqual(
            canonical_schedule(parse("0 9 * * 1-5")),
            canonical_schedule(parse("0 9 * * 1,2,3,4,5")),
        )

    def test_unrestricted_star_differs_from_expanded_day_field(self):
        # 0-6 in day-of-week is restricted, which turns on the OR rule when
        # day-of-month is also restricted; "*" does not.
        self.assertNotEqual(
            canonical_schedule(parse("0 0 1 * 0-6")),
            canonical_schedule(parse("0 0 1 * *")),
        )


class ExpectationsTests(unittest.TestCase):
    def _write(self, payload) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        with handle as stream:
            json.dump(payload, stream)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_list_shape(self):
        path = self._write(
            [{"schedule": "@daily", "command": "/opt/a.sh"}]
        )
        entries = load_expectations(path)
        self.assertEqual(entries[0].schedule, "0 0 * * *")
        self.assertEqual(entries[0].source, f"{path}[0]")

    def test_object_shape_with_user(self):
        path = self._write(
            {"entries": [{"schedule": "0 3 * * *", "command": "/x", "user": "root"}]}
        )
        entries = load_expectations(path)
        self.assertEqual(entries[0].user, "root")

    def test_bad_json(self):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        handle.write("{not json")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(ManifestError) as ctx:
            load_expectations(handle.name)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_missing_fields(self):
        path = self._write([{"schedule": "0 0 * * *"}])
        with self.assertRaises(ManifestError) as ctx:
            load_expectations(path)
        self.assertIn("'command'", str(ctx.exception))

    def test_invalid_schedule(self):
        path = self._write([{"schedule": "0 0 32 * *", "command": "/x"}])
        with self.assertRaises(ManifestError) as ctx:
            load_expectations(path)
        self.assertIn("invalid schedule", str(ctx.exception))


class CompareTests(unittest.TestCase):
    def _expected(self, text):
        return parse_crontab(text, source="expect")

    def test_identical_manifests(self):
        report = compare_manifests(self._expected(CRONTAB), parse_crontab(CRONTAB))
        self.assertEqual(report.matched, 3)
        self.assertFalse(report.has_issues)

    def test_missing_entry(self):
        expected = self._expected("0 9 * * 1-5 /a.sh\n*/5 * * * * /b.sh\n")
        report = compare_manifests(expected, parse_crontab("0 9 * * 1-5 /a.sh\n"))
        self.assertEqual(len(report.missing), 1)
        self.assertEqual(report.missing[0].command, "/b.sh")
        self.assertEqual(report.matched, 1)

    def test_unexpected_entry(self):
        expected = self._expected("0 9 * * 1-5 /a.sh\n")
        actual = parse_crontab("0 9 * * 1-5 /a.sh\n0 3 * * * /tmp/oneoff.sh\n")
        report = compare_manifests(expected, actual)
        self.assertEqual([e.command for e in report.unexpected], ["/tmp/oneoff.sh"])

    def test_changed_schedule_same_command(self):
        expected = self._expected("0 9 * * 1-5 /a.sh\n")
        actual = parse_crontab("0 18 * * 1-5 /a.sh\n")
        report = compare_manifests(expected, actual)
        self.assertEqual(len(report.changed), 1)
        self.assertEqual(report.changed[0].expected.schedule, "0 9 * * 1-5")
        self.assertEqual(report.changed[0].actual.schedule, "0 18 * * 1-5")
        self.assertFalse(report.missing)
        self.assertFalse(report.unexpected)

    def test_equivalent_spellings_do_not_drift(self):
        expected = self._expected("@daily /a.sh\n")
        actual = parse_crontab("0 0 * * * /a.sh\n")
        report = compare_manifests(expected, actual)
        self.assertEqual(report.matched, 1)
        self.assertFalse(report.has_drift)

    def test_whitespace_in_command_is_ignored(self):
        expected = self._expected("0 9 * * * /a.sh   --flag\n")
        actual = parse_crontab("0 9 * * *\t/a.sh --flag\n")
        report = compare_manifests(expected, actual)
        self.assertEqual(report.matched, 1)

    def test_duplicates_are_reported(self):
        actual = parse_crontab("0 9 * * * /a.sh\n0 9 * * * /a.sh\n")
        report = compare_manifests(self._expected("0 9 * * * /a.sh\n"), actual)
        self.assertEqual(len(report.duplicates), 1)
        self.assertTrue(report.has_issues)

    def test_never_fires_entry(self):
        text = "0 0 30 2 * /bad.sh\n"
        report = compare_manifests(parse_crontab(text), parse_crontab(text))
        self.assertEqual(len(report.never_fires), 1)
        self.assertEqual(report.never_fires[0].command, "/bad.sh")

    def test_system_user_participates_in_matching(self):
        live = parse_crontab("0 3 * * * root /x.sh\n", system=True)
        expected_root = parse_crontab("0 3 * * * root /x.sh\n", system=True)
        expected_backup = parse_crontab("0 3 * * * backup /x.sh\n", system=True)
        self.assertTrue(compare_manifests(expected_root, live).matched == 1)
        report = compare_manifests(expected_backup, live)
        self.assertEqual(len(report.missing), 1)
        self.assertEqual(len(report.unexpected), 1)

    def test_json_payload_shape(self):
        report = compare_manifests(self._expected("0 9 * * * /a.sh\n"), [])
        payload = report.as_dict()
        self.assertTrue(payload["drift"])
        self.assertEqual(payload["missing"][0]["command"], "/a.sh")
        json.dumps(payload)


class DriftCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crontab = os.path.join(self.tmp.name, "crontab")
        with open(self.crontab, "w", encoding="utf-8") as handle:
            handle.write("0 9 * * 1-5 /a.sh\n")
        self.expect = os.path.join(self.tmp.name, "expect.json")
        with open(self.expect, "w", encoding="utf-8") as handle:
            json.dump([{"schedule": "0 9 * * 1-5", "command": "/a.sh"}], handle)

    def test_no_drift(self):
        code, out, _ = run_cli("drift", self.crontab, "--expect", self.expect)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no drift", out)

    def test_missing_entry_exit_code(self):
        with open(self.expect, "w", encoding="utf-8") as handle:
            json.dump(
                [{"schedule": "0 9 * * 1-5", "command": "/a.sh"},
                 {"schedule": "0 3 * * *", "command": "/b.sh"}],
                handle,
            )
        code, out, _ = run_cli("drift", self.crontab, "--expect", self.expect)
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("missing (1)", out)
        self.assertIn("/b.sh", out)

    def test_json_output(self):
        with open(self.expect, "w", encoding="utf-8") as handle:
            json.dump([{"schedule": "0 3 * * *", "command": "/b.sh"}], handle)
        code, out, _ = run_cli("drift", self.crontab, "--expect", self.expect, "--json")
        self.assertEqual(code, EXIT_DRIFT)
        payload = json.loads(out)
        self.assertEqual(payload["crontab"], self.crontab)
        self.assertEqual(payload["missing"][0]["command"], "/b.sh")

    def test_invalid_crontab(self):
        with open(self.crontab, "w", encoding="utf-8") as handle:
            handle.write("0 0 32 * * /bad.sh\n")
        code, _, err = run_cli("drift", self.crontab, "--expect", self.expect)
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("invalid schedule", err)

    def test_never_fires_warning(self):
        with open(self.crontab, "w", encoding="utf-8") as handle:
            handle.write("0 0 30 2 * /bad.sh\n")
        with open(self.expect, "w", encoding="utf-8") as handle:
            json.dump([{"schedule": "0 0 30 2 *", "command": "/bad.sh"}], handle)
        code, out, _ = run_cli("drift", self.crontab, "--expect", self.expect)
        self.assertEqual(code, EXIT_DRIFT)
        self.assertIn("never fire", out)

    def test_stdin(self):
        code, out, _ = run_cli(
            "drift", "-", "--expect", self.expect, stdin_text="0 9 * * 1-5 /a.sh\n"
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no drift", out)


if __name__ == "__main__":
    unittest.main()
