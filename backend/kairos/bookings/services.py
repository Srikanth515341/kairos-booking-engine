"""All booking write logic lives here, never in views (RFC v1.0 §17). Every
future write path — an admin override, a future bulk-import script — gets
consistent SQLSTATE translation, session settings, and audit attribution for
free, rather than reimplementing them per code path.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries
from kairos.core.constants import OFFER_WINDOW_MINUTES
from kairos.core.db import apply_write_path_session_settings
from kairos.core.exceptions import ServiceUnavailableError, SlotUnavailableError
from kairos.core.models import AuditActorType
from kairos.core.notifications import notify_admin_cancellation
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from kairos.waitlist.tasks import dispatch_cascade

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
    # NULL for a one-off booking (every caller before Phase 12). Set by
    # confirm_recurring_series (Phase 12) when this INSERT is one
    # occurrence of a series — the FK that makes series-level cancellation
    # (Spec v1.0 §5.10) a filtered update rather than tracking membership
    # some other way.
    series: RecurringSeries | None = None
    # USER for every caller through Phase 12 (default, unchanged). Phase 13
    # overrides this to SYSTEM for rolling-materialization/re-materialization
    # writes — `req.user` still owns the resulting booking (the series'
    # `created_by`), but the ACTOR that performed the write is the
    # background job, not that user, so the audit trail must say so.
    actor_type: str = AuditActorType.USER
    # CONFIRMED for every caller through Phase 14 (default, unchanged).
    # Phase 15 (RFC v1.0 §10.1) sets this to HELD for a hold — a waitlist
    # offer's reservation — created by a future cascade worker (Phase 16):
    # `req.user` is then the WAITLISTED user the slot is held FOR, not a
    # self-service booker. Reusing this same function/request, rather than
    # a bespoke create_hold(), is what makes the hold's INSERT go through
    # the identical session-settings/SQLSTATE-translation/refresh_from_db
    # machinery a confirmed booking's does — the same "one write path, one
    # correctness proof" reasoning Phase 13 already applied to actor_type.
    status: str = BookingStatus.CONFIRMED


def create_booking(req: BookingCreateRequest) -> Booking:
    """The write. No availability check precedes it — that is the entire
    point of RFC v1.0 §3: the EXCLUDE constraint IS the check, and no window
    exists between "looks free" and "is free" for a second writer to land
    in. Idempotency is applied by the caller (`run_idempotent_write`,
    Phase 5), wrapping this whole function — not part of THIS transaction.
    Hold reclamation (Phase 17), by contrast, genuinely IS part of this
    transaction: the cleanup-on-write DELETE below runs on every call, for
    every caller, unconditionally.

    Called once per occurrence by confirm_recurring_series (Phase 12,
    RFC v1.0 §5d) exactly as it's called for a one-off booking — same
    function, same per-call transaction, same fresh
    apply_write_path_session_settings on every invocation. That reuse is
    deliberate and load-bearing: it's what gives each occurrence its own
    independently-committed transaction (never one shared transaction
    across a series) and its own correctly-timed session settings, without
    Phase 12 needing to reimplement either. Phase 13's rolling-
    materialization job reuses it a third time, for the identical reason.
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.user.id),
        "resource_id": str(req.resource.id),
    }
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # actor_id is the empty string — NOT req.user.id — for a
                # SYSTEM-initiated write (Phase 13): apply_write_path_
                # session_settings' own established convention is that an
                # empty string becomes SQL NULL via the trigger's
                # NULLIF(..., ''), the same convention already used for an
                # absent `reason` — and a background job genuinely has no
                # human actor to attribute the row to. `req.user` here only
                # says who the booking is FOR.
                apply_write_path_session_settings(
                    cursor,
                    actor_id=(str(req.user.id) if req.actor_type != AuditActorType.SYSTEM else ""),
                    actor_type=req.actor_type,
                    request_id=req.request_id,
                )
                # ========================================================
                # CLEANUP-ON-WRITE (RFC v1.0 §10.4 mechanism 1; Spec v1.0
                # §4.1 step 2) — reclaims expired holds for THIS resource,
                # scoped to the range about to be written, inside the SAME
                # transaction as the insert below. A constraint predicate
                # cannot express expiry (Postgres requires index predicates
                # to be IMMUTABLE; `now()` is not), so an expired hold
                # would otherwise block this write FOREVER, independent of
                # whether the reaper (below) is healthy or Redis is even
                # reachable. This is the self-healing property RECLAIM-01
                # exists to prove: a stalled reaper, or a Redis outage,
                # never makes a resource permanently unbookable, because
                # the very next writer clears the stale hold themselves.
                # Runs for EVERY caller of create_booking — an ordinary
                # booking, a recurring occurrence, rolling materialization,
                # AND the offer-cascade worker's own hold creation — never
                # skipped, never conditional on who's calling. If you are
                # reading this while considering removing or narrowing it:
                # STOP. Without it, PRD FR18 ("an expired hold must not
                # block any booking") depends entirely on the reaper's
                # uptime, which RFC v1.0 §4.3 explicitly says must not be
                # true.
                # ========================================================
                cursor.execute(
                    "DELETE FROM booking WHERE resource_id = %s AND status = 'held' "
                    "AND expires_at <= now() AND time_range && tstzrange(%s, %s)",
                    [str(req.resource.id), req.start, req.end],
                )
            # ============================================================
            # RFC v1.0 §10.1: a hold (status='held') occupies the SAME
            # exclusion domain as a confirmed booking, because the
            # `no_overlapping_bookings` predicate is `status IN
            # ('confirmed', 'held')` (bookings/0002_exclusion_constraint.py).
            # This INSERT is what makes that true in practice, not just in
            # the schema — a hold is a real row in THIS table, enforced by
            # THIS SAME constraint, with no second coordination mechanism.
            # If a future change ever narrows that predicate back to
            # 'confirmed' alone, this INSERT keeps succeeding and every
            # waitlist guarantee (PRD FR17, S1) silently stops meaning
            # anything — an ordinary booking could then take a slot out
            # from under an outstanding offer, while every test outside
            # RECON-05/HOLD-01 still passes. STOP and re-read RFC v1.0
            # §10.1 before ever changing what this writes.
            # ============================================================
            expires_at = (
                timezone.now() + timedelta(minutes=OFFER_WINDOW_MINUTES)
                if req.status == BookingStatus.HELD
                else None
            )
            booking = Booking.objects.create(
                resource=req.resource,
                user=req.user,
                series=req.series,
                time_range=(req.start, req.end),
                status=req.status,
                expires_at=expires_at,
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
        "hold_created" if req.status == BookingStatus.HELD else "booking_created",
        extra={**log_context, "booking_id": str(booking.id), "outcome": "success"},
    )
    return booking


@dataclass(frozen=True)
class BookingEditRequest:
    booking: Booking
    start: datetime
    end: datetime
    request_id: str
    # USER for every caller through Phase 7 (default, unchanged). Phase 13
    # overrides this to SYSTEM when re-materialization (RFC v1.0 §9.4)
    # updates an occurrence's stored instant after a tzdata rule change —
    # the booking's owner didn't act, the tzdata-check job did.
    actor_type: str = AuditActorType.USER


def edit_booking(req: BookingEditRequest) -> Booking:
    """Spec v1.0 §5.5 / PRD FR5. The UPDATE is evaluated against
    `no_overlapping_bookings` exactly as a create's INSERT is — an EXCLUDE
    constraint checked on UPDATE compares the new row against every
    *other* row, so a booking's own current range never self-conflicts
    with its own new one.

    Reused for RFC v1.0 §9.4's re-materialization (Phase 13): recomputing
    an occurrence's instant from its series definition and writing it here
    is structurally an edit — the booking's identity and owner don't
    change, only its `time_range` — so it goes through the SAME
    conflict-checked UPDATE path a user's own edit does, not a bespoke one.
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.booking.user_id),
        "booking_id": str(req.booking.id),
    }
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                # actor_id empty for a SYSTEM-initiated edit — same
                # NULLIF(..., '') convention as create_booking above.
                apply_write_path_session_settings(
                    cursor,
                    actor_id=(
                        str(req.booking.user_id) if req.actor_type != AuditActorType.SYSTEM else ""
                    ),
                    actor_type=req.actor_type,
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
                # function re-deriving it. actor_id is empty for a
                # SYSTEM-initiated cancel (Phase 19's offboarding cascade,
                # the first caller to pass actor_type=SYSTEM here) — the
                # same NULLIF(..., '')-becomes-NULL convention create_
                # booking/edit_booking already established; `req.actor`
                # still supplies `cancelled_by` below (a real, meaningful
                # value: the admin who triggered the offboarding), only
                # the SESSION-VARIABLE actor_id — and therefore audit_log.
                # actor_id — is emptied.
                apply_write_path_session_settings(
                    cursor,
                    actor_id=(str(req.actor.id) if req.actor_type != AuditActorType.SYSTEM else ""),
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
                # RFC v1.0 §5c step 4 (Phase 16). Registered here, INSIDE
                # this atomic block, so Django defers it until the OUTER
                # transaction (run_idempotent_write's, which also records
                # the idempotency outcome) actually commits — never fired
                # from inside the write itself, where a later rollback
                # could leave a worker acting on a range that was never
                # really freed.
                resource_id = booking.resource_id
                range_start, range_end = booking.time_range.lower, booking.time_range.upper
                transaction.on_commit(
                    lambda: dispatch_cascade(
                        str(resource_id), range_start, range_end, req.request_id
                    )
                )
                if req.actor_type == AuditActorType.ADMIN:
                    # PRD FR53 / Implementation Plan Phase 18. Same
                    # on_commit deferral as the cascade dispatch above and
                    # the identical reason: this only ever fires from a
                    # resource-admin override (never a self-cancel — the
                    # view only sets actor_type=ADMIN for is_override,
                    # never for the booking's own owner), and `req.reason`
                    # is guaranteed non-empty here because
                    # BookingCancelSerializer already requires it whenever
                    # is_owner=False (Spec v1.0 §5.6 / PRD FR47).
                    # req.booking (not the re-fetched `booking` above)
                    # carries the view's select_related("resource"), so
                    # this doesn't cost an extra query.
                    owner = req.booking.user
                    resource_name = req.booking.resource.name
                    reason = req.reason or ""
                    transaction.on_commit(
                        lambda: notify_admin_cancellation(
                            recipient=owner,
                            resource_name=resource_name,
                            start=range_start,
                            end=range_end,
                            reason=reason,
                            request_id=req.request_id,
                        )
                    )
    except DatabaseError as exc:
        _handle_write_database_error(exc, log_context)

    logger.info(
        "booking_cancelled" if updated else "booking_cancel_replay",
        extra={**log_context, "outcome": "success"},
    )
    return BookingCancelResult(booking=booking, already_cancelled=not updated)


@dataclass(frozen=True)
class BookingTransferRequest:
    booking: Booking
    new_owner: AppUser
    request_id: str


def transfer_booking_ownership(req: BookingTransferRequest) -> Booking:
    """PRD FR49 / Implementation Plan Phase 19 — the `offboarding_policy=
    'transfer'` outcome: a deactivated user's future booking on a
    resource with this policy moves to that resource's administrator
    rather than being cancelled. Only `user_id` changes — `time_range` is
    untouched, so this can never conflict with `no_overlapping_bookings`
    (the row already legitimately occupies this exact range; changing
    who it belongs to doesn't change what it occupies). Always
    `actor_type=SYSTEM`: no self-service or admin-override caller exists
    for this operation, only the offboarding cascade.
    """
    log_context = {
        "request_id": req.request_id,
        "booking_id": str(req.booking.id),
        "new_owner_id": str(req.new_owner.id),
    }
    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor, actor_id="", actor_type=AuditActorType.SYSTEM, request_id=req.request_id
            )
        Booking.objects.filter(id=req.booking.id).update(user=req.new_owner)
        booking = Booking.objects.get(id=req.booking.id)

    logger.info("booking_ownership_transferred", extra={**log_context, "outcome": "success"})
    return booking


@dataclass(frozen=True)
class RecurringSeriesCancelRequest:
    series: RecurringSeries
    actor: AppUser
    actor_type: str
    reason: str | None
    request_id: str


@dataclass(frozen=True)
class RecurringSeriesCancelResult:
    cancelled_booking_ids: list[uuid.UUID]
    occurrences_already_past: int


def cancel_recurring_series(req: RecurringSeriesCancelRequest) -> RecurringSeriesCancelResult:
    """Spec v1.0 §5.10; PRD FR15. Only still-CONFIRMED, still-FUTURE
    occurrences are touched (`starts_at >= now()`) — past occurrences stay
    exactly as they are, historical fact, matching PRD FR16's "past
    occurrences are historical fact and immutable" even though this isn't
    the edit path FR16 was written for.

    `req.reason` is required by the view whenever this is a resource-admin
    override, not a self-cancel by the series' own `created_by` (PRD FR47:
    "administrative override of another user's booking requires a
    recorded reason" — unconditional, and a series-cancel-by-admin is
    exactly such an override on every occurrence it touches, the same as
    single-booking cancel's existing rule).

    Unlike `cancel_booking`, this doesn't need per-row transaction
    isolation the way `confirm_recurring_series`'s CREATE does: cancelling
    can never lose to the exclusion constraint (moving a row OUT of
    `status IN ('confirmed','held')` never conflicts with anything), so
    there is no "one contested occurrence" failure mode to isolate. A
    single bulk UPDATE is correct and is what Postgres's row-level audit
    trigger already handles correctly — it fires once per affected row
    regardless of whether the UPDATE is issued as one statement or many.
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.actor.id),
        "series_id": str(req.series.id),
    }
    now = timezone.now()
    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(req.actor.id),
                actor_type=req.actor_type,
                request_id=req.request_id,
                reason=req.reason,
            )
        cancelled_ids = list(
            Booking.objects.filter(
                series=req.series, status=BookingStatus.CONFIRMED, starts_at__gte=now
            ).values_list("id", flat=True)
        )
        Booking.objects.filter(id__in=cancelled_ids).update(
            status=BookingStatus.CANCELLED,
            cancelled_at=now,
            cancelled_by=req.actor,
            cancellation_reason=req.reason,
        )
        occurrences_already_past = Booking.objects.filter(
            series=req.series, starts_at__lt=now
        ).count()

    logger.info(
        "recurring_series_cancelled",
        extra={
            **log_context,
            "outcome": "success",
            "cancelled_count": len(cancelled_ids),
        },
    )
    return RecurringSeriesCancelResult(
        cancelled_booking_ids=cancelled_ids,
        occurrences_already_past=occurrences_already_past,
    )
