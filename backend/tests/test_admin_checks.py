"""GET /api/v1/admin/checks/latest (Spec v1.0 §5.15) — RECON-03, RECON-04,
RECON-06 (Implementation Plan Phase 20; PRD v1.0 M2/M3; RFC v1.0 §14).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.bookings.tasks import (
    series_materialization_heartbeat_is_stale,
    tzdata_rematerialization_heartbeat_is_stale,
)
from kairos.core.constants import (
    OFFER_CASCADE_STALE_THRESHOLD_SECONDS,
    SERIES_MATERIALIZATION_STALE_THRESHOLD_SECONDS,
    TZDATA_REMATERIALIZATION_STALE_THRESHOLD_SECONDS,
)
from kairos.core.models import SystemCheckRun
from kairos.core.reconciliation import RECONCILIATION_FAILURE_MESSAGE, check_reconciliation
from kairos.identity.models import AppUser, AppUserPlatformRole
from kairos.resources.models import Resource
from kairos.waitlist.services import (
    HOLD_REAPER_INTERVAL_SECONDS,
    hold_reaper_heartbeat_is_stale,
    offer_cascade_heartbeat_is_stale,
)

CHECKS_URL = "/api/v1/admin/checks/latest"

DROP_CONSTRAINT_SQL = "ALTER TABLE booking DROP CONSTRAINT no_overlapping_bookings;"
RESTORE_CONSTRAINT_SQL = """
ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        resource_id WITH =,
        time_range  WITH &&
    )
    WHERE (status IN ('confirmed', 'held'));
"""


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def operations_user(db: None) -> AppUser:
    return AppUser.objects.create(
        email="ops@example.com", display_name="Ops", platform_role=AppUserPlatformRole.OPERATIONS
    )


@pytest.fixture
def system_admin(db: None) -> AppUser:
    return AppUser.objects.create(
        email="sysadmin-checks@example.com",
        display_name="Sys Admin",
        platform_role=AppUserPlatformRole.SYSTEM_ADMIN,
    )


# --------------------------------------------------------------------
# Permission and response shape
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_regular_user_gets_403(client: APIClient, app_user: AppUser) -> None:
    response = client.get(CHECKS_URL, **_headers(app_user))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


@pytest.mark.django_db
def test_operations_can_read(client: APIClient, operations_user: AppUser) -> None:
    response = client.get(CHECKS_URL, **_headers(operations_user))
    assert response.status_code == 200


@pytest.mark.django_db
def test_system_admin_can_read(client: APIClient, system_admin: AppUser) -> None:
    response = client.get(CHECKS_URL, **_headers(system_admin))
    assert response.status_code == 200


@pytest.mark.django_db
def test_response_lists_all_six_checks_in_spec_order(
    client: APIClient, system_admin: AppUser
) -> None:
    response = client.get(CHECKS_URL, **_headers(system_admin))
    body = response.json()
    names = [c["check_name"] for c in body["checks"]]
    assert names == [
        "schema_assertion",
        "reconciliation",
        "hold_reaper",
        "offer_cascade",
        "series_materialization",
        "tzdata_rematerialization",
    ]


@pytest.mark.django_db
def test_operations_user_sees_all_six_checks_in_spec_order(
    client: APIClient, operations_user: AppUser
) -> None:
    """§10 row 65 (Implementation Plan Phase 28 audit): the six-check,
    spec-ordered content assertion above (`test_response_lists_all_six_
    checks_in_spec_order`) only ever exercised the `system_admin` fixture
    — no test previously proved an `operations`-role caller sees the same
    full payload, only that they get a bare 200 (`test_operations_can_
    read`). Both roles are named together everywhere else this endpoint's
    permission gate is described (Spec v1.0 §5.15: "operations/
    system_admin"), so both deserve the identical content proof.
    """
    response = client.get(CHECKS_URL, **_headers(operations_user))
    assert response.status_code == 200
    body = response.json()
    names = [c["check_name"] for c in body["checks"]]
    assert names == [
        "schema_assertion",
        "reconciliation",
        "hold_reaper",
        "offer_cascade",
        "series_materialization",
        "tzdata_rematerialization",
    ]


@pytest.mark.django_db
def test_a_check_that_has_never_run_reports_null_not_a_fake_pass(
    client: APIClient, system_admin: AppUser
) -> None:
    # Nothing in this test has ever run any check — absence must be
    # reported honestly (RFC v1.0 §14), never collapsed into a
    # misleadingly reassuring default status.
    response = client.get(CHECKS_URL, **_headers(system_admin))
    body = response.json()
    for check in body["checks"]:
        assert check["last_run_at"] is None
        assert check["status"] is None
        assert check["findings"] == {}


# --------------------------------------------------------------------
# RECON-03 / RECON-04 — end-to-end path and alert text
# --------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_recon_03_and_04_violation_surfaces_through_the_real_endpoint_with_correct_text(
    system_admin: AppUser,
) -> None:
    """RECON-03: seed a violation, run the FULL scheduled job (not the
    bare query), query the real endpoint. RECON-04: the alert states the
    guarantee was REMOVED, explicitly not that a race occurred.
    `transaction=True` for the identical reason test_reconciliation.py's
    RECON-01 needs it (DELETE + DDL in one transaction conflicts).
    """
    api = APIClient()
    user = AppUser.objects.create(email="recon03-user@example.com", display_name="U")
    resource = Resource.objects.create(
        name="Recon03 Room",
        timezone="UTC",
        bookable_start_time="00:00:00",
        bookable_end_time="23:59:00",
        created_by=user,
    )
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    overlap_start = start + timedelta(minutes=30)
    overlap_end = overlap_start + timedelta(hours=1)

    with connection.cursor() as cur:
        cur.execute(DROP_CONSTRAINT_SQL)
    try:
        b1 = Booking.objects.create(resource=resource, user=user, time_range=(start, end))
        b2 = Booking.objects.create(
            resource=resource, user=user, time_range=(overlap_start, overlap_end)
        )

        # RECON-03: the full job — not find_overlapping_active_bookings()
        # directly — is what a scheduled Celery Beat run actually calls.
        job_findings = check_reconciliation()
        assert job_findings["overlaps_found"] == 1

        response = api.get(CHECKS_URL, **_headers(system_admin))
        assert response.status_code == 200
        checks = {c["check_name"]: c for c in response.json()["checks"]}
        reconciliation = checks["reconciliation"]
        assert reconciliation["status"] == "fail"
        assert reconciliation["findings"]["overlaps_found"] == 1
        assert str(b1.id) in reconciliation["findings"]["flagged_booking_ids"]
        assert str(b2.id) in reconciliation["findings"]["flagged_booking_ids"]

        # RECON-04: the exact alert-text assertion — states the
        # guarantee was REMOVED, and explicitly negates "a race
        # occurred" as the interpretation, rather than merely omitting
        # the word "race" (an on-call engineer searching the payload for
        # "race" must find it explicitly ruled out, not just absent).
        message = reconciliation["findings"]["message"]
        assert message == RECONCILIATION_FAILURE_MESSAGE
        assert "removed" in message.lower()
        assert "not a race detector" in message.lower()
        assert "not that a race occurred" in message.lower()

        b2.delete()
    finally:
        with connection.cursor() as cur:
            cur.execute(RESTORE_CONSTRAINT_SQL)


# --------------------------------------------------------------------
# RECON-06 — background-job heartbeats
# --------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("check_name", "stale_fn", "threshold_seconds"),
    [
        (
            SystemCheckRun.CheckName.HOLD_REAPER,
            hold_reaper_heartbeat_is_stale,
            HOLD_REAPER_INTERVAL_SECONDS * 3,
        ),
        (
            SystemCheckRun.CheckName.OFFER_CASCADE,
            offer_cascade_heartbeat_is_stale,
            OFFER_CASCADE_STALE_THRESHOLD_SECONDS,
        ),
        (
            SystemCheckRun.CheckName.SERIES_MATERIALIZATION,
            series_materialization_heartbeat_is_stale,
            SERIES_MATERIALIZATION_STALE_THRESHOLD_SECONDS,
        ),
        (
            SystemCheckRun.CheckName.TZDATA_REMATERIALIZATION,
            tzdata_rematerialization_heartbeat_is_stale,
            TZDATA_REMATERIALIZATION_STALE_THRESHOLD_SECONDS,
        ),
    ],
)
def test_recon_06_each_named_job_produces_a_stale_heartbeat_alert(
    check_name: str, stale_fn, threshold_seconds: int
) -> None:
    """Each of the four background jobs RECON-06 names, individually
    stalled — Test Plan v1.0's own "controllable time" requirement (§13):
    a seeded old `system_check_run` row, not a real multi-hour wait.
    """
    now = timezone.now()

    # No run at all — absence is itself stale.
    assert stale_fn(now=now) is True

    # A run just past the threshold — stale.
    SystemCheckRun.objects.create(
        check_name=check_name,
        status=SystemCheckRun.Status.PASS,
        findings={},
    )
    SystemCheckRun.objects.filter(check_name=check_name).update(
        run_at=now - timedelta(seconds=threshold_seconds + 1)
    )
    assert stale_fn(now=now) is True

    # A fresh run — not stale.
    SystemCheckRun.objects.create(
        check_name=check_name,
        status=SystemCheckRun.Status.PASS,
        findings={},
    )
    assert stale_fn(now=now) is False
