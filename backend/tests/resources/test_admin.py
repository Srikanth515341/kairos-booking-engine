"""Resource CRUD, admin-scope grants, and utilization (Implementation
Plan Phase 19; Spec v1.0 §5.14, §5.15; Test Plan §10). No `Idempotency-
Key` on any of these — Spec v1.0 §7's coverage list doesn't name them.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser, AppUserPlatformRole, ResourceAdmin
from kairos.resources.models import Resource
from kairos.waitlist.models import WaitlistEntry

RESOURCES_URL = "/api/v1/resources"


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def system_admin(db: None) -> AppUser:
    return AppUser.objects.create(
        email="sysadmin@example.com",
        display_name="Sys Admin",
        platform_role=AppUserPlatformRole.SYSTEM_ADMIN,
    )


# --------------------------------------------------------------------
# POST /resources
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_create_resource_as_system_admin_succeeds(client: APIClient, system_admin: AppUser) -> None:
    response = client.post(
        RESOURCES_URL,
        {
            "name": "New Room",
            "timezone": "UTC",
            "bookable_start_time": "09:00:00",
            "bookable_end_time": "17:00:00",
        },
        format="json",
        **_headers(system_admin),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "New Room"
    assert body["offboarding_policy"] == "transfer"
    assert body["status"] == "active"
    assert Resource.objects.filter(id=body["id"]).exists()


@pytest.mark.django_db
def test_create_resource_rejects_fixed_offset_timezone(
    client: APIClient, system_admin: AppUser
) -> None:
    response = client.post(
        RESOURCES_URL,
        {
            "name": "Bad TZ Room",
            "timezone": "+01:00",
            "bookable_start_time": "09:00:00",
            "bookable_end_time": "17:00:00",
        },
        format="json",
        **_headers(system_admin),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_create_resource_as_non_admin_is_403(client: APIClient, app_user: AppUser) -> None:
    response = client.post(
        RESOURCES_URL,
        {
            "name": "New Room",
            "timezone": "UTC",
            "bookable_start_time": "09:00:00",
            "bookable_end_time": "17:00:00",
        },
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


# --------------------------------------------------------------------
# PATCH /resources/{id}
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_resource_as_resource_admin_succeeds(
    client: APIClient, app_user: AppUser, active_resource: Resource, system_admin: AppUser
) -> None:
    ResourceAdmin.objects.create(resource=active_resource, user=app_user, granted_by=system_admin)

    response = client.patch(
        f"{RESOURCES_URL}/{active_resource.id}",
        {"name": "Renamed Room", "status": "inactive"},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed Room"
    assert body["status"] == "inactive"

    active_resource.refresh_from_db()
    assert active_resource.name == "Renamed Room"
    assert active_resource.status == "inactive"


@pytest.mark.django_db
def test_patch_resource_partial_update_leaves_other_fields_untouched(
    client: APIClient, active_resource: Resource, system_admin: AppUser
) -> None:
    original_start = active_resource.bookable_start_time
    response = client.patch(
        f"{RESOURCES_URL}/{active_resource.id}",
        {"name": "Only Name Changed"},
        format="json",
        **_headers(system_admin),
    )
    assert response.status_code == 200
    active_resource.refresh_from_db()
    assert active_resource.name == "Only Name Changed"
    assert active_resource.bookable_start_time == original_start


@pytest.mark.django_db
def test_patch_resource_as_non_admin_is_403_not_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    # Spec v1.0 §5.14: "visible via GET, so existence isn't the protected
    # thing" — a non-admin gets 403, never 404, for a resource that
    # genuinely exists.
    response = client.patch(
        f"{RESOURCES_URL}/{active_resource.id}",
        {"name": "Hijacked"},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_patch_resource_invalid_bookable_window_is_400(
    client: APIClient, active_resource: Resource, system_admin: AppUser
) -> None:
    response = client.patch(
        f"{RESOURCES_URL}/{active_resource.id}",
        {"bookable_start_time": "17:00:00", "bookable_end_time": "09:00:00"},
        format="json",
        **_headers(system_admin),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_no_delete_endpoint_exists_for_resources(
    client: APIClient, active_resource: Resource, system_admin: AppUser
) -> None:
    response = client.delete(f"{RESOURCES_URL}/{active_resource.id}", **_headers(system_admin))
    assert response.status_code == 405


# --------------------------------------------------------------------
# POST/DELETE /resources/{id}/admins
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_grant_resource_admin_as_system_admin_succeeds(
    client: APIClient, active_resource: Resource, system_admin: AppUser
) -> None:
    grantee = AppUser.objects.create(email="grantee@example.com", display_name="Grantee")
    response = client.post(
        f"{RESOURCES_URL}/{active_resource.id}/admins",
        {"user_id": str(grantee.id)},
        format="json",
        **_headers(system_admin),
    )
    assert response.status_code == 201
    assert ResourceAdmin.objects.filter(resource=active_resource, user=grantee).exists()


@pytest.mark.django_db
def test_grant_resource_admin_already_granted_is_409(
    client: APIClient, active_resource: Resource, system_admin: AppUser
) -> None:
    grantee = AppUser.objects.create(email="grantee2@example.com", display_name="Grantee")
    ResourceAdmin.objects.create(resource=active_resource, user=grantee, granted_by=system_admin)

    response = client.post(
        f"{RESOURCES_URL}/{active_resource.id}/admins",
        {"user_id": str(grantee.id)},
        format="json",
        **_headers(system_admin),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_resource_admin"


@pytest.mark.django_db
def test_revoke_resource_admin_succeeds(
    client: APIClient, active_resource: Resource, system_admin: AppUser
) -> None:
    grantee = AppUser.objects.create(email="grantee3@example.com", display_name="Grantee")
    ResourceAdmin.objects.create(resource=active_resource, user=grantee, granted_by=system_admin)

    response = client.delete(
        f"{RESOURCES_URL}/{active_resource.id}/admins/{grantee.id}", **_headers(system_admin)
    )
    assert response.status_code == 204
    assert not ResourceAdmin.objects.filter(resource=active_resource, user=grantee).exists()


@pytest.mark.django_db
def test_revoke_resource_admin_nonexistent_grant_is_404(
    client: APIClient, active_resource: Resource, system_admin: AppUser
) -> None:
    response = client.delete(
        f"{RESOURCES_URL}/{active_resource.id}/admins/{uuid.uuid4()}", **_headers(system_admin)
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_grant_resource_admin_as_non_admin_is_403(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    grantee = AppUser.objects.create(email="grantee4@example.com", display_name="Grantee")
    response = client.post(
        f"{RESOURCES_URL}/{active_resource.id}/admins",
        {"user_id": str(grantee.id)},
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 403


# --------------------------------------------------------------------
# GET /admin/resources/{id}/utilization
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_utilization_returns_correct_aggregates(
    client: APIClient, app_user: AppUser, active_resource: Resource, system_admin: AppUser
) -> None:
    now = timezone.now()
    start = now + timedelta(days=1)
    Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start, start + timedelta(hours=1))
    )
    start2 = now + timedelta(days=2)
    Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start2, start2 + timedelta(minutes=30))
    )
    cancelled_start = now + timedelta(days=3)
    cancelled = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(cancelled_start, cancelled_start + timedelta(hours=1)),
    )
    cancelled.status = "cancelled"
    cancelled.cancelled_at = now
    cancelled.save(update_fields=["status", "cancelled_at"])

    WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(now + timedelta(days=4), now + timedelta(days=4, hours=1)),
    )

    from_param = (now - timedelta(days=1)).isoformat()
    to_param = (now + timedelta(days=10)).isoformat()
    response = client.get(
        f"/api/v1/admin/resources/{active_resource.id}/utilization",
        {"from": from_param, "to": to_param},
        **_headers(system_admin),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resource_id"] == str(active_resource.id)
    assert body["total_bookings"] == 2
    assert body["total_booked_minutes"] == 90
    assert body["waitlist_joins"] == 1
    assert body["cancellation_count"] == 1


@pytest.mark.django_db
def test_utilization_as_non_admin_is_403(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    now = timezone.now()
    response = client.get(
        f"/api/v1/admin/resources/{active_resource.id}/utilization",
        {"from": now.isoformat(), "to": (now + timedelta(days=1)).isoformat()},
        **_headers(app_user),
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_utilization_accessible_to_resource_admin(
    client: APIClient, app_user: AppUser, active_resource: Resource, system_admin: AppUser
) -> None:
    ResourceAdmin.objects.create(resource=active_resource, user=app_user, granted_by=system_admin)
    now = timezone.now()
    response = client.get(
        f"/api/v1/admin/resources/{active_resource.id}/utilization",
        {"from": now.isoformat(), "to": (now + timedelta(days=1)).isoformat()},
        **_headers(app_user),
    )
    assert response.status_code == 200
