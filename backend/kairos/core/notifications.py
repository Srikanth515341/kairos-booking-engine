"""NotificationService (Implementation Plan Phase 18; PRD v1.0 FR52-55;
RFC v1.0 §15a) — delivers offer, admin-cancellation, tzdata
re-materialization, and rollback-hold-release notifications.

Delivery reuses Django's own `EMAIL_BACKEND` abstraction (console in dev,
SMTP in prod, `locmem` — Django's own capturing backend, `django.core.
mail.outbox` — in test; see kairos/settings/{dev,test,prod}.py) instead
of inventing a parallel one: `EMAIL_BACKEND` already IS the pluggable
console/SMTP/capturing seam this phase asks for, and reusing it means a
delivery failure raises the exact exception class Django's own backend
raises, not a bespoke wrapper exception the retry logic downstream would
need to reason about separately. The same "reuse a framework/database
guarantee instead of reinventing it" choice this project has made
repeatedly (CompositePrimaryKey, set_config over literal SET, Postgres
triggers over an application-level audit call).

Every `notify_*` function here only BUILDS a message and hands it to
`kairos.core.tasks.dispatch_notification`, which enqueues
`send_notification_task.delay(...)` and returns immediately — the actual
`send_mail()` call happens inside that Celery task, in a worker process,
never inline in an HTTP request thread and never inside a request-path
transaction (RFC v1.0 §15a's "called from workers, never synchronously
from the request path").
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import F
from django.utils import timezone as django_timezone

from kairos.core.models import NotificationLog, NotificationStatus, NotificationType
from kairos.identity.models import AppUser

logger = logging.getLogger(__name__)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class NotificationService:
    """The console/SMTP/capturing "interface" this phase names — a thin
    wrapper over `django.core.mail.send_mail`, not a parallel backend
    abstraction. Called ONLY from inside `send_notification_task`
    (kairos.core.tasks), never directly by a `notify_*` builder function
    below or by any request-path code.
    """

    @staticmethod
    def send(*, recipient_email: str, subject: str, body: str) -> None:
        # fail_silently=False (the default) is load-bearing: a delivery
        # failure MUST raise so send_notification_task's retry/record
        # logic (PRD FR55) actually observes it, rather than Django's
        # backend swallowing it and reporting success either way.
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [recipient_email])


def _execute_notification_delivery(
    *,
    notification_id: str,
    notification_type: str,
    recipient_user_id: str,
    recipient_email: str,
    subject: str,
    body: str,
    context: dict[str, Any],
    request_id: str,
) -> None:
    """The delivery attempt itself — one call per Celery attempt (the
    task body calls this directly; Celery's own retry_backoff schedules
    the NEXT attempt, which calls this again with the SAME
    `notification_id`). Kept as a plain function, not inlined into the
    `@shared_task` in kairos/core/tasks.py, so a test can drive two
    attempts back to back (a failure then a success) without depending
    on Celery's own retry scheduling/timing at all — the same "test the
    mechanism directly" precedent `expand_occurrences` (Phase 11) and
    `reap_expired_holds` (Phase 17) already established for logic that's
    awkward to exercise through the real scheduler.

    `get_or_create` on the FIRST call, then `filter(...).update(...)` on
    every call thereafter — one row per logical notification survives
    across every retry, its `attempts` counter accumulating, rather than
    one row per attempt (which would make "was this notification ever
    delivered" a query over N rows instead of one).
    """
    log, _created = NotificationLog.objects.get_or_create(
        id=notification_id,
        defaults={
            "notification_type": notification_type,
            "recipient_user_id": recipient_user_id,
            "recipient_email": recipient_email,
            "subject": subject,
            "body": body,
            "context": context,
            "request_id": request_id,
        },
    )
    NotificationLog.objects.filter(id=log.id).update(attempts=F("attempts") + 1)

    try:
        NotificationService.send(recipient_email=recipient_email, subject=subject, body=body)
    except Exception as exc:
        NotificationLog.objects.filter(id=log.id).update(
            status=NotificationStatus.FAILED, last_error=str(exc)
        )
        logger.warning(
            "notification_delivery_failed",
            extra={
                "request_id": request_id,
                "notification_id": notification_id,
                "notification_type": notification_type,
            },
            exc_info=True,
        )
        raise  # re-raised so Celery's autoretry_for/retry_backoff schedules the next attempt

    NotificationLog.objects.filter(id=log.id).update(
        status=NotificationStatus.SENT, sent_at=django_timezone.now()
    )
    logger.info(
        "notification_delivered",
        extra={
            "request_id": request_id,
            "notification_id": notification_id,
            "notification_type": notification_type,
        },
    )


def _notify(
    *,
    recipient: AppUser,
    notification_type: str,
    subject: str,
    body: str,
    context: dict[str, Any],
    request_id: str,
) -> None:
    # Deferred import: kairos.core.tasks imports FROM this module
    # (dispatch_notification needs nothing here, but send_notification_task
    # wraps _execute_notification_delivery above) — importing it at this
    # module's top level would risk the same circular-import shape
    # kairos.waitlist.tasks/services already had to break with a
    # deferred, call-time-only import (Phase 16).
    from kairos.core.tasks import dispatch_notification

    dispatch_notification(
        notification_id=str(uuid.uuid4()),
        notification_type=notification_type,
        recipient_user_id=str(recipient.id),
        recipient_email=recipient.email,
        subject=subject,
        body=body,
        context=context,
        request_id=request_id,
    )


def notify_offer_created(
    *,
    recipient: AppUser,
    resource_name: str,
    start: datetime,
    end: datetime,
    expires_at: datetime,
    request_id: str,
) -> None:
    """PRD FR52: "must state the expiry time explicitly" — the expiry is
    in the subject line itself, not buried in the body, so it survives
    even a truncated notification preview.
    """
    subject = f"A slot is available for {resource_name} — respond by {_iso(expires_at)}"
    body = (
        f"{resource_name} is available for {_iso(start)} to {_iso(end)}.\n\n"
        f"This offer expires at {_iso(expires_at)}. Confirm before then or it will be "
        "released to the next person on the waitlist."
    )
    _notify(
        recipient=recipient,
        notification_type=NotificationType.OFFER_CREATED,
        subject=subject,
        body=body,
        context={
            "resource_name": resource_name,
            "start": _iso(start),
            "end": _iso(end),
            "expires_at": _iso(expires_at),
        },
        request_id=request_id,
    )


def notify_admin_cancellation(
    *,
    recipient: AppUser,
    resource_name: str,
    start: datetime,
    end: datetime,
    reason: str,
    request_id: str,
) -> None:
    """PRD FR53: "must be notified, including the recorded reason." Only
    reachable for an actual administrative override (kairos.bookings.
    services.cancel_booking, actor_type=ADMIN) — a self-cancel never
    reaches this function at all.
    """
    subject = f"Your booking for {resource_name} was cancelled by an administrator"
    body = (
        f"Your booking for {resource_name} ({_iso(start)} to {_iso(end)}) was cancelled by "
        f"an administrator.\n\nReason given: {reason}"
    )
    _notify(
        recipient=recipient,
        notification_type=NotificationType.ADMIN_CANCELLATION,
        subject=subject,
        body=body,
        context={
            "resource_name": resource_name,
            "start": _iso(start),
            "end": _iso(end),
            "reason": reason,
        },
        request_id=request_id,
    )


def notify_rematerialization(
    *,
    recipient: AppUser,
    resource_name: str,
    old_start: datetime,
    old_end: datetime,
    new_start: datetime,
    new_end: datetime,
    request_id: str,
) -> None:
    """PRD FR54: "must be notified of the change to their occurrence
    times." Fires only when a tzdata rule change actually moved an
    occurrence's stored instant (kairos.bookings.tasks.
    rematerialize_stale_series) — an unaffected occurrence in the same
    series generates no notification at all.
    """
    subject = f"A recurring booking time for {resource_name} has changed"
    body = (
        f"A timezone rule update changed one of your recurring booking times for "
        f"{resource_name}.\n\n"
        f"Previously: {_iso(old_start)} to {_iso(old_end)}\n"
        f"Now: {_iso(new_start)} to {_iso(new_end)}\n\n"
        "The booked duration is unchanged; only the underlying UTC instant was recomputed."
    )
    _notify(
        recipient=recipient,
        notification_type=NotificationType.TZDATA_REMATERIALIZATION,
        subject=subject,
        body=body,
        context={
            "resource_name": resource_name,
            "old_start": _iso(old_start),
            "old_end": _iso(old_end),
            "new_start": _iso(new_start),
            "new_end": _iso(new_end),
        },
        request_id=request_id,
    )


def notify_rematerialization_conflict(
    *,
    recipient: AppUser,
    resource_name: str,
    occurrence_date: str,
    request_id: str,
) -> None:
    """PRD FR13b: a re-materialization conflict is surfaced for HUMAN
    decision, not auto-resolved. Sent to both the series owner and every
    resource administrator (kairos.bookings.tasks.
    rematerialize_stale_series calls this once per recipient) — closing
    the "resource_administrators: pending Phase 18 delivery" placeholder
    that function's own conflict-recording has carried since Phase 13.
    """
    subject = f"Action needed: a recurring booking for {resource_name} could not be updated"
    body = (
        f"A timezone rule update could not be automatically applied to a recurring booking "
        f"for {resource_name} on {occurrence_date}, because the recomputed time now conflicts "
        "with another booking.\n\n"
        "This occurrence has been left at its original (now incorrect) time. Please review "
        "and resolve it manually."
    )
    _notify(
        recipient=recipient,
        notification_type=NotificationType.TZDATA_REMATERIALIZATION_CONFLICT,
        subject=subject,
        body=body,
        context={"resource_name": resource_name, "occurrence_date": occurrence_date},
        request_id=request_id,
    )


def notify_series_owner_deactivated(
    *,
    recipient: AppUser,
    resource_name: str,
    series_id: str,
    occurrences_remaining: int,
    request_id: str,
) -> None:
    """PRD FR51 / Implementation Plan Phase 19: a recurring series' owner
    was deactivated — sent to the resource administrator(s) (kairos.
    identity.services.deactivate_user), never to the deactivated owner
    themselves (they no longer have an active account to act on it).
    """
    subject = f"Action needed: a recurring series for {resource_name} needs a new owner"
    body = (
        f"The owner of a recurring booking series for {resource_name} ({occurrences_remaining} "
        "occurrence(s) remaining) has been deactivated.\n\n"
        "Please transfer ownership of this series to another user or terminate it — it will "
        "otherwise keep materializing new occurrences for a principal who is no longer active."
    )
    _notify(
        recipient=recipient,
        notification_type=NotificationType.SERIES_OWNER_DEACTIVATED,
        subject=subject,
        body=body,
        context={
            "resource_name": resource_name,
            "series_id": series_id,
            "occurrences_remaining": occurrences_remaining,
        },
        request_id=request_id,
    )


def notify_rollback_hold_released(
    *,
    recipient: AppUser,
    resource_name: str,
    start: datetime,
    end: datetime,
    request_id: str,
) -> None:
    """Rollout v1.0 §4.5: "An offer that lapsed because of a system
    rollback must not be indistinguishable from an ordinary expiry. Tell
    them plainly what happened." Deliberately does NOT reuse
    `notify_offer_created`'s "expires at" framing or any expiry-sounding
    language — Rollout's own requirement is that this reads as an
    operational event that happened TO the user, not a deadline the user
    missed. Also states explicitly that queue position was preserved
    (Rollout §4.5 step 4), which an ordinary expiry notification has no
    reason to mention at all.

    No production caller exists yet (see kairos.core.models.
    NotificationType.ROLLBACK_HOLD_RELEASED's docstring and CLAUDE.md's
    Open Questions) — Rollout §4.5's hold release is a manual runbook
    procedure, not automated by any phase. Exercised directly, with a
    manually-constructed event, the same "mechanism proven ahead of its
    real caller" pattern already used repeatedly in this project
    (actor_type='system', containment eligibility, hold acceptance).
    """
    subject = f"Your reserved offer for {resource_name} was released — system maintenance"
    body = (
        f"Your held offer for {resource_name} ({_iso(start)} to {_iso(end)}) was released "
        "because of a system rollback, not because you missed the response window.\n\n"
        "Your place in the waitlist queue has been preserved. You will be offered this or "
        "another matching slot again once the system is confirmed healthy."
    )
    _notify(
        recipient=recipient,
        notification_type=NotificationType.ROLLBACK_HOLD_RELEASED,
        subject=subject,
        body=body,
        context={"resource_name": resource_name, "start": _iso(start), "end": _iso(end)},
        request_id=request_id,
    )
