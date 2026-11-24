"""Cron expression parsing.

CronPulse deliberately implements the classic five-field cron dialect
(minute, hour, day-of-month, month, day-of-week) with the usual
extensions: ranges, lists, steps, month/day names and ``@macros``.

The parser is strict: anything it cannot represent unambiguously is a
``CronError`` rather than a silently ignored token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

__all__ = [
    "CronError",
    "CronExpr",
    "parse",
    "MONTH_NAMES",
    "DOW_NAMES",
]

MONTH_NAMES: Dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

DOW_NAMES: Dict[str, int] = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

MACROS: Dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_FIELD_RE = re.compile(r"^[0-9a-zA-Z*?/,\-]+$")


class CronError(ValueError):
    """Raised when a cron expression cannot be parsed."""


@dataclass(frozen=True)
class _Field:
    name: str
    low: int
    high: int
    names: Dict[str, int]
    allow_seven_as_zero: bool = False


_MINUTE = _Field("minute", 0, 59, {})
_HOUR = _Field("hour", 0, 23, {})
_DOM = _Field("day-of-month", 1, 31, {})
_MONTH = _Field("month", 1, 12, MONTH_NAMES)
_DOW = _Field("day-of-week", 0, 7, DOW_NAMES, allow_seven_as_zero=True)


def _parse_value(token: str, field: _Field) -> int:
    raw = token.strip().lower()
    if raw in field.names:
        return field.names[raw]
    try:
        value = int(raw, 10)
    except ValueError:
        raise CronError(
            f"{field.name}: {token!r} is not a number"
            + (f" or one of {', '.join(sorted(field.names))}" if field.names else "")
        ) from None
    if not field.low <= value <= field.high:
        raise CronError(
            f"{field.name}: {value} out of range {field.low}-{field.high}"
        )
    return value


def _parse_field(text: str, field: _Field) -> Tuple[FrozenSet[int], bool]:
    """Return (allowed values, restricted-flag).

    ``restricted`` tells whether the field was anything other than ``*`` or
    ``?``.  The flag drives the classic day-of-month / day-of-week OR rule.
    """
    text = text.strip()
    if not text:
        raise CronError(f"{field.name}: empty field")
    if not _FIELD_RE.match(text):
        raise CronError(f"{field.name}: invalid characters in {text!r}")

    restricted = text not in ("*", "?")
    values: set = set()

    for item in text.split(","):
        item = item.strip()
        if not item:
            raise CronError(f"{field.name}: empty list element in {text!r}")

        step = 1
        if "/" in item:
            base, _, step_text = item.partition("/")
            try:
                step = int(step_text, 10)
            except ValueError:
                raise CronError(f"{field.name}: step {step_text!r} is not an integer") from None
            if step <= 0:
                raise CronError(f"{field.name}: step must be positive, got {step}")
        else:
            base = item

        if base in ("*", "?"):
            start, end = field.low, field.high
        elif "-" in base:
            left, _, right = base.partition("-")
            start = _parse_value(left, field)
            end = _parse_value(right, field)
            if start > end:
                raise CronError(
                    f"{field.name}: range {base!r} is descending ({start} > {end})"
                )
        else:
            start = _parse_value(base, field)
            # ``a/n`` means "from a to the maximum, every n".
            end = field.high if "/" in item else start

        for value in range(start, end + 1, step):
            if field.allow_seven_as_zero and value == 7:
                value = 0
            values.add(value)

    if not values:
        raise CronError(f"{field.name}: expression {text!r} selects no values")
    return frozenset(values), restricted


@dataclass(frozen=True)
class CronExpr:
    """A parsed five-field cron expression."""

    expression: str
    minute: FrozenSet[int]
    hour: FrozenSet[int]
    day_of_month: FrozenSet[int]
    month: FrozenSet[int]
    day_of_week: FrozenSet[int]
    dom_restricted: bool
    dow_restricted: bool

    # -- description -------------------------------------------------
    def describe(self) -> str:
        def part(name: str, values: FrozenSet[int], low: int, high: int) -> str:
            if values == frozenset(range(low, high + 1)):
                return f"{name}=*"
            return f"{name}={','.join(str(v) for v in sorted(values))}"

        return "; ".join(
            (
                part("minute", self.minute, 0, 59),
                part("hour", self.hour, 0, 23),
                part("day-of-month", self.day_of_month, 1, 31),
                part("month", self.month, 1, 12),
                part("day-of-week", self.day_of_week, 0, 6),
            )
        )

    # -- matching ----------------------------------------------------
    def matches_day(self, day: date) -> bool:
        if day.month not in self.month:
            return False
        if not self.dom_restricted and not self.dow_restricted:
            return True
        dom_ok = day.day in self.day_of_month
        dow_ok = (day.isoweekday() % 7) in self.day_of_week
        if self.dom_restricted and self.dow_restricted:
            # Classic cron: the two fields are OR-ed when both are set.
            return dom_ok or dow_ok
        return dom_ok if self.dom_restricted else dow_ok

    # -- schedule ----------------------------------------------------
    def next_runs(
        self,
        after: Optional[datetime] = None,
        count: int = 1,
        horizon_years: int = 8,
    ) -> List[datetime]:
        """Return up to ``count`` strictly-after-``after`` occurrences.

        Naive input yields naive output; aware input preserves the tzinfo.
        An empty list means "does not fire within the horizon".
        """
        if count <= 0:
            raise ValueError("count must be positive")
        if horizon_years <= 0:
            raise ValueError("horizon_years must be positive")

        after = after or datetime.now()
        allowed_after = after
        start_day = after.date()
        results: List[datetime] = []
        minutes = sorted(self.minute)
        hours = sorted(self.hour)
        max_days = horizon_years * 366 + 1

        for offset in range(max_days):
            day = start_day + timedelta(days=offset)
            if not self.matches_day(day):
                continue
            for hour in hours:
                for minute in minutes:
                    candidate = datetime(
                        day.year,
                        day.month,
                        day.day,
                        hour,
                        minute,
                        tzinfo=after.tzinfo,
                    )
                    if candidate > allowed_after:
                        results.append(candidate)
                        if len(results) >= count:
                            return results
        return results

    def never_fires(
        self,
        after: Optional[datetime] = None,
        horizon_years: int = 8,
    ) -> bool:
        """True when no occurrence exists inside the horizon.

        Eight years covers a full leap-year cycle for day-of-month checks
        such as ``0 0 29 2 *`` (fires 2028-02-29).
        """
        return not self.next_runs(after, count=1, horizon_years=horizon_years)


def parse(expression: str) -> CronExpr:
    """Parse a cron expression into a :class:`CronExpr`.

    Raises :class:`CronError` for malformed or impossible expressions.
    """
    if not isinstance(expression, str):
        raise CronError("expression must be a string")

    text = expression.strip()
    if not text:
        raise CronError("empty expression")

    lowered = text.lower()
    if lowered.startswith("@"):
        if lowered not in MACROS:
            raise CronError(
                f"unknown macro {text!r}; supported: {', '.join(sorted(MACROS))}"
            )
        text = MACROS[lowered]

    fields = text.split()
    if len(fields) != 5:
        raise CronError(
            f"expected 5 fields (minute hour day-of-month month day-of-week), got {len(fields)}"
        )

    minute, _ = _parse_field(fields[0], _MINUTE)
    hour, _ = _parse_field(fields[1], _HOUR)
    dom, dom_restricted = _parse_field(fields[2], _DOM)
    month, _ = _parse_field(fields[3], _MONTH)
    dow, dow_restricted = _parse_field(fields[4], _DOW)

    return CronExpr(
        expression=" ".join(fields),
        minute=minute,
        hour=hour,
        day_of_month=dom,
        month=month,
        day_of_week=dow,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
    )
