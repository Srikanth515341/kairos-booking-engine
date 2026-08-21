"""All waitlist-entry write logic lives here, never in views (RFC v1.0
§17, the same convention `kairos.bookings.services` established) — the
same SQLSTATE-translation-and-audit-attribution shape as the booking write
path, applied to a different table.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from dataclasses import dataclass

from django.db import DatabaseError, connection, transaction

from kairos.bookings.models import Booking, BookingStatus
from kairos.core.db import apply_write_path_session_settings
from kairos.core.exceptions import AlreadyOnWaitlistError, ServiceUnavailableError
from kairos.core.models import AuditActorType
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

from .models import WaitlistEntry, WaitlistEntryStatus

logger = logging.getLogger(__name__)

# SQLSTATE 23505 (unique_violation) on `uniq_live_waitlist_per_user_slot`
# (waitlist/0002) — checked by this SPECIFIC code, never a generic
# exception-type catch, exactly like bookings.services' own
# EXCLUSION_VIOLATION check (RFC v1.0 §6.1). No other constraint on this
# table can raise 23505.
UNIQUE_VIOLATION = "23505"

# Same three SQLSTATEs bookings.services treats as "outcome unknown, retry"
# (RFC v1.0 §7.1; Phase 3's empirical 57014 addition) — duplicated here
# rather than imported, matching kairos.core.idempotency's own precedent
# of a local, statement-scoped constant over a shared import (see that
# module's KEY_CLAIM_CONTENDED_SQLSTATES): the SET of codes is identical,
# but what they mean is specific to which statement raised them.
RETRYABLE_SQLSTATES = frozenset({"55P03", "40P01", "57014"})


def slot_is_free(resource: Resource, start: datetime.datetime, end: datetime.datetime) -> bool:
    """Spec v1.0 §5.11's 422 `slot_already_available` check: true iff NO
    confirmed/held booking overlaps the requested range at all — i.e. a
    direct booking attempt right now would succeed rather than conflict.
    Advisory (check-then-act), like every availability read in this
    system — the caller (WaitlistJoinSerializer) treats it that way,
    never as a substitute for the exclusion constraint.
    """
    return (
        not Booking.objects.filter(
            resource=resource,
            status__in=[BookingStatus.CONFIRMED, BookingStatus.HELD],
        )
        .filter(time_range__overlap=(start, end))
        .exists()
    )


def find_eligible_entries(
    resource_id: uuid.UUID, freed_start: datetime.datetime, freed_end: datetime.datetime
) -> list[WaitlistEntry]:
    # Containment (@>), not overlap (&&). A freed range must FULLY CONTAIN
    # the entry's requested range or the entry is not eligible.
    # PRD v1.0 FR21.
    #
    # Ordered FCFS (PRD FR22) — Phase 16's offer-cascade job consumes this
    # ordering directly to offer to the highest-ranked eligible entry
    # first. No live caller yet (holds/offers are Phase 15/16): proven
    # directly against ORM-created rows here, the same "mechanism before
    # its real caller" pattern already used for actor_type='system'
    # (Phase 8/13) and rolling materialization (Phase 13).
    return list(
        WaitlistEntry.objects.filter(
            resource_id=resource_id,
            status=WaitlistEntryStatus.WAITING,
            time_range__contained_by=(freed_start, freed_end),
        ).order_by("joined_at", "id")
    )


@dataclass(frozen=True)
class WaitlistJoinRequest:
    resource: Resource
    user: AppUser
    start: datetime.datetime
    end: datetime.datetime
    request_id: str


def join_waitlist(req: WaitlistJoinRequest) -> WaitlistEntry:
    """Spec v1.0 §5.11. No availability check precedes this INSERT the way
    booking creation has none — the check that matters here
    (`already_on_waitlist`) is enforced by `uniq_live_waitlist_per_user_slot`
    itself, not a check-then-insert race the way `slot_already_available`
    genuinely is (that one lives in WaitlistJoinSerializer.validate(),
    BEFORE the idempotency key is claimed, mirroring
    BookingCreateSerializer's "policy validation before key-claim"
    precedent — a request that can never succeed shouldn't consume a key
    slot).
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.user.id),
        "resource_id": str(req.resource.id),
    }
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                apply_write_path_session_settings(
                    cursor,
                    actor_id=str(req.user.id),
                    actor_type=AuditActorType.USER,
                    request_id=req.request_id,
                )
            entry = WaitlistEntry.objects.create(
                resource=req.resource,
                user=req.user,
                time_range=(req.start, req.end),
            )
            # Same reasoning as create_booking's identical call: .create()
            # leaves time_range as the plain tuple assigned in Python, not
            # the Range object a fresh SELECT returns, and joined_at's
            # db_default is only visible after a real read.
            entry.refresh_from_db()
    except DatabaseError as exc:
        sqlstate = getattr(exc.__cause__, "sqlstate", None)
        if sqlstate == UNIQUE_VIOLATION:
            logger.info(
                "waitlist_join_conflict",
                extra={**log_context, "outcome": "already_on_waitlist"},
            )
            raise AlreadyOnWaitlistError from exc
        if sqlstate in RETRYABLE_SQLSTATES:
            logger.warning(
                "waitlist_join_retryable_failure",
                extra={**log_context, "outcome": "service_unavailable", "sqlstate": sqlstate},
            )
            raise ServiceUnavailableError from exc
        raise

    logger.info(
        "waitlist_entry_joined",
        extra={**log_context, "entry_id": str(entry.id), "outcome": "success"},
    )
    return entry


@dataclass(frozen=True)
class WaitlistCancelRequest:
    entry: WaitlistEntry
    actor: AppUser
    request_id: str


@dataclass(frozen=True)
class WaitlistCancelResult:
    entry: WaitlistEntry
    already_cancelled: bool


def cancel_waitlist_entry(req: WaitlistCancelRequest) -> WaitlistCancelResult:
    """Owner-only self-service withdrawal — Spec v1.0 §5.11/§5.12 document
    no admin-override cancel for a waitlist entry (unlike booking cancel),
    so there is no `reason` field and no ADMIN actor_type branch here.

    The conditional UPDATE is guarded on the entry's CURRENT status
    ('waiting'), mirroring cancel_booking's identical guard-on-current-
    status pattern (kairos.bookings.services) — cancelling an
    already-cancelled entry is a 200 no-op, not an error. 'offered' isn't
    reachable yet (Phase 16 is the first real writer of that status), so
    unlike cancel_booking this guard has only one live non-terminal state
    to cover today; Phase 16 revisiting offer decline/cascade should
    extend this the same way cancel_booking will eventually need to
    account for 'held'.
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.actor.id),
        "entry_id": str(req.entry.id),
    }
    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(req.actor.id),
                actor_type=AuditActorType.USER,
                request_id=req.request_id,
            )
        updated = WaitlistEntry.objects.filter(
            id=req.entry.id, status=WaitlistEntryStatus.WAITING
        ).update(status=WaitlistEntryStatus.CANCELLED)
        entry = WaitlistEntry.objects.get(id=req.entry.id)

    logger.info(
        "waitlist_entry_cancelled" if updated else "waitlist_entry_cancel_replay",
        extra={**log_context, "outcome": "success"},
    )
    return WaitlistCancelResult(entry=entry, already_cancelled=not updated)
