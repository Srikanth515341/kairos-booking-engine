"""Recurrence expansion (Implementation Plan Phase 11; RFC v1.0 §9.1-§9.3,
§15b; Test Plan TZ-01, TZ-05, TZ-06, TZ-09, TZ-10). `expand_occurrences` is
a pure function — these are unit tests against it directly, with no HTTP
surface to go through yet (Phase 12). The `recurring_series`/`booking.
series` schema tests at the bottom are the DB-facing half of the DoD.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError

from kairos.bookings.models import Booking, RecurringSeries
from kairos.bookings.recurrence import expand_occurrences
from kairos.bookings.serializers import BookingResponseSerializer
from kairos.core.exceptions import PolicyValidationError
from kairos.core.timezones import tzdata_version
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------
# TZ-01 — America/New_York, the fall-back transition date itself as an
# occurrence, not merely dates either side of it.
# --------------------------------------------------------------------


def test_tz_01_america_new_york_fall_back_transition_as_an_occurrence() -> None:
    occurrences = expand_occurrences(
        timezone="America/New_York",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        series_start_date=date(2026, 10, 25),
        occurrence_count=4,
    )

    assert [o.occurrence_date for o in occurrences] == [
        date(2026, 10, 25),
        date(2026, 11, 1),
        date(2026, 11, 8),
        date(2026, 11, 15),
    ]
    # All four render 10:00 local, regardless of which side of the
    # transition they land on.
    for o in occurrences:
        assert o.start.astimezone(ZoneInfo("America/New_York")).time() == time(10, 0)
        assert o.adjustment is None

    # Oct 25 is still EDT (UTC-4); Nov 1 onward is EST (UTC-5) — the
    # transition happens at 2:00 AM, so by the 10:00 occurrence on Nov 1
    # the zone has already changed. A test checking only before/after
    # dates would miss exactly this boundary.
    assert occurrences[0].start.isoformat() == "2026-10-25T14:00:00+00:00"
    assert occurrences[1].start.isoformat() == "2026-11-01T15:00:00+00:00"
    assert occurrences[2].start.isoformat() == "2026-11-08T15:00:00+00:00"
    assert occurrences[3].start.isoformat() == "2026-11-15T15:00:00+00:00"


# --------------------------------------------------------------------
# TZ-05 — nonexistent local time (Europe/Paris spring-forward)
# --------------------------------------------------------------------


def test_tz_05_paris_nonexistent_local_time_shifted_forward_and_disclosed() -> None:
    occurrences = expand_occurrences(
        timezone="Europe/Paris",
        local_start_time=time(2, 30),
        local_end_time=time(3, 0),
        series_start_date=date(2027, 3, 28),
        occurrence_count=1,
    )
    occurrence = occurrences[0]

    assert occurrence.adjustment is not None
    assert occurrence.adjustment.issue == "nonexistent_local_time"
    assert occurrence.adjustment.requested_local == time(2, 30)
    assert occurrence.adjustment.adjusted_local == time(3, 30)
    # 03:30 CEST (UTC+2) == 01:30Z.
    assert occurrence.start.isoformat() == "2027-03-28T01:30:00+00:00"


# --------------------------------------------------------------------
# TZ-06 — ambiguous local time (Europe/Paris fall-back)
# --------------------------------------------------------------------


def test_tz_06_paris_ambiguous_local_time_takes_first_instance() -> None:
    occurrences = expand_occurrences(
        timezone="Europe/Paris",
        local_start_time=time(2, 30),
        local_end_time=time(3, 0),
        series_start_date=date(2027, 10, 31),
        occurrence_count=1,
    )
    occurrence = occurrences[0]

    assert occurrence.adjustment is not None
    assert occurrence.adjustment.issue == "ambiguous_local_time"
    # No shift — same wall-clock time, just a specific (earlier) instant.
    assert occurrence.adjustment.requested_local == time(2, 30)
    assert occurrence.adjustment.adjusted_local == time(2, 30)
    # 02:30 CEST (UTC+2, first/pre-transition instance) == 00:30Z, not
    # 01:30Z (02:30 CET, the second instance — that would be the bug).
    assert occurrence.start.isoformat() == "2027-10-31T00:30:00+00:00"


# --------------------------------------------------------------------
# TZ-09 — Australia/Sydney, opposite transition direction. A sign error
# in offset arithmetic would pass every northern-hemisphere test above
# and only fail here.
# --------------------------------------------------------------------


def test_tz_09_sydney_spring_forward_october() -> None:
    # AEST (UTC+10) -> AEDT (UTC+11) on 2026-10-04.
    occurrences = expand_occurrences(
        timezone="Australia/Sydney",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        series_start_date=date(2026, 9, 27),
        occurrence_count=3,
    )
    assert [o.adjustment for o in occurrences] == [None, None, None]
    assert occurrences[0].start.isoformat() == "2026-09-27T00:00:00+00:00"  # AEST
    assert occurrences[1].start.isoformat() == "2026-10-03T23:00:00+00:00"  # AEDT
    assert occurrences[2].start.isoformat() == "2026-10-10T23:00:00+00:00"  # AEDT


def test_tz_09_sydney_fall_back_april() -> None:
    # AEDT (UTC+11) -> AEST (UTC+10) on 2026-04-05.
    occurrences = expand_occurrences(
        timezone="Australia/Sydney",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        series_start_date=date(2026, 3, 29),
        occurrence_count=3,
    )
    assert [o.adjustment for o in occurrences] == [None, None, None]
    assert occurrences[0].start.isoformat() == "2026-03-28T23:00:00+00:00"  # AEDT
    assert occurrences[1].start.isoformat() == "2026-04-05T00:00:00+00:00"  # AEST
    assert occurrences[2].start.isoformat() == "2026-04-12T00:00:00+00:00"  # AEST


# --------------------------------------------------------------------
# TZ-10 — Asia/Kolkata, no DST at all. Verifies no spurious adjustment
# is applied to a zone that never transitions, spanning dates where
# other zones (Paris, New York) do.
# --------------------------------------------------------------------


def test_tz_10_kolkata_has_no_dst_and_no_spurious_adjustment() -> None:
    occurrences = expand_occurrences(
        timezone="Asia/Kolkata",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        series_start_date=date(2026, 3, 1),
        occurrence_count=6,
    )
    assert [o.adjustment for o in occurrences] == [None] * 6
    offsets = {o.start.astimezone(ZoneInfo("Asia/Kolkata")).utcoffset() for o in occurrences}
    assert len(offsets) == 1  # identical offset throughout — UTC+5:30, always


# --------------------------------------------------------------------
# Bounds (PRD FR14a)
# --------------------------------------------------------------------


def test_occurrence_count_101_raises_validation_error() -> None:
    with pytest.raises(PolicyValidationError) as exc_info:
        expand_occurrences(
            timezone="UTC",
            local_start_time=time(10, 0),
            local_end_time=time(10, 30),
            series_start_date=date(2026, 9, 1),
            occurrence_count=101,
        )
    assert exc_info.value.field == "occurrence_count"


def test_occurrence_count_100_is_valid() -> None:
    occurrences = expand_occurrences(
        timezone="UTC",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        series_start_date=date(2026, 9, 1),
        occurrence_count=100,
    )
    assert len(occurrences) == 100


# --------------------------------------------------------------------
# recurring_series schema (Spec v1.0 §3)
# --------------------------------------------------------------------


def test_recurring_series_stores_series_start_date_and_tzdata_version(
    app_user: AppUser, active_resource: Resource
) -> None:
    series = RecurringSeries.objects.create(
        resource=active_resource,
        created_by=app_user,
        timezone="Europe/Paris",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        weekday=2,
        series_start_date=date(2026, 9, 1),
        occurrence_count=8,
        tzdata_version=tzdata_version(),
        materialized_through=date(2026, 10, 20),
    )

    series.refresh_from_db()
    assert series.series_start_date == date(2026, 9, 1)
    assert series.tzdata_version == tzdata_version()
    assert series.status == RecurringSeries.Status.ACTIVE


def test_recurring_series_rejects_a_fixed_offset_timezone(
    app_user: AppUser, active_resource: Resource
) -> None:
    series = RecurringSeries(
        resource=active_resource,
        created_by=app_user,
        timezone="+01:00",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        weekday=2,
        series_start_date=date(2026, 9, 1),
        occurrence_count=8,
        tzdata_version=tzdata_version(),
        materialized_through=date(2026, 10, 20),
    )
    with pytest.raises(PolicyValidationError):
        series.save()


def test_recurring_series_occurrence_count_101_violates_the_db_check_constraint(
    app_user: AppUser, active_resource: Resource
) -> None:
    # Defense in depth beneath expand_occurrences' own guard (Spec v1.0
    # §3's literal DDL) — a raw SQL or bulk write that bypasses the
    # expansion engine entirely still can't create an out-of-bounds series.
    series = RecurringSeries(
        resource=active_resource,
        created_by=app_user,
        timezone="UTC",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        weekday=2,
        series_start_date=date(2026, 9, 1),
        occurrence_count=101,
        tzdata_version=tzdata_version(),
        materialized_through=date(2026, 10, 20),
    )
    with pytest.raises(IntegrityError):
        series.save()


def test_booking_series_fk_round_trips_through_the_response_serializer(
    app_user: AppUser, active_resource: Resource
) -> None:
    series = RecurringSeries.objects.create(
        resource=active_resource,
        created_by=app_user,
        timezone="UTC",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        weekday=2,
        series_start_date=date(2026, 9, 1),
        occurrence_count=1,
        tzdata_version=tzdata_version(),
        materialized_through=date(2026, 9, 1),
    )
    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        series=series,
        time_range=(
            datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        ),
    )
    booking.refresh_from_db()

    assert booking.series_id == series.id
    body = BookingResponseSerializer(booking).data
    assert body["series_id"] == str(series.id)
