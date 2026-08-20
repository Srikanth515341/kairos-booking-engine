"""Recurring series preview/confirm orchestration (Implementation Plan
Phase 12; RFC v1.0 §5d; Spec v1.0 §5.8, §5.9). Builds on Phase 11's pure
`kairos.bookings.recurrence.expand_occurrences` — this module is where
that computation meets the write path, conflict-checking, the preview
token, and PRD FR33's explicit-acknowledgment gate. `recurrence.py` itself
stays pure and untouched by this phase.
"""

from __future__ import annotations

import time as time_module
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import jwt
from django.conf import settings
from django.utils import timezone as django_timezone

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries
from kairos.bookings.recurrence import Occurrence, expand_occurrences
from kairos.bookings.services import BookingCreateRequest, create_booking
from kairos.core.constants import MAX_ADVANCE_HORIZON_DAYS, PREVIEW_TOKEN_TTL_SECONDS
from kairos.core.exceptions import PolicyValidationError, PreviewExpiredError, SlotUnavailableError
from kairos.core.timezones import tzdata_version
from kairos.identity.models import AppUser
from kairos.resources.models import Resource


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _occurrence_conflicts(resource: Resource, occurrences: list[Occurrence]) -> frozenset[date]:
    """RFC v1.0 §5d step 4: "each expanded occurrence is checked against
    current availability in a read-only pass" — advisory, exactly like
    ResourceAvailabilityView (Phase 6), never the authoritative decision.
    The authoritative decision is still, and only ever, the INSERT that
    `no_overlapping_bookings` evaluates at confirm time.
    """
    conflicting: set[date] = set()
    for occurrence in occurrences:
        overlap = Booking.objects.filter(
            resource=resource,
            status__in=[BookingStatus.CONFIRMED, BookingStatus.HELD],
            time_range__overlap=(occurrence.start, occurrence.end),
        ).exists()
        if overlap:
            conflicting.add(occurrence.occurrence_date)
    return frozenset(conflicting)


@dataclass(frozen=True)
class PreviewResult:
    token: str
    resource_id: uuid.UUID
    tzdata_version: str
    would_create: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    time_adjustments: list[dict[str, Any]]
    conflict_dates: frozenset[date]
    adjustment_dates: frozenset[date]


def preview_recurring_series(
    *,
    user: AppUser,
    resource: Resource,
    timezone: str,
    local_start_time: time,
    local_end_time: time,
    weekday: int,
    series_start_date: date,
    occurrence_count: int,
) -> PreviewResult:
    """Spec v1.0 §5.8. Commits nothing — no `RecurringSeries` row, no
    `booking` row, not even a preview-tracking row of its own. Everything
    confirm (Spec v1.0 §5.9) needs to reproduce this exact computation
    travels inside the signed `preview_token` instead — see
    `_issue_preview_token`/`decode_preview_token`.
    """
    occurrences = expand_occurrences(  # raises PolicyValidationError for occurrence_count
        timezone=timezone,
        local_start_time=local_start_time,
        local_end_time=local_end_time,
        series_start_date=series_start_date,
        occurrence_count=occurrence_count,
    )

    last_occurrence = occurrences[-1]
    horizon_cutoff = django_timezone.now() + timedelta(days=MAX_ADVANCE_HORIZON_DAYS)
    if last_occurrence.start > horizon_cutoff:
        raise PolicyValidationError(
            "occurrence_count",
            f"series extends beyond the {MAX_ADVANCE_HORIZON_DAYS}-day advance horizon",
        )

    conflict_dates = _occurrence_conflicts(resource, occurrences)
    adjustment_dates = frozenset(o.occurrence_date for o in occurrences if o.adjustment is not None)

    would_create = []
    conflicts = []
    time_adjustments = []
    for o in occurrences:
        if o.occurrence_date in conflict_dates:
            conflicts.append(
                {
                    "occurrence_date": o.occurrence_date.isoformat(),
                    "start": _iso(o.start),
                    "end": _iso(o.end),
                    "reason": "slot_unavailable",
                }
            )
        else:
            would_create.append(
                {
                    "occurrence_date": o.occurrence_date.isoformat(),
                    "start": _iso(o.start),
                    "end": _iso(o.end),
                }
            )
        if o.adjustment is not None:
            time_adjustments.append(
                {
                    "occurrence_date": o.occurrence_date.isoformat(),
                    "issue": o.adjustment.issue,
                    "requested_local": o.adjustment.requested_local.isoformat(),
                    "adjusted_local": o.adjustment.adjusted_local.isoformat(),
                    "explanation": o.adjustment.explanation,
                }
            )

    token = _issue_preview_token(
        user=user,
        resource=resource,
        timezone=timezone,
        local_start_time=local_start_time,
        local_end_time=local_end_time,
        weekday=weekday,
        series_start_date=series_start_date,
        occurrence_count=occurrence_count,
        conflict_dates=conflict_dates,
        adjustment_dates=adjustment_dates,
    )

    return PreviewResult(
        token=token,
        resource_id=resource.id,
        tzdata_version=tzdata_version(),
        would_create=would_create,
        conflicts=conflicts,
        time_adjustments=time_adjustments,
        conflict_dates=conflict_dates,
        adjustment_dates=adjustment_dates,
    )


def _issue_preview_token(
    *,
    user: AppUser,
    resource: Resource,
    timezone: str,
    local_start_time: time,
    local_end_time: time,
    weekday: int,
    series_start_date: date,
    occurrence_count: int,
    conflict_dates: frozenset[date],
    adjustment_dates: frozenset[date],
) -> str:
    """HS256, signed with the SAME `KAIROS_SESSION_SIGNING_KEY` the
    session token already uses (Phase 9) — another short-lived, internal,
    self-issued-and-self-verified token, the identical pattern, not a
    reason to mint a third signing key. The whole series definition plus
    the computed conflict/adjustment date sets travels inside the token
    itself, which is what lets confirm re-run the EXACT SAME
    `expand_occurrences` call with the EXACT SAME inputs (REC-07) without
    needing a server-side preview table — "commits nothing" means nothing,
    not even a row that merely records the preview happened.
    """
    now = int(time_module.time())
    payload = {
        "user_id": str(user.id),
        "resource_id": str(resource.id),
        "timezone": timezone,
        "local_start_time": local_start_time.isoformat(),
        "local_end_time": local_end_time.isoformat(),
        "weekday": weekday,
        "series_start_date": series_start_date.isoformat(),
        "occurrence_count": occurrence_count,
        "conflict_dates": sorted(d.isoformat() for d in conflict_dates),
        "adjustment_dates": sorted(d.isoformat() for d in adjustment_dates),
        "iat": now,
        "exp": now + PREVIEW_TOKEN_TTL_SECONDS,
    }
    return jwt.encode(payload, settings.KAIROS_SESSION_SIGNING_KEY, algorithm="HS256")


@dataclass(frozen=True)
class DecodedPreview:
    resource_id: uuid.UUID
    timezone: str
    local_start_time: time
    local_end_time: time
    weekday: int
    series_start_date: date
    occurrence_count: int
    conflict_dates: frozenset[date]
    adjustment_dates: frozenset[date]


def decode_preview_token(token: str, user: AppUser) -> DecodedPreview:
    """Spec v1.0 §5.9 / Test Plan REC-04. Expired, tampered, malformed, or
    issued to a different user all collapse to the same `PreviewExpiredError`
    (see that exception's docstring) — Spec defines only one code for a
    bad token, and this endpoint has no reason to tell an attacker which
    specific way a token is invalid.
    """
    try:
        claims = jwt.decode(token, settings.KAIROS_SESSION_SIGNING_KEY, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise PreviewExpiredError from exc

    if claims.get("user_id") != str(user.id):
        raise PreviewExpiredError

    try:
        return DecodedPreview(
            resource_id=uuid.UUID(claims["resource_id"]),
            timezone=claims["timezone"],
            local_start_time=time.fromisoformat(claims["local_start_time"]),
            local_end_time=time.fromisoformat(claims["local_end_time"]),
            weekday=int(claims["weekday"]),
            series_start_date=date.fromisoformat(claims["series_start_date"]),
            occurrence_count=int(claims["occurrence_count"]),
            conflict_dates=frozenset(date.fromisoformat(d) for d in claims["conflict_dates"]),
            adjustment_dates=frozenset(date.fromisoformat(d) for d in claims["adjustment_dates"]),
        )
    except (KeyError, ValueError) as exc:
        raise PreviewExpiredError from exc


@dataclass(frozen=True)
class ConfirmOutcome:
    series_id: uuid.UUID
    created: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]


def confirm_recurring_series(
    *, user: AppUser, decoded: DecodedPreview, request_id: str
) -> ConfirmOutcome:
    """Spec v1.0 §5.9; RFC v1.0 §5d steps 6-8. Re-expands via the IDENTICAL
    `expand_occurrences` call preview used (REC-07's guarantee, by
    construction rather than by two implementations happening to agree),
    then attempts ONLY the occurrences the preview did not already know to
    conflict — those are reported directly as `acknowledged: true`
    conflicts, with no INSERT attempt at all, matching the acknowledgment
    the user just gave: they agreed these will not be created. Each
    remaining occurrence is attempted through the ordinary `create_booking`
    path — its own transaction, its own session settings — so an
    occurrence that conflicts here despite a clean preview (REC-03: taken
    by someone else between preview and confirm) is reported as
    `acknowledged: false`, distinguishable from one the user already knew
    about.
    """
    resource = Resource.objects.get(id=decoded.resource_id)
    occurrences = expand_occurrences(
        timezone=decoded.timezone,
        local_start_time=decoded.local_start_time,
        local_end_time=decoded.local_end_time,
        series_start_date=decoded.series_start_date,
        occurrence_count=decoded.occurrence_count,
    )

    series = RecurringSeries.objects.create(
        resource=resource,
        created_by=user,
        timezone=decoded.timezone,
        local_start_time=decoded.local_start_time,
        local_end_time=decoded.local_end_time,
        weekday=decoded.weekday,
        series_start_date=decoded.series_start_date,
        occurrence_count=decoded.occurrence_count,
        tzdata_version=tzdata_version(),
        materialized_through=occurrences[-1].occurrence_date,
    )

    created: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for o in occurrences:
        if o.occurrence_date in decoded.conflict_dates:
            conflicts.append(
                {
                    "occurrence_date": o.occurrence_date.isoformat(),
                    "reason": "slot_unavailable",
                    "acknowledged": True,
                }
            )
            continue
        try:
            booking = create_booking(
                BookingCreateRequest(
                    resource=resource,
                    user=user,
                    start=o.start,
                    end=o.end,
                    request_id=request_id,
                    series=series,
                )
            )
        except SlotUnavailableError:
            conflicts.append(
                {
                    "occurrence_date": o.occurrence_date.isoformat(),
                    "reason": "slot_unavailable",
                    "acknowledged": False,
                }
            )
            continue
        created.append(
            {
                "booking_id": str(booking.id),
                "occurrence_date": o.occurrence_date.isoformat(),
                "start": _iso(o.start),
                "end": _iso(o.end),
            }
        )

    return ConfirmOutcome(series_id=series.id, created=created, conflicts=conflicts)
