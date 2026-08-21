"""User offboarding (Implementation Plan Phase 19; PRD v1.0 FR49-51; Spec
v1.0 §5.15). `kairos.identity` already depends on `kairos.bookings`
(`AuthorizationService` imports `Booking`/`RecurringSeries`) — extending
that same dependency direction to `kairos.bookings.services`/`kairos.
waitlist.services` here is not a new direction in the dependency graph,
just a deeper use of an existing one.

`deactivate_user` reuses `run_idempotent_write` (Spec v1.0 §7 lists
`POST /admin/users/{id}/deactivate` in its Idempotency-Key coverage) —
ONE shared transaction, the same choice `cancel_recurring_series` (Phase
12) already made for the identical reason: every write this cascade
performs (booking cancel/transfer, hold release, waitlist-entry cancel,
the user's own status flip) moves rows OUT of or sideways from the
exclusion domain, never INTO a newly-contested range, so there is no
"one contested item" isolation problem the way booking CREATE has. Each
individual cascade step still opens its OWN nested `atomic()` (a
savepoint) via the write function it reuses — matching every other
write function in this codebase, whether or not it's always called
through `run_idempotent_write`.
"""

from __future__ import annotations

import functools
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.db import connection, transaction
from django.utils import timezone as django_timezone

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries, RecurringSeriesStatus
from kairos.bookings.services import (
    BookingCancelRequest,
    BookingTransferRequest,
    cancel_booking,
    transfer_booking_ownership,
)
from kairos.core.db import apply_write_path_session_settings
from kairos.core.models import AuditActorType
from kairos.core.notifications import notify_admin_cancellation, notify_series_owner_deactivated
from kairos.identity.models import AppUser, AppUserStatus, ResourceAdmin
from kairos.resources.models import ResourceOffboardingPolicy
from kairos.waitlist.models import (
    WaitlistEntry,
    WaitlistEntryStatus,
    WaitlistOffer,
    WaitlistOfferStatus,
)
from kairos.waitlist.services import (
    DeclineOfferRequest,
    WaitlistCancelRequest,
    cancel_waitlist_entry,
    decline_offer,
)

logger = logging.getLogger(__name__)


def _resource_admin_for(resource_id: uuid.UUID) -> AppUser | None:
    """FCFS by grant time — the earliest-granted admin, mirroring this
    project's other tie-break conventions (waitlist FCFS by joined_at/
    id). No admin at all for a `transfer`-policy resource is a genuine,
    un-Spec'd edge case (OFF-01's own setup guarantees one exists for the
    resource it exercises this policy against) — falls back to retaining
    the booking rather than raising; see `deactivate_user` below.
    """
    grant = (
        ResourceAdmin.objects.filter(resource_id=resource_id)
        .select_related("user")
        .order_by("granted_at", "id")
        .first()
    )
    return grant.user if grant else None


@dataclass(frozen=True)
class DeactivateUserRequest:
    target: AppUser
    actor: AppUser  # the system_admin who called the endpoint
    reason: str
    request_id: str


@dataclass
class DeactivateUserResult:
    user_id: uuid.UUID
    status: str
    bookings_transferred: int = 0
    bookings_cancelled: int = 0
    bookings_retained: int = 0
    waitlist_entries_cancelled: int = 0
    offers_released: int = 0
    series_flagged_for_admin: list[dict[str, Any]] = field(default_factory=list)


def deactivate_user(req: DeactivateUserRequest) -> DeactivateUserResult:
    """Spec v1.0 §5.15 / Test Plan OFF-01. The status flip below is
    guarded on the target's CURRENT status ('active'), the same
    conditional-UPDATE-decides-whether-to-cascade shape `cancel_booking`
    established (Phase 7): a repeat deactivation is a 200 no-op with
    zeroed counts, never a re-run of a cascade that already happened.
    """
    now = django_timezone.now()

    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(req.actor.id),
                actor_type=AuditActorType.ADMIN,
                request_id=req.request_id,
                reason=req.reason,
            )
        updated = AppUser.objects.filter(id=req.target.id, status=AppUserStatus.ACTIVE).update(
            status=AppUserStatus.DEACTIVATED, deactivated_at=now
        )
        target = AppUser.objects.get(id=req.target.id)

    result = DeactivateUserResult(user_id=target.id, status=target.status)
    if not updated:
        logger.info(
            "user_deactivate_replay",
            extra={"request_id": req.request_id, "user_id": str(target.id)},
        )
        return result

    # ================================================================
    # PRD FR49: one-off future confirmed bookings, per-resource policy.
    # series-generated occurrences are deliberately excluded here
    # (series__isnull=True) — FR51 governs those at the SERIES level
    # (flagged below), not per-occurrence.
    # ================================================================
    bookings = Booking.objects.filter(
        user=target, status=BookingStatus.CONFIRMED, series__isnull=True, starts_at__gte=now
    ).select_related("resource")
    for booking in bookings:
        policy = booking.resource.offboarding_policy
        if policy == ResourceOffboardingPolicy.TRANSFER:
            new_owner = _resource_admin_for(booking.resource_id)
            if new_owner is None:
                logger.warning(
                    "offboarding_transfer_no_resource_admin",
                    extra={
                        "request_id": req.request_id,
                        "booking_id": str(booking.id),
                        "resource_id": str(booking.resource_id),
                    },
                )
                result.bookings_retained += 1
                continue
            transfer_booking_ownership(
                BookingTransferRequest(
                    booking=booking, new_owner=new_owner, request_id=req.request_id
                )
            )
            result.bookings_transferred += 1
        elif policy == ResourceOffboardingPolicy.CANCEL_AND_NOTIFY:
            cancel_booking(
                BookingCancelRequest(
                    booking=booking,
                    actor=req.actor,
                    actor_type=AuditActorType.SYSTEM,
                    reason=req.reason,
                    request_id=req.request_id,
                )
            )
            # Deferred, matching every other notification dispatch in
            # this codebase (RFC v1.0 §15a) — this whole function runs
            # inside run_idempotent_write's outer transaction, so a
            # direct call here would fire before the cascade is even
            # guaranteed to commit. functools.partial (not a lambda)
            # binds this ITERATION's booking values now, avoiding the
            # classic late-binding-closure bug a loop variable captured
            # by reference would otherwise cause.
            transaction.on_commit(
                functools.partial(
                    notify_admin_cancellation,
                    recipient=target,
                    resource_name=booking.resource.name,
                    start=booking.time_range.lower,
                    end=booking.time_range.upper,
                    reason=req.reason,
                    request_id=req.request_id,
                )
            )
            result.bookings_cancelled += 1
        else:  # RETAIN — left untouched, flagged only via this count.
            result.bookings_retained += 1

    # ================================================================
    # PRD FR50: outstanding offer/hold released immediately, so the
    # slot cascades rather than expiring uselessly. decline_offer
    # already releases the hold AND dispatches the SAME cascade worker
    # cancellation/an ordinary decline use (Phase 16/17) — reused
    # unchanged, only actor_type differs.
    # ================================================================
    active_offers = WaitlistOffer.objects.filter(
        waitlist_entry__user=target, status=WaitlistOfferStatus.ACTIVE
    )
    for offer in active_offers:
        decline_offer(
            DeclineOfferRequest(
                offer=offer,
                user=target,
                request_id=req.request_id,
                actor_type=AuditActorType.SYSTEM,
            )
        )
        result.offers_released += 1

    # ================================================================
    # PRD FR50: waitlist entries cancelled immediately (the 'offered'
    # ones above are handled via decline_offer instead — cancelling
    # them here too would double-count and race the entry status
    # decline_offer already sets to EXPIRED).
    # ================================================================
    waiting_entries = WaitlistEntry.objects.filter(user=target, status=WaitlistEntryStatus.WAITING)
    for entry in waiting_entries:
        cancel_waitlist_entry(
            WaitlistCancelRequest(
                entry=entry,
                actor=target,
                request_id=req.request_id,
                actor_type=AuditActorType.SYSTEM,
            )
        )
        result.waitlist_entries_cancelled += 1

    # ================================================================
    # PRD FR51: active recurring series flagged to the resource admin —
    # no schema field for "flagged" exists (Spec v1.0 §3 predates this
    # requirement), so the flag IS this response entry plus the
    # notification dispatched below, matching this project's own
    # "log/report now, full workflow later" precedent (Phase 13's
    # tzdata-drift check, Phase 17's reaper heartbeat). Series editing
    # (actually transferring/terminating) has no endpoint anywhere in
    # this codebase yet — see CLAUDE.md Open Questions.
    # ================================================================
    active_series = RecurringSeries.objects.filter(
        created_by=target, status=RecurringSeriesStatus.ACTIVE
    ).select_related("resource")
    for series in active_series:
        occurrences_remaining = Booking.objects.filter(
            series=series, status=BookingStatus.CONFIRMED, starts_at__gte=now
        ).count()
        result.series_flagged_for_admin.append(
            {
                "series_id": str(series.id),
                "resource_id": str(series.resource_id),
                "occurrences_remaining": occurrences_remaining,
            }
        )
        admin = _resource_admin_for(series.resource_id)
        if admin is None:
            logger.warning(
                "offboarding_series_flag_no_resource_admin",
                extra={"request_id": req.request_id, "series_id": str(series.id)},
            )
            continue
        transaction.on_commit(
            functools.partial(
                notify_series_owner_deactivated,
                recipient=admin,
                resource_name=series.resource.name,
                series_id=str(series.id),
                occurrences_remaining=occurrences_remaining,
                request_id=req.request_id,
            )
        )

    logger.info(
        "user_deactivated",
        extra={
            "request_id": req.request_id,
            "user_id": str(target.id),
            "bookings_transferred": result.bookings_transferred,
            "bookings_cancelled": result.bookings_cancelled,
            "bookings_retained": result.bookings_retained,
            "waitlist_entries_cancelled": result.waitlist_entries_cancelled,
            "offers_released": result.offers_released,
            "series_flagged": len(result.series_flagged_for_admin),
        },
    )
    return result
