"""GET /api/v1/bookings/{id}/history — Spec v1.0 §5.3, Test Plan v1.0
AUD-03 (a/b), AUD-04, AUD-05. AUD-01/02 (grant/trigger-level proofs) and
AUD-03's system-actor mechanism live in tests/test_audit_trail.py; AUD-03(c)
(admin override without reason -> 400) is already covered by
tests/bookings/test_cancel_edit.py::test_admin_override_without_reason_returns_400
— not duplicated here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser, ResourceAdmin
from kairos.identity.oidc import issue_session_token
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(
    user: AppUser, idempotency_key: uuid.UUID | None = None, request_id: str | None = None
) -> dict[str, str]:
    headers = {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }
    if request_id is not None:
        headers["HTTP_X_REQUEST_ID"] = request_id
    return headers


def _bearer_headers(
    user: AppUser, idempotency_key: uuid.UUID | None = None, request_id: str | None = None
) -> dict[str, str]:
    """A REAL minted session token (Phase 9) — see
    tests/bookings/test_views.py's identical helper for the full
    rationale. Used on AUD-03(a) specifically: proves the audit trigger
    correctly attributes `actor_id`/`actor_type` from a REAL authenticated
    principal end-to-end through the history endpoint, not just at the
    session-variable level (that's tests/identity/test_authentication.py's
    spy test) or via the stub.
    """
    token, _ = issue_session_token(user.id)
    headers = {
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }
    if request_id is not None:
        headers["HTTP_X_REQUEST_ID"] = request_id
    return headers


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.mark.django_db
def test_history_non_owner_non_admin_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)),
    )
    stranger = AppUser.objects.create(email="history-stranger@example.com", display_name="S")

    response = client.get(f"{BOOKINGS_URL}/{booking.id}/history", **_headers(stranger))
    assert response.status_code == 404


@pytest.mark.django_db
def test_history_nonexistent_booking_returns_404(client: APIClient, app_user: AppUser) -> None:
    response = client.get(f"{BOOKINGS_URL}/{uuid.uuid4()}/history", **_headers(app_user))
    assert response.status_code == 404


# --------------------------------------------------------------------
# AUD-03(a) — normal user booking
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_aud_03a_create_records_actor_type_user_with_matching_request_id(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Real minted session token (Phase 9) throughout — proves the audit
    trail correctly attributes a REAL authenticated principal end to end,
    not just that the session variable was set (that's the separate spy
    test in tests/identity/test_authentication.py).
    """
    start = timezone.now() + timedelta(hours=1)
    create_response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        HTTP_X_REQUEST_ID="req-aud-03a",
        **_bearer_headers(app_user),
    )
    assert create_response.status_code == 201
    booking_id = create_response.json()["id"]
    assert create_response.headers["X-Request-Id"] == "req-aud-03a"

    history_response = client.get(
        f"{BOOKINGS_URL}/{booking_id}/history", **_bearer_headers(app_user)
    )
    assert history_response.status_code == 200
    body = history_response.json()
    assert body["booking_id"] == booking_id
    assert len(body["events"]) == 1

    event = body["events"][0]
    assert event["action"] == "insert"
    assert event["actor"]["type"] == "user"
    assert event["actor"]["id"] == str(app_user.id)
    assert event["actor"]["display_name"] == app_user.display_name
    assert event["reason"] is None
    # Correlates to the SAME X-Request-Id the 201 response carried.
    assert event["request_id"] == "req-aud-03a"
    assert event["changes"]["status"] == [None, "confirmed"]


# --------------------------------------------------------------------
# AUD-03(b) — admin override cancellation with a reason
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_aud_03b_admin_cancel_records_actor_type_admin_and_reason(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)),
    )
    admin = AppUser.objects.create(email="aud03b-admin@example.com", display_name="Facilities Bot")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    cancel_response = client.post(
        f"{BOOKINGS_URL}/{booking.id}/cancel",
        data={"reason": "Room offline for maintenance"},
        format="json",
        **_headers(admin),
    )
    assert cancel_response.status_code == 200

    history_response = client.get(f"{BOOKINGS_URL}/{booking.id}/history", **_headers(app_user))
    assert history_response.status_code == 200
    events = history_response.json()["events"]
    cancel_event = events[-1]

    assert cancel_event["action"] == "update"
    assert cancel_event["actor"]["type"] == "admin"
    assert cancel_event["actor"]["id"] == str(admin.id)
    assert cancel_event["actor"]["display_name"] == "Facilities Bot"
    assert cancel_event["reason"] == "Room offline for maintenance"
    assert cancel_event["changes"]["status"] == ["confirmed", "cancelled"]


@pytest.mark.django_db
def test_no_unknown_actor_type_during_normal_operation(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Test Plan §8 AUD-03: 'No audit row is ever written with
    actor_type=unknown during normal operation.' Exercises create, edit,
    and cancel through the real API — every write path this phase touches.
    """
    start = timezone.now() + timedelta(hours=1)
    create_response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_headers(app_user),
    )
    booking_id = create_response.json()["id"]

    new_start = start + timedelta(hours=5)
    client.patch(
        f"{BOOKINGS_URL}/{booking_id}",
        data={"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))},
        format="json",
        **_headers(app_user),
    )
    client.post(f"{BOOKINGS_URL}/{booking_id}/cancel", data={}, format="json", **_headers(app_user))

    history_response = client.get(f"{BOOKINGS_URL}/{booking_id}/history", **_headers(app_user))
    events = history_response.json()["events"]
    assert len(events) == 3
    assert all(event["actor"]["type"] != "unknown" for event in events)


# --------------------------------------------------------------------
# AUD-04 — full lifecycle reconstruction
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_aud_04_create_edit_admin_cancel_reconstructs_in_order(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    create_response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_headers(app_user),
    )
    booking_id = create_response.json()["id"]

    new_start = start + timedelta(hours=10)
    edit_response = client.patch(
        f"{BOOKINGS_URL}/{booking_id}",
        data={"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))},
        format="json",
        **_headers(app_user),
    )
    assert edit_response.status_code == 200

    admin = AppUser.objects.create(email="aud04-admin@example.com", display_name="Ops Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)
    cancel_response = client.post(
        f"{BOOKINGS_URL}/{booking_id}/cancel",
        data={"reason": "Double-booked with a maintenance window"},
        format="json",
        **_headers(admin),
    )
    assert cancel_response.status_code == 200

    history_response = client.get(f"{BOOKINGS_URL}/{booking_id}/history", **_headers(app_user))
    events = history_response.json()["events"]

    assert len(events) == 3
    actions = [e["action"] for e in events]
    assert actions == ["insert", "update", "update"]

    # Strictly non-decreasing occurred_at — "in order" (AUD-04).
    timestamps = [e["occurred_at"] for e in events]
    assert timestamps == sorted(timestamps)

    create_event, edit_event, cancel_event = events
    assert create_event["actor"]["type"] == "user"
    assert create_event["reason"] is None

    assert edit_event["actor"]["type"] == "user"
    assert "time_range" in edit_event["changes"] or "starts_at" in edit_event["changes"]

    assert cancel_event["actor"]["type"] == "admin"
    assert cancel_event["actor"]["id"] == str(admin.id)
    assert cancel_event["reason"] == "Double-booked with a maintenance window"
    assert cancel_event["changes"]["status"] == ["confirmed", "cancelled"]


# --------------------------------------------------------------------
# AUD-05 — audit survives cancellation
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_aud_05_cancellation_does_not_remove_audit_history(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    create_response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_headers(app_user),
    )
    booking_id = create_response.json()["id"]

    before_cancel = client.get(f"{BOOKINGS_URL}/{booking_id}/history", **_headers(app_user))
    assert len(before_cancel.json()["events"]) == 1

    cancel_response = client.post(
        f"{BOOKINGS_URL}/{booking_id}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert cancel_response.status_code == 200

    after_cancel = client.get(f"{BOOKINGS_URL}/{booking_id}/history", **_headers(app_user))
    events = after_cancel.json()["events"]
    # The insert event is still there — cancelling never removes it.
    assert len(events) == 2
    assert events[0]["action"] == "insert"
    assert events[1]["action"] == "update"

    # Owner can still fetch the (now-cancelled) booking's history at all —
    # the object itself remains visible/accessible after cancellation.
    assert after_cancel.status_code == 200
