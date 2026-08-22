"""GET /api/v1/resources, GET /api/v1/resources/{id}, and
GET /api/v1/resources/{id}/availability (Spec v1.0 §5.7, §5.14; Test Plan
SEC-05; Implementation Plan Phase 6).
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

RESOURCES_URL = "/api/v1/resources"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------------
# GET /resources, GET /resources/{id}
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_resource_detail_returns_expected_shape(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    response = client.get(f"{RESOURCES_URL}/{active_resource.id}", **_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(active_resource.id)
    assert body["name"] == active_resource.name
    assert body["status"] == "active"
    assert body["created_by"] == str(app_user.id)


@pytest.mark.django_db
def test_resource_detail_nonexistent_returns_404(client: APIClient, app_user: AppUser) -> None:
    response = client.get(f"{RESOURCES_URL}/{uuid.uuid4()}", **_headers(app_user))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_resource_list_paginates_by_name(client: APIClient, app_user: AppUser) -> None:
    names = ["Charlie Room", "Alpha Room", "Bravo Room"]
    for name in names:
        Resource.objects.create(
            name=name,
            timezone="UTC",
            bookable_start_time=time(0, 0),
            bookable_end_time=time(23, 59),
            created_by=app_user,
        )

    response = client.get(RESOURCES_URL, {"limit": "2"}, **_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    assert [r["name"] for r in body["data"]] == ["Alpha Room", "Bravo Room"]
    assert body["next_cursor"] is not None

    page2 = client.get(
        RESOURCES_URL, {"limit": "2", "cursor": body["next_cursor"]}, **_headers(app_user)
    )
    assert [r["name"] for r in page2.json()["data"]] == ["Charlie Room"]
    assert page2.json()["next_cursor"] is None


# --------------------------------------------------------------------
# GET /resources/{id}/availability
# --------------------------------------------------------------------


def _availability_url(resource: Resource) -> str:
    return f"{RESOURCES_URL}/{resource.id}/availability"


@pytest.mark.django_db
def test_availability_valid_range_returns_expected_shape(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=7)
    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(from_dt + timedelta(hours=1), from_dt + timedelta(hours=2)),
    )

    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(app_user),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resource_id"] == str(active_resource.id)
    assert body["timezone"] == active_resource.timezone
    assert body["data_freshness"] == "primary"
    assert "as_of" in body
    assert len(body["busy_blocks"]) == 1
    block = body["busy_blocks"][0]
    assert block["booking_id"] == str(booking.id)
    assert block["owner"] == {"id": str(app_user.id), "display_name": app_user.display_name}


@pytest.mark.django_db
def test_availability_exactly_92_days_succeeds(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=92)
    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(app_user),
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_availability_93_days_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=93)
    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(app_user),
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "to"


@pytest.mark.django_db
def test_availability_to_before_from_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    from_dt = timezone.now() + timedelta(days=2)
    to_dt = timezone.now() + timedelta(days=1)
    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(app_user),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_sec_05_non_owner_non_admin_sees_no_booking_id_or_owner_keys(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    stranger = AppUser.objects.create(email="sec05-stranger@example.com", display_name="Stranger")
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=1)
    Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(from_dt + timedelta(hours=1), from_dt + timedelta(hours=2)),
    )

    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(stranger),
    )
    assert response.status_code == 200
    block = response.json()["busy_blocks"][0]
    # Key ABSENCE, not a null value (SEC-05) — "owner": null still leaks
    # the schema pattern.
    assert "booking_id" not in block
    assert "owner" not in block
    assert set(block.keys()) == {"start", "end"}


@pytest.mark.django_db
def test_resource_admin_sees_booking_id_and_owner_for_others_booking(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    admin = AppUser.objects.create(email="avail-admin@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=1)
    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(from_dt + timedelta(hours=1), from_dt + timedelta(hours=2)),
    )

    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(admin),
    )
    block = response.json()["busy_blocks"][0]
    assert block["booking_id"] == str(booking.id)


@pytest.mark.django_db
def test_held_slot_never_reveals_identifying_fields_even_to_admin(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    admin = AppUser.objects.create(email="held-admin@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=1)
    Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(from_dt + timedelta(hours=1), from_dt + timedelta(hours=2)),
        status=BookingStatus.HELD,
        expires_at=timezone.now() + timedelta(minutes=15),
    )

    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(admin),
    )
    assert response.status_code == 200
    block = response.json()["busy_blocks"][0]
    assert set(block.keys()) == {"start", "end"}


@pytest.mark.django_db
def test_availability_query_count_is_bounded(
    client: APIClient, app_user: AppUser, active_resource: Resource, django_assert_max_num_queries
) -> None:
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=7)
    for i in range(10):
        Booking.objects.create(
            resource=active_resource,
            user=app_user,
            time_range=(
                from_dt + timedelta(hours=3 * i),
                from_dt + timedelta(hours=3 * i + 1),
            ),
        )

    # RFC v1.0 §7.2's N+1 guard: the query count must not grow with the
    # number of busy blocks returned.
    with django_assert_max_num_queries(6):
        response = client.get(
            _availability_url(active_resource),
            {"from": _iso(from_dt), "to": _iso(to_dt)},
            **_headers(app_user),
        )
    assert response.status_code == 200
    assert len(response.json()["busy_blocks"]) == 10


# --------------------------------------------------------------------
# FAIL-03 (Implementation Plan Phase 28; Test Plan v1.0 §12) — simulated
# replica-lag degradation through the REAL endpoint, not just the unit-
# level kairos.core.replica proofs in tests/test_replica.py. No real
# replica exists (Phase 30) — this monkeypatches the one seam Phase 28
# built (kairos.resources.views.current_replica_lag_seconds) to report an
# artificial lag, per that phase's own explicit instruction not to stand
# up real infrastructure just for this test.
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_fail_03_stale_replica_lag_falls_back_to_primary_over_real_http(
    client: APIClient,
    app_user: AppUser,
    active_resource: Resource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kairos.resources.views as resources_views

    monkeypatch.setattr(resources_views, "current_replica_lag_seconds", lambda: 999.0)

    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=7)
    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(app_user),
    )
    assert response.status_code == 200
    # Never silently stale (Spec v1.0 §5.7): a lag this far over threshold
    # must fall back to "primary", explicitly, not a fabricated "replica".
    assert response.json()["data_freshness"] == "primary"


@pytest.mark.django_db
def test_fail_03_fresh_simulated_replica_lag_would_be_used(
    client: APIClient,
    app_user: AppUser,
    active_resource: Resource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the test above: proves the fallback isn't unconditional
    — a lag well under threshold routes to "replica", confirming the
    degradation logic actually branches on the reported lag rather than
    always returning "primary" regardless of input. No production code
    path exercises this branch today (current_replica_lag_seconds always
    returns None until Phase 30) — this monkeypatches the same seam to
    prove the LOGIC, not the current deployment's behavior.
    """
    import kairos.resources.views as resources_views

    monkeypatch.setattr(resources_views, "current_replica_lag_seconds", lambda: 0.5)

    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=7)
    response = client.get(
        _availability_url(active_resource),
        {"from": _iso(from_dt), "to": _iso(to_dt)},
        **_headers(app_user),
    )
    assert response.status_code == 200
    assert response.json()["data_freshness"] == "replica"
