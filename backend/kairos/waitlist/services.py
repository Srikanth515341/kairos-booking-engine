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
from typing import Any

from django.db import DatabaseError, connection, transaction
from django.utils import timezone as django_timezone

from kairos.bookings.models import Booking, BookingStatus
from kairos.bookings.services import BookingCreateRequest, create_booking
from kairos.core.constants import HOLD_REAPER_INTERVAL_SECONDS
from kairos.core.db import apply_write_path_session_settings
from kairos.core.exceptions import (
    AlreadyOnWaitlistError,
    OfferAlreadyResolvedError,
    OfferExpiredError,
    ServiceUnavailableError,
    SlotUnavailableError,
)
from kairos.core.models import AuditActorType, SystemCheckRun
from kairos.core.notifications import notify_offer_created
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

from .models import WaitlistEntry, WaitlistEntryStatus, WaitlistOffer, WaitlistOfferStatus
from .tasks import dispatch_cascade

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
    # Ordered FCFS (PRD FR22) — create_offer_for_freed_range (below,
    # Phase 16) consumes this ordering directly to offer to the
    # highest-ranked eligible entry first, advancing to the next one on a
    # hold-creation conflict.
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
    # USER for every caller through Phase 16 (default, unchanged). Phase
    # 19's offboarding cascade overrides this to SYSTEM: the entry's own
    # owner didn't choose to withdraw, the deactivation policy did.
    actor_type: str = AuditActorType.USER


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
    already-cancelled entry is a 200 no-op, not an error.

    Documented, NOT fixed this phase: an 'offered' entry (reachable for
    real as of Phase 16) also fails this guard (0 rows) and is reported
    back as `already_cancelled=True` with its REAL current status
    ('offered', not 'cancelled') in the response body — truthful, but a
    user calling THIS endpoint on an outstanding offer gets a silent
    200 no-op instead of either an error steering them to
    `decline_offer` (below) or this function correctly performing that
    release itself. Fixing it correctly means this function absorbing
    decline_offer's hold-release-and-cascade logic, which is a real
    change, not a guard tweak — flagged for a future phase rather than
    silently patched under this one's own unrelated scope.
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
                actor_id=(str(req.actor.id) if req.actor_type != AuditActorType.SYSTEM else ""),
                actor_type=req.actor_type,
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


def create_offer_for_freed_range(
    resource: Resource,
    freed_start: datetime.datetime,
    freed_end: datetime.datetime,
    request_id: str,
) -> WaitlistOffer | None:
    """RFC v1.0 §10.2 / Spec v1.0 §4.2 — the worker `transaction.on_commit()`
    enqueues (via `create_offer_for_freed_range_task`) after a cancellation
    or an offer decline frees a range. Not an HTTP-request-scoped
    function: `request_id` here is a correlation id carried forward
    through the audit trail (Phase 8's convention), not tied to any
    live request the caller is blocking on.

    PRD FR23: the hold is created BEFORE the offer — the loop below never
    constructs a `WaitlistOffer` row until `create_booking` has already
    succeeded, so an offer without a hold cannot exist even transiently.
    """
    log_context = {"request_id": request_id, "resource_id": str(resource.id)}
    candidates = find_eligible_entries(resource.id, freed_start, freed_end)

    for entry in candidates:
        entry_start, entry_end = entry.time_range.lower, entry.time_range.upper
        try:
            hold = create_booking(
                BookingCreateRequest(
                    resource=resource,
                    user=entry.user,
                    start=entry_start,
                    end=entry_end,
                    request_id=request_id,
                    actor_type=AuditActorType.SYSTEM,
                    status=BookingStatus.HELD,
                )
            )
        except SlotUnavailableError:
            # Spec v1.0 §4.2: something already occupies the range — a
            # race with a direct booking, or another worker attempting
            # the same freed range. Re-query and try the next candidate —
            # the SAME retry-on-conflict pattern used everywhere else in
            # this design (RFC v1.0 §10.2 point 3), not a new one invented
            # for this subsystem.
            logger.info(
                "offer_hold_conflict",
                extra={**log_context, "entry_id": str(entry.id)},
            )
            continue

        # create_booking(..., status=HELD) always sets expires_at (Phase
        # 15's hold_has_expiry DB CHECK requires it) — the assertion is
        # for mypy, not a runtime possibility.
        assert hold.expires_at is not None
        offer = WaitlistOffer.objects.create(
            waitlist_entry=entry,
            hold_booking=hold,
            resource=resource,
            time_range=(entry_start, entry_end),
            status=WaitlistOfferStatus.ACTIVE,
            expires_at=hold.expires_at,
        )
        WaitlistEntry.objects.filter(id=entry.id).update(status=WaitlistEntryStatus.OFFERED)
        logger.info(
            "waitlist_offer_created",
            extra={**log_context, "offer_id": str(offer.id), "entry_id": str(entry.id)},
        )
        # PRD FR52 / Implementation Plan Phase 18. Called directly, not
        # deferred via transaction.on_commit(): this function is never
        # itself wrapped in an open transaction.atomic() (the hold's own
        # commit already happened inside create_booking, above; the two
        # statements right above this one are plain autocommitted writes)
        # — there is no later rollback that could un-free what this
        # notification describes, the same reasoning that already lets
        # reap_expired_holds call this whole function directly rather
        # than through an on_commit deferral.
        notify_offer_created(
            recipient=entry.user,
            resource_name=resource.name,
            start=entry_start,
            end=entry_end,
            expires_at=hold.expires_at,
            request_id=request_id,
        )
        return offer

    logger.info("no_eligible_waitlist_entry", extra=log_context)
    return None


@dataclass(frozen=True)
class AcceptOfferRequest:
    offer: WaitlistOffer
    user: AppUser
    request_id: str


def accept_offer(req: AcceptOfferRequest) -> Booking:
    """RFC v1.0 §10.3 / Spec v1.0 §4.3, executed exactly. No
    `slot_unavailable` is structurally possible on this path — there is
    no INSERT here, only a conditional UPDATE against a row that already
    occupies the exclusion domain (Spec v1.0 §5.13: "If it ever fires,
    the hold mechanism is broken and it is an incident, not a
    user-facing error").
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.user.id),
        "offer_id": str(req.offer.id),
    }
    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(req.user.id),
                actor_type=AuditActorType.USER,
                request_id=req.request_id,
            )
        # The literal Spec v1.0 §4.3 conditional UPDATE — status, owner,
        # AND expiry all guard the same statement, so a wrong user or an
        # expired hold both fail identically (0 rows), never a partial
        # success.
        updated = Booking.objects.filter(
            id=req.offer.hold_booking_id,
            status=BookingStatus.HELD,
            user=req.user,
            expires_at__gt=django_timezone.now(),
        ).update(status=BookingStatus.CONFIRMED, expires_at=None)
        if not updated:
            logger.info("offer_expired", extra=log_context)
            raise OfferExpiredError
        WaitlistOffer.objects.filter(id=req.offer.id, status=WaitlistOfferStatus.ACTIVE).update(
            status=WaitlistOfferStatus.CONFIRMED
        )
        WaitlistEntry.objects.filter(id=req.offer.waitlist_entry_id).update(
            status=WaitlistEntryStatus.FULFILLED
        )
        booking = Booking.objects.get(id=req.offer.hold_booking_id)

    logger.info("offer_accepted", extra={**log_context, "outcome": "success"})
    return booking


@dataclass(frozen=True)
class DeclineOfferRequest:
    offer: WaitlistOffer
    user: AppUser
    request_id: str
    # USER for every caller through Phase 16 (default, unchanged). Phase
    # 19's offboarding cascade overrides this to SYSTEM: an outstanding
    # hold is released because its beneficiary was deactivated, not
    # because they chose to decline.
    actor_type: str = AuditActorType.USER


def decline_offer(req: DeclineOfferRequest) -> WaitlistOffer:
    """Spec v1.0 §5.13: releases the hold immediately (never waits for
    expiry) and cascades sooner than expiry would, via the SAME
    `create_offer_for_freed_range` worker a cancellation dispatches,
    enqueued through the identical `transaction.on_commit()` pattern (RFC
    v1.0 §5c step 4) so a rollback can never leave a worker acting on a
    range that wasn't really freed.

    Guarded on the offer's CURRENT status ('active'), but — unlike
    `cancel_booking`/`cancel_waitlist_entry` — an already-resolved offer
    is NOT a 200 no-op: Spec v1.0 §5.13 names `offer_already_resolved` as
    its own distinct failure code, a deliberate choice this function
    honors rather than reusing the idempotent-cancel convention.

    The declining entry's own status becomes 'expired', not back to
    'waiting' — this is what stops it from immediately re-winning the
    very cascade its own decline triggers (`find_eligible_entries` filters
    `WHERE status='waiting'`); it also gives `WaitlistEntryStatus.EXPIRED`
    — otherwise unused before this phase — its actual meaning: this
    entry's specific opportunity is over, distinct from CANCELLED (the
    user withdrew the request entirely, Phase 14) or FULFILLED (accepted).
    """
    log_context = {
        "request_id": req.request_id,
        "user_id": str(req.user.id),
        "offer_id": str(req.offer.id),
    }
    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=(str(req.user.id) if req.actor_type != AuditActorType.SYSTEM else ""),
                actor_type=req.actor_type,
                request_id=req.request_id,
            )
        updated = WaitlistOffer.objects.filter(
            id=req.offer.id, status=WaitlistOfferStatus.ACTIVE
        ).update(status=WaitlistOfferStatus.DECLINED)
        if not updated:
            raise OfferAlreadyResolvedError

        # expires_at must be cleared, not just status changed — the
        # hold_has_expiry DB CHECK constraint (Phase 2) requires
        # expires_at IS NULL for any non-'held' row. Caught empirically
        # (SQLSTATE 23514) while building WL-02's reaper simulation,
        # which needed the identical fix.
        Booking.objects.filter(id=req.offer.hold_booking_id, status=BookingStatus.HELD).update(
            status=BookingStatus.CANCELLED,
            cancelled_at=django_timezone.now(),
            expires_at=None,
        )
        WaitlistEntry.objects.filter(id=req.offer.waitlist_entry_id).update(
            status=WaitlistEntryStatus.EXPIRED
        )

        resource_id = req.offer.resource_id
        range_start, range_end = req.offer.time_range.lower, req.offer.time_range.upper
        # Registered INSIDE this atomic block, so Django defers it until
        # the OUTER transaction (run_idempotent_write's) actually commits
        # — the same cancel_booking precedent (kairos.bookings.services),
        # never fired from inside the write itself, where a later rollback
        # could leave a worker acting on a range that was never really
        # freed.
        transaction.on_commit(
            lambda: dispatch_cascade(str(resource_id), range_start, range_end, req.request_id)
        )

    offer = WaitlistOffer.objects.get(id=req.offer.id)
    logger.info("offer_declined", extra={**log_context, "outcome": "success"})
    return offer


def reap_expired_holds(*, now: datetime.datetime | None = None) -> dict[str, Any]:
    """RFC v1.0 §10.4 mechanism 2 — the periodic sweep. Cleanup-on-write
    (`create_booking`, above) only fires when there IS booking traffic;
    this is what guarantees cascade fires even when nobody is trying to
    book at all (RECLAIM-02's whole point).

    Each expired hold is reclaimed via the IDENTICAL conditional UPDATE
    shape `accept_offer` races against (`WHERE id=... AND status='held'
    AND expires_at<=now()`, guarded — never a blind unconditional update)
    — this is the race-safety RECLAIM-03 proves: whichever of this
    UPDATE or acceptance's own conditional UPDATE commits first wins the
    row outright; the loser's WHERE clause simply matches zero rows on
    re-evaluation, no application-level lock required (RFC v1.0 §10.4's
    own "the race... is safe in both orderings" claim).

    One independent transaction per hold, mirroring `confirm_recurring_
    series`'s per-occurrence isolation (Phase 12) and `rolling_
    materialize_series`'s per-occurrence isolation (Phase 13) — one
    contested hold losing its race to acceptance must never roll back
    the reclamation of every OTHER expired hold in the same sweep.
    """
    now = now or django_timezone.now()
    request_id = str(uuid.uuid4())

    holds_reclaimed = 0
    cascades_triggered = 0
    entries_returned_to_waiting = 0

    expired_holds = list(
        Booking.objects.filter(status=BookingStatus.HELD, expires_at__lte=now).select_related(
            "resource"
        )
    )

    for hold in expired_holds:
        freed_range: tuple[datetime.datetime, datetime.datetime] | None = None
        with transaction.atomic():
            with connection.cursor() as cursor:
                apply_write_path_session_settings(
                    cursor, actor_id="", actor_type=AuditActorType.SYSTEM, request_id=request_id
                )
            updated = Booking.objects.filter(
                id=hold.id, status=BookingStatus.HELD, expires_at__lte=now
            ).update(status=BookingStatus.CANCELLED, cancelled_at=now, expires_at=None)
            if not updated:
                # Lost the race — acceptance already claimed this exact
                # row (RECLAIM-03). Nothing left to reclaim here.
                continue

            offer = WaitlistOffer.objects.filter(
                hold_booking_id=hold.id, status=WaitlistOfferStatus.ACTIVE
            ).first()
            if offer is not None:
                WaitlistOffer.objects.filter(id=offer.id).update(status=WaitlistOfferStatus.EXPIRED)
                WaitlistEntry.objects.filter(id=offer.waitlist_entry_id).update(
                    status=WaitlistEntryStatus.EXPIRED
                )
                entries_returned_to_waiting += 1
                freed_range = (offer.time_range.lower, offer.time_range.upper)

        holds_reclaimed += 1
        if freed_range is not None:
            # Outside the reclaim transaction, same reasoning as every
            # on_commit-dispatched cascade elsewhere: never attempt a new
            # hold for a range while the transaction that freed it might
            # still roll back. Called directly here (not via the Celery
            # task) — the reaper task itself IS the background worker;
            # there is no further "after commit" boundary to defer past.
            new_offer = create_offer_for_freed_range(hold.resource, *freed_range, request_id)
            if new_offer is not None:
                cascades_triggered += 1

    findings: dict[str, Any] = {
        "holds_reclaimed": holds_reclaimed,
        "cascades_triggered": cascades_triggered,
        "entries_returned_to_waiting": entries_returned_to_waiting,
    }
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.HOLD_REAPER,
        status=SystemCheckRun.Status.PASS,
        findings=findings,
    )
    logger.info("hold_reaper_run", extra={"request_id": request_id, **findings})
    return findings


def hold_reaper_heartbeat_is_stale(
    *,
    now: datetime.datetime | None = None,
    threshold_seconds: int = HOLD_REAPER_INTERVAL_SECONDS * 3,
) -> bool:
    """WL-05 Part B's actual mechanism: the DATA a future alert would
    consume, not the alert itself — full alert-routing/the `GET /admin/
    checks/latest` surface is Phase 21 (Scope — DEFERRED, this phase's own
    instructions: "Heartbeat alerting → Phase 21, the heartbeat is written
    here"). A missing heartbeat (the reaper has never run at all) counts
    as stale too — "no evidence of health" is not health, the same
    "absence of a heartbeat is the only signal" principle this phase's
    own Scope IN states explicitly.

    Threshold defaults to 3x the sweep interval — one or two slow/skipped
    cycles isn't yet an incident, matching the kind of tolerance a real
    alert threshold needs against ordinary jitter; not tuned against real
    data, the same honestly-a-placeholder status HOLD_REAPER_INTERVAL_
    SECONDS itself has (RFC v1.0 §18).
    """
    now = now or django_timezone.now()
    latest = (
        SystemCheckRun.objects.filter(check_name=SystemCheckRun.CheckName.HOLD_REAPER)
        .order_by("-run_at")
        .first()
    )
    if latest is None:
        return True
    return (now - latest.run_at).total_seconds() > threshold_seconds
