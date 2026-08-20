"""Server-side recurrence expansion (Implementation Plan Phase 11; RFC v1.0
§9.1-§9.3, §15b). No API surface yet — `expand_occurrences` is a pure
function consumed by Phase 12's preview/confirm endpoints, deliberately
isolated from conflict-checking and write-path concerns so the DST
correctness engine can be proven on its own terms first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from kairos.core.constants import MAX_SERIES_OCCURRENCES
from kairos.core.exceptions import PolicyValidationError
from kairos.core.timezones import (
    is_ambiguous_local_time,
    is_nonexistent_local_time,
    local_to_instant,
)


@dataclass(frozen=True)
class TimeAdjustment:
    """Spec v1.0 §5.8's `time_adjustments` entry shape (minus
    `occurrence_date`, which the caller already has from the enclosing
    `Occurrence`). Disclosure is mandatory whenever local time doesn't map
    cleanly to one instant — RFC v1.0 §9.3: "the system never guesses
    silently."
    """

    issue: str  # "nonexistent_local_time" | "ambiguous_local_time"
    requested_local: time
    adjusted_local: time
    explanation: str


@dataclass(frozen=True)
class Occurrence:
    occurrence_date: date
    start: datetime  # UTC
    end: datetime  # UTC
    adjustment: TimeAdjustment | None


def _transition_gap(naive_dt: datetime, zone: str) -> timedelta:
    """The size of a spring-forward gap at `naive_dt`, computed by
    comparing the UTC instant `naive_dt` would map to under each side's
    rule (`fold=0` extrapolates the pre-transition offset, `fold=1` the
    post-transition one). For a standard 1-hour DST transition this is
    exactly one hour — computed rather than assumed, since not every
    transition in `zoneinfo`'s data is 1 hour.
    """
    zone_info = ZoneInfo(zone)
    before = naive_dt.replace(tzinfo=zone_info, fold=0).astimezone(UTC)
    after = naive_dt.replace(tzinfo=zone_info, fold=1).astimezone(UTC)
    return before - after


def _materialize_occurrence(
    occurrence_date: date, local_start_time: time, local_end_time: time, zone: str
) -> Occurrence:
    naive_start = datetime.combine(occurrence_date, local_start_time)
    naive_end = datetime.combine(occurrence_date, local_end_time)
    adjustment: TimeAdjustment | None = None

    if is_nonexistent_local_time(naive_start, zone):
        # PRD FR11: shift the whole occurrence forward by the transition
        # gap, preserving its local duration, rather than only moving the
        # start and silently changing how long it lasts.
        gap = _transition_gap(naive_start, zone)
        naive_start += gap
        naive_end += gap
        adjustment = TimeAdjustment(
            issue="nonexistent_local_time",
            requested_local=local_start_time,
            adjusted_local=naive_start.time(),
            explanation=(
                "Local time does not exist due to a DST transition; shifted "
                "forward by the transition gap."
            ),
        )
    elif is_ambiguous_local_time(naive_start, zone):
        # PRD FR12: take the first (pre-transition) instance. Python's
        # default fold=0 — which local_to_instant uses below — already IS
        # that earlier instant, so no shift is needed, only disclosure.
        adjustment = TimeAdjustment(
            issue="ambiguous_local_time",
            requested_local=local_start_time,
            adjusted_local=local_start_time,
            explanation=(
                "Local time occurs twice due to a DST transition; the first "
                "(pre-transition) instance was used."
            ),
        )

    start = local_to_instant(naive_start, zone, naive_start.date())
    end = local_to_instant(naive_end, zone, naive_end.date())
    return Occurrence(occurrence_date, start, end, adjustment)


def expand_occurrences(
    *,
    timezone: str,
    local_start_time: time,
    local_end_time: time,
    series_start_date: date,
    occurrence_count: int,
) -> list[Occurrence]:
    """RFC v1.0 §9.2's expansion mechanism, and the reason Phase 11 exists
    as its own phase: each occurrence is computed independently, in LOCAL
    wall-clock time first (`series_start_date` plus a whole number of
    7-day steps — date arithmetic, never touching a UTC instant), and ONLY
    THEN converted to UTC using the zone rules in effect on that specific
    occurrence's own date. An occurrence never inherits its instant by
    adding a fixed 7x24h duration to the previous occurrence's UTC value —
    that arithmetic is correct until the series crosses a DST boundary,
    after which every subsequent occurrence silently drifts by an hour
    (RFC v1.0 §9.1), with no exception and no failed test unless one
    specifically checks for it.
    """
    if not (1 <= occurrence_count <= MAX_SERIES_OCCURRENCES):
        raise PolicyValidationError(
            "occurrence_count", f"must be between 1 and {MAX_SERIES_OCCURRENCES}"
        )

    return [
        _materialize_occurrence(
            series_start_date + timedelta(days=7 * i), local_start_time, local_end_time, timezone
        )
        for i in range(occurrence_count)
    ]
