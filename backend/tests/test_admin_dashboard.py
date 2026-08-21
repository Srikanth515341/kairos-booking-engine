"""GET /api/v1/admin/dashboard + GET /admin/dashboard/ (Implementation
Plan Phase 21) — the self-contained JSON + HTML dashboard the Definition
of Done requires "reachable and showing live values."
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.core.alerting import fire_or_resolve
from kairos.core.models import AlertKey
from kairos.core.rate_limit import BookingCreatePrincipalThrottle, TokenBucket
from kairos.identity.models import AppUser, AppUserPlatformRole
from kairos.resources.models import Resource

DASHBOARD_URL = "/api/v1/admin/dashboard"
DASHBOARD_PAGE_URL = "/admin/dashboard/"
BOOKINGS_URL = "/api/v1/bookings"


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
    # Rate limiting is real as of Phase 22 (Phase 21 left this an honest
    # "not available" slot) — see test_metrics.py and test_rate_limit.py
    # for the mechanism itself; this just confirms the dashboard reports
    # the real shape now, not the old placeholder.
    assert metrics["rate_limiting"]["available"] is True
    assert "total_429" in metrics["rate_limiting"]
    assert "by_cause" in metrics["rate_limiting"]
    assert "top_principals" in metrics["rate_limiting"]


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
def test_dashboard_reflects_a_real_429_event_after_it_happens(
    settings, monkeypatch, client: APIClient, operations_user: AppUser
) -> None:
    """Implementation Plan Phase 22's own follow-up instruction: the
    dashboard must show LIVE rate-limit data, not the honest-but-now-
    stale "not available" state Phase 21 shipped — proven here by
    actually tripping the real throttle over real HTTP and then reading
    it back through the real dashboard endpoint, not by constructing a
    RequestMetric row directly (that's test_metrics.py's job).
    """
    settings.RATE_LIMIT_ENABLED = True
    monkeypatch.setattr(
        BookingCreatePrincipalThrottle, "_bucket", TokenBucket(capacity=1, window_seconds=60)
    )
    booker = AppUser.objects.create(
        email=f"dash-booker-{uuid.uuid4()}@example.com", display_name="Booker"
    )
    resource = Resource.objects.create(
        name="Dashboard RL Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=booker,
    )
    start = (timezone.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)

    def _post(offset_minutes: int) -> object:
        request_start = start + timedelta(minutes=offset_minutes)
        request_end = end + timedelta(minutes=offset_minutes)
        return client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(resource.id),
                "start": request_start.isoformat().replace("+00:00", "Z"),
                "end": request_end.isoformat().replace("+00:00", "Z"),
            },
            format="json",
            HTTP_X_DEV_USER_ID=str(booker.id),
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )

    # Before: dashboard shows no rate-limiting activity at all.
    before = client.get(DASHBOARD_URL, **_headers(operations_user)).json()
    assert before["metrics"]["rate_limiting"]["total_429"] == 0

    assert _post(0).status_code == 201  # uses the bucket's one token
    throttled_response = _post(60)
    assert throttled_response.status_code == 429  # trips it for real

    after = client.get(DASHBOARD_URL, **_headers(operations_user)).json()
    rate_limiting = after["metrics"]["rate_limiting"]
    assert rate_limiting["available"] is True
    assert rate_limiting["total_429"] >= 1
    assert rate_limiting["by_cause"].get("per_principal_token_bucket", 0) >= 1
    assert any(p["principal_id"] == str(booker.id) for p in rate_limiting["top_principals"])


@pytest.mark.django_db
def test_dashboard_html_page_is_reachable_without_a_template_engine() -> None:
    client = APIClient()
    response = client.get(DASHBOARD_PAGE_URL)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/html")
    assert b"Kairos Operations Dashboard" in response.content
    assert b"/api/v1/admin/dashboard" in response.content
