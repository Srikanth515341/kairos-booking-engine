"""HOLD-01 and HOLD-03 (Test Plan v1.0 §3) — Implementation Plan Phase 15.
HOLD-02 (barrier-released concurrent contention) lives in
tests/concurrency/test_hold_02.py, alongside the other CONC-style proofs.

No offer-cascade worker exists yet (Phase 16) — holds are created directly
via `create_booking(..., status=HELD)`, the same "mechanism proven directly,
before its real caller" pattern already used for `actor_type='system'`
(Phase 8→13) and containment eligibility (Phase 14). Acceptance (RFC v1.0
§10.3 / Spec v1.0 §4.3) has no HTTP endpoint until Phase 16 either — proven
here by executing the literal conditional UPDATE directly, per this phase's
own explicit instruction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking, BookingStatus
from kairos.bookings.services import BookingCreateRequest, create_booking
from kairos.core.models import AuditActorType
from kairos.identity.models import AppUser
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


def _create_hold(resource: Resource, waitlisted_user: AppUser) -> Booking:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    return create_booking(
        BookingCreateRequest(
            resource=resource,
            user=waitlisted_user,
            start=start,
            end=end,
            request_id=str(uuid.uuid4()),
            actor_type=AuditActorType.SYSTEM,
            status=BookingStatus.HELD,
        )
    )


def _accept_offer(hold: Booking) -> int:
    """The literal conditional UPDATE RFC v1.0 §10.3 / Spec v1.0 §4.3
    specifies, executed directly (no HTTP endpoint until Phase 16).
    Returns the affected row count — 1 means accepted, 0 means the offer
    was no longer valid.
    """
    with connection.cursor() as cur:
        cur.execute(
            "UPDATE booking SET status = 'confirmed', expires_at = NULL "
            "WHERE id = %s AND status = 'held' AND user_id = %s AND expires_at > now()",
            [str(hold.id), str(hold.user_id)],
        )
        return cur.rowcount


@pytest.mark.django_db
def test_hold_01_ordinary_booking_loses_to_outstanding_offer(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """HOLD-01 ★ — the most important test in the Test Plan. If step 3
    below ever returns 201, the hold is not occupying the exclusion
    domain and the entire waitlist guarantee (PRD FR17, S1) is broken.
    """
    waitlisted_user = AppUser.objects.create(
        email="hold01-waiter@example.com", display_name="Waitlisted"
    )
    unrelated_user = AppUser.objects.create(
        email="hold01-unrelated@example.com", display_name="Unrelated"
    )

    # 1/2: create the hold directly (no offer worker yet) and verify
    # ground truth.
    hold = _create_hold(active_resource, waitlisted_user)
    hold.refresh_from_db()
    assert hold.status == BookingStatus.HELD
    assert hold.user_id == waitlisted_user.id
    assert hold.expires_at is not None
    assert hold.expires_at > timezone.now()

    # 3: an unrelated user attempts the exact same range over real HTTP.
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(hold.time_range.lower),
            "end": _iso(hold.time_range.upper),
        },
        format="json",
        **_headers(unrelated_user),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "slot_unavailable"
    assert not Booking.objects.filter(
        resource=active_resource, user=unrelated_user, status=BookingStatus.CONFIRMED
    ).exists()

    # 4: W accepts. Same row, id unchanged, status transitioned, expiry
    # cleared. No second row created.
    rowcount = _accept_offer(hold)
    assert rowcount == 1
    hold.refresh_from_db()
    assert hold.status == BookingStatus.CONFIRMED
    assert hold.expires_at is None
    assert Booking.objects.filter(resource=active_resource, user=waitlisted_user).count() == 1


@pytest.mark.django_db
def test_hold_01_acceptance_fails_once_hold_has_expired(
    app_user: AppUser, active_resource: Resource
) -> None:
    """The `expires_at > now()` predicate is what makes an expired hold's
    acceptance fail cleanly (0 rows, not an error) — this is the exact
    predicate Phase 17's reclamation race (RFC v1.0 §10.4) depends on
    behaving correctly; proven here independent of that phase existing
    yet.
    """
    waitlisted_user = AppUser.objects.create(
        email="hold01-expired@example.com", display_name="Expired Waiter"
    )
    hold = _create_hold(active_resource, waitlisted_user)
    Booking.objects.filter(id=hold.id).update(expires_at=timezone.now() - timedelta(seconds=1))
    hold.refresh_from_db()

    rowcount = _accept_offer(hold)
    assert rowcount == 0
    hold.refresh_from_db()
    assert hold.status == BookingStatus.HELD


@pytest.mark.django_db
def test_hold_01_acceptance_rejects_wrong_user(
    app_user: AppUser, active_resource: Resource
) -> None:
    """The `user_id = :user_id` predicate (Spec v1.0 §4.3) is what stops
    anyone other than the actual waitlisted user from accepting — proven
    directly since the acceptance mechanism has no HTTP/auth layer yet to
    exercise this through."""
    waitlisted_user = AppUser.objects.create(
        email="hold01-real-waiter@example.com", display_name="Real Waiter"
    )
    hold = _create_hold(active_resource, waitlisted_user)

    with connection.cursor() as cur:
        cur.execute(
            "UPDATE booking SET status = 'confirmed', expires_at = NULL "
            "WHERE id = %s AND status = 'held' AND user_id = %s AND expires_at > now()",
            [str(hold.id), str(uuid.uuid4())],
        )
        assert cur.rowcount == 0
    hold.refresh_from_db()
    assert hold.status == BookingStatus.HELD


@pytest.mark.django_db
def test_hold_03_opaque_in_availability_view(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """HOLD-03 — a hold created through the REAL Phase 15 mechanism
    (`create_booking(..., status=HELD)`), not just an ORM-constructed row,
    is still opaque in GET /resources/{id}/availability: no `booking_id`,
    no `owner`, no status marker, to a user with no relationship to it.
    Complements (does not replace) tests/resources/test_views.py's
    pre-existing Phase 6 coverage of the same rule, which already went
    further by proving it's opaque even to the resource's own admin.
    """
    waitlisted_user = AppUser.objects.create(
        email="hold03-waiter@example.com", display_name="Waiter"
    )
    stranger = AppUser.objects.create(email="hold03-stranger@example.com", display_name="Stranger")
    hold = _create_hold(active_resource, waitlisted_user)

    response = client.get(
        f"/api/v1/resources/{active_resource.id}/availability",
        {
            "from": _iso(hold.time_range.lower - timedelta(hours=1)),
            "to": _iso(hold.time_range.upper + timedelta(hours=1)),
        },
        **_headers(stranger),
    )
    assert response.status_code == 200
    busy_blocks = response.json()["busy_blocks"]
    assert len(busy_blocks) == 1
    assert set(busy_blocks[0].keys()) == {"start", "end"}


@pytest.mark.django_db
def test_holds_never_returned_by_get_bookings(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Spec v1.0 §5.4 — a held row is a reservation, not a booking a user
    "has." Complements tests/bookings/test_read_endpoints.py's existing
    coverage by using the real Phase 15 hold-creation mechanism."""
    hold = _create_hold(active_resource, app_user)

    response = client.get(BOOKINGS_URL, **_headers(app_user))
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()["data"]]
    assert str(hold.id) not in ids
