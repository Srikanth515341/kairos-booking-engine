"""OFF-01, OFF-02 (Implementation Plan Phase 19; PRD v1.0 FR49-51; Spec
v1.0 §5.15). `POST /admin/users/{id}/deactivate` is exercised through the
real HTTP endpoint (`system_admin`-gated, `Idempotency-Key` required),
`captureOnCommitCallbacks(execute=True)` firing the same on_commit-
deferred cascade/notification dispatch chain `tests/waitlist/
test_offers.py` already established for cancellation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import pytest
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries
from kairos.core.models import AuditLog
from kairos.identity.models import AppUser, AppUserPlatformRole, AppUserStatus, ResourceAdmin
from kairos.resources.models import Resource, ResourceOffboardingPolicy
from kairos.waitlist.models import (
    WaitlistEntry,
    WaitlistEntryStatus,
    WaitlistOffer,
    WaitlistOfferStatus,
)

DEACTIVATE_URL = "/api/v1/admin/users/{id}/deactivate"


def _headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


def _resource(*, policy: str, created_by: AppUser) -> Resource:
    return Resource.objects.create(
        name=f"Room {uuid.uuid4().hex[:6]}",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        offboarding_policy=policy,
        created_by=created_by,
    )


def _booking(*, resource: Resource, user: AppUser, start: datetime) -> Booking:
    return Booking.objects.create(
        resource=resource, user=user, time_range=(start, start + timedelta(hours=1))
    )


def _seed_offer(
    resource: Resource, holder: AppUser, start: datetime, end: datetime
) -> tuple[Booking, WaitlistOffer, WaitlistEntry]:
    entry = WaitlistEntry.objects.create(resource=resource, user=holder, time_range=(start, end))
    hold = Booking.objects.create(
        resource=resource,
        user=holder,
        time_range=(start, end),
        status=BookingStatus.HELD,
        expires_at=timezone.now() + timedelta(minutes=15),
    )
    offer = WaitlistOffer.objects.create(
        waitlist_entry=entry,
        hold_booking=hold,
        resource=resource,
        time_range=(start, end),
        status=WaitlistOfferStatus.ACTIVE,
        expires_at=hold.expires_at,
    )
    WaitlistEntry.objects.filter(id=entry.id).update(status=WaitlistEntryStatus.OFFERED)
    return hold, offer, entry


@pytest.mark.django_db
def test_off_01_offboarding_applies_the_per_resource_policy() -> None:
    api = APIClient()

    admin_grantor = AppUser.objects.create(email="grantor@example.com", display_name="Grantor")
    system_admin = AppUser.objects.create(
        email="off01-sysadmin@example.com",
        display_name="Sys Admin",
        platform_role=AppUserPlatformRole.SYSTEM_ADMIN,
    )
    w = AppUser.objects.create(email="off01-w@example.com", display_name="W")
    resource_a_admin = AppUser.objects.create(
        email="off01-a-admin@example.com", display_name="A Admin"
    )
    resource_c_admin = AppUser.objects.create(
        email="off01-c-admin@example.com", display_name="C Admin"
    )

    resource_a = _resource(policy=ResourceOffboardingPolicy.TRANSFER, created_by=admin_grantor)
    ResourceAdmin.objects.create(
        resource=resource_a, user=resource_a_admin, granted_by=admin_grantor
    )
    resource_b = _resource(
        policy=ResourceOffboardingPolicy.CANCEL_AND_NOTIFY, created_by=admin_grantor
    )
    resource_c = _resource(policy=ResourceOffboardingPolicy.RETAIN, created_by=admin_grantor)
    ResourceAdmin.objects.create(
        resource=resource_c, user=resource_c_admin, granted_by=admin_grantor
    )
    resource_d = _resource(policy=ResourceOffboardingPolicy.RETAIN, created_by=admin_grantor)

    now = timezone.now()
    booking_a1 = _booking(resource=resource_a, user=w, start=now + timedelta(days=1))
    booking_a2 = _booking(resource=resource_a, user=w, start=now + timedelta(days=2))
    booking_b = _booking(resource=resource_b, user=w, start=now + timedelta(days=3))
    booking_c = _booking(resource=resource_c, user=w, start=now + timedelta(days=4))

    entries = [
        WaitlistEntry.objects.create(
            resource=resource_d,
            user=w,
            time_range=(now + timedelta(days=5 + i), now + timedelta(days=5 + i, hours=1)),
        )
        for i in range(3)
    ]

    hold_start = now + timedelta(days=10)
    hold_end = hold_start + timedelta(hours=1)
    hold, offer, hold_entry = _seed_offer(resource_d, w, hold_start, hold_end)
    # The next eligible candidate for the SAME freed range, proving the
    # release actually cascades (PRD FR50), not merely releases.
    next_candidate = AppUser.objects.create(email="off01-next@example.com", display_name="Next")
    next_entry = WaitlistEntry.objects.create(
        resource=resource_d, user=next_candidate, time_range=(hold_start, hold_end)
    )

    series = RecurringSeries.objects.create(
        resource=resource_a,
        created_by=w,
        timezone="UTC",
        local_start_time=time(10, 0),
        local_end_time=time(10, 30),
        weekday=0,
        series_start_date=(now + timedelta(days=7)).date(),
        occurrence_count=3,
        tzdata_version="2026.3",
        materialized_through=(now + timedelta(days=7)).date(),
    )

    reason = "Employee offboarding — ticket HR-4821"
    with TestCase.captureOnCommitCallbacks(execute=True):
        response = api.post(
            DEACTIVATE_URL.format(id=w.id),
            {"reason": reason},
            format="json",
            **_headers(system_admin),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(w.id)
    assert body["status"] == "deactivated"
    assert body["bookings_transferred"] == 2
    assert body["bookings_cancelled"] == 1
    assert body["bookings_retained"] == 1
    assert body["waitlist_entries_cancelled"] == 3
    assert body["offers_released"] == 1
    assert len(body["series_flagged_for_admin"]) == 1
    assert body["series_flagged_for_admin"][0]["series_id"] == str(series.id)

    # Resource A: transferred to its resource admin.
    booking_a1.refresh_from_db()
    booking_a2.refresh_from_db()
    assert booking_a1.user_id == resource_a_admin.id
    assert booking_a2.user_id == resource_a_admin.id
    assert booking_a1.status == BookingStatus.CONFIRMED

    # Resource B: cancelled, W notified.
    booking_b.refresh_from_db()
    assert booking_b.status == BookingStatus.CANCELLED
    assert any(m.to == [w.email] for m in mail.outbox)

    # Resource C: retained, untouched.
    booking_c.refresh_from_db()
    assert booking_c.status == BookingStatus.CONFIRMED
    assert booking_c.user_id == w.id

    # 3 waitlist entries cancelled.
    for entry in entries:
        entry.refresh_from_db()
        assert entry.status == WaitlistEntryStatus.CANCELLED

    # Outstanding hold released — not left to expire — and cascades to
    # the next eligible entry.
    hold.refresh_from_db()
    assert hold.status == BookingStatus.CANCELLED
    assert hold.expires_at is None
    hold_entry.refresh_from_db()
    assert hold_entry.status == WaitlistEntryStatus.EXPIRED
    assert WaitlistOffer.objects.filter(
        waitlist_entry=next_entry, status=WaitlistOfferStatus.ACTIVE
    ).exists()

    # Series flagged to its resource admin.
    assert any(m.to == [resource_a_admin.email] for m in mail.outbox)

    # No booking silently orphaned — every one of W's one-off bookings is
    # in a defined post-offboarding state.
    for booking in (booking_a1, booking_a2, booking_b, booking_c):
        booking.refresh_from_db()
        assert booking.status in (BookingStatus.CONFIRMED, BookingStatus.CANCELLED)

    # Every cascade action audited as actor_type='system' with the
    # offboarding reason.
    for booking in (booking_a1, booking_a2, booking_b):
        row = AuditLog.objects.filter(
            entity_type="booking", entity_id=booking.id, action="update"
        ).latest("occurred_at")
        assert row.actor_type == "system"
    cancel_row = AuditLog.objects.filter(
        entity_type="booking", entity_id=booking_b.id, action="update"
    ).latest("occurred_at")
    assert cancel_row.reason == reason


@pytest.mark.django_db
def test_off_01_repeat_deactivation_is_idempotent_no_op() -> None:
    api = APIClient()
    system_admin = AppUser.objects.create(
        email="off01-repeat-admin@example.com",
        display_name="Sys Admin",
        platform_role=AppUserPlatformRole.SYSTEM_ADMIN,
    )
    w = AppUser.objects.create(
        email="off01-repeat-w@example.com",
        display_name="W",
        status=AppUserStatus.DEACTIVATED,
        deactivated_at=timezone.now(),
    )

    with TestCase.captureOnCommitCallbacks(execute=True):
        response = api.post(
            DEACTIVATE_URL.format(id=w.id),
            {"reason": "already gone"},
            format="json",
            **_headers(system_admin),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deactivated"
    assert body["bookings_transferred"] == 0
    assert body["bookings_cancelled"] == 0


# --------------------------------------------------------------------
# OFF-02 — a deactivated user cannot book, waitlist, or confirm.
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_off_02_deactivated_user_cannot_book(active_resource: Resource) -> None:
    api = APIClient()
    deactivated = AppUser.objects.create(
        email="off02-booker@example.com",
        display_name="Deactivated",
        status=AppUserStatus.DEACTIVATED,
        deactivated_at=timezone.now(),
    )
    start = timezone.now() + timedelta(hours=1)
    response = api.post(
        "/api/v1/bookings",
        {
            "resource_id": str(active_resource.id),
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
        },
        format="json",
        **_headers(deactivated),
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_off_02_deactivated_user_cannot_join_waitlist(active_resource: Resource) -> None:
    api = APIClient()
    deactivated = AppUser.objects.create(
        email="off02-waiter@example.com",
        display_name="Deactivated",
        status=AppUserStatus.DEACTIVATED,
        deactivated_at=timezone.now(),
    )
    start = timezone.now() + timedelta(hours=1)
    response = api.post(
        "/api/v1/waitlist-entries",
        {
            "resource_id": str(active_resource.id),
            "start": start.isoformat(),
            "end": (start + timedelta(hours=1)).isoformat(),
        },
        format="json",
        **_headers(deactivated),
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_off_02_deactivated_user_cannot_confirm_an_offer(active_resource: Resource) -> None:
    api = APIClient()
    deactivated = AppUser.objects.create(
        email="off02-confirmer@example.com",
        display_name="Deactivated",
    )
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    _hold, offer, _entry = _seed_offer(active_resource, deactivated, start, end)

    # Deactivate AFTER the offer exists, mirroring "a user is offboarded
    # while an offer is outstanding" rather than one that could never
    # have received it in the first place.
    AppUser.objects.filter(id=deactivated.id).update(
        status=AppUserStatus.DEACTIVATED, deactivated_at=timezone.now()
    )

    response = api.post(
        f"/api/v1/waitlist-offers/{offer.id}/confirm",
        {},
        format="json",
        **_headers(deactivated),
    )
    assert response.status_code == 401
