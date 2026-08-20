"""Timezone foundation (Implementation Plan Phase 10; RFC v1.0 §9.1-§9.3;
Test Plan TZ-02, TZ-03 Test A, TZ-04). `kairos/core/timezones.py`'s
conversion and detection helpers are exercised directly as unit tests —
they have no HTTP surface of their own yet (recurrence, which consumes
them, is Phase 11). TZ-04 and the fixed-offset-rejection case are
integration tests against the one real write/read path that exists today:
resource creation (model-level; Phase 19 adds a real write endpoint) and
`GET /resources/{id}/availability`.
"""

from __future__ import annotations

import tomllib
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.core.exceptions import PolicyValidationError
from kairos.core.timezones import (
    is_ambiguous_local_time,
    is_nonexistent_local_time,
    local_to_instant,
    tzdata_version,
    validate_iana_zone,
)
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

pytestmark = pytest.mark.django_db

RESOURCES_URL = "/api/v1/resources"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------------
# TZ-02 — created in one DST regime, occurring in another (RFC v1.0 §9.1)
# --------------------------------------------------------------------


def test_tz_02_local_to_instant_uses_occurrence_dates_offset_not_creation_dates() -> None:
    # "Now" implied at Oct 20, 2026 (EDT) — deliberately carried on local_dt
    # to prove it is NOT what decides the offset. The occurrence is Nov 10,
    # 2026 (EST), passed separately as on_date.
    local_dt = datetime(2026, 10, 20, 10, 0)
    on_date = date(2026, 11, 10)

    instant = local_to_instant(local_dt, "America/New_York", on_date)

    # EST (UTC-5), correct for the occurrence date. The bug this guards
    # against is 14:00:00Z — EDT (UTC-4), the offset in effect at creation.
    assert instant.isoformat() == "2026-11-10T15:00:00+00:00"


def test_local_to_instant_zone_without_dst_is_unaffected() -> None:
    local_dt = datetime(2026, 1, 1, 10, 0)
    on_date = date(2026, 7, 15)

    instant = local_to_instant(local_dt, "Asia/Kolkata", on_date)

    assert instant.isoformat() == "2026-07-15T04:30:00+00:00"


# --------------------------------------------------------------------
# Nonexistent / ambiguous local time detection (RFC v1.0 §9.3, used by
# Phase 11) — Europe/Paris spring-forward and fall-back transitions,
# Test Plan TZ-05/TZ-06's exact dates.
# --------------------------------------------------------------------


def test_detects_nonexistent_local_time_paris_spring_forward() -> None:
    # Paris springs forward 02:00 -> 03:00 on 2027-03-28. 02:30 never
    # happens that day.
    naive_dt = datetime(2027, 3, 28, 2, 30)

    assert is_nonexistent_local_time(naive_dt, "Europe/Paris") is True
    assert is_ambiguous_local_time(naive_dt, "Europe/Paris") is False


def test_does_not_flag_an_ordinary_time_as_nonexistent() -> None:
    naive_dt = datetime(2027, 3, 28, 10, 0)

    assert is_nonexistent_local_time(naive_dt, "Europe/Paris") is False


def test_detects_ambiguous_local_time_paris_fall_back() -> None:
    # Paris falls back 03:00 -> 02:00 on 2027-10-31. 02:30 occurs twice.
    naive_dt = datetime(2027, 10, 31, 2, 30)

    assert is_ambiguous_local_time(naive_dt, "Europe/Paris") is True
    assert is_nonexistent_local_time(naive_dt, "Europe/Paris") is False


def test_does_not_flag_an_ordinary_time_as_ambiguous() -> None:
    naive_dt = datetime(2027, 10, 31, 10, 0)

    assert is_ambiguous_local_time(naive_dt, "Europe/Paris") is False


# --------------------------------------------------------------------
# IANA validation — a fixed offset is rejected (PRD FR8)
# --------------------------------------------------------------------


def test_validate_iana_zone_accepts_a_real_zone() -> None:
    validate_iana_zone("Europe/Paris")  # must not raise


def test_validate_iana_zone_rejects_a_fixed_offset() -> None:
    with pytest.raises(PolicyValidationError) as exc_info:
        validate_iana_zone("+01:00")
    assert exc_info.value.field == "timezone"


def test_resource_save_accepts_a_real_iana_zone(app_user: AppUser) -> None:
    resource = Resource(
        name="Paris Room",
        timezone="Europe/Paris",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=app_user,
    )
    resource.save()  # must not raise


def test_resource_save_rejects_a_fixed_offset_timezone(app_user: AppUser) -> None:
    # DoD: "A fixed UTC offset submitted as a timezone -> 400." No live
    # resource-write endpoint exists yet (Phase 19) — PolicyValidationError
    # is the exact exception kairos_exception_handler already turns into
    # 400 validation_error for every other write path, so this proves the
    # mechanism Phase 19's endpoint will reuse verbatim, the same way
    # Phase 8/9 proved trigger/session-variable mechanisms directly ahead
    # of their real callers.
    resource = Resource(
        name="Bad Room",
        timezone="+01:00",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=app_user,
    )
    with pytest.raises(PolicyValidationError) as exc_info:
        resource.save()
    assert exc_info.value.field == "timezone"
    assert not Resource.objects.filter(name="Bad Room").exists()


# --------------------------------------------------------------------
# tzdata pinning (Test Plan TZ-03 Test A)
# --------------------------------------------------------------------


def _pinned_tzdata_spec() -> str:
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text())
    for dependency in data["project"]["dependencies"]:
        if dependency.split("=")[0].split(">")[0].split("<")[0].strip() == "tzdata":
            return dependency
    raise AssertionError("tzdata is not declared in [project.dependencies]")


def test_tzdata_is_pinned_to_an_exact_version_in_pyproject() -> None:
    spec = _pinned_tzdata_spec()
    # Exact pin only — "==", never a range ("*", ">=", "~="). Environment
    # integrity (TZ-03's premise) requires staleness to be a deliberate,
    # tracked quantity rather than "whatever got resolved this run."
    assert "==" in spec, f"tzdata must be pinned with ==, got: {spec!r}"


def test_installed_tzdata_matches_the_pinned_version() -> None:
    spec = _pinned_tzdata_spec()
    pinned_version = spec.split("==", 1)[1].strip().strip('"').strip("'")
    assert tzdata_version() == pinned_version


# --------------------------------------------------------------------
# TZ-04 — cross-timezone viewing: identical UTC to every viewer
# --------------------------------------------------------------------


def test_tz_04_availability_returns_identical_utc_regardless_of_viewer(
    client: APIClient, app_user: AppUser
) -> None:
    resource = Resource.objects.create(
        name="Paris Meeting Room",
        timezone="Europe/Paris",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=app_user,
    )
    from_dt = timezone.now().replace(microsecond=0) + timedelta(days=1)
    to_dt = from_dt + timedelta(days=1)
    Booking.objects.create(
        resource=resource,
        user=app_user,
        time_range=(from_dt + timedelta(hours=1), from_dt + timedelta(hours=2)),
    )

    # Two different authenticated users — the backend has no concept of a
    # "viewer timezone" at all (Spec v1.0 §1: rendering is the client's
    # job), so any two viewers must see byte-identical UTC.
    viewer_a = AppUser.objects.create(email="tz04-a@example.com", display_name="Viewer A")
    viewer_b = AppUser.objects.create(email="tz04-b@example.com", display_name="Viewer B")

    params = {"from": _iso(from_dt), "to": _iso(to_dt)}
    response_a = client.get(
        f"{RESOURCES_URL}/{resource.id}/availability", params, **_headers(viewer_a)
    )
    response_b = client.get(
        f"{RESOURCES_URL}/{resource.id}/availability", params, **_headers(viewer_b)
    )

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    blocks_a = response_a.json()["busy_blocks"]
    blocks_b = response_b.json()["busy_blocks"]
    assert blocks_a == blocks_b
    assert len(blocks_a) == 1
    # Both end in "Z" — UTC, never a Paris-local offset — regardless of
    # which viewer asked.
    assert blocks_a[0]["start"].endswith("Z")
    assert blocks_a[0]["end"].endswith("Z")
