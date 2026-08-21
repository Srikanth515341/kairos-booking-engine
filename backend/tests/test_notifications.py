"""NotificationService and the four notification points (Implementation
Plan Phase 18; PRD v1.0 FR52-55; RFC v1.0 §15a).

`kairos.settings.test` sets `EMAIL_BACKEND` to Django's own `locmem`
backend — every `send_mail()` call appends to `django.core.mail.outbox`
in-process — combined with `CELERY_TASK_ALWAYS_EAGER` (Phase 16), so
`send_notification_task.delay(...)` runs synchronously here without a
real broker or SMTP server, the same "test the mechanism, not the
transport" approach `test_tzdata_check.py`/`test_rematerialization.py`
already established for other background jobs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries
from kairos.bookings.services import BookingCancelRequest, cancel_booking
from kairos.bookings.tasks import rematerialize_stale_series
from kairos.core.constants import NOTIFICATION_MAX_RETRIES
from kairos.core.models import AuditActorType, NotificationLog, NotificationStatus, NotificationType
from kairos.core.notifications import (
    NotificationService,
    _execute_notification_delivery,
    notify_admin_cancellation,
    notify_offer_created,
    notify_rematerialization,
    notify_rollback_hold_released,
)
from kairos.core.tasks import dispatch_notification, send_notification_task
from kairos.identity.models import AppUser, ResourceAdmin
from kairos.resources.models import Resource
from kairos.waitlist.models import WaitlistEntry

pytestmark = pytest.mark.django_db


def _booking(*, resource: Resource, user: AppUser, start: datetime, end: datetime) -> Booking:
    return Booking.objects.create(resource=resource, user=user, time_range=(start, end))


# ==========================================================
# The four notification points — capturing-backend content proofs.
# ==========================================================


def test_offer_created_notification_states_expiry_explicitly(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    expires_at = timezone.now() + timedelta(minutes=15)

    notify_offer_created(
        recipient=app_user,
        resource_name=active_resource.name,
        start=start,
        end=end,
        expires_at=expires_at,
        request_id="req-1",
    )

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [app_user.email]
    # PRD FR52: the expiry must be stated EXPLICITLY, not implied.
    expires_iso = expires_at.isoformat().replace("+00:00", "Z")
    assert expires_iso in sent.subject
    assert expires_iso in sent.body

    log = NotificationLog.objects.get(notification_type=NotificationType.OFFER_CREATED)
    assert log.status == NotificationStatus.SENT
    assert log.context["expires_at"] == expires_iso


def test_admin_cancellation_notification_includes_the_reason(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)

    notify_admin_cancellation(
        recipient=app_user,
        resource_name=active_resource.name,
        start=start,
        end=end,
        reason="Resource undergoing emergency maintenance",
        request_id="req-2",
    )

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == [app_user.email]
    # PRD FR53: "must be notified, including the recorded reason."
    assert "Resource undergoing emergency maintenance" in sent.body


def test_rematerialization_notification_states_old_and_new_times(
    app_user: AppUser, active_resource: Resource
) -> None:
    old_start = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    old_end = old_start + timedelta(minutes=30)
    new_start = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    new_end = new_start + timedelta(minutes=30)

    notify_rematerialization(
        recipient=app_user,
        resource_name=active_resource.name,
        old_start=old_start,
        old_end=old_end,
        new_start=new_start,
        new_end=new_end,
        request_id="req-3",
    )

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    # PRD FR54: "notified of the change to their occurrence times" — both
    # the old and new instant must be present, not just the new one, or
    # the recipient has no way to tell WHAT changed.
    assert "2026-11-08T16:00:00Z" in sent.body
    assert "2026-11-08T15:00:00Z" in sent.body


def test_rollback_hold_released_reads_distinctly_from_an_ordinary_expiry(
    app_user: AppUser, active_resource: Resource
) -> None:
    """Rollout v1.0 §4.5: "An offer that lapsed because of a system
    rollback must not be indistinguishable from an ordinary expiry."
    Exercised standalone with a manually-constructed event — Rollout
    §4.5's hold release is a manual runbook procedure with no application
    caller (see kairos.core.models.NotificationType.
    ROLLBACK_HOLD_RELEASED's docstring and CLAUDE.md Open Questions).
    """
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)

    notify_rollback_hold_released(
        recipient=app_user,
        resource_name=active_resource.name,
        start=start,
        end=end,
        request_id="req-4",
    )
    rollback_body = mail.outbox[0].body.lower()
    rollback_subject = mail.outbox[0].subject.lower()

    mail.outbox.clear()
    notify_offer_created(
        recipient=app_user,
        resource_name=active_resource.name,
        start=start,
        end=end,
        expires_at=end,
        request_id="req-5",
    )
    ordinary_body = mail.outbox[0].body.lower()
    ordinary_subject = mail.outbox[0].subject.lower()

    # Distinct wording, not a shared template with a substituted noun:
    # the rollback message must name the rollback explicitly and must
    # NOT read like a deadline the user missed.
    assert "rollback" in rollback_body or "rollback" in rollback_subject
    assert "rollback" not in ordinary_body and "rollback" not in ordinary_subject
    assert "expire" not in rollback_body and "expire" not in rollback_subject
    # Rollout §4.5 step 4: queue position is explicitly preserved — an
    # ordinary expiry notification has no reason to say this at all.
    assert "queue" in rollback_body or "place" in rollback_body


# ==========================================================
# PRD FR55 — "must be recorded and retried."
# ==========================================================


def test_delivery_failure_then_success_is_recorded_and_the_same_row_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives two attempts of the SAME logical notification directly
    (bypassing Celery's own retry scheduling entirely — see
    kairos.core.notifications._execute_notification_delivery's own
    docstring for why) to prove the DATA mechanism: one row, `attempts`
    accumulating, `status` reflecting the latest outcome.
    """
    notification_id = str(uuid.uuid4())
    kwargs = {
        "notification_id": notification_id,
        "notification_type": NotificationType.OFFER_CREATED,
        "recipient_user_id": str(uuid.uuid4()),
        "recipient_email": "someone@example.com",
        "subject": "subject",
        "body": "body",
        "context": {},
        "request_id": "req-retry",
    }

    def _boom(**_: object) -> None:
        raise TimeoutError("simulated SMTP outage")

    monkeypatch.setattr(NotificationService, "send", staticmethod(_boom))
    with pytest.raises(TimeoutError):
        _execute_notification_delivery(**kwargs)  # type: ignore[arg-type]

    log = NotificationLog.objects.get(id=notification_id)
    assert log.status == NotificationStatus.FAILED
    assert log.attempts == 1
    assert "simulated SMTP outage" in (log.last_error or "")

    monkeypatch.undo()
    _execute_notification_delivery(**kwargs)  # type: ignore[arg-type]

    log.refresh_from_db()
    assert log.status == NotificationStatus.SENT
    assert log.attempts == 2  # accumulated across both attempts, not reset
    assert log.sent_at is not None
    assert len(mail.outbox) == 1  # only the successful attempt actually sent mail


def test_send_notification_task_is_configured_to_retry_with_backoff() -> None:
    """A regression guard, not the full proof — the same style as
    `tests/waitlist/test_dispatch_cascade.py` (Phase 17): asserts the
    retry MECHANISM is wired up rather than exercising Celery's own
    scheduling/timing, which `CELERY_TASK_ALWAYS_EAGER` makes awkward to
    observe faithfully under pytest in the first place.
    """
    assert send_notification_task.autoretry_for == (Exception,)
    assert send_notification_task.retry_backoff is True
    assert send_notification_task.max_retries == NOTIFICATION_MAX_RETRIES


# ==========================================================
# "A simulated provider outage does not fail the underlying operation."
# ==========================================================


def test_admin_cancellation_commits_even_if_notification_dispatch_fails(
    monkeypatch: pytest.MonkeyPatch, app_user: AppUser, active_resource: Resource
) -> None:
    admin = AppUser.objects.create(email="outage-admin@example.com", display_name="Admin")
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = _booking(resource=active_resource, user=app_user, start=start, end=end)

    def _boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("simulated notification-provider outage")

    monkeypatch.setattr("kairos.core.tasks.send_notification_task.delay", _boom)

    with TestCase.captureOnCommitCallbacks(execute=True):
        result = cancel_booking(
            BookingCancelRequest(
                booking=booking,
                actor=admin,
                actor_type=AuditActorType.ADMIN,
                reason="Emergency maintenance",
                request_id="req-outage",
            )
        )

    # The cancellation itself is unaffected — PRD FR55's literal
    # requirement — even though the notification dispatch just raised.
    assert result.booking.status == BookingStatus.CANCELLED
    assert not result.already_cancelled
    booking.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED


def test_dispatch_notification_swallows_a_broker_failure_and_logs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("simulated broker outage")

    monkeypatch.setattr(send_notification_task, "delay", _boom)

    with caplog.at_level(logging.ERROR, logger="kairos.core.tasks"):
        dispatch_notification(
            notification_id=str(uuid.uuid4()),
            notification_type=NotificationType.OFFER_CREATED,
            recipient_user_id=str(uuid.uuid4()),
            recipient_email="someone@example.com",
            subject="subject",
            body="body",
            context={},
            request_id="req-broker",
        )

    assert any(
        r.message == "notification_dispatch_failed_broker_unavailable" for r in caplog.records
    )


# ==========================================================
# Wiring — each notification point actually fires from its real caller.
# ==========================================================


def test_offer_created_fires_when_cancellation_cascades_to_a_waitlist_entry(
    app_user: AppUser, active_resource: Resource
) -> None:
    waiting_user = AppUser.objects.create(email="waiting@example.com", display_name="Waiter")
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = _booking(resource=active_resource, user=app_user, start=start, end=end)

    WaitlistEntry.objects.create(
        resource=active_resource, user=waiting_user, time_range=(start, end)
    )

    with TestCase.captureOnCommitCallbacks(execute=True):
        cancel_booking(
            BookingCancelRequest(
                booking=booking,
                actor=app_user,
                actor_type=AuditActorType.USER,
                reason=None,
                request_id="req-cascade",
            )
        )

    assert NotificationLog.objects.filter(
        notification_type=NotificationType.OFFER_CREATED, recipient_user_id=waiting_user.id
    ).exists()
    assert any(m.to == [waiting_user.email] for m in mail.outbox)


def test_admin_cancellation_notification_fires_only_for_a_real_admin_override(
    app_user: AppUser, active_resource: Resource
) -> None:
    admin = AppUser.objects.create(email="real-admin@example.com", display_name="Admin")
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = _booking(resource=active_resource, user=app_user, start=start, end=end)

    with TestCase.captureOnCommitCallbacks(execute=True):
        cancel_booking(
            BookingCancelRequest(
                booking=booking,
                actor=admin,
                actor_type=AuditActorType.ADMIN,
                reason="Facility closed for repairs",
                request_id="req-admin-cancel",
            )
        )

    assert NotificationLog.objects.filter(
        notification_type=NotificationType.ADMIN_CANCELLATION, recipient_user_id=app_user.id
    ).exists()


def test_no_admin_cancellation_notification_on_a_self_cancel(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = _booking(resource=active_resource, user=app_user, start=start, end=end)

    with TestCase.captureOnCommitCallbacks(execute=True):
        cancel_booking(
            BookingCancelRequest(
                booking=booking,
                actor=app_user,
                actor_type=AuditActorType.USER,
                reason=None,
                request_id="req-self-cancel",
            )
        )

    assert not NotificationLog.objects.filter(
        notification_type=NotificationType.ADMIN_CANCELLATION
    ).exists()


def test_rematerialization_notification_fires_on_a_successful_recompute(
    app_user: AppUser, active_resource: Resource
) -> None:
    occurrence_date = date(2026, 11, 8)
    stale_instant = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    series = RecurringSeries.objects.create(
        resource=active_resource,
        created_by=app_user,
        timezone="America/New_York",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        weekday=0,
        series_start_date=occurrence_date,
        occurrence_count=1,
        tzdata_version="2020.1",
        materialized_through=occurrence_date,
    )
    Booking.objects.create(
        resource=active_resource,
        user=app_user,
        series=series,
        time_range=(stale_instant, stale_instant + timedelta(minutes=30)),
    )

    rematerialize_stale_series(now=timezone.now())

    assert NotificationLog.objects.filter(
        notification_type=NotificationType.TZDATA_REMATERIALIZATION, recipient_user_id=app_user.id
    ).exists()


def test_rematerialization_conflict_notification_reaches_owner_and_resource_admin(
    app_user: AppUser, active_resource: Resource
) -> None:
    admin = AppUser.objects.create(email="rematz-admin@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    occurrence_date = date(2026, 11, 8)
    correct_instant = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    stale_instant = datetime(2026, 11, 8, 16, 0, tzinfo=UTC)
    series = RecurringSeries.objects.create(
        resource=active_resource,
        created_by=app_user,
        timezone="America/New_York",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        weekday=0,
        series_start_date=occurrence_date,
        occurrence_count=1,
        tzdata_version="2020.1",
        materialized_through=occurrence_date,
    )
    Booking.objects.create(
        resource=active_resource,
        user=app_user,
        series=series,
        time_range=(stale_instant, stale_instant + timedelta(minutes=30)),
    )
    other_user = AppUser.objects.create(email="rematz-other@example.com", display_name="Other")
    Booking.objects.create(
        resource=active_resource,
        user=other_user,
        time_range=(correct_instant, correct_instant + timedelta(minutes=30)),
    )

    rematerialize_stale_series(now=timezone.now())

    conflict_notifications = NotificationLog.objects.filter(
        notification_type=NotificationType.TZDATA_REMATERIALIZATION_CONFLICT
    )
    recipients = {n.recipient_user_id for n in conflict_notifications}
    assert recipients == {app_user.id, admin.id}
