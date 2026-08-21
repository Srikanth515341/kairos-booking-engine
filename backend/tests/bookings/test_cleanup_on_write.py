"""RECLAIM-01 — Cleanup-on-write makes the system self-healing ★ (Test Plan
v1.0 §4; Implementation Plan Phase 17). "Stop the reaper entirely" (Test
Plan's own setup) is satisfied trivially and honestly here: no reaper task
is ever invoked in this test at all — proving the DELETE inside
`create_booking` (kairos/bookings/services.py) is what clears the stale
hold, with nothing else running that could take credit for it.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking, BookingStatus
from kairos.bookings.services import BookingCreateRequest, create_booking
from kairos.core.models import AuditActorType
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"


def _headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
def test_reclaim_01_booking_succeeds_over_an_expired_hold_with_no_reaper_running(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    waiter = AppUser.objects.create(email="reclaim01-waiter@example.com", display_name="Waiter")
    stale_hold = Booking.objects.create(
        resource=active_resource,
        user=waiter,
        time_range=(start, end),
        status=BookingStatus.HELD,
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        },
        format="json",
        **_headers(app_user),
    )

    assert response.status_code == 201
    assert not Booking.objects.filter(id=stale_hold.id).exists(), (
        "the expired hold must be GONE (DELETEd), not merely superseded"
    )
    confirmed = Booking.objects.filter(resource=active_resource, status=BookingStatus.CONFIRMED)
    assert confirmed.count() == 1
    assert confirmed.get().user_id == app_user.id


@pytest.mark.django_db
def test_reclaim_01_unexpired_hold_is_never_touched(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """The DELETE is scoped to `expires_at <= now()` — a hold that hasn't
    expired yet must survive a write attempt over an OVERLAPPING range
    (which itself then correctly fails, since the hold still occupies the
    exclusion domain — HOLD-01's own guarantee, not re-tested here)."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    waiter = AppUser.objects.create(email="reclaim01-live@example.com", display_name="Waiter")
    live_hold = Booking.objects.create(
        resource=active_resource,
        user=waiter,
        time_range=(start, end),
        status=BookingStatus.HELD,
        expires_at=timezone.now() + timedelta(minutes=15),
    )

    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        },
        format="json",
        **_headers(app_user),
    )

    assert response.status_code == 409
    live_hold.refresh_from_db()
    assert live_hold.status == BookingStatus.HELD


@pytest.mark.django_db
def test_reclaim_01_only_deletes_holds_overlapping_the_written_range(
    app_user: AppUser, active_resource: Resource
) -> None:
    """Scoped to the resource AND the range being written (Spec v1.0
    §4.1 step 2's own "narrow, indexed delete rather than a table scan")
    — an expired hold on a DIFFERENT, non-overlapping range on the same
    resource must survive."""
    far_start = timezone.now() + timedelta(hours=5)
    far_end = far_start + timedelta(hours=1)
    waiter = AppUser.objects.create(email="reclaim01-far@example.com", display_name="Waiter")
    unrelated_expired_hold = Booking.objects.create(
        resource=active_resource,
        user=waiter,
        time_range=(far_start, far_end),
        status=BookingStatus.HELD,
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    near_start = timezone.now() + timedelta(hours=1)
    near_end = near_start + timedelta(hours=1)
    create_booking(
        BookingCreateRequest(
            resource=active_resource,
            user=app_user,
            start=near_start,
            end=near_end,
            request_id=str(uuid.uuid4()),
            actor_type=AuditActorType.USER,
        )
    )

    assert Booking.objects.filter(id=unrelated_expired_hold.id, status=BookingStatus.HELD).exists()
