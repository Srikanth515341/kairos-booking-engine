"""Timezone utilities (Implementation Plan Phase 10; RFC v1.0 §9). The one
place every IANA-zone validation and local-to-UTC conversion in this
codebase goes through — Phase 11's recurrence engine builds directly on
`local_to_instant` and the detection helpers here instead of reimplementing
DST handling per call site.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib.metadata import version as _package_version
from zoneinfo import ZoneInfo, available_timezones

from kairos.core.exceptions import PolicyValidationError

# Computed once at import time — available_timezones() walks the tzdata
# package's contents, which don't change while the process is running.
_VALID_ZONES = available_timezones()


def validate_iana_zone(value: str) -> None:
    """Raises PolicyValidationError unless `value` is a real IANA zone
    identifier. A fixed offset such as '+01:00' is rejected (PRD FR8) — an
    offset cannot express when the underlying rules change, which is the
    entire reason this project stores zone identifiers instead of offsets.
    """
    if value not in _VALID_ZONES:
        raise PolicyValidationError(
            "timezone", f"{value!r} is not a known IANA timezone identifier"
        )


def local_to_instant(local_dt: datetime, zone: str, on_date: date) -> datetime:
    """Combines `on_date` with `local_dt`'s wall-clock time and localizes
    the result using the offset in effect on `on_date` specifically — never
    on whatever date `local_dt` itself happens to carry.

    This is the mechanism RFC v1.0 §9.1/§9.2 require and Test Plan TZ-02
    guards: the naive bug is computing an occurrence's instant using the
    offset in effect when the *request* was made (or a series was created)
    rather than the offset in effect on the occurrence's own date. Taking
    `on_date` as a separate, authoritative argument makes that bug
    structurally impossible to reintroduce here by accident — there is no
    "creation date" left in this function's scope to use by mistake.
    """
    naive = datetime.combine(on_date, local_dt.time())
    return naive.replace(tzinfo=ZoneInfo(zone)).astimezone(UTC)


def is_nonexistent_local_time(naive_dt: datetime, zone: str) -> bool:
    """RFC v1.0 §9.3: a spring-forward gap (e.g. 02:30 where the clock
    jumps 02:00 -> 03:00) names no real instant. `zoneinfo` will silently
    produce *some* UTC instant for it anyway, so detection must be
    explicit: round-trip the localized datetime back through UTC and
    compare wall-clock values.
    """
    localized = naive_dt.replace(tzinfo=ZoneInfo(zone))
    round_tripped = localized.astimezone(UTC).astimezone(ZoneInfo(zone))
    return round_tripped.replace(tzinfo=None) != naive_dt


def is_ambiguous_local_time(naive_dt: datetime, zone: str) -> bool:
    """RFC v1.0 §9.3: a fall-back overlap (e.g. 01:30 occurring twice) is
    detected via the `fold` attribute — the identical wall-clock value maps
    to two different UTC instants depending on which side of the
    transition it falls on. A nonexistent time also yields differing
    fold=0/fold=1 instants, so nonexistent is checked and excluded first —
    the two cases are mutually exclusive by construction.
    """
    if is_nonexistent_local_time(naive_dt, zone):
        return False
    zone_info = ZoneInfo(zone)
    before = naive_dt.replace(tzinfo=zone_info, fold=0).astimezone(UTC)
    after = naive_dt.replace(tzinfo=zone_info, fold=1).astimezone(UTC)
    return before != after


def tzdata_version() -> str:
    """The installed `tzdata` package's version, which tracks the IANA
    release identifier it packages (e.g. '2026.3'). Pinned exactly in
    pyproject.toml, not a range — see tests/test_timezones.py for the CI
    form of Test Plan TZ-03 Test A, and kairos.core.apps.CoreConfig.ready()
    for the startup log.
    """
    return _package_version("tzdata")
