"""Rolling materialization and tzdata re-materialization (Implementation
Plan Phase 13; PRD FR13/FR13a-c/FR14c/FR54; RFC v1.0 §9.4; Test Plan
TZ-07, TZ-08). Both jobs are tested by calling them directly — no Celery
worker or broker involved, the same "test the mechanism, not the
transport" approach Phase 11 used for `expand_occurrences`.

No real caller produces a partially-materialized series yet (Phase 12's
confirm rejects a series exceeding the horizon outright — Test Plan
REC-06), so rolling-materialization tests construct that state directly
via the ORM, matching this project's established "prove the mechanism
ahead of its real caller" pattern (Phase 8's actor_type='system' before
Phase 13 gave it one).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from django.db import connection
from django.utils import timezone

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries
from kairos.bookings.tasks import rematerialize_stale_series, rolling_materialize_series
from kairos.core.constants import MAX_ADVANCE_HORIZON_DAYS
from kairos.core.models import AuditLog, SystemCheckRun
from kairos.core.timezones import tzdata_version
from kairos.identity.models import AppUser, ResourceAdmin
from kairos.resources.models import Resource

pytestmark = pytest.mark.django_db


def _series(
    *,
    resource: Resource,
    created_by: AppUser,
    series_start_date: date,
    occurrence_count: int,
    materialized_through: date,
    tzdata_version_value: str,
    timezone_name: str = "America/New_York",
    local_start_time: time = time(10, 0),
    local_end_time: time = time(10, 30),
) -> RecurringSeries:
    return RecurringSeries.objects.create(
        resource=resource,
        created_by=created_by,
        timezone=timezone_name,
        local_start_time=local_start_time,
        local_end_time=local_end_time,
        weekday=0,
        series_start_date=series_start_date,
        occurrence_count=occurrence_count,
        tzdata_version=tzdata_version_value,
        materialized_through=materialized_through,
    )


def _booking(
    *, resource: Resource, user: AppUser, series: RecurringSeries, start: datetime, end: datetime
) -> Booking:
    return Booking.objects.create(
        resource=resource, user=user, series=series, time_range=(start, end)
    )


# --------------------------------------------------------------------
# Rolling materialization (PRD FR14c)
# --------------------------------------------------------------------


def test_rolling_materialization_extends_a_series_past_its_original_horizon(
    app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    # A 20-occurrence weekly series spans 19*7 = 133 days — comfortably
    # inside 365 on its own, but materialized_through is set to only the
    # FIRST occurrence, simulating a series a future confirm revision left
    # partially materialized (see module docstring: no real caller does
    # this yet).
    start_date = (now + timedelta(days=7)).date()
    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=start_date,
        occurrence_count=20,
        materialized_through=start_date,
        tzdata_version_value=tzdata_version(),
    )
    first_start = datetime.combine(start_date, time(10, 0), tzinfo=UTC)
    _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=first_start,
        end=first_start + timedelta(minutes=30),
    )
    assert Booking.objects.filter(series=series).count() == 1

    findings = rolling_materialize_series(now=now)

    assert findings["series_extended"] == 1
    assert findings["occurrences_created"] == 19  # occurrences 2 through 20
    assert findings["conflicts"] == []

    series.refresh_from_db()
    assert series.materialized_through == start_date + timedelta(days=7 * 19)
    assert Booking.objects.filter(series=series).count() == 20

    run = SystemCheckRun.objects.filter(
        check_name=SystemCheckRun.CheckName.SERIES_MATERIALIZATION
    ).latest("run_at")
    assert run.status == SystemCheckRun.Status.PASS
    assert run.findings["occurrences_created"] == 19


def test_rolling_materialization_respects_the_365_day_horizon(
    app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    # 100 occurrences span 693 days — only what falls within the horizon
    # should be created THIS run, not all of them.
    start_date = (now - timedelta(days=300)).date()
    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=start_date,
        occurrence_count=100,
        materialized_through=start_date,
        tzdata_version_value=tzdata_version(),
    )
    first_start = datetime.combine(start_date, time(10, 0), tzinfo=UTC)
    _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=first_start,
        end=first_start + timedelta(minutes=30),
    )

    findings = rolling_materialize_series(now=now)

    series.refresh_from_db()
    theoretical_last = start_date + timedelta(days=7 * 99)
    horizon_cutoff_date = (now + timedelta(days=MAX_ADVANCE_HORIZON_DAYS)).date()
    # Materialized up to the horizon, NOT all the way to the series' own
    # theoretical end — there is more left to roll on a future run.
    assert series.materialized_through < theoretical_last
    assert series.materialized_through <= horizon_cutoff_date
    assert findings["occurrences_created"] > 0
    assert Booking.objects.filter(series=series).count() == findings["occurrences_created"] + 1


def test_rolling_materialization_is_a_noop_for_a_fully_materialized_series(
    app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    start_date = (now + timedelta(days=7)).date()
    last_date = start_date + timedelta(days=7 * 3)
    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=start_date,
        occurrence_count=4,
        materialized_through=last_date,  # already fully materialized
        tzdata_version_value=tzdata_version(),
    )

    findings = rolling_materialize_series(now=now)

    assert findings["series_extended"] == 0
    assert findings["occurrences_created"] == 0
    series.refresh_from_db()
    assert series.materialized_through == last_date


# --------------------------------------------------------------------
# TZ-07 — tzdata re-materialization (PRD FR13)
# --------------------------------------------------------------------


def test_tz_07_stale_occurrence_is_recomputed_from_the_series_definition(
    app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    occurrence_date = date(2026, 11, 8)  # EST in America/New_York
    correct_instant = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)  # 10:00 local, EST (UTC-5)
    stale_instant = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)  # what a wrong EDT (-4) offset gives

    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=occurrence_date,
        occurrence_count=1,
        materialized_through=occurrence_date,
        tzdata_version_value="2020.1",  # stale — differs from what's installed
    )
    booking = _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=stale_instant,
        end=stale_instant + timedelta(minutes=30),
    )

    findings = rematerialize_stale_series(now=now)

    assert findings["series_checked"] == 1
    assert findings["occurrences_updated"] == 1
    assert findings["conflicts"] == []
    assert findings["current_tzdata_version"] == tzdata_version()

    booking.refresh_from_db()
    # Recomputed FROM THE DEFINITION, not delta-adjusted: the stored
    # instant changed to the genuinely correct one.
    assert booking.time_range.lower == correct_instant
    # Local wall-clock preserved: still 10:00 America/New_York.
    from zoneinfo import ZoneInfo

    assert booking.time_range.lower.astimezone(ZoneInfo("America/New_York")).time() == time(10, 0)

    series.refresh_from_db()
    assert series.tzdata_version == tzdata_version()

    run = SystemCheckRun.objects.filter(
        check_name=SystemCheckRun.CheckName.TZDATA_REMATERIALIZATION
    ).latest("run_at")
    assert run.status == SystemCheckRun.Status.PASS
    assert run.findings["occurrences_updated"] == 1

    # It's a booking write — the exclusion constraint's audit trigger
    # fired on it like any other UPDATE (PRD FR13a).
    assert AuditLog.objects.filter(
        entity_type="booking", entity_id=booking.id, action="update"
    ).exists()


def test_tz_07_series_unaffected_by_any_rule_change_is_still_marked_current(
    app_user: AppUser, active_resource: Resource
) -> None:
    """A series recorded under a stale version whose occurrences happen
    not to have changed still gets its tzdata_version bumped — "checked
    against current" is the claim, not "something changed."
    """
    now = timezone.now()
    occurrence_date = (now + timedelta(days=30)).date()
    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=occurrence_date,
        occurrence_count=1,
        materialized_through=occurrence_date,
        tzdata_version_value="2020.1",
        timezone_name="UTC",  # no DST at all — nothing could have changed
    )
    correct_start = datetime.combine(occurrence_date, time(10, 0), tzinfo=UTC)
    _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=correct_start,
        end=correct_start + timedelta(minutes=30),
    )

    findings = rematerialize_stale_series(now=now)

    assert findings["occurrences_updated"] == 0
    series.refresh_from_db()
    assert series.tzdata_version == tzdata_version()


# --------------------------------------------------------------------
# TZ-08 — re-materialization that conflicts (PRD FR13b) ★
# --------------------------------------------------------------------


def test_tz_08_conflicting_rematerialization_is_flagged_not_dropped(
    app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    occurrence_date = date(2026, 11, 8)
    correct_instant = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    stale_instant = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)

    admin = AppUser.objects.create(email="tz08-admin@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=occurrence_date,
        occurrence_count=1,
        materialized_through=occurrence_date,
        tzdata_version_value="2020.1",
    )
    stale_booking = _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=stale_instant,
        end=stale_instant + timedelta(minutes=30),
    )
    # Between the (simulated) original materialization and the rule
    # change, another user booked the range O's re-materialized instant
    # requires.
    other_user = AppUser.objects.create(email="tz08-other@example.com", display_name="Other")
    Booking.objects.create(
        resource=active_resource,
        user=other_user,
        time_range=(correct_instant, correct_instant + timedelta(minutes=30)),
    )

    findings = rematerialize_stale_series(now=now)

    assert findings["occurrences_updated"] == 0
    assert len(findings["conflicts"]) == 1
    conflict = findings["conflicts"][0]
    assert conflict["series_id"] == str(series.id)
    assert conflict["booking_id"] == str(stale_booking.id)
    assert conflict["notify"]["series_owner"] == str(app_user.id)

    # NEVER silently dropped: the occurrence still exists, at its old
    # (still-wrong, but not lost) instant.
    stale_booking.refresh_from_db()
    assert stale_booking.time_range.lower == stale_instant
    assert stale_booking.status == BookingStatus.CONFIRMED

    # This series stays genuinely stale — not marked current — so a
    # future run retries exactly this occurrence.
    series.refresh_from_db()
    assert series.tzdata_version == "2020.1"

    run = SystemCheckRun.objects.filter(
        check_name=SystemCheckRun.CheckName.TZDATA_REMATERIALIZATION
    ).latest("run_at")
    assert run.status == SystemCheckRun.Status.FAIL
    assert len(run.findings["conflicts"]) == 1


def test_tz_08_remaining_occurrences_still_succeed_when_one_conflicts(
    app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    conflict_date = date(2026, 11, 8)
    conflict_stale = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    conflict_correct = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    clean_date = date(2026, 11, 15)
    clean_stale = datetime(2026, 11, 15, 16, 0, tzinfo=UTC)

    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=conflict_date,
        occurrence_count=2,
        materialized_through=clean_date,
        tzdata_version_value="2020.1",
    )
    _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=conflict_stale,
        end=conflict_stale + timedelta(minutes=30),
    )
    clean_booking = _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=clean_stale,
        end=clean_stale + timedelta(minutes=30),
    )
    other_user = AppUser.objects.create(email="tz08b-other@example.com", display_name="Other")
    Booking.objects.create(
        resource=active_resource,
        user=other_user,
        time_range=(conflict_correct, conflict_correct + timedelta(minutes=30)),
    )

    findings = rematerialize_stale_series(now=now)

    assert findings["occurrences_updated"] == 1
    assert len(findings["conflicts"]) == 1

    clean_booking.refresh_from_db()
    assert clean_booking.time_range.lower == datetime(2026, 11, 15, 15, 0, tzinfo=UTC)


# --------------------------------------------------------------------
# actor_type='system' — Phase 13's explicit instruction. Same spy style
# as Phase 7/8/9's session-settings checks: read current_setting() on the
# SAME connection immediately before the real write, not after the fact.
# --------------------------------------------------------------------


def test_rolling_materialization_writes_with_actor_type_system(
    monkeypatch: pytest.MonkeyPatch, app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    start_date = (now + timedelta(days=7)).date()
    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=start_date,
        occurrence_count=2,
        materialized_through=start_date,
        tzdata_version_value=tzdata_version(),
    )
    first_start = datetime.combine(start_date, time(10, 0), tzinfo=UTC)
    _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=first_start,
        end=first_start + timedelta(minutes=30),
    )

    observed: dict[str, str] = {}
    original_create = Booking.objects.create

    def _spy_create(*args: object, **kwargs: object) -> Booking:
        with connection.cursor() as cur:
            cur.execute("SELECT current_setting('app.actor_type', true)")
            observed["actor_type"] = cur.fetchone()[0]  # type: ignore[index]
            cur.execute("SELECT current_setting('app.actor_id', true)")
            observed["actor_id"] = cur.fetchone()[0]  # type: ignore[index]
        return original_create(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(Booking.objects, "create", _spy_create)

    rolling_materialize_series(now=now)

    assert observed["actor_type"] == "system"
    # Empty string here — the session-settings convention that becomes
    # SQL NULL via the trigger's NULLIF(current_setting(...), '') — proven
    # end to end below via the persisted row, not just the session var.
    assert observed["actor_id"] == ""

    new_booking = Booking.objects.get(series=series, starts_at__gt=first_start)
    audit_row = AuditLog.objects.get(entity_type="booking", entity_id=new_booking.id)
    assert audit_row.actor_type == "system"
    assert audit_row.actor_id is None


def test_rematerialization_writes_with_actor_type_system(
    monkeypatch: pytest.MonkeyPatch, app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    occurrence_date = date(2026, 11, 8)
    stale_instant = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    series = _series(
        resource=active_resource,
        created_by=app_user,
        series_start_date=occurrence_date,
        occurrence_count=1,
        materialized_through=occurrence_date,
        tzdata_version_value="2020.1",
    )
    booking = _booking(
        resource=active_resource,
        user=app_user,
        series=series,
        start=stale_instant,
        end=stale_instant + timedelta(minutes=30),
    )

    observed: dict[str, str] = {}
    # Spy on the QuerySet.update path edit_booking actually calls —
    # patched at the manager level via a wrapper QuerySet method is
    # awkward, so instead spy at the connection level around the one
    # edit_booking call this test triggers.
    original_filter = Booking.objects.filter

    def _spy_filter(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        qs = original_filter(*args, **kwargs)
        if kwargs.get("id") == booking.id:
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('app.actor_type', true)")
                observed["actor_type"] = cur.fetchone()[0]  # type: ignore[index]
                cur.execute("SELECT current_setting('app.actor_id', true)")
                observed["actor_id"] = cur.fetchone()[0]  # type: ignore[index]
        return qs

    monkeypatch.setattr(Booking.objects, "filter", _spy_filter)

    rematerialize_stale_series(now=now)

    assert observed["actor_type"] == "system"
    assert observed["actor_id"] == ""

    audit_row = AuditLog.objects.filter(
        entity_type="booking", entity_id=booking.id, action="update"
    ).latest("occurred_at")
    assert audit_row.actor_type == "system"
    assert audit_row.actor_id is None
