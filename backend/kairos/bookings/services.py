"""All booking write logic lives here, never in views (RFC v1.0 §17). Every
future write path — an admin override, a future bulk-import script — gets
consistent SQLSTATE translation, session settings, and audit attribution for
free, rather than reimplementing them per code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from kairos.bookings.models import Booking, BookingStatus
from kairos.core.db import apply_write_path_session_settings
from kairos.core.exceptions import ServiceUnavailableError, SlotUnavailableError
from kairos.core.models import AuditActorType
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

logger = logging.getLogger(__name__)

# SQLSTATE 23P01 (exclusion_violation) — the constraint did its job; someone
# else's row already occupies the range (Spec v1.0 §6.1). Checked by this
# SPECIFIC code, never a generic exception-type catch, which would conflate
# a real conflict with an unrelated integrity error such as a foreign-key
# violation.
EXCLUSION_VIOLATION = "23P01"

# SQLSTATEs where the outcome is unknown, not decided (Spec v1.0 §5.1) — all
# three retryable and mapped identically to 503 + Retry-After, never a bare
# 500 or 409. 55P03/40P01 are RFC v1.0 §7.1's documented pair; 57014 is
# Phase 3's empirical addition (CLAUDE.md): under heavy contention, most
# losers exceed statement_timeout via cumulative pileup rather than any
# single lock_timeout wait.
LOCK_TIMEOUT = "55P03"
DEADLOCK_DETECTED = "40P01"
QUERY_CANCELED = "57014"
RETRYABLE_SQLSTATES = frozenset({LOCK_TIMEOUT, DEADLOCK_DETECTED, QUERY_CANCELED})


def _handle_write_database_error(exc: DatabaseError, log_context: dict[str, str]) -> NoReturn:
    """Shared SQLSTATE translation for every write path (create, cancel,
    edit) — RFC v1.0 §17: consistent translation for free, rather than
    reimplemented per code path. Always raises.
    """
    sqlstate = getattr(exc.__cause__, "sqlstate", None)

    if sqlstate == EXCLUSION_VIOLATION:
        logger.info("booking_conflict", extra={**log_context, "outcome": "slot_unavailable"})
        raise SlotUnavailableError from exc

    if sqlstate in RETRYABLE_SQLSTATES:
        logger.warning(
            "booking_retryable_failure",
            extra={**log_context, "outcome": "service_unavailable", "sqlstate": sqlstate},
        )
        raise ServiceUnavailableError from exc

    raise exc


@dataclass(frozen=True)
class BookingCreateRequest:
    resource: Resource
    user: AppUser
    start: datetime
    end: datetime
    request_id: str


def create_booking(req: BookingCreateRequest) -> Booking:
    """The write. No availability check precedes it — that is the entire
    point of RFC v1.0 §3: the EXCLUDE constraint IS the check, and no window
    exists between "looks free" and "is free" for a second writer to land
    in. Idempotency (Phase 5) and hold reclamation (Phase 17) are not yet
    part of this transaction — both are documented, temporary gaps.
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.user.id),
        "resource_id": str(req.resource.id),
    }
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Always the booker themselves in Phase 8's scope — there
                # is no admin-create-on-behalf-of-another-user path yet.
                apply_write_path_session_settings(
                    cursor,
                    actor_id=str(req.user.id),
                    actor_type=AuditActorType.USER,
                    request_id=req.request_id,
                )
            booking = Booking.objects.create(
                resource=req.resource,
                user=req.user,
                time_range=(req.start, req.end),
                status=BookingStatus.CONFIRMED,
            )
            # .create() leaves fields exactly as assigned in Python — here,
            # `time_range` as the plain tuple passed in, not the Range
            # object a fresh SELECT returns. Refresh so the returned
            # instance reflects true DB-computed state (also picks up the
            # generated `starts_at` and the DB-stored `created_at`
            # precision) rather than only what Python assigned.
            booking.refresh_from_db()
    except DatabaseError as exc:
        _handle_write_database_error(exc, log_context)

    logger.info(
        "booking_created",
        extra={**log_context, "booking_id": str(booking.id), "outcome": "success"},
    )
    return booking


@dataclass(frozen=True)
class BookingEditRequest:
    booking: Booking
    start: datetime
    end: datetime
    request_id: str


def edit_booking(req: BookingEditRequest) -> Booking:
    """Spec v1.0 §5.5 / PRD FR5. The UPDATE is evaluated against
    `no_overlapping_bookings` exactly as a create's INSERT is — an EXCLUDE
    constraint checked on UPDATE compares the new row against every
    *other* row, so a booking's own current range never self-conflicts
    with its own new one.
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.booking.user_id),
        "booking_id": str(req.booking.id),
    }
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # Owner-only (Spec v1.0 §5.5) — no admin override for
                # edit, so the actor is always the booking's own owner.
                apply_write_path_session_settings(
                    cursor,
                    actor_id=str(req.booking.user_id),
                    actor_type=AuditActorType.USER,
                    request_id=req.request_id,
                )
            Booking.objects.filter(id=req.booking.id).update(time_range=(req.start, req.end))
            booking = Booking.objects.get(id=req.booking.id)
    except DatabaseError as exc:
        _handle_write_database_error(exc, log_context)

    logger.info("booking_edited", extra={**log_context, "outcome": "success"})
    return booking


@dataclass(frozen=True)
class BookingCancelRequest:
    booking: Booking
    actor: AppUser
    actor_type: str
    reason: str | None
    request_id: str


@dataclass(frozen=True)
class BookingCancelResult:
    booking: Booking
    already_cancelled: bool


def cancel_booking(req: BookingCancelRequest) -> BookingCancelResult:
    """Spec v1.0 §5.6. Cancelling an already-cancelled booking is not an
    error: the conditional UPDATE below (guarded on the CURRENT status,
    not merely the target one) matches zero rows in that case, and the
    already-cancelled state is simply returned — "make sure this is
    cancelled" achieves its intent either way. Moving out of
    `status IN ('confirmed','held')` removes the row from the exclusion
    domain immediately (RFC v1.0 §5c step 2); no separate step "frees" the
    range.
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.actor.id),
        "booking_id": str(req.booking.id),
    }
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # actor_type distinguishes a self-cancel from a resource-
                # admin override (Spec v1.0 §5.6) — the view already knows
                # which this is (that's what decides whether `reason` is
                # required at all) and passes it through rather than this
                # function re-deriving it.
                apply_write_path_session_settings(
                    cursor,
                    actor_id=str(req.actor.id),
                    actor_type=req.actor_type,
                    request_id=req.request_id,
                    reason=req.reason,
                )
            updated = Booking.objects.filter(
                id=req.booking.id, status=BookingStatus.CONFIRMED
            ).update(
                status=BookingStatus.CANCELLED,
                cancelled_at=timezone.now(),
                cancelled_by=req.actor,
                cancellation_reason=req.reason,
            )
            booking = Booking.objects.get(id=req.booking.id)
            if updated:
                # RFC v1.0 §5c step 4 — real dispatch arrives in Phase 16.
                # Registered here, INSIDE this atomic block, so Django
                # defers it until the OUTER transaction (run_idempotent_
                # write's, which also records the idempotency outcome)
                # actually commits — never fired from inside the write
                # itself, where a later rollback could leave a worker
                # acting on a range that was never really freed.
                transaction.on_commit(
                    lambda: logger.info("would_enqueue_waitlist_check", extra=log_context)
                )
    except DatabaseError as exc:
        _handle_write_database_error(exc, log_context)

    logger.info(
        "booking_cancelled" if updated else "booking_cancel_replay",
        extra={**log_context, "outcome": "success"},
    )
    return BookingCancelResult(booking=booking, already_cancelled=not updated)
