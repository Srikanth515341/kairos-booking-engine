"""Rolling materialization and tzdata re-materialization (Implementation
Plan Phase 13; PRD FR13/FR13a-c/FR14c/FR54; RFC v1.0 §9.4, §15b). The
first writes in this codebase NOT initiated by an HTTP request — both
functions below reuse `create_booking`/`edit_booking` (Phase 4/7) exactly
as `confirm_recurring_series` (Phase 12) does, passing
`actor_type=AuditActorType.SYSTEM` so the audit trail attributes each row
to the background job rather than fabricating a human actor.

Both are plain, directly-callable functions — `@shared_task` wrappers at
the bottom are thin, so REC/TZ-style tests can call the logic synchronously
without a running worker or broker, the same "test the mechanism directly"
approach Phase 11 used for `expand_occurrences`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from celery import shared_task
from django.utils import timezone as django_timezone

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries
from kairos.bookings.recurrence import expand_occurrences
from kairos.bookings.services import (
    BookingCreateRequest,
    BookingEditRequest,
    create_booking,
    edit_booking,
)
from kairos.core.constants import MAX_ADVANCE_HORIZON_DAYS
from kairos.core.exceptions import SlotUnavailableError
from kairos.core.models import AuditActorType, SystemCheckRun
from kairos.core.timezones import tzdata_version

logger = logging.getLogger(__name__)


def rolling_materialize_series(*, now: datetime | None = None) -> dict[str, Any]:
    """PRD FR14c: "series longer than the horizon are materialized to the
    horizon and extended by a rolling background job." The horizon is a
    MOVING window (365 days from "now"), so a series whose full span
    originally exceeded it becomes reachable, occurrence by occurrence, as
    calendar time passes.

    No real caller produces a partially-materialized series yet — Phase
    12's confirm rejects a series whose full span exceeds the horizon
    outright (Test Plan REC-06), rather than materializing part of it and
    leaving the rest for this job. This is the same "mechanism proven
    directly, ahead of its real caller" situation Phase 8's actor_type=
    'system' was in before this phase gave it one — see CLAUDE.md's Open
    Questions for which future phase should connect confirm to this.
    """
    now = now or django_timezone.now()
    horizon_cutoff = now + timedelta(days=MAX_ADVANCE_HORIZON_DAYS)
    request_id = str(uuid.uuid4())

    series_extended = 0
    occurrences_created = 0
    conflicts: list[dict[str, Any]] = []

    active_series = RecurringSeries.objects.filter(
        status=RecurringSeries.Status.ACTIVE
    ).select_related("resource", "created_by")

    for series in active_series:
        theoretical_last_date = series.series_start_date + timedelta(
            days=7 * (series.occurrence_count - 1)
        )
        if series.materialized_through >= theoretical_last_date:
            continue  # nothing left to roll — fully materialized already

        occurrences = expand_occurrences(
            timezone=series.timezone,
            local_start_time=series.local_start_time,
            local_end_time=series.local_end_time,
            series_start_date=series.series_start_date,
            occurrence_count=series.occurrence_count,
        )
        to_materialize = [
            o
            for o in occurrences
            if o.occurrence_date > series.materialized_through and o.start <= horizon_cutoff
        ]
        if not to_materialize:
            continue

        series_extended += 1
        new_materialized_through = series.materialized_through
        for occurrence in to_materialize:
            try:
                create_booking(
                    BookingCreateRequest(
                        resource=series.resource,
                        user=series.created_by,
                        start=occurrence.start,
                        end=occurrence.end,
                        request_id=request_id,
                        series=series,
                        actor_type=AuditActorType.SYSTEM,
                    )
                )
                occurrences_created += 1
            except SlotUnavailableError:
                conflicts.append(
                    {
                        "series_id": str(series.id),
                        "occurrence_date": occurrence.occurrence_date.isoformat(),
                        "reason": "slot_unavailable",
                    }
                )
            # Advances past a conflicted occurrence too — matching
            # confirm's own "acknowledged: false, not retried" precedent
            # (Phase 12): a slot taken by someone else isn't retried
            # indefinitely, it's flagged and the series moves on.
            new_materialized_through = occurrence.occurrence_date

        RecurringSeries.objects.filter(id=series.id).update(
            materialized_through=new_materialized_through
        )

    findings: dict[str, Any] = {
        "series_extended": series_extended,
        "occurrences_created": occurrences_created,
        "conflicts": conflicts,
    }
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.SERIES_MATERIALIZATION,
        status=SystemCheckRun.Status.FAIL if conflicts else SystemCheckRun.Status.PASS,
        findings=findings,
    )
    logger.info("rolling_materialization_run", extra={"request_id": request_id, **findings})
    return findings


def rematerialize_stale_series(*, now: datetime | None = None) -> dict[str, Any]:
    """RFC v1.0 §9.4; PRD FR13/FR13a-c/FR54. Finds ACTIVE series whose
    recorded `tzdata_version` differs from what's installed NOW, recomputes
    every still-confirmed, still-future occurrence FROM THE SERIES
    DEFINITION (never a delta applied to the stored value — PRD FR13),
    and writes the recomputed instant wherever it changed.

    A conflicting re-materialization is NEVER silently dropped (PRD
    FR13b): both the series owner and the resource administrator are
    recorded as needing notification (delivery is Phase 18 — this records
    that one is due, the same stand-in Phase 7's `would_enqueue_
    waitlist_check` already established), and the rest of the series
    still re-materializes. A series with any unresolved conflict keeps its
    OLD `tzdata_version` — genuinely still stale — so the NEXT run retries
    exactly the occurrences that didn't yet succeed, rather than either
    forgetting about them or re-touching already-fixed ones.
    """
    now = now or django_timezone.now()
    current_version = tzdata_version()
    request_id = str(uuid.uuid4())

    stale_series = (
        RecurringSeries.objects.filter(status=RecurringSeries.Status.ACTIVE)
        .exclude(tzdata_version=current_version)
        .select_related("resource", "created_by")
    )

    series_checked = 0
    occurrences_updated = 0
    conflicts: list[dict[str, Any]] = []

    for series in stale_series:
        series_checked += 1
        occurrences = expand_occurrences(
            timezone=series.timezone,
            local_start_time=series.local_start_time,
            local_end_time=series.local_end_time,
            series_start_date=series.series_start_date,
            occurrence_count=series.occurrence_count,
        )
        existing_bookings = Booking.objects.filter(
            series=series, status=BookingStatus.CONFIRMED, starts_at__gte=now
        )

        series_had_conflict = False
        for booking in existing_bookings:
            # Matched by NEAREST recomputed instant, not by re-deriving a
            # local date from the (possibly stale) stored one — a rule
            # change can shift the offset enough to make that misleading.
            # Occurrences are 7 days apart and any DST shift is well under
            # 24h, so "closest current occurrence, within 2 days" is
            # unambiguous.
            closest = min(
                occurrences,
                key=lambda o: abs((o.start - booking.time_range.lower).total_seconds()),
            )
            if abs((closest.start - booking.time_range.lower).total_seconds()) > 2 * 24 * 3600:
                continue  # no plausible match for this booking in this series
            if (
                closest.start == booking.time_range.lower
                and closest.end == booking.time_range.upper
            ):
                continue  # unaffected by the rule change

            try:
                edit_booking(
                    BookingEditRequest(
                        booking=booking,
                        start=closest.start,
                        end=closest.end,
                        request_id=request_id,
                        actor_type=AuditActorType.SYSTEM,
                    )
                )
                occurrences_updated += 1
                logger.info(
                    "rematerialization_notification_due",
                    extra={
                        "request_id": request_id,
                        "series_id": str(series.id),
                        "booking_id": str(booking.id),
                        "user_id": str(booking.user_id),
                    },
                )
            except SlotUnavailableError:
                series_had_conflict = True
                conflicts.append(
                    {
                        "series_id": str(series.id),
                        "booking_id": str(booking.id),
                        "occurrence_date": closest.occurrence_date.isoformat(),
                        "notify": {
                            "series_owner": str(series.created_by_id),
                            "resource_administrators": "pending Phase 18 delivery",
                        },
                    }
                )
                logger.warning(
                    "rematerialization_conflict",
                    extra={
                        "request_id": request_id,
                        "series_id": str(series.id),
                        "booking_id": str(booking.id),
                    },
                )

        if not series_had_conflict:
            RecurringSeries.objects.filter(id=series.id).update(tzdata_version=current_version)

    findings: dict[str, Any] = {
        "current_tzdata_version": current_version,
        "series_checked": series_checked,
        "occurrences_updated": occurrences_updated,
        "conflicts": conflicts,
    }
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.TZDATA_REMATERIALIZATION,
        status=SystemCheckRun.Status.FAIL if conflicts else SystemCheckRun.Status.PASS,
        findings=findings,
    )
    logger.info("tzdata_rematerialization_run", extra={"request_id": request_id, **findings})
    return findings


@shared_task(name="kairos.bookings.tasks.rolling_materialize_series_task")
def rolling_materialize_series_task() -> dict[str, Any]:
    return rolling_materialize_series()


@shared_task(name="kairos.bookings.tasks.rematerialize_stale_series_task")
def rematerialize_stale_series_task() -> dict[str, Any]:
    return rematerialize_stale_series()
