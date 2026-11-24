"""systemd ``OnCalendar=`` parsing and preview.

systemd timers use a calendar specification that is deliberately *not* cron:
fields are AND-ed (a weekday and a day-of-month must both match), weekday
names come first, and ranges use ``..`` / lists / ``/`` steps.  CronPulse
implements the documented subset below and rejects anything it cannot
represent exactly, instead of silently translating it into a different
schedule.

Supported

* shortcuts: ``minutely`` ``hourly`` ``daily`` ``midnight`` ``weekly``
  ``monthly`` ``quarterly`` ``semi-annually`` ``yearly`` ``annually``
* optional weekday prefix: ``Mon``, ``Mon..Fri``, ``Sat,Sun`` (full English
  names accepted)
* date: ``[YYYY-]MM-DD`` where each part may be ``*``, ``N``, ``N..M``,
  ``N/M``, ``N..M/M`` or a comma list; month names are accepted
* time: ``HH:MM`` or ``HH:MM:SS`` with the same per-component syntax;
  omitted seconds default to ``00``, omitted time defaults to ``00:00:00``

Not supported (explicit error): the ``~`` last-day-of-month marker, the
``?`` cron wildcard, microseconds, and the trailing ``UTC`` / timezone
suffixes (use ``--tz`` instead).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from .expr import ScanResult, _wall_instants

__all__ = [
    "CalendarError",
    "CalendarSpec",
    "parse_calendar",
    "SHORTCUTS",
]

MONTH_NAMES: Dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

WEEKDAY_NAMES: Dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# systemd expands these shorthands; CronPulse keeps the expansion visible in
# the normalised output so users can see what the alias actually means.
SHORTCUTS: Dict[str, str] = {
    "minutely": "*-*-* *:*:00",
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 00:00:00",
    "midnight": "*-*-* 00:00:00",
    "weekly": "Mon *-*-* 00:00:00",
    "monthly": "*-*-01 00:00:00",
    "quarterly": "*-01,04,07,10-01 00:00:00",
    "semi-annually": "*-01,07-01 00:00:00",
    "semiannually": "*-01,07-01 00:00:00",
    "yearly": "*-01-01 00:00:00",
    "annually": "*-01-01 00:00:00",
}

YEAR_LOW = 1970
YEAR_HIGH = 2199

_Range = Tuple[int, int, int]  # start, end, step


class CalendarError(ValueError):
    """Raised when an ``OnCalendar=`` specification cannot be parsed."""


def _parse_value(
    token: str,
    name: str,
    low: int,
    high: int,
    names: Optional[Dict[str, int]],
) -> int:
    raw = token.strip().lower()
    if names and raw in names:
        return names[raw]
    try:
        value = int(raw, 10)
    except ValueError:
        hint = f" or a name such as {sorted(names)[0]}" if names else ""
        raise CalendarError(f"{name}: {token!r} is not a number{hint}") from None
    if not low <= value <= high:
        raise CalendarError(f"{name}: {value} out of range {low}-{high}")
    return value


def _parse_component(
    text: str,
    name: str,
    low: int,
    high: int,
    names: Optional[Dict[str, int]] = None,
) -> Tuple[FrozenSet[int], Tuple[_Range, ...]]:
    """Parse one ``OnCalendar=`` component into values and normalised ranges."""
    text = text.strip()
    if not text:
        raise CalendarError(f"{name}: empty component")
    if "?" in text:
        raise CalendarError(
            f"{name}: '?' is a Quartz/cron wildcard; systemd OnCalendar uses '*'"
        )
    if "~" in text:
        raise CalendarError(
            f"{name}: the '~' last-day-of-month marker is not supported; "
            "write the concrete days or use a monthly shortcut"
        )

    values: set = set()
    ranges: List[_Range] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            raise CalendarError(f"{name}: empty list element in {text!r}")

        step = 1
        has_step = "/" in item
        if has_step:
            base, _, step_text = item.partition("/")
            try:
                step = int(step_text, 10)
            except ValueError:
                raise CalendarError(
                    f"{name}: step {step_text!r} is not an integer"
                ) from None
            if step <= 0:
                raise CalendarError(f"{name}: step must be positive, got {step}")
        else:
            base = item

        if base == "*":
            start, end = low, high
        elif ".." in base:
            left, _, right = base.partition("..")
            start = _parse_value(left, name, low, high, names)
            end = _parse_value(right, name, low, high, names)
            if start > end:
                raise CalendarError(
                    f"{name}: range {base!r} is descending ({start} > {end})"
                )
        else:
            start = _parse_value(base, name, low, high, names)
            # ``a/b`` iterates from a to the maximum, like systemd.
            end = high if has_step else start

        if not has_step:
            step = 1
        elif start == end:
            raise CalendarError(
                f"{name}: step on single value {base!r} selects nothing"
            )

        for value in range(start, end + 1, step):
            values.add(value)
        ranges.append((start, end, step))

    if not values:
        raise CalendarError(f"{name}: expression {text!r} selects no values")
    return frozenset(values), tuple(ranges)


def _format_values(
    ranges: Sequence[_Range],
    low: int,
    high: int,
    pad: int,
    names: Optional[Sequence[str]] = None,
) -> str:
    if len(ranges) == 1 and ranges[0] == (low, high, 1):
        return "*"

    def label(value: int) -> str:
        if names is not None:
            return names[value]
        return f"{value:0{pad}d}"

    items: List[str] = []
    for start, end, step in ranges:
        if start == end:
            items.append(label(start))
        elif step == 1 and end - start == 1:
            items.append(f"{label(start)},{label(end)}")
        elif step == 1:
            items.append(f"{label(start)}..{label(end)}")
        else:
            items.append(f"{label(start)}..{label(end)}/{step}")
    return ",".join(items)


def _parse_weekday(text: str) -> Tuple[FrozenSet[int], str]:
    text = text.strip()
    if not text:
        raise CalendarError("weekday: empty component")
    if "*" == text:
        return frozenset(range(7)), "*"

    values: set = set()
    bounds: List[Tuple[int, int]] = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            raise CalendarError(f"weekday: empty list element in {text!r}")
        if "/" in item:
            raise CalendarError(
                "weekday: steps are not supported; list the weekdays explicitly"
            )
        if ".." in item:
            left, _, right = item.partition("..")
        elif "-" in item:
            left, _, right = item.partition("-")
            item = f"{left}..{right}"
        else:
            left = right = item
        start = _parse_weekday_name(left, text)
        end = _parse_weekday_name(right, text)
        if start > end:
            raise CalendarError(
                f"weekday: range {item!r} is descending ({left} > {right})"
            )
        values.update(range(start, end + 1))
        bounds.append((start, end))

    rendered = ",".join(
        WEEKDAY_LABELS[start]
        if start == end
        else f"{WEEKDAY_LABELS[start]}..{WEEKDAY_LABELS[end]}"
        for start, end in bounds
    )
    return frozenset(values), rendered


def _parse_weekday_name(token: str, whole: str) -> int:
    raw = token.strip().lower()
    if re.fullmatch(r"\d+", raw):
        raise CalendarError(
            f"weekday: numeric weekday {token!r} in {whole!r} is not systemd "
            "syntax; use Mon..Sun names"
        )
    if raw not in WEEKDAY_NAMES:
        raise CalendarError(
            f"weekday: unknown weekday {token!r}; use Mon, Tue, … or full names"
        )
    return WEEKDAY_NAMES[raw]


def _split_date(text: str) -> Tuple[str, str, str]:
    parts = text.split("-")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return "*", parts[0], parts[1]
    raise CalendarError(
        f"date: expected [YYYY-]MM-DD, got {text!r} "
        "(ranges use '..', not '-')"
    )


def _split_time(text: str) -> Tuple[str, str, str]:
    if re.search(r"(?<!\.)\.(?!\.)", text):
        raise CalendarError(
            f"time: fractional seconds ({text!r}) are not supported; "
            "use whole seconds"
        )
    parts = text.split(":")
    if len(parts) == 2:
        return parts[0], parts[1], "0"
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise CalendarError(
        f"time: expected HH:MM or HH:MM:SS, got {text!r}"
    )


@dataclass(frozen=True)
class CalendarSpec:
    """A parsed systemd ``OnCalendar=`` specification."""

    expression: str
    normalized: str
    year_ranges: Tuple[_Range, ...]
    years: FrozenSet[int]
    month_ranges: Tuple[_Range, ...]
    months: FrozenSet[int]
    day_ranges: Tuple[_Range, ...]
    days: FrozenSet[int]
    weekday: Optional[FrozenSet[int]]
    weekday_text: str
    hour_ranges: Tuple[_Range, ...]
    hours: FrozenSet[int]
    minute_ranges: Tuple[_Range, ...]
    minutes: FrozenSet[int]
    second_ranges: Tuple[_Range, ...]
    seconds: FrozenSet[int]

    # -- matching ----------------------------------------------------
    def matches_day(self, day: date) -> bool:
        if day.year not in self.years:
            return False
        if day.month not in self.months:
            return False
        if day.day not in self.days:
            return False
        if self.weekday is not None and day.weekday() not in self.weekday:
            return False
        return True

    # -- schedule ----------------------------------------------------
    def scan(
        self,
        after: Optional[datetime] = None,
        count: int = 1,
        horizon_years: int = 8,
    ) -> ScanResult:
        """Collect up to ``count`` runs strictly after ``after``.

        Matching follows systemd: every component is AND-ed.  With an aware
        ``after`` the preview is wall-clock based in that zone; times inside a
        DST gap are reported in ``skipped`` (they never happen), and times
        inside an overlap are listed once (the first instant) and reported in
        ``ambiguous``.
        """
        if count <= 0:
            raise ValueError("count must be positive")
        if horizon_years <= 0:
            raise ValueError("horizon_years must be positive")

        after = after or datetime.now()
        tz = after.tzinfo
        after_wall = after.replace(tzinfo=None) if tz is not None else after
        start_day = after_wall.date()
        runs: List[datetime] = []
        skipped: List[datetime] = []
        ambiguous: List[datetime] = []

        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        seconds = sorted(self.seconds)
        max_days = horizon_years * 366 + 1

        for offset in range(max_days):
            day = start_day + timedelta(days=offset)
            if not self.matches_day(day):
                continue
            for hour in hours:
                for minute in minutes:
                    for second in seconds:
                        wall = datetime(
                            day.year, day.month, day.day, hour, minute, second
                        )
                        if wall <= after_wall:
                            continue
                        instants, kind = _wall_instants(wall, tz)
                        if not instants:
                            skipped.append(wall)
                            continue
                        if kind == "ambiguous":
                            ambiguous.append(wall)
                        run = instants[0]
                        if run > after:
                            runs.append(run)
                            if len(runs) >= count:
                                return ScanResult(runs, skipped, ambiguous)
        return ScanResult(runs, skipped, ambiguous)

    def next_runs(
        self,
        after: Optional[datetime] = None,
        count: int = 1,
        horizon_years: int = 8,
    ) -> List[datetime]:
        return self.scan(after, count=count, horizon_years=horizon_years).runs

    def never_fires(
        self,
        after: Optional[datetime] = None,
        horizon_years: int = 8,
    ) -> bool:
        """True when no occurrence exists inside the horizon.

        Eight years covers a full leap-year cycle, so ``*-02-29`` is correctly
        reported as firing while ``*-02-30`` is reported as impossible.
        """
        return not self.next_runs(after, count=1, horizon_years=horizon_years)


def _looks_like_weekday(token: str) -> bool:
    return (
        ":" not in token
        and not any(char.isdigit() for char in token)
        and any(char.isalpha() for char in token)
    )


def parse_calendar(expression: str) -> CalendarSpec:
    """Parse a systemd ``OnCalendar=`` value.

    Raises :class:`CalendarError` for malformed or unsupported specifications.
    """
    if not isinstance(expression, str):
        raise CalendarError("expression must be a string")
    original = expression
    text = " ".join(expression.split())
    if not text:
        raise CalendarError("empty expression")

    lowered = text.lower()
    if lowered in SHORTCUTS:
        text = SHORTCUTS[lowered]
    elif lowered.endswith(" utc") or lowered.endswith(" local"):
        raise CalendarError(
            f"timezone suffix in {expression!r} is not supported; "
            "pass --tz to cronpulse instead"
        )

    tokens = text.split()
    if (
        len(tokens) == 1
        and tokens[0].lower() not in WEEKDAY_NAMES
        and _looks_like_weekday(tokens[0])
    ):
        raise CalendarError(
            f"unknown shortcut {tokens[0]!r}; supported: "
            + ", ".join(sorted(set(SHORTCUTS) - {"semiannually"}))
        )
    weekday: Optional[FrozenSet[int]] = None
    weekday_text = "*"
    if len(tokens) >= 3 and re.fullmatch(r"\d+", tokens[0]):
        raise CalendarError(
            f"weekday: numeric weekday {tokens[0]!r} is not systemd syntax; "
            "use Mon..Sun names"
        )
    if _looks_like_weekday(tokens[0]):
        weekday, weekday_text = _parse_weekday(tokens.pop(0))

    date_text = "*-*-*"
    time_text = "00:00:00"
    if len(tokens) == 1:
        token = tokens[0]
        if ":" in token:
            time_text = token
        else:
            date_text = token
    elif len(tokens) == 2:
        date_text, time_text = tokens
        if ":" in date_text or ":" not in time_text:
            raise CalendarError(
                f"expected '<date> <time>', got {expression!r}"
            )
    elif len(tokens) == 0:
        # "Mon" alone means "every Monday at 00:00:00", like systemd.
        pass
    else:
        raise CalendarError(
            f"too many components in {expression!r}; "
            "expected '[Weekday] [YYYY-MM-DD] HH:MM[:SS]'"
        )

    year_text, month_text, day_text = _split_date(date_text)
    month_values, month_ranges = _parse_component(
        month_text, "month", 1, 12, MONTH_NAMES
    )
    day_values, day_ranges = _parse_component(day_text, "day", 1, 31)
    year_values, year_ranges = _parse_component(
        year_text, "year", YEAR_LOW, YEAR_HIGH
    )

    hour_text, minute_text, second_text = _split_time(time_text)
    hour_values, hour_ranges = _parse_component(hour_text, "hour", 0, 23)
    minute_values, minute_ranges = _parse_component(minute_text, "minute", 0, 59)
    second_values, second_ranges = _parse_component(second_text, "second", 0, 59)

    normalized = ""
    weekday_prefix = (
        "" if weekday is None or weekday_text == "*" else weekday_text + " "
    )
    date_norm = "-".join(
        (
            _format_values(year_ranges, YEAR_LOW, YEAR_HIGH, 4),
            _format_values(month_ranges, 1, 12, 2),
            _format_values(day_ranges, 1, 31, 2),
        )
    )
    time_norm = ":".join(
        (
            _format_values(hour_ranges, 0, 23, 2),
            _format_values(minute_ranges, 0, 59, 2),
            _format_values(second_ranges, 0, 59, 2),
        )
    )
    normalized = f"{weekday_prefix}{date_norm} {time_norm}"

    return CalendarSpec(
        expression=original.strip(),
        normalized=normalized,
        year_ranges=year_ranges,
        years=year_values,
        month_ranges=month_ranges,
        months=month_values,
        day_ranges=day_ranges,
        days=day_values,
        weekday=weekday,
        weekday_text=weekday_text,
        hour_ranges=hour_ranges,
        hours=hour_values,
        minute_ranges=minute_ranges,
        minutes=minute_values,
        second_ranges=second_ranges,
        seconds=second_values,
    )
