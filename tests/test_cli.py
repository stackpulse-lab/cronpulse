"""CLI smoke tests (no subprocesses, standard library only)."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from cronpulse.cli import EXIT_INVALID, EXIT_NEVER, EXIT_OK, main


def run_cli(*argv: str):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_next_default(self):
        code, out, _ = run_cli("next", "*/15 * * * *", "--after", "2026-01-01T00:07", "-n", "2")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("2026-01-01 00:15:00", out)
        self.assertIn("2026-01-01 00:30:00", out)

    def test_next_json(self):
        code, out, _ = run_cli(
            "next", "0 9 * * 1-5", "--after", "2026-01-02T12:00", "--json"
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["runs"][0], "2026-01-05T09:00:00")

    def test_check_fires(self):
        code, out, _ = run_cli("check", "0 9 * * *", "--after", "2026-01-01T00:00")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("fires next at", out)

    def test_check_never_fires(self):
        code, out, _ = run_cli("check", "0 0 30 2 *", "--after", "2026-01-01T00:00")
        self.assertEqual(code, EXIT_NEVER)
        self.assertIn("NEVER", out)

    def test_check_json_never(self):
        code, out, _ = run_cli(
            "check", "0 0 30 2 *", "--after", "2026-01-01T00:00", "--json"
        )
        self.assertEqual(code, EXIT_NEVER)
        self.assertTrue(json.loads(out)["never_fires"])

    def test_explain(self):
        code, out, _ = run_cli("explain", "0 9 * * 1-5")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("day-of-week=1,2,3,4,5", out)
        self.assertNotIn("OR-ed", out)

    def test_explain_or_note(self):
        code, out, _ = run_cli("explain", "0 9 1 * 1-5")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("OR-ed", out)

    def test_invalid_expression(self):
        code, _, err = run_cli("next", "* * *")
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("invalid expression", err)

    def test_unknown_timezone(self):
        code, _, err = run_cli("next", "* * * * *", "--tz", "Mars/Olympus")
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("unknown timezone", err)

    def test_bad_after(self):
        code, _, err = run_cli("next", "* * * * *", "--after", "yesterday")
        self.assertEqual(code, EXIT_INVALID)
        self.assertIn("ISO-8601", err)

    def test_no_command_prints_help(self):
        code, out, _ = run_cli()
        self.assertEqual(code, EXIT_OK)
        self.assertIn("usage: cronpulse", out)


if __name__ == "__main__":
    unittest.main()
