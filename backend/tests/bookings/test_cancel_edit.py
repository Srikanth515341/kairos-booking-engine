"""PATCH /bookings/{id} and POST /bookings/{id}/cancel — Spec v1.0 §5.5/
§5.6, Implementation Plan Phase 7's Definition of Done, and the relevant
rows of Test Plan v1.0 §10. Concurrency proofs (CONC-03, CONC-04) live in
tests/concurrency/.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking, BookingStatus
from kairos.identity.models import AppUser, ResourceAdmin
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def owned_booking(app_user: AppUser, active_resource: Resource) -> Booking:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )
    # .create() leaves `time_range` as the plain tuple assigned in Python,
    # not the Range object a fresh SELECT returns (see
    # kairos.bookings.services.create_booking's identical refresh) — tests
    # below read .time_range.lower/.upper, which needs the real Range.
    booking.refresh_from_db()
    return booking


# --------------------------------------------------------------------
# PATCH /bookings/{id} — edit
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_by_owner_to_free_target_returns_200(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    new_start = timezone.now() + timedelta(hours=5)
    new_end = new_start + timedelta(hours=1)

    response = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}",
        data={"start": _iso(new_start), "end": _iso(new_end)},
        format="json",
        **_headers(app_user),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(owned_booking.id)
    assert body["start"] == _iso(new_start)
    assert body["end"] == _iso(new_end)
    owned_booking.refresh_from_db()
    assert owned_booking.time_range.lower == new_start


@pytest.mark.django_db
def test_edit_by_non_owner_returns_404(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    other = AppUser.objects.create(email="other@example.com", display_name="Other")
    new_start = timezone.now() + timedelta(hours=5)
    response = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}",
        data={"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))},
        format="json",
        **_headers(other),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_edit_resource_admin_is_not_owner_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource, owned_booking: Booking
) -> None:
    # Edit has NO admin override (Spec v1.0 §5.5) — unlike cancel, being a
    # resource admin for the booking's resource is not sufficient.
    admin = AppUser.objects.create(email="edit-admin@example.com", display_name="Edit Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    new_start = timezone.now() + timedelta(hours=5)
    response = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}",
        data={"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))},
        format="json",
        **_headers(admin),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_edit_nonexistent_booking_returns_404(client: APIClient, app_user: AppUser) -> None:
    new_start = timezone.now() + timedelta(hours=5)
    response = client.patch(
        f"{BOOKINGS_URL}/{uuid.uuid4()}",
        data={"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_edit_target_overlapping_another_booking_returns_409(
    client: APIClient, app_user: AppUser, active_resource: Resource, owned_booking: Booking
) -> None:
    blocker_start = timezone.now() + timedelta(hours=10)
    blocker_end = blocker_start + timedelta(hours=1)
    Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(blocker_start, blocker_end)
    )

    response = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}",
        data={"start": _iso(blocker_start), "end": _iso(blocker_end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_unavailable"

    # The loser's row is verified unchanged (Spec v1.0 §5.5's constraint
    # comparing the UPDATE against every OTHER row must not have partially
    # applied anything to this one).
    owned_booking.refresh_from_db()
    assert owned_booking.time_range.lower != blocker_start


@pytest.mark.django_db
def test_edit_own_current_range_does_not_self_conflict(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    """PRD FR5 / Spec v1.0 §5.5: an EXCLUDE constraint checked on UPDATE
    compares the new row against every *other* row, so "editing" a booking
    to its own existing range (or a range that merely still overlaps its
    own prior range) must not be rejected as a self-conflict."""
    same_start = owned_booking.time_range.lower
    same_end = owned_booking.time_range.upper

    response = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}",
        data={"start": _iso(same_start), "end": _iso(same_end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_edit_missing_idempotency_key_returns_400(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    new_start = timezone.now() + timedelta(hours=5)
    response = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}",
        data={"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))},
        format="json",
        HTTP_X_DEV_USER_ID=str(app_user.id),
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "Idempotency-Key"


@pytest.mark.django_db
def test_edit_replay_with_same_key_returns_cached_response(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    new_start = timezone.now() + timedelta(hours=5)
    payload = {"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))}
    key = uuid.uuid4()

    first = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}", data=payload, format="json", **_headers(app_user, key)
    )
    assert first.status_code == 200

    replay = client.patch(
        f"{BOOKINGS_URL}/{owned_booking.id}", data=payload, format="json", **_headers(app_user, key)
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()


@pytest.mark.django_db
def test_edit_same_key_across_two_bookings_is_conflict_not_replay(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """The idempotency fingerprint hashes the request BODY, and an edit
    body is only {"start","end"} — the booking id comes from the URL, not
    the body. Without folding the booking id into what gets fingerprinted,
    reusing one key for two edits that happen to carry an IDENTICAL range
    would be misread as a replay of the first — silently returning
    booking_a's response for what is actually a request about booking_b,
    and never touching booking_b at all. Folding the booking id in makes
    the two fingerprints differ, so this instead surfaces as the same
    422 `idempotency_key_conflict` any other same-key-different-body reuse
    gets (Spec v1.0 §7, IDEM-03) — safe, and distinguishable from a
    genuine replay, even though it's a less friendly error than a client
    would get by using two separate keys as intended.
    """
    base = timezone.now() + timedelta(hours=20)
    booking_a = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(base, base + timedelta(hours=1))
    )
    booking_b = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(base + timedelta(hours=2), base + timedelta(hours=3)),
    )
    booking_b.refresh_from_db()
    original_b_start = booking_b.time_range.lower

    shared_key = uuid.uuid4()
    new_start = timezone.now() + timedelta(hours=30)
    payload = {"start": _iso(new_start), "end": _iso(new_start + timedelta(hours=1))}

    response_a = client.patch(
        f"{BOOKINGS_URL}/{booking_a.id}",
        data=payload,
        format="json",
        **_headers(app_user, shared_key),
    )
    assert response_a.status_code == 200
    assert "Idempotent-Replay" not in response_a.headers

    response_b = client.patch(
        f"{BOOKINGS_URL}/{booking_b.id}",
        data=payload,
        format="json",
        **_headers(app_user, shared_key),
    )
    assert response_b.status_code == 422
    assert response_b.json()["error"]["code"] == "idempotency_key_conflict"

    # booking_b must be untouched — the conflict was rejected before any
    # write to it was attempted, not applied-then-reported.
    booking_b.refresh_from_db()
    assert booking_b.time_range.lower == original_b_start


# --------------------------------------------------------------------
# POST /bookings/{id}/cancel
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_self_cancel_without_reason_returns_200(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    response = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None
    assert body["cancelled_by"] == str(app_user.id)
    owned_booking.refresh_from_db()
    assert owned_booking.status == BookingStatus.CANCELLED


@pytest.mark.django_db
def test_admin_override_without_reason_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource, owned_booking: Booking
) -> None:
    admin = AppUser.objects.create(email="cancel-admin@example.com", display_name="Cancel Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    response = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel", data={}, format="json", **_headers(admin)
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["field"] == "reason"
    owned_booking.refresh_from_db()
    assert owned_booking.status == BookingStatus.CONFIRMED


@pytest.mark.django_db
def test_admin_override_with_reason_returns_200_and_persists_reason(
    client: APIClient, app_user: AppUser, active_resource: Resource, owned_booking: Booking
) -> None:
    admin = AppUser.objects.create(email="cancel-admin2@example.com", display_name="Cancel Admin 2")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    response = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel",
        data={"reason": "Room offline for maintenance"},
        format="json",
        **_headers(admin),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["cancellation_reason"] == "Room offline for maintenance"
    assert body["cancelled_by"] == str(admin.id)


@pytest.mark.django_db
def test_double_cancel_returns_200_idempotent(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    first = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert first.status_code == 200
    first_cancelled_at = first.json()["cancelled_at"]

    # A DIFFERENT idempotency key — this is a genuinely new request, not a
    # replay, and must still succeed with 200 per Spec v1.0 §5.6.
    second = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"
    # The original cancellation timestamp is untouched by the no-op replay.
    assert second.json()["cancelled_at"] == first_cancelled_at


@pytest.mark.django_db
def test_cancel_by_non_owner_non_admin_returns_404(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    stranger = AppUser.objects.create(email="stranger@example.com", display_name="Stranger")
    response = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel", data={}, format="json", **_headers(stranger)
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_cancel_nonexistent_booking_returns_404(client: APIClient, app_user: AppUser) -> None:
    response = client.post(
        f"{BOOKINGS_URL}/{uuid.uuid4()}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_cancel_missing_idempotency_key_returns_400(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    response = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel",
        data={},
        format="json",
        HTTP_X_DEV_USER_ID=str(app_user.id),
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "Idempotency-Key"


@pytest.mark.django_db
def test_cancel_replay_with_same_key_returns_cached_response(
    client: APIClient, app_user: AppUser, owned_booking: Booking
) -> None:
    key = uuid.uuid4()
    first = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel",
        data={},
        format="json",
        **_headers(app_user, key),
    )
    assert first.status_code == 200

    replay = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel",
        data={},
        format="json",
        **_headers(app_user, key),
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()


@pytest.mark.django_db
def test_cancellation_frees_the_range_for_a_new_booking(
    client: APIClient, app_user: AppUser, active_resource: Resource, owned_booking: Booking
) -> None:
    """RFC v1.0 §5c step 2: leaving status IN ('confirmed','held') removes
    the row from the exclusion domain immediately — a subsequent create for
    the identical range must succeed without waiting on anything."""
    start = owned_booking.time_range.lower
    end = owned_booking.time_range.upper
    assert start is not None
    assert end is not None

    cancel = client.post(
        f"{BOOKINGS_URL}/{owned_booking.id}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert cancel.status_code == 200

    rebook = client.post(
        BOOKINGS_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert rebook.status_code == 201


@pytest.mark.django_db
def test_cancel_same_key_across_two_bookings_is_conflict_not_replay(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Same reasoning as the edit-endpoint version of this test: cancel's
    body is just {"reason": ...} (here, identically empty for both), so
    without folding the booking id into the fingerprint, reusing a key
    across two different bookings' cancellations would misread the second
    as a replay of the first and leave booking_b never actually cancelled.
    """
    start_a = timezone.now() + timedelta(hours=40)
    booking_a = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start_a, start_a + timedelta(hours=1))
    )
    start_b = timezone.now() + timedelta(hours=41)
    booking_b = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start_b, start_b + timedelta(hours=1))
    )

    shared_key = uuid.uuid4()
    response_a = client.post(
        f"{BOOKINGS_URL}/{booking_a.id}/cancel",
        data={},
        format="json",
        **_headers(app_user, shared_key),
    )
    assert response_a.status_code == 200
    assert "Idempotent-Replay" not in response_a.headers

    response_b = client.post(
        f"{BOOKINGS_URL}/{booking_b.id}/cancel",
        data={},
        format="json",
        **_headers(app_user, shared_key),
    )
    assert response_b.status_code == 422
    assert response_b.json()["error"]["code"] == "idempotency_key_conflict"

    # booking_b was never touched by the rejected request — still confirmed.
    booking_b.refresh_from_db()
    assert booking_b.status == BookingStatus.CONFIRMED


@pytest.mark.django_db
def test_edit_outside_bookable_hours_returns_400(client: APIClient, app_user: AppUser) -> None:
    resource = Resource.objects.create(
        name="9-to-5 Edit Room",
        timezone="UTC",
        bookable_start_time=time(9, 0),
        bookable_end_time=time(17, 0),
        created_by=app_user,
    )
    start = timezone.now().replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=2)
    booking = Booking.objects.create(
        resource=resource, user=app_user, time_range=(start, start + timedelta(hours=1))
    )

    bad_start = timezone.now().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(
        days=2
    )
    response = client.patch(
        f"{BOOKINGS_URL}/{booking.id}",
        data={"start": _iso(bad_start), "end": _iso(bad_start + timedelta(hours=1))},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "start"
