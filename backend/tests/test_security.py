"""Security & Lifecycle Test Suite (Test Plan v1.0 §12): SEC-01 and SEC-06
— the two Implementation Plan Phase 9 explicitly scopes in. SEC-02
(rate limiting) is Phase 22; SEC-03/04 (waitlist manipulation, injection)
need endpoints/paths this project hasn't built yet; SEC-05 (field-level
authorization) is already covered by tests/resources/test_views.py
(Phase 6); SEC-07 is AUD-01, covered by tests/test_audit_trail.py.
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser, UserGroup, UserGroupMembership
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"
RESOURCES_URL = "/api/v1/resources"


def _headers(user: AppUser) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------------
# SEC-01 — IDOR, with response-body leakage check
# --------------------------------------------------------------------


@pytest.fixture
def user_b_booking(app_user: AppUser, active_resource: Resource) -> tuple[AppUser, Booking]:
    user_b = AppUser.objects.create(email="sec01-user-b@example.com", display_name="User B")
    booking = Booking.objects.create(
        resource=active_resource,
        user=user_b,
        time_range=(timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)),
    )
    return user_b, booking


def _assert_empty_error_envelope(body: dict[str, object]) -> None:
    """Status-code-only checks would miss a bug returning the right code
    with an informative body (Test Plan §12 SEC-01's own stated premise)."""
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error.keys()) == {"code", "message", "details", "request_id"}
    assert error["code"] == "not_found"
    # No leaked resource_id, time range, or any field from B's booking —
    # the details object for a 404 is empty, not a partial echo.
    assert error["details"] in ({}, None)


@pytest.mark.django_db
def test_sec_01_get_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    response = client.get(f"{BOOKINGS_URL}/{booking.id}", **_headers(app_user))
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)
    assert str(booking.resource_id) not in str(body)


@pytest.mark.django_db
def test_sec_01_patch_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    new_start = timezone.now() + timedelta(hours=10)
    response = client.patch(
        f"{BOOKINGS_URL}/{booking.id}",
        data={
            "start": new_start.isoformat().replace("+00:00", "Z"),
            "end": (new_start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)

    booking.refresh_from_db()
    assert booking.time_range.lower != new_start


@pytest.mark.django_db
def test_sec_01_cancel_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    response = client.post(
        f"{BOOKINGS_URL}/{booking.id}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)

    booking.refresh_from_db()
    assert booking.status == "confirmed"


@pytest.mark.django_db
def test_sec_01_history_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    response = client.get(f"{BOOKINGS_URL}/{booking.id}/history", **_headers(app_user))
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)


# --------------------------------------------------------------------
# SEC-06 — restricted resources don't leak existence (PRD FR46)
# --------------------------------------------------------------------


@pytest.fixture
def restricted_resource(app_user: AppUser) -> Resource:
    group = UserGroup.objects.create(name="Facilities Team")
    return Resource.objects.create(
        name="Restricted Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=app_user,
        restricted_group=group,
    )


@pytest.mark.django_db
def test_sec_06_non_member_gets_404_on_direct_access(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider@example.com", display_name="Outsider")
    response = client.get(f"{RESOURCES_URL}/{restricted_resource.id}", **_headers(outsider))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_sec_06_non_member_absent_from_list(
    client: APIClient, app_user: AppUser, active_resource: Resource, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider2@example.com", display_name="Outsider2")
    response = client.get(RESOURCES_URL, **_headers(outsider))
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["data"]}
    assert str(restricted_resource.id) not in ids
    assert str(active_resource.id) in ids  # the open resource is still listed


@pytest.mark.django_db
def test_sec_06_group_member_can_access(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    member = AppUser.objects.create(email="sec06-member@example.com", display_name="Member")
    assert restricted_resource.restricted_group is not None
    UserGroupMembership.objects.create(group=restricted_resource.restricted_group, user=member)

    detail_response = client.get(f"{RESOURCES_URL}/{restricted_resource.id}", **_headers(member))
    assert detail_response.status_code == 200

    list_response = client.get(RESOURCES_URL, **_headers(member))
    ids = {row["id"] for row in list_response.json()["data"]}
    assert str(restricted_resource.id) in ids


@pytest.mark.django_db
def test_sec_06_non_member_cannot_book_restricted_resource(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider3@example.com", display_name="Outsider3")
    start = timezone.now() + timedelta(hours=1)
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(restricted_resource.id),
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
        format="json",
        **_headers(outsider),
    )
    # PRD FR46: "Non-members may neither book nor join its waitlist" — 404,
    # the same as a nonexistent resource, not a distinct "restricted" code.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_sec_06_non_member_gets_404_on_availability(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider4@example.com", display_name="Outsider4")
    response = client.get(
        f"{RESOURCES_URL}/{restricted_resource.id}/availability?from=2026-09-01&to=2026-09-08",
        **_headers(outsider),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_sec_06_resource_admin_can_access_restricted_resource_they_administer(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    from kairos.identity.models import ResourceAdmin

    admin = AppUser.objects.create(email="sec06-admin@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=restricted_resource, user=admin, granted_by=app_user)

    response = client.get(f"{RESOURCES_URL}/{restricted_resource.id}", **_headers(admin))
    assert response.status_code == 200
