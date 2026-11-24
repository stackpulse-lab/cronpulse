"""Command line interface for CronPulse."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import List, Optional, Sequence

from . import __version__
from .expr import CronError, parse

try:  # Python >= 3.9
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

HORIZON_YEARS = 8
EXIT_OK = 0
EXIT_INVALID = 2
EXIT_NEVER = 3


def _add_time_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--after",
        metavar="ISO8601",
        help="start searching strictly after this local time (default: now)",
    )
    parser.add_argument(
        "--tz",
        metavar="ZONE",
        help="IANA timezone for --after and output, e.g. Asia/Shanghai",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cronpulse",
        description="Explain cron expressions, preview upcoming runs and detect schedules that never fire.",
    )
    parser.add_argument("--version", action="version", version=f"cronpulse {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p_next = sub.add_parser("next", help="preview upcoming run times")
    p_next.add_argument("expression", help="cron expression, e.g. '*/15 9-18 * * 1-5'")
    p_next.add_argument("-n", "--count", type=int, default=5, help="how many runs (default: 5)")
    _add_time_options(p_next)
    p_next.add_argument("--json", action="store_true", help="machine-readable output")

    p_check = sub.add_parser("check", help="detect a schedule that never fires")
    p_check.add_argument("expression", help="cron expression")
    _add_time_options(p_check)
    p_check.add_argument("--json", action="store_true", help="machine-readable output")

    p_explain = sub.add_parser("explain", help="show how each field was parsed")
    p_explain.add_argument("expression", help="cron expression")
    p_explain.add_argument("--json", action="store_true", help="machine-readable output")

    return parser


def _resolve_tz(name: Optional[str]):
    if not name:
        return None
    if ZoneInfo is None:  # pragma: no cover
        raise CronError("timezone support requires Python 3.9+ (zoneinfo)")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise CronError(f"unknown timezone {name!r}") from None


def _resolve_after(value: Optional[str], tz) -> datetime:
    if not value:
        return datetime.now(tz).replace(second=0, microsecond=0)
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise CronError(
            f"--after must be ISO-8601 (e.g. 2026-11-24T09:00), got {value!r}"
        ) from None
    if tz is not None and parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed


def _isoformat(values: Sequence[datetime]) -> List[str]:
    return [dt.isoformat() for dt in values]


def _dst_payload(result) -> dict:
    return {
        "skipped": [dt.isoformat(sep=" ") for dt in result.skipped],
        "ambiguous": [dt.isoformat(sep=" ") for dt in result.ambiguous],
    }


def _print_dst_notes(result) -> None:
    if result.skipped:
        print(
            "note: skipped {0} wall-clock time(s) that do not exist (DST gap): {1}".format(
                len(result.skipped), ", ".join(dt.isoformat(sep=" ") for dt in result.skipped)
            )
        )
    if result.ambiguous:
        print(
            "note: {0} wall-clock time(s) occur twice (DST overlap), both listed: {1}".format(
                len(result.ambiguous), ", ".join(dt.isoformat(sep=" ") for dt in result.ambiguous)
            )
        )


def _cmd_next(args: argparse.Namespace, expr) -> int:
    tz = _resolve_tz(args.tz)
    after = _resolve_after(args.after, tz)
    result = expr.scan(after, count=args.count, horizon_years=HORIZON_YEARS)
    runs = result.runs
    if args.json:
        payload = {
            "expression": expr.expression,
            "after": after.isoformat(),
            "runs": _isoformat(runs),
            "dst": _dst_payload(result),
        }
        print(json.dumps(payload, ensure_ascii=False))
        return EXIT_OK
    if not runs:
        print(f"{expr.expression}: never fires within {HORIZON_YEARS} years", file=sys.stderr)
        return EXIT_NEVER
    print(f"{expr.expression}  (after {after.isoformat()})")
    for index, run in enumerate(runs, 1):
        print(f"{index:>2}. {run.isoformat(sep=' ')}")
    _print_dst_notes(result)
    return EXIT_OK


def _cmd_check(args: argparse.Namespace, expr) -> int:
    tz = _resolve_tz(args.tz)
    after = _resolve_after(args.after, tz)
    result = expr.scan(after, count=3, horizon_years=HORIZON_YEARS)
    runs = result.runs
    never = not runs
    if args.json:
        payload = {
            "expression": expr.expression,
            "after": after.isoformat(),
            "never_fires": never,
            "horizon_years": HORIZON_YEARS,
            "preview": _isoformat(runs),
            "dst": _dst_payload(result),
        }
        print(json.dumps(payload, ensure_ascii=False))
    elif never:
        print(f"{expr.expression}: NEVER fires within {HORIZON_YEARS} years")
    else:
        print(f"{expr.expression}: fires next at {runs[0].isoformat(sep=' ')}")
        for run in runs[1:]:
            print(f"  then {run.isoformat(sep=' ')}")
        _print_dst_notes(result)
    return EXIT_NEVER if never else EXIT_OK


def _cmd_explain(args: argparse.Namespace, expr) -> int:
    hint = (
        "cron daemons run in the system timezone; classic Vixie cron also honours "
        "a per-crontab CRON_TZ=<IANA zone> line, systemd timers ignore CRON_TZ "
        "(use OnCalendar with its own DST rules). Preview another zone with --tz."
    )
    if args.json:
        print(
            json.dumps(
                {
                    "expression": expr.expression,
                    "fields": expr.describe(),
                    "day_of_month_and_day_of_week": (
                        "or" if expr.dom_restricted and expr.dow_restricted else "standard"
                    ),
                    "timezone_hint": hint,
                },
                ensure_ascii=False,
            )
        )
        return EXIT_OK
    print(f"expression : {expr.expression}")
    print(f"fields     : {expr.describe()}")
    if expr.dom_restricted and expr.dow_restricted:
        print("note       : day-of-month and day-of-week are OR-ed (classic cron)")
    print(f"hint       : {hint}")
    return EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_OK

    try:
        expr = parse(args.expression)
        if args.command == "next":
            return _cmd_next(args, expr)
        if args.command == "check":
            return _cmd_check(args, expr)
        if args.command == "explain":
            return _cmd_explain(args, expr)
    except CronError as exc:
        print(f"cronpulse: invalid expression: {exc}", file=sys.stderr)
        return EXIT_INVALID

    parser.print_help()
    return EXIT_INVALID


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
