"""systemd timer unit parsing and calendar drift detection.

A systemd host describes recurring work in ``*.timer`` unit files.  The files on
disk are the source of truth at runtime; a manifest in the repository says which
timers and which ``OnCalendar=`` values should exist.  Comparing the two catches
timers that were removed during a migration, enabled by hand, or silently
rescheduled -- the same class of drift that :mod:`cronpulse.manifest` detects
for crontabs.

Only calendar timers are compared.  Relative timers (``OnBootSec=`` and
friends) have no fixed calendar, so they are rejected by default instead of
being silently ignored; pass ``allow_relative=True`` to skip them explicitly.
Unit files are parsed as plain text: section names are matched
case-insensitively, continuations with a trailing backslash are unfolded, and
keys outside ``[Timer]`` are ignored.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .oncalendar import CalendarError, CalendarSpec, parse_calendar

__all__ = [
    "TimerError",
    "TimerEntry",
    "TimerChange",
    "TimerDriftReport",
    "RELATIVE_KEYS",
    "canonical_calendar",
    "parse_timer_unit",
    "load_timer_units",
    "load_timer_expectations",
    "compare_timers",
]

RELATIVE_KEYS = (
    "OnBootSec",
    "OnStartupSec",
    "OnUnitActiveSec",
    "OnUnitInactiveSec",
)


class TimerError(ValueError):
    """Raised when a timer unit or timer manifest cannot be read."""


@dataclass(frozen=True)
class TimerEntry:
    """One ``OnCalendar=`` value belonging to a timer unit.

    ``calendar`` keeps the value as written (whitespace-normalised),
    ``normalized`` is the systemd-style expansion printed by ``calendar``, and
    ``canonical`` is the semantic form used for comparison, so ``weekly``,
    ``Mon 00:00`` and ``Mon *-*-* 00:00:00`` are the same schedule.
    """

    name: str
    calendar: str
    canonical: str
    normalized: str
    service: Optional[str] = None
    source: str = ""
    line: int = 0

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.name, self.canonical, self.service or "")

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "name": self.name,
            "calendar": self.calendar,
            "canonical": self.canonical,
            "normalized": self.normalized,
            "source": self.source,
            "line": self.line,
        }
        if self.service is not None:
            payload["service"] = self.service
        return payload


@dataclass(frozen=True)
class TimerChange:
    """Same timer name, different calendar or service unit."""

    expected: TimerEntry
    actual: TimerEntry


@dataclass
class TimerDriftReport:
    """Difference between an expected timer manifest and the live units."""

    matched: int
    missing: List[TimerEntry]
    unexpected: List[TimerEntry]
    changed: List[TimerChange]
    duplicates: List[TimerEntry]
    never_fires: List[TimerEntry]
    expected_count: int
    actual_count: int

    @property
    def has_drift(self) -> bool:
        return bool(self.missing or self.unexpected or self.changed)

    @property
    def has_issues(self) -> bool:
        return self.has_drift or bool(self.duplicates) or bool(self.never_fires)

    def as_dict(self) -> Dict[str, object]:
        return {
            "expected_count": self.expected_count,
            "actual_count": self.actual_count,
            "matched": self.matched,
            "drift": self.has_drift,
            "missing": [entry.as_dict() for entry in self.missing],
            "unexpected": [entry.as_dict() for entry in self.unexpected],
            "changed": [
                {
                    "name": change.actual.name,
                    "expected_calendar": change.expected.calendar,
                    "actual_calendar": change.actual.calendar,
                    "expected_service": change.expected.service,
                    "actual_service": change.actual.service,
                    "source": change.actual.source,
                    "line": change.actual.line,
                }
                for change in self.changed
            ],
            "duplicates": [entry.as_dict() for entry in self.duplicates],
            "never_fires": [entry.as_dict() for entry in self.never_fires],
        }


def _canonical_values(values: Iterable[int], low: int, high: int) -> str:
    frozen = frozenset(values)
    if frozen == frozenset(range(low, high + 1)):
        return "*"
    return ",".join(str(value) for value in sorted(frozen))


def canonical_calendar(spec: CalendarSpec) -> str:
    """Return a semantic, comparable form of a parsed ``OnCalendar=`` value.

    Weekday restrictions are compared as a set: ``Mon..Sun`` is the same as
    no weekday prefix, and ``Mon,Tue`` equals ``Mon..Tue``.  This mirrors the
    cron manifest comparison and keeps reformatting from showing up as drift.
    """

    weekday = "*"
    if spec.weekday is not None and frozenset(spec.weekday) != frozenset(range(7)):
        weekday = ",".join(str(value) for value in sorted(spec.weekday))
    return "|".join(
        (
            weekday,
            _canonical_values(spec.years, 1970, 2199),
            _canonical_values(spec.months, 1, 12),
            _canonical_values(spec.days, 1, 31),
            _canonical_values(spec.hours, 0, 23),
            _canonical_values(spec.minutes, 0, 59),
            _canonical_values(spec.seconds, 0, 59),
        )
    )


def _entry(
    name: str,
    calendar: str,
    service: Optional[str],
    source: str,
    line: int,
) -> TimerEntry:
    try:
        spec = parse_calendar(calendar)
    except CalendarError as exc:
        raise TimerError(f"{source}:{line}: invalid OnCalendar: {exc}") from None
    return TimerEntry(
        name=name,
        calendar=" ".join(calendar.split()),
        canonical=canonical_calendar(spec),
        normalized=spec.normalized,
        service=service,
        source=source,
        line=line,
    )


def _unfold(text: str) -> Iterable[Tuple[int, str]]:
    """Yield ``(line_number, logical_line)`` with continuations unfolded."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        number = index + 1
        line = lines[index]
        while line.rstrip().endswith("\\"):
            line = line.rstrip()[:-1]
            index += 1
            if index >= len(lines):
                break
            line += " " + lines[index].strip()
        yield number, line
        index += 1


def parse_timer_unit(
    text: str,
    name: str,
    *,
    source: str = "<timer>",
    allow_relative: bool = False,
) -> List[TimerEntry]:
    """Parse a systemd timer unit into calendar entries.

    Raises :class:`TimerError` with a line number for malformed units, for
    relative-only timers (unless ``allow_relative`` is true), and for timers
    with no schedule at all.
    """
    if not name or not name.strip():
        raise TimerError(f"{source}: timer name must not be empty")

    section: Optional[str] = None
    calendars: List[Tuple[str, int]] = []
    relative: List[Tuple[str, int]] = []
    service: Optional[str] = None

    for number, raw_line in _unfold(text):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("["):
            if not line.endswith("]"):
                raise TimerError(f"{source}:{number}: malformed section header {line!r}")
            section = line[1:-1].strip().lower()
            continue
        if "=" not in line:
            raise TimerError(f"{source}:{number}: expected 'Key=value', got {line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if section != "timer":
            continue
        if key in RELATIVE_KEYS:
            relative.append((key, number))
            continue
        if key == "OnCalendar":
            if not value:
                raise TimerError(f"{source}:{number}: OnCalendar= must not be empty")
            calendars.append((value, number))
        elif key == "Unit":
            service = value or None

    if not calendars:
        if relative and allow_relative:
            return []
        if relative:
            key, number = relative[0]
            raise TimerError(
                f"{source}:{number}: {key}= is a relative timer with no fixed "
                "calendar; pass --allow-relative to skip it"
            )
        raise TimerError(f"{source}: timer {name} has no OnCalendar= entry")

    entries = [
        _entry(name, calendar, service, source, number)
        for calendar, number in calendars
    ]
    entries.sort(key=lambda entry: (entry.line, entry.calendar))
    return entries


def load_timer_units(
    paths: Sequence[str],
    *,
    allow_relative: bool = False,
) -> List[TimerEntry]:
    """Load timer units from files and directories.

    Directories are scanned non-recursively for ``*.timer`` files.  Results are
    sorted by timer name and line so reports are stable.
    """
    files: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            try:
                names = sorted(os.listdir(path))
            except OSError as exc:
                raise TimerError(f"{path}: cannot list directory: {exc}") from None
            files.extend(
                os.path.join(path, name)
                for name in names
                if name.endswith(".timer")
                and os.path.isfile(os.path.join(path, name))
            )
        else:
            files.append(path)

    entries: List[TimerEntry] = []
    seen = set()
    for path in files:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise TimerError(f"{path}: cannot read timer unit: {exc}") from None
        entries.extend(
            parse_timer_unit(
                text,
                os.path.basename(path),
                source=path,
                allow_relative=allow_relative,
            )
        )

    entries.sort(key=lambda entry: (entry.name, entry.line, entry.calendar))
    return entries


def _expand_expectation(item: object, where: str) -> List[Tuple[str, str, Optional[str]]]:
    if not isinstance(item, dict):
        raise TimerError(f"{where}: expected an object")
    name = item.get("name")
    service = item.get("service")
    if not isinstance(name, str) or not name.strip():
        raise TimerError(f"{where}: 'name' must be a non-empty string")
    if service is not None and not isinstance(service, str):
        raise TimerError(f"{where}: 'service' must be a string")

    calendars = item.get("calendars")
    calendar = item.get("calendar")
    if calendars is not None and calendar is not None:
        raise TimerError(f"{where}: use either 'calendar' or 'calendars', not both")
    if calendars is not None:
        if not isinstance(calendars, list) or not calendars:
            raise TimerError(f"{where}: 'calendars' must be a non-empty list")
        values = calendars
    elif calendar is not None:
        values = [calendar]
    else:
        raise TimerError(f"{where}: 'calendar' or 'calendars' is required")

    expanded: List[Tuple[str, str, Optional[str]]] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise TimerError(f"{where}.calendar[{index}]: expected a non-empty string")
        expanded.append((name, value, service))
    return expanded


def load_timer_expectations(path: str) -> List[TimerEntry]:
    """Load a JSON timer manifest.

    Accepted shapes: a list of timer objects, or an object with a ``timers``
    list.  Each timer has ``name``, ``service`` (optional), and either
    ``calendar`` or a non-empty ``calendars`` list.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise TimerError(f"{path}: cannot read manifest: {exc}") from None
    except json.JSONDecodeError as exc:
        raise TimerError(f"{path}: invalid JSON: {exc}") from None

    if isinstance(payload, dict):
        raw_entries = payload.get("timers")
    else:
        raw_entries = payload
    if not isinstance(raw_entries, list):
        raise TimerError(f"{path}: expected a list or an object with a 'timers' list")

    entries: List[TimerEntry] = []
    for index, item in enumerate(raw_entries):
        where = f"{path}[{index}]"
        for name, calendar, service in _expand_expectation(item, where):
            entries.append(_entry(name, calendar, service, where, 0))
    return entries


def _first_by_key(entries: Sequence[TimerEntry]) -> Dict[Tuple[str, str, str], TimerEntry]:
    first: Dict[Tuple[str, str, str], TimerEntry] = {}
    for entry in entries:
        first.setdefault(entry.key, entry)
    return first


def compare_timers(
    expected: Sequence[TimerEntry],
    actual: Sequence[TimerEntry],
) -> TimerDriftReport:
    """Compare an expected timer manifest with the live calendar entries.

    Matching is a multiset comparison on ``(name, canonical, service)``.  A
    timer present on both sides with a different calendar or service is
    reported as ``changed`` rather than as missing plus unexpected.
    """
    expected_counts: Dict[Tuple[str, str, str], int] = {}
    actual_counts: Dict[Tuple[str, str, str], int] = {}
    for entry in expected:
        expected_counts[entry.key] = expected_counts.get(entry.key, 0) + 1
    for entry in actual:
        actual_counts[entry.key] = actual_counts.get(entry.key, 0) + 1

    matched = sum(
        min(count, actual_counts.get(key, 0)) for key, count in expected_counts.items()
    )

    expected_first = _first_by_key(expected)
    actual_first = _first_by_key(actual)

    missing: List[TimerEntry] = []
    for key, count in expected_counts.items():
        surplus = count - actual_counts.get(key, 0)
        missing.extend([expected_first[key]] * max(0, surplus))

    unexpected: List[TimerEntry] = []
    for key, count in actual_counts.items():
        surplus = count - expected_counts.get(key, 0)
        unexpected.extend([actual_first[key]] * max(0, surplus))

    expected_by_name: Dict[str, List[TimerEntry]] = {}
    for entry in missing:
        expected_by_name.setdefault(entry.name, []).append(entry)

    changed: List[TimerChange] = []
    remaining_unexpected: List[TimerEntry] = []
    for entry in unexpected:
        candidates = expected_by_name.get(entry.name)
        if candidates:
            changed.append(TimerChange(expected=candidates.pop(0), actual=entry))
        else:
            remaining_unexpected.append(entry)

    remaining_missing = [
        entry for entries in expected_by_name.values() for entry in entries
    ]
    remaining_missing.sort(key=lambda entry: (entry.source, entry.line, entry.name))

    duplicates = [
        actual_first[key] for key, count in actual_counts.items() if count > 1
    ]
    duplicates.sort(key=lambda entry: (entry.source, entry.line, entry.name))

    never_fires: List[TimerEntry] = []
    seen = set()
    for entry in actual:
        if entry.canonical in seen:
            continue
        seen.add(entry.canonical)
        try:
            spec = parse_calendar(entry.calendar)
        except CalendarError:  # pragma: no cover - entries are validated on parse
            continue
        if spec.never_fires():
            never_fires.append(entry)

    return TimerDriftReport(
        matched=matched,
        missing=remaining_missing,
        unexpected=remaining_unexpected,
        changed=changed,
        duplicates=duplicates,
        never_fires=never_fires,
        expected_count=len(expected),
        actual_count=len(actual),
    )
