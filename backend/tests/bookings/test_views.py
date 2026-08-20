"""POST /api/v1/bookings — Spec v1.0 §5.1, every documented failure case
from Implementation Plan Phase 4's Definition of Done and the relevant rows
of Test Plan v1.0 §10. Idempotency-Key coverage (missing header -> 400,
replay, conflict, concurrent replay) is tests/bookings/test_idempotency.py
(Phase 5) — this file covers the non-idempotency-specific request surface.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser
from kairos.identity.oidc import issue_session_token
from kairos.resources.models import Resource, ResourceStatus

BOOKINGS_URL = "/api/v1/bookings"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


def _bearer_headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    """A REAL session token, minted via the exact function
    `POST /api/v1/auth/token` calls internally and verified on the request
    by the REAL `OIDCSessionAuthentication` class — not the stub. Skips
    only the ID-token exchange HTTP round trip itself, which
    tests/identity/test_authentication.py already covers end-to-end on its
    own terms; what THESE tests need to prove is that the write path
    (idempotency, session settings, audit) behaves identically once a real
    principal is resolved, which this exercises for real.
    """
    token, _ = issue_session_token(user.id)
    return {
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
def test_create_booking_returns_201_with_exact_body_shape(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Converted to the FULL real-auth round trip (Phase 9) — not just a
    minted session token but the actual login handshake a real client
    performs: mock IdP issues an ID token, it's exchanged for a session
    token, and THAT authenticates the write. The flagship "does create
    work" test is the right one to prove the entire chain end-to-end,
    rather than every write-path test repeating the full handshake.
    """
    mock_response = client.post(
        "/api/v1/auth/dev-mock-login",
        data={"email": app_user.email, "display_name": app_user.display_name},
        format="json",
    )
    assert mock_response.status_code == 200
    token_response = client.post(
        "/api/v1/auth/token", data={"id_token": mock_response.json()["id_token"]}, format="json"
    )
    assert token_response.status_code == 200
    access_token = token_response.json()["access_token"]

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
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
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
        "cancelled_at",
        "cancelled_by",
        "cancellation_reason",
    }
    assert body["resource_id"] == str(active_resource.id)
    assert body["user_id"] == str(app_user.id)
    assert body["status"] == "confirmed"
    assert body["series_id"] is None
    assert body["cancelled_at"] is None
    assert body["cancelled_by"] is None
    assert body["cancellation_reason"] is None
    assert Booking.objects.filter(id=body["id"]).exists()


@pytest.mark.django_db
def test_conflicting_booking_returns_409_slot_unavailable(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Real minted session token (Phase 9), not the stub — proves the
    exclusion-constraint conflict path (create_booking's 23P01 -> 409
    translation) is unaffected by which authenticator resolved the actor.
    """
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    payload = {
        "resource_id": str(active_resource.id),
        "start": _iso(start),
        "end": _iso(end),
    }

    # Two DISTINCT idempotency keys (each _bearer_headers() call mints a
    # fresh one) — this test proves a genuine slot conflict (23P01), not a
    # replay.
    first = client.post(BOOKINGS_URL, data=payload, format="json", **_bearer_headers(app_user))
    assert first.status_code == 201

    second = client.post(BOOKINGS_URL, data=payload, format="json", **_bearer_headers(app_user))
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
        **_headers(app_user),
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
        **_headers(app_user),
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
        **_headers(app_user),
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
            **_headers(app_user),
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
            **_headers(app_user),
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
            **_headers(app_user),
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
            **_headers(app_user),
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
            **_headers(app_user),
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
            **_headers(app_user),
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
            **_headers(app_user),
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
            **_headers(app_user),
        )
        assert response.status_code == 201
