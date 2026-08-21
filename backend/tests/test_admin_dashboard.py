"""GET /api/v1/admin/dashboard + GET /admin/dashboard/ (Implementation
Plan Phase 21) — the self-contained JSON + HTML dashboard the Definition
of Done requires "reachable and showing live values."
"""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from kairos.core.alerting import fire_or_resolve
from kairos.core.models import AlertKey
from kairos.identity.models import AppUser, AppUserPlatformRole

DASHBOARD_URL = "/api/v1/admin/dashboard"
DASHBOARD_PAGE_URL = "/admin/dashboard/"


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def operations_user(db: None) -> AppUser:
    return AppUser.objects.create(
        email="dash-ops@example.com",
        display_name="Ops",
        platform_role=AppUserPlatformRole.OPERATIONS,
    )


@pytest.fixture
def regular_user(db: None) -> AppUser:
    return AppUser.objects.create(email="dash-regular@example.com", display_name="Regular")


@pytest.mark.django_db
def test_regular_user_is_denied(client: APIClient, regular_user: AppUser) -> None:
    response = client.get(DASHBOARD_URL, **_headers(regular_user))
    assert response.status_code == 403


@pytest.mark.django_db
def test_operations_gets_full_shape_with_live_values(
    client: APIClient, operations_user: AppUser
) -> None:
    response = client.get(DASHBOARD_URL, **_headers(operations_user))
    assert response.status_code == 200
    body = response.json()

    assert [c["check_name"] for c in body["checks"]] == [
        "schema_assertion",
        "reconciliation",
        "hold_reaper",
        "offer_cascade",
        "series_materialization",
        "tzdata_rematerialization",
    ]
    assert body["open_alerts"] == []

    metrics = body["metrics"]
    assert "booking_write_p95_ms" in metrics
    assert "availability_read_p95_ms" in metrics
    assert metrics["error_503"]["total_requests"] >= 0
    assert metrics["auth_failures"]["total"] >= 0
    assert isinstance(metrics["redis_available"], bool)
    assert "total_keys" in metrics["idempotency"]
    assert metrics["audit_actor_unknown_count"] >= 0
    # Rate limiting is a genuine slot, not fabricated data (Phase 21's
    # own clarification #2) — Phase 22 dependency.
    assert metrics["rate_limiting"]["available"] is False


@pytest.mark.django_db
def test_open_alerts_appear_on_the_dashboard(client: APIClient, operations_user: AppUser) -> None:
    fire_or_resolve(alert_key=AlertKey.HOLD_REAPER, active=True, message="test", context={"x": 1})
    response = client.get(DASHBOARD_URL, **_headers(operations_user))
    body = response.json()
    assert len(body["open_alerts"]) == 1
    assert body["open_alerts"][0]["alert_key"] == "hold_reaper"
    assert body["open_alerts"][0]["severity"] == "sev_2"

    fire_or_resolve(alert_key=AlertKey.HOLD_REAPER, active=False, message="test", context={})
    response = client.get(DASHBOARD_URL, **_headers(operations_user))
    assert response.json()["open_alerts"] == []


@pytest.mark.django_db
def test_dashboard_html_page_is_reachable_without_a_template_engine() -> None:
    client = APIClient()
    response = client.get(DASHBOARD_PAGE_URL)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert b"Kairos Operations Dashboard" in response.content
    assert b"/api/v1/admin/dashboard" in response.content
