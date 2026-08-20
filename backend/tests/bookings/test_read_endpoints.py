"""GET /api/v1/bookings/{id} and GET /api/v1/bookings (Spec v1.0 §5.2,
§5.4; Implementation Plan Phase 6).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking, BookingStatus
from kairos.identity.models import AppUser, ResourceAdmin
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


def _create_booking(resource: Resource, user: AppUser, start: datetime, hours: int = 1) -> Booking:
    return Booking.objects.create(
        resource=resource, user=user, time_range=(start, start + timedelta(hours=hours))
    )


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------------
# GET /bookings/{id}
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_owner_can_get_own_booking(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    booking = _create_booking(active_resource, app_user, start)

    response = client.get(f"{BOOKINGS_URL}/{booking.id}", **_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(booking.id)
    assert body["status"] == "confirmed"


@pytest.mark.django_db
def test_resource_admin_can_get_others_booking(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    owner = AppUser.objects.create(email="owner-detail@example.com", display_name="Owner")
    admin = AppUser.objects.create(email="admin-detail@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)
    start = timezone.now() + timedelta(hours=1)
    booking = _create_booking(active_resource, owner, start)

    response = client.get(f"{BOOKINGS_URL}/{booking.id}", **_headers(admin))
    assert response.status_code == 200
    assert response.json()["id"] == str(booking.id)


@pytest.mark.django_db
def test_operations_can_get_any_booking(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    ops = AppUser.objects.create(
        email="ops-detail@example.com",
        display_name="Ops",
        platform_role=AppUser.PlatformRole.OPERATIONS,
    )
    start = timezone.now() + timedelta(hours=1)
    booking = _create_booking(active_resource, app_user, start)

    response = client.get(f"{BOOKINGS_URL}/{booking.id}", **_headers(ops))
    assert response.status_code == 200
    assert response.json()["id"] == str(booking.id)


@pytest.mark.django_db
def test_non_owner_non_admin_gets_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    stranger = AppUser.objects.create(email="stranger@example.com", display_name="Stranger")
    start = timezone.now() + timedelta(hours=1)
    booking = _create_booking(active_resource, app_user, start)

    response = client.get(f"{BOOKINGS_URL}/{booking.id}", **_headers(stranger))
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    # SEC-01 partial: nothing about the booking leaks into a 404 body.
    assert str(booking.id) not in response.content.decode()
    assert str(booking.user_id) not in response.content.decode()


@pytest.mark.django_db
def test_nonexistent_booking_returns_404(client: APIClient, app_user: AppUser) -> None:
    response = client.get(f"{BOOKINGS_URL}/{uuid.uuid4()}", **_headers(app_user))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------
# GET /bookings (list)
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_list_returns_only_own_bookings_and_excludes_held(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    other_user = AppUser.objects.create(email="other-list@example.com", display_name="Other")
    start = timezone.now() + timedelta(hours=1)

    mine = _create_booking(active_resource, app_user, start)
    _create_booking(active_resource, other_user, start + timedelta(hours=5))
    # A held row — status='held' is a reservation, never returned here
    # (Spec v1.0 §5.4). Created directly via the ORM since hold creation
    # doesn't exist as an API path until Phase 15.
    Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(start + timedelta(hours=10), start + timedelta(hours=11)),
        status=BookingStatus.HELD,
        expires_at=timezone.now() + timedelta(minutes=15),
    )

    response = client.get(BOOKINGS_URL, **_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    ids = [row["id"] for row in body["data"]]
    assert ids == [str(mine.id)]
    assert all(row["status"] != "held" for row in body["data"])


@pytest.mark.django_db
def test_list_by_resource_id_requires_admin_or_operations(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="outsider@example.com", display_name="Outsider")
    response = client.get(
        BOOKINGS_URL, {"resource_id": str(active_resource.id)}, **_headers(outsider)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


@pytest.mark.django_db
def test_list_by_resource_id_as_admin_succeeds(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    admin = AppUser.objects.create(email="admin-list@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)
    start = timezone.now() + timedelta(hours=1)
    booking = _create_booking(active_resource, app_user, start)

    response = client.get(BOOKINGS_URL, {"resource_id": str(active_resource.id)}, **_headers(admin))
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [str(booking.id)]


@pytest.mark.django_db
def test_list_time_filters(client: APIClient, app_user: AppUser, active_resource: Resource) -> None:
    past = _create_booking(active_resource, app_user, timezone.now() - timedelta(days=10))
    future = _create_booking(active_resource, app_user, timezone.now() + timedelta(days=10))

    upcoming = client.get(BOOKINGS_URL, {"time": "upcoming"}, **_headers(app_user))
    assert [r["id"] for r in upcoming.json()["data"]] == [str(future.id)]

    past_resp = client.get(BOOKINGS_URL, {"time": "past"}, **_headers(app_user))
    assert [r["id"] for r in past_resp.json()["data"]] == [str(past.id)]

    all_resp = client.get(BOOKINGS_URL, {"time": "all"}, **_headers(app_user))
    assert {r["id"] for r in all_resp.json()["data"]} == {str(past.id), str(future.id)}


@pytest.mark.django_db
def test_list_pagination_covers_all_rows_without_duplicates(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    base = timezone.now() + timedelta(days=1)
    created = [
        _create_booking(active_resource, app_user, base + timedelta(hours=i)) for i in range(5)
    ]

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous upper bound on page count
        params = {"limit": "2"}
        if cursor:
            params["cursor"] = cursor
        response = client.get(BOOKINGS_URL, params, **_headers(app_user))
        assert response.status_code == 200
        body = response.json()
        seen.extend(row["id"] for row in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == [str(b.id) for b in created]
    assert len(seen) == len(set(seen))


@pytest.mark.django_db
def test_list_cursor_pagination_stable_under_concurrent_insert(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    base = timezone.now() + timedelta(days=1)
    first = _create_booking(active_resource, app_user, base)
    second = _create_booking(active_resource, app_user, base + timedelta(hours=1))
    fourth = _create_booking(active_resource, app_user, base + timedelta(hours=3))

    page1 = client.get(BOOKINGS_URL, {"limit": "2"}, **_headers(app_user))
    assert [r["id"] for r in page1.json()["data"]] == [str(first.id), str(second.id)]
    cursor = page1.json()["next_cursor"]
    assert cursor is not None

    # Insert a row BETWEEN the already-fetched page and the cursor position
    # — offset pagination would skip or duplicate rows here; keyset
    # pagination must not.
    third = _create_booking(active_resource, app_user, base + timedelta(hours=2))

    page2 = client.get(BOOKINGS_URL, {"limit": "2", "cursor": cursor}, **_headers(app_user))
    page2_ids = [r["id"] for r in page2.json()["data"]]
    assert page2_ids == [str(third.id), str(fourth.id)]
    # No overlap with page 1.
    assert set(page2_ids).isdisjoint({str(first.id), str(second.id)})
