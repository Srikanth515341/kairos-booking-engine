"""POST /api/v1/bookings — Spec v1.0 §5.1, every documented failure case
from Implementation Plan Phase 4's Definition of Done and the relevant rows
of Test Plan v1.0 §10. Idempotency-Key is deliberately not required yet —
that's Phase 5's documented, temporary gap (Implementation Plan Phase 4
scope).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser
from kairos.resources.models import Resource, ResourceStatus

BOOKINGS_URL = "/api/v1/bookings"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _auth_headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
def test_create_booking_returns_201_with_exact_body_shape(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)

    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(end),
        },
        format="json",
        **_auth_headers(app_user),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body.keys()) == {
        "id",
        "resource_id",
        "user_id",
        "start",
        "end",
        "status",
        "series_id",
        "created_at",
    }
    assert body["resource_id"] == str(active_resource.id)
    assert body["user_id"] == str(app_user.id)
    assert body["status"] == "confirmed"
    assert body["series_id"] is None
    assert Booking.objects.filter(id=body["id"]).exists()


@pytest.mark.django_db
def test_conflicting_booking_returns_409_slot_unavailable(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    payload = {
        "resource_id": str(active_resource.id),
        "start": _iso(start),
        "end": _iso(end),
    }

    first = client.post(BOOKINGS_URL, data=payload, format="json", **_auth_headers(app_user))
    assert first.status_code == 201

    second = client.post(BOOKINGS_URL, data=payload, format="json", **_auth_headers(app_user))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "slot_unavailable"


@pytest.mark.django_db
def test_nonexistent_resource_returns_404(client: APIClient, app_user: AppUser) -> None:
    start = timezone.now() + timedelta(hours=1)
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": "00000000-0000-0000-0000-000000000000",
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_auth_headers(app_user),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_inactive_resource_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    active_resource.status = ResourceStatus.INACTIVE
    active_resource.save(update_fields=["status"])

    start = timezone.now() + timedelta(hours=1)
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_auth_headers(app_user),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_missing_auth_header_returns_401(client: APIClient, active_resource: Resource) -> None:
    start = timezone.now() + timedelta(hours=1)
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.django_db
def test_request_id_present_on_success_and_error(client: APIClient, app_user: AppUser) -> None:
    response = client.post(
        BOOKINGS_URL,
        data={},
        format="json",
        HTTP_X_REQUEST_ID="req-fixed-123",
        **_auth_headers(app_user),
    )
    assert response.status_code == 400
    assert response.headers["X-Request-Id"] == "req-fixed-123"
    assert response.json()["error"]["request_id"] == "req-fixed-123"


@pytest.mark.django_db
def test_request_id_generated_when_absent(client: APIClient) -> None:
    response = client.post(BOOKINGS_URL, data={}, format="json")
    assert "X-Request-Id" in response.headers
    assert response.headers["X-Request-Id"]


class TestPolicyValidation:
    """Test Plan v1.0 §10 — each case returns 400 `validation_error` with
    the offending field named in `details` (Spec v1.0 §6)."""

    @pytest.mark.django_db
    def test_end_equals_start(
        self, client: APIClient, app_user: AppUser, active_resource: Resource
    ) -> None:
        start = timezone.now() + timedelta(hours=1)
        response = client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(active_resource.id),
                "start": _iso(start),
                "end": _iso(start),
            },
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "validation_error"
        assert body["error"]["details"]["field"] == "end"

    @pytest.mark.django_db
    def test_start_in_the_past(
        self, client: APIClient, app_user: AppUser, active_resource: Resource
    ) -> None:
        start = timezone.now() - timedelta(seconds=1)
        response = client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(active_resource.id),
                "start": _iso(start),
                "end": _iso(start + timedelta(hours=1)),
            },
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"]["field"] == "start"

    @pytest.mark.django_db
    def test_beyond_365_day_horizon(
        self, client: APIClient, app_user: AppUser, active_resource: Resource
    ) -> None:
        start = timezone.now() + timedelta(days=366)
        response = client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(active_resource.id),
                "start": _iso(start),
                "end": _iso(start + timedelta(hours=1)),
            },
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"]["field"] == "start"

    @pytest.mark.django_db
    def test_exactly_365_days_ahead_succeeds(
        self, client: APIClient, app_user: AppUser, active_resource: Resource
    ) -> None:
        start = timezone.now() + timedelta(days=365) - timedelta(minutes=5)
        response = client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(active_resource.id),
                "start": _iso(start),
                "end": _iso(start + timedelta(hours=1)),
            },
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 201

    @pytest.mark.django_db
    def test_outside_bookable_hours(self, client: APIClient, app_user: AppUser) -> None:
        resource = Resource.objects.create(
            name="9-to-5 Room",
            timezone="UTC",
            bookable_start_time=time(9, 0),
            bookable_end_time=time(17, 0),
            created_by=app_user,
        )
        # Fixed future date at 08:00 UTC — before the 09:00 window opens.
        start = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(
            days=2
        )
        response = client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(resource.id),
                "start": _iso(start),
                "end": _iso(start + timedelta(hours=1)),
            },
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"]["field"] == "start"

    @pytest.mark.django_db
    def test_exactly_at_bookable_window_succeeds(
        self, client: APIClient, app_user: AppUser
    ) -> None:
        resource = Resource.objects.create(
            name="9-to-5 Room Exact",
            timezone="UTC",
            bookable_start_time=time(9, 0),
            bookable_end_time=time(17, 0),
            created_by=app_user,
        )
        start = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(
            days=2
        )
        end = start.replace(hour=17, minute=0)
        response = client.post(
            BOOKINGS_URL,
            data={"resource_id": str(resource.id), "start": _iso(start), "end": _iso(end)},
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 201

    @pytest.mark.django_db
    def test_one_minute_over_max_duration(self, client: APIClient, app_user: AppUser) -> None:
        resource = Resource.objects.create(
            name="Short Meetings Room",
            timezone="UTC",
            bookable_start_time=time(0, 0),
            bookable_end_time=time(23, 59),
            max_booking_duration_minutes=60,
            created_by=app_user,
        )
        start = timezone.now() + timedelta(hours=1)
        response = client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(resource.id),
                "start": _iso(start),
                "end": _iso(start + timedelta(minutes=61)),
            },
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 400
        assert response.json()["error"]["details"]["field"] == "end"

    @pytest.mark.django_db
    def test_exactly_max_duration_succeeds(self, client: APIClient, app_user: AppUser) -> None:
        resource = Resource.objects.create(
            name="Short Meetings Room Exact",
            timezone="UTC",
            bookable_start_time=time(0, 0),
            bookable_end_time=time(23, 59),
            max_booking_duration_minutes=60,
            created_by=app_user,
        )
        start = timezone.now() + timedelta(hours=1)
        response = client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(resource.id),
                "start": _iso(start),
                "end": _iso(start + timedelta(minutes=60)),
            },
            format="json",
            **_auth_headers(app_user),
        )
        assert response.status_code == 201
