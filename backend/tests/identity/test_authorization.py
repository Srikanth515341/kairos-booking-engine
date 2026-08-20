"""AuthorizationService (Implementation Plan Phase 9; RFC v1.0 §8.1) —
"scope logic exists in exactly one place." PRD FR44's four roles, and the
DoD's two explicit assertions: a booker cannot reach admin actions, and a
resource admin for Resource A cannot administer Resource B.
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.identity.authorization import AuthorizationService
from kairos.identity.models import AppUser, AppUserPlatformRole, ResourceAdmin
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"


def _headers(user: AppUser) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def resource_b(app_user: AppUser) -> Resource:
    return Resource.objects.create(
        name="Resource B",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=app_user,
    )


# --------------------------------------------------------------------
# Four roles (PRD FR44) — unit-level, against AuthorizationService directly
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_booker_cannot_administer_any_resource(
    app_user: AppUser, active_resource: Resource
) -> None:
    assert app_user.platform_role == AppUserPlatformRole.BOOKER
    assert AuthorizationService.can_administer_resource(app_user, active_resource.id) is False


@pytest.mark.django_db
def test_system_admin_can_administer_any_resource(active_resource: Resource) -> None:
    admin = AppUser.objects.create(
        email="sysadmin@example.com",
        display_name="Sys Admin",
        platform_role=AppUserPlatformRole.SYSTEM_ADMIN,
    )
    # No resource_admin grant exists for this user at all — system_admin
    # is a GLOBAL role (PRD FR44), never derived from a grant table.
    assert not ResourceAdmin.objects.filter(user=admin).exists()
    assert AuthorizationService.can_administer_resource(admin, active_resource.id) is True


@pytest.mark.django_db
def test_operations_can_view_but_not_edit_or_administer(
    app_user: AppUser, active_resource: Resource
) -> None:
    ops = AppUser.objects.create(
        email="ops@example.com",
        display_name="Ops",
        platform_role=AppUserPlatformRole.OPERATIONS,
    )
    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)),
    )
    assert AuthorizationService.can_view_booking(ops, booking) is True
    assert AuthorizationService.can_edit_booking(ops, booking) is False
    assert AuthorizationService.can_administer_resource(ops, active_resource.id) is False
    # Operations has no cancel authority either (Spec v1.0 §5.6) — only
    # owner or scoped resource admin.
    allowed, _ = AuthorizationService.can_cancel_booking(ops, booking)
    assert allowed is False


# --------------------------------------------------------------------
# Scoped administration — an admin for Resource A cannot administer
# Resource B (PRD FR45; explicit Phase 9 DoD item)
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_resource_admin_for_a_cannot_administer_b(
    app_user: AppUser, active_resource: Resource, resource_b: Resource
) -> None:
    admin_of_a = AppUser.objects.create(email="admin-a@example.com", display_name="Admin A")
    ResourceAdmin.objects.create(resource=active_resource, user=admin_of_a, granted_by=app_user)

    assert AuthorizationService.can_administer_resource(admin_of_a, active_resource.id) is True
    assert AuthorizationService.can_administer_resource(admin_of_a, resource_b.id) is False


@pytest.mark.django_db
def test_scoped_admin_cannot_cancel_booking_on_resource_they_do_not_administer(
    client: APIClient, app_user: AppUser, active_resource: Resource, resource_b: Resource
) -> None:
    """The DoD's exact scenario, exercised end-to-end through the real
    cancel endpoint, not just the service-level check above."""
    admin_of_a = AppUser.objects.create(email="admin-a2@example.com", display_name="Admin A2")
    ResourceAdmin.objects.create(resource=active_resource, user=admin_of_a, granted_by=app_user)

    booking_on_b = Booking.objects.create(
        resource=resource_b,
        user=app_user,
        time_range=(timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)),
    )

    response = client.post(
        f"{BOOKINGS_URL}/{booking_on_b.id}/cancel",
        data={"reason": "trying to override outside my scope"},
        format="json",
        **_headers(admin_of_a),
    )
    # 404, not 403 — Spec v1.0 §1: object-level protection, existence
    # itself isn't confirmed to someone without access.
    assert response.status_code == 404
    booking_on_b.refresh_from_db()
    assert booking_on_b.status == "confirmed"


@pytest.mark.django_db
def test_scoped_admin_can_cancel_booking_on_the_resource_they_administer(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    admin_of_a = AppUser.objects.create(email="admin-a3@example.com", display_name="Admin A3")
    ResourceAdmin.objects.create(resource=active_resource, user=admin_of_a, granted_by=app_user)

    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)),
    )

    response = client.post(
        f"{BOOKINGS_URL}/{booking.id}/cancel",
        data={"reason": "legitimate override within scope"},
        format="json",
        **_headers(admin_of_a),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
