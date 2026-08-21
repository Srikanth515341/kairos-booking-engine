"""POST/GET /api/v1/waitlist-entries and POST /api/v1/waitlist-entries/{id}/
cancel — Spec v1.0 §5.11/§5.12, Implementation Plan Phase 14's Definition of
Done, and Test Plan v1.0 SEC-03.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking, BookingStatus
from kairos.core.models import AuditLog
from kairos.identity.models import AppUser
from kairos.resources.models import Resource, ResourceStatus
from kairos.waitlist.models import WaitlistEntry, WaitlistEntryStatus

WAITLIST_URL = "/api/v1/waitlist-entries"


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


# --------------------------------------------------------------------
# POST /waitlist-entries — join
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_join_returns_201_with_exact_body_shape(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    # A live confirmed booking on the SAME range makes it genuinely
    # unavailable — otherwise the 422 slot_already_available advisory
    # check (Spec v1.0 §5.11) would correctly refuse the join.
    other = AppUser.objects.create(email="holder@example.com", display_name="Holder")
    Booking.objects.create(resource=active_resource, user=other, time_range=(start, end))

    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body.keys()) == {
        "id",
        "resource_id",
        "start",
        "end",
        "status",
        "joined_at",
        "queue_position",
        "active_offer",
    }
    assert body["resource_id"] == str(active_resource.id)
    assert body["start"] == _iso(start)
    assert body["end"] == _iso(end)
    assert body["status"] == "waiting"
    assert body["queue_position"] == 1
    assert body["active_offer"] is None
    entry = WaitlistEntry.objects.get(id=body["id"])
    assert entry.user_id == app_user.id
    assert entry.status == WaitlistEntryStatus.WAITING


@pytest.mark.django_db
def test_join_ignores_client_supplied_joined_at(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """SEC-03(a): a submitted `joined_at` is ignored and the server value
    used — there is no field for the serializer to even bind it to, and
    the column itself is a genuine DB-level DEFAULT (WaitlistEntry.
    joined_at's db_default), not merely a serializer omission.
    """
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    other = AppUser.objects.create(email="holder2@example.com", display_name="Holder2")
    Booking.objects.create(resource=active_resource, user=other, time_range=(start, end))

    forged = timezone.now() - timedelta(days=365)
    response = client.post(
        WAITLIST_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(end),
            "joined_at": _iso(forged),
        },
        format="json",
        **_headers(app_user),
    )

    assert response.status_code == 201
    entry = WaitlistEntry.objects.get(id=response.json()["id"])
    assert entry.joined_at > forged + timedelta(days=1)


@pytest.mark.django_db
def test_join_duplicate_live_entry_returns_409(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """SEC-03(b): a second live entry for the same user/resource/range."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    other = AppUser.objects.create(email="holder3@example.com", display_name="Holder3")
    Booking.objects.create(resource=active_resource, user=other, time_range=(start, end))

    first = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert first.status_code == 201

    second = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_on_waitlist"
    assert WaitlistEntry.objects.filter(user=app_user, resource=active_resource).count() == 1


@pytest.mark.django_db
def test_join_while_already_offered_returns_409(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """SEC-03(c): the unique index covers 'offered' too — a user cannot
    re-join while already holding an outstanding offer (RFC v1.0 §8.2).
    No offer mechanism exists yet (Phase 16); the 'offered' row is created
    directly via the ORM, the same "mechanism before its real caller"
    pattern already used elsewhere in this project (e.g. actor_type=
    'system' from Phase 8 to Phase 13).
    """
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    other = AppUser.objects.create(email="offered-holder@example.com", display_name="OfferHold")
    Booking.objects.create(resource=active_resource, user=other, time_range=(start, end))
    WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(start, end),
        status=WaitlistEntryStatus.OFFERED,
    )

    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_on_waitlist"


@pytest.mark.django_db
def test_join_replay_with_same_key_returns_same_conflict(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Spec v1.0 §7 point 7: a 409 conflict outcome is recorded too — a
    retry with the same idempotency key must receive the identical 409,
    not attempt the write again (which would raise the SAME conflict
    anyway here, but this proves it's actually being RECORDED, not just
    coincidentally re-derived)."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    other = AppUser.objects.create(email="replay-holder@example.com", display_name="ReplayHold")
    Booking.objects.create(resource=active_resource, user=other, time_range=(start, end))
    WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(start, end),
        status=WaitlistEntryStatus.WAITING,
    )

    key = uuid.uuid4()
    first = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user, key),
    )
    assert first.status_code == 409

    second = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user, key),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "already_on_waitlist"
    assert second["Idempotent-Replay"] == "true"


@pytest.mark.django_db
def test_join_fully_free_range_returns_422(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Spec v1.0 §5.11: joining a waitlist for a range with no conflicting
    booking at all is nonsensical — book it directly."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)

    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "slot_already_available"
    assert not WaitlistEntry.objects.exists()


@pytest.mark.django_db
def test_join_nonexistent_resource_returns_404(client: APIClient, app_user: AppUser) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(uuid.uuid4()), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_join_inactive_resource_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    active_resource.status = ResourceStatus.INACTIVE
    active_resource.save()
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_join_end_before_start_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start - timedelta(minutes=30)
    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.django_db
def test_join_without_idempotency_key_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        HTTP_X_DEV_USER_ID=str(app_user.id),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.django_db
def test_join_writes_an_audit_row(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Audit trigger fires on waitlist_entry the same way it does on
    booking (Phase 8's mechanism, this phase's own table)."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    other = AppUser.objects.create(email="holder4@example.com", display_name="Holder4")
    Booking.objects.create(resource=active_resource, user=other, time_range=(start, end))

    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 201
    entry_id = response.json()["id"]

    events = list(AuditLog.objects.filter(entity_type="waitlist_entry", entity_id=entry_id))
    assert len(events) == 1
    assert events[0].action == "insert"
    assert events[0].actor_id == app_user.id
    assert events[0].actor_type == "user"


# --------------------------------------------------------------------
# GET /waitlist-entries — list
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_list_is_self_scoped_and_orders_by_joined_at(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    other = AppUser.objects.create(email="other-list@example.com", display_name="Other")
    now = timezone.now()

    mine_first = WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(now + timedelta(hours=1), now + timedelta(hours=2)),
    )
    mine_second = WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(now + timedelta(hours=3), now + timedelta(hours=4)),
    )
    WaitlistEntry.objects.create(
        resource=active_resource,
        user=other,
        time_range=(now + timedelta(hours=5), now + timedelta(hours=6)),
    )

    response = client.get(WAITLIST_URL, **_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    ids = [row["id"] for row in body["data"]]
    assert ids == [str(mine_first.id), str(mine_second.id)]
    assert body["data"][0]["queue_position"] == 1
    assert body["data"][1]["queue_position"] == 2


@pytest.mark.django_db
def test_list_filters_by_status(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    waiting = WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(now + timedelta(hours=1), now + timedelta(hours=2)),
    )
    WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(now + timedelta(hours=3), now + timedelta(hours=4)),
        status=WaitlistEntryStatus.CANCELLED,
    )

    response = client.get(f"{WAITLIST_URL}?status=waiting", **_headers(app_user))
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()["data"]]
    assert ids == [str(waiting.id)]


@pytest.mark.django_db
def test_list_invalid_status_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    response = client.get(f"{WAITLIST_URL}?status=bogus", **_headers(app_user))
    assert response.status_code == 400


# --------------------------------------------------------------------
# POST /waitlist-entries/{id}/cancel — withdraw
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_cancel_by_owner_returns_200(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    entry = WaitlistEntry.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )

    response = client.post(f"{WAITLIST_URL}/{entry.id}/cancel", **_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["queue_position"] is None
    entry.refresh_from_db()
    assert entry.status == WaitlistEntryStatus.CANCELLED


@pytest.mark.django_db
def test_cancel_by_non_owner_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    entry = WaitlistEntry.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )
    other = AppUser.objects.create(email="stranger@example.com", display_name="Stranger")

    response = client.post(f"{WAITLIST_URL}/{entry.id}/cancel", **_headers(other))
    assert response.status_code == 404
    entry.refresh_from_db()
    assert entry.status == WaitlistEntryStatus.WAITING


@pytest.mark.django_db
def test_cancel_is_idempotent_double_cancel_returns_200(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    entry = WaitlistEntry.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )

    first = client.post(f"{WAITLIST_URL}/{entry.id}/cancel", **_headers(app_user))
    assert first.status_code == 200

    second = client.post(f"{WAITLIST_URL}/{entry.id}/cancel", **_headers(app_user))
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"


@pytest.mark.django_db
def test_cancel_nonexistent_entry_returns_404(client: APIClient, app_user: AppUser) -> None:
    response = client.post(f"{WAITLIST_URL}/{uuid.uuid4()}/cancel", **_headers(app_user))
    assert response.status_code == 404


@pytest.mark.django_db
def test_cancel_writes_an_audit_row(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    entry = WaitlistEntry.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )

    response = client.post(f"{WAITLIST_URL}/{entry.id}/cancel", **_headers(app_user))
    assert response.status_code == 200

    events = list(
        AuditLog.objects.filter(entity_type="waitlist_entry", entity_id=entry.id).order_by(
            "occurred_at"
        )
    )
    assert [e.action for e in events] == ["insert", "update"]
    assert events[-1].actor_id == app_user.id
    assert events[-1].after_state["status"] == "cancelled"


@pytest.mark.django_db
def test_confirmed_or_held_booking_blocks_slot_already_available(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """A HELD booking also makes the range unavailable for a direct
    booking, not only a CONFIRMED one — the same exclusion domain
    (RFC v1.0 §10.1) `slot_is_free` must check both against."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    other = AppUser.objects.create(email="held-holder@example.com", display_name="HeldHolder")
    Booking.objects.create(
        resource=active_resource,
        user=other,
        time_range=(start, end),
        status=BookingStatus.HELD,
        expires_at=timezone.now() + timedelta(minutes=15),
    )

    response = client.post(
        WAITLIST_URL,
        data={"resource_id": str(active_resource.id), "start": _iso(start), "end": _iso(end)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 201
