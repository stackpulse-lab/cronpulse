"""Crontab manifest parsing and drift detection.

A "manifest" is the set of scheduled commands that a host is supposed to run.
The live crontab is the source of truth at runtime; the manifest is what the
repository or the runbook says should be there.  Comparing the two catches
rules that were added by hand, deleted during a migration, or silently
rescheduled -- the kind of drift that only shows up when a backup did not run.

Only the classic five-field Vixie cron format is parsed.  Environment lines
(``SHELL=``, ``CRON_TZ=``, ...), comments and blank lines are ignored.  System
crontabs (``/etc/crontab``, ``/etc/cron.d/*``) carry a user field, selected
with the ``system=True`` flag.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .expr import CronError, CronExpr, parse

__all__ = [
    "ManifestError",
    "ManifestEntry",
    "ScheduleChange",
    "DriftReport",
    "canonical_schedule",
    "parse_crontab",
    "load_expectations",
    "compare_manifests",
]

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")


class ManifestError(ValueError):
    """Raised when a crontab or expectation file cannot be read."""


@dataclass(frozen=True)
class ManifestEntry:
    """One scheduled command.

    ``schedule`` keeps the schedule as written (macro expanded only for
    readability), while ``canonical`` is the semantic form used for
    comparison: field values are expanded, ``@daily`` and ``0 0 * * *`` are
    the same rule, and the day-field restriction flags are preserved because
    they decide the classic OR rule.
    """

    schedule: str
    command: str
    canonical: str
    user: Optional[str] = None
    source: str = ""
    line: int = 0

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.canonical, self.command, self.user or "")

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "schedule": self.schedule,
            "canonical": self.canonical,
            "command": self.command,
            "source": self.source,
            "line": self.line,
        }
        if self.user is not None:
            payload["user"] = self.user
        return payload


@dataclass(frozen=True)
class ScheduleChange:
    """Same command and user, different schedule."""

    expected: ManifestEntry
    actual: ManifestEntry


@dataclass
class DriftReport:
    """Difference between an expectation file and a live crontab."""

    matched: int
    missing: List[ManifestEntry]
    unexpected: List[ManifestEntry]
    changed: List[ScheduleChange]
    duplicates: List[ManifestEntry]
    never_fires: List[ManifestEntry]
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
                    "command": change.actual.command,
                    "user": change.actual.user,
                    "expected_schedule": change.expected.schedule,
                    "actual_schedule": change.actual.schedule,
                    "source": change.actual.source,
                    "line": change.actual.line,
                }
                for change in self.changed
            ],
            "duplicates": [entry.as_dict() for entry in self.duplicates],
            "never_fires": [entry.as_dict() for entry in self.never_fires],
        }


def canonical_schedule(expr: CronExpr) -> str:
    """Return a semantic, comparable form of a parsed schedule."""

    def part(
        values: Iterable[int], low: int, high: int, restricted: bool = True
    ) -> str:
        frozen = frozenset(values)
        if not restricted and frozen == frozenset(range(low, high + 1)):
            return "*"
        return ",".join(str(value) for value in sorted(frozen))

    return " ".join(
        (
            part(expr.minute, 0, 59, False),
            part(expr.hour, 0, 23, False),
            part(expr.day_of_month, 1, 31, expr.dom_restricted),
            part(expr.month, 1, 12, False),
            part(expr.day_of_week, 0, 6, expr.dow_restricted),
        )
    )


def _normalise_command(command: str) -> str:
    return " ".join(command.split())


def _entry(
    schedule_text: str,
    command: str,
    user: Optional[str],
    source: str,
    line: int,
) -> ManifestEntry:
    try:
        expr = parse(schedule_text)
    except CronError as exc:
        raise ManifestError(f"{source}:{line}: invalid schedule: {exc}") from None
    return ManifestEntry(
        schedule=expr.expression,
        command=_normalise_command(command),
        canonical=canonical_schedule(expr),
        user=user,
        source=source,
        line=line,
    )


def parse_crontab(text: str, *, system: bool = False, source: str = "<crontab>") -> List[ManifestEntry]:
    """Parse crontab text into manifest entries.

    ``system=True`` expects the user field used by ``/etc/crontab`` and
    ``/etc/cron.d`` files.  Raises :class:`ManifestError` with the line number
    for anything that cannot be parsed, instead of skipping it silently.
    """
    entries: List[ManifestEntry] = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if _ENV_ASSIGNMENT.match(line):
            continue

        tokens = line.split()
        if tokens[0].startswith("@"):
            schedule = tokens[0]
            rest = tokens[1:]
        else:
            if len(tokens) < 5:
                raise ManifestError(
                    f"{source}:{number}: expected 5 schedule fields, got {len(tokens)}"
                )
            schedule = " ".join(tokens[:5])
            rest = tokens[5:]

        user: Optional[str] = None
        if system:
            if not rest:
                raise ManifestError(
                    f"{source}:{number}: system crontab entry is missing user and command"
                )
            user = rest[0]
            rest = rest[1:]

        if not rest:
            raise ManifestError(f"{source}:{number}: entry has no command")

        entries.append(
            _entry(schedule, " ".join(rest), user, source, number)
        )

    return entries


def load_expectations(path: str) -> List[ManifestEntry]:
    """Load an expectation file.

    Accepted shapes: a JSON list of ``{"schedule": ..., "command": ...,
    "user": ...}`` objects, or an object with an ``entries`` list.  ``user`` is
    optional and only needed for system crontabs.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise ManifestError(f"{path}: cannot read expectations: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path}: invalid JSON: {exc}") from None

    if isinstance(payload, dict):
        raw_entries = payload.get("entries")
    else:
        raw_entries = payload
    if not isinstance(raw_entries, list):
        raise ManifestError(f"{path}: expected a list or an object with an 'entries' list")

    entries: List[ManifestEntry] = []
    for index, item in enumerate(raw_entries):
        where = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where}: expected an object")
        schedule = item.get("schedule")
        command = item.get("command")
        user = item.get("user")
        if not isinstance(schedule, str) or not schedule.strip():
            raise ManifestError(f"{where}: 'schedule' must be a non-empty string")
        if not isinstance(command, str) or not command.strip():
            raise ManifestError(f"{where}: 'command' must be a non-empty string")
        if user is not None and not isinstance(user, str):
            raise ManifestError(f"{where}: 'user' must be a string")
        entries.append(_entry(schedule, command, user, where, 0))
    return entries


def _first_by_key(entries: Sequence[ManifestEntry]) -> Dict[Tuple[str, str, str], ManifestEntry]:
    first: Dict[Tuple[str, str, str], ManifestEntry] = {}
    for entry in entries:
        first.setdefault(entry.key, entry)
    return first


def compare_manifests(
    expected: Sequence[ManifestEntry],
    actual: Sequence[ManifestEntry],
) -> DriftReport:
    """Compare expected entries with the live crontab.

    Matching is a multiset comparison on ``(schedule, command, user)``.  A
    command present on both sides with a different schedule is reported as
    ``changed`` rather than as one missing plus one unexpected entry.
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

    missing: List[ManifestEntry] = []
    for key, count in expected_counts.items():
        surplus = count - actual_counts.get(key, 0)
        missing.extend([expected_first[key]] * max(0, surplus))

    unexpected: List[ManifestEntry] = []
    for key, count in actual_counts.items():
        surplus = count - expected_counts.get(key, 0)
        unexpected.extend([actual_first[key]] * max(0, surplus))

    # Pair leftovers that share command+user but differ in schedule.
    expected_by_command: Dict[Tuple[str, str], List[ManifestEntry]] = {}
    for entry in missing:
        expected_by_command.setdefault((entry.command, entry.user or ""), []).append(entry)

    changed: List[ScheduleChange] = []
    remaining_unexpected: List[ManifestEntry] = []
    for entry in unexpected:
        candidates = expected_by_command.get((entry.command, entry.user or ""))
        if candidates:
            changed.append(ScheduleChange(expected=candidates.pop(0), actual=entry))
        else:
            remaining_unexpected.append(entry)

    remaining_missing = [
        entry for entries in expected_by_command.values() for entry in entries
    ]
    remaining_missing.sort(key=lambda entry: (entry.source, entry.line, entry.command))

    duplicates = [
        actual_first[key] for key, count in actual_counts.items() if count > 1
    ]
    duplicates.sort(key=lambda entry: (entry.source, entry.line, entry.command))

    never_fires = []
    seen = set()
    for entry in actual:
        if entry.canonical in seen:
            continue
        seen.add(entry.canonical)
        try:
            expr = parse(entry.schedule)
        except CronError:  # pragma: no cover - entries are validated on parse
            continue
        if expr.never_fires():
            never_fires.append(entry)

    return DriftReport(
        matched=matched,
        missing=remaining_missing,
        unexpected=remaining_unexpected,
        changed=changed,
        duplicates=duplicates,
        never_fires=never_fires,
        expected_count=len(expected),
        actual_count=len(actual),
    )
