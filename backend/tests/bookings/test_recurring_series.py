"""POST /api/v1/bookings/recurring/preview, POST /api/v1/bookings/recurring,
POST /api/v1/recurring-series/{id}/cancel (Implementation Plan Phase 12;
RFC v1.0 §5d; Spec v1.0 §5.8-§5.10; Test Plan REC-01 through REC-07,
IDEM-05).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.core.models import AuditLog
from kairos.identity.models import AppUser, ResourceAdmin
from kairos.resources.models import Resource

PREVIEW_URL = "/api/v1/bookings/recurring/preview"
CONFIRM_URL = "/api/v1/bookings/recurring"


def _cancel_url(series_id: str) -> str:
    return f"/api/v1/recurring-series/{series_id}/cancel"


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


def _confirm_headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def _preview_body(
    resource: Resource,
    *,
    series_start_date: date,
    occurrence_count: int = 4,
    local_start: str = "10:00:00",
    local_end: str = "10:30:00",
    tz: str = "UTC",
    weekday: int = 0,
) -> dict[str, Any]:
    return {
        "resource_id": str(resource.id),
        "timezone": tz,
        "local_start_time": local_start,
        "local_end_time": local_end,
        "weekday": weekday,
        "series_start_date": series_start_date.isoformat(),
        "occurrence_count": occurrence_count,
    }


def _book(resource: Resource, user: AppUser, on: date, at: time = time(10, 0)) -> Booking:
    start = datetime.combine(on, at, tzinfo=UTC)
    return Booking.objects.create(
        resource=resource, user=user, time_range=(start, start + timedelta(minutes=30))
    )


def _confirm(
    client: APIClient,
    user: AppUser,
    token: str,
    *,
    acknowledged_conflicts: list[str] | None = None,
    acknowledged_adjustments: list[str] | None = None,
    idempotency_key: uuid.UUID | None = None,
) -> Any:
    return client.post(
        CONFIRM_URL,
        {
            "preview_token": token,
            "acknowledged_conflicts": acknowledged_conflicts or [],
            "acknowledged_adjustments": acknowledged_adjustments or [],
        },
        format="json",
        **_confirm_headers(user, idempotency_key),
    )


@pytest.mark.django_db
def test_rec_01_preview_creates_zero_booking_rows(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    conflict_date = start_date + timedelta(days=14)  # occurrence 3 (0-indexed: 2)
    _book(active_resource, app_user, conflict_date)  # the one pre-existing row

    response = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=4),
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["would_create"]) == 3
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["occurrence_date"] == conflict_date.isoformat()
    assert "preview_token" in body
    # Ground truth: exactly the ONE pre-existing row — preview wrote nothing.
    assert Booking.objects.count() == 1


@pytest.mark.django_db
def test_rec_02_confirm_without_acknowledgment_is_rejected(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    conflict_date = start_date + timedelta(days=14)
    _book(active_resource, app_user, conflict_date)

    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=4),
        format="json",
        **_headers(app_user),
    ).json()

    response = _confirm(client, app_user, preview["preview_token"], acknowledged_conflicts=[])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "unacknowledged_conflicts"
    # Ground truth: still exactly the one pre-existing row.
    assert Booking.objects.count() == 1


@pytest.mark.django_db
def test_rec_03_conflict_arising_between_preview_and_confirm(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()

    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=4),
        format="json",
        **_headers(app_user),
    ).json()
    assert preview["conflicts"] == []

    # Another user takes occurrence 4's exact slot AFTER the clean preview.
    other_user = AppUser.objects.create(email="rec03-other@example.com", display_name="Other")
    occ4_date = start_date + timedelta(days=21)
    _book(active_resource, other_user, occ4_date)

    response = _confirm(client, app_user, preview["preview_token"], acknowledged_conflicts=[])
    assert response.status_code == 207
    body = response.json()
    assert len(body["created"]) == 3
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["occurrence_date"] == occ4_date.isoformat()
    assert body["conflicts"][0]["acknowledged"] is False


@pytest.mark.django_db
def test_rec_04_expired_token_returns_409(
    client: APIClient, app_user: AppUser, active_resource: Resource, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kairos.bookings.recurring_series.PREVIEW_TOKEN_TTL_SECONDS", -1)
    start_date = (timezone.now() + timedelta(days=7)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=1),
        format="json",
        **_headers(app_user),
    ).json()

    response = _confirm(client, app_user, preview["preview_token"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "preview_expired"
    assert Booking.objects.count() == 0


@pytest.mark.django_db
def test_rec_05_per_occurrence_transaction_isolation(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    occ3_date = start_date + timedelta(days=14)  # index 2
    occ6_date = start_date + timedelta(days=35)  # index 5
    occ7_date = start_date + timedelta(days=42)  # index 6
    _book(active_resource, app_user, occ3_date)
    _book(active_resource, app_user, occ7_date)

    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=10),
        format="json",
        **_headers(app_user),
    ).json()
    assert len(preview["conflicts"]) == 2
    ack = [c["occurrence_date"] for c in preview["conflicts"]]

    response = _confirm(client, app_user, preview["preview_token"], acknowledged_conflicts=ack)
    assert response.status_code == 207
    body = response.json()
    assert len(body["created"]) == 8
    assert len(body["conflicts"]) == 2
    assert all(c["acknowledged"] for c in body["conflicts"])

    # Occurrence 6 committed even though occurrence 7 failed — each
    # occurrence commits in its OWN transaction (RFC v1.0 §5d), never one
    # shared transaction across the series.
    assert Booking.objects.filter(
        resource=active_resource, starts_at__date=occ6_date, status="confirmed"
    ).exists()


@pytest.mark.django_db
def test_confirm_writes_one_audit_row_per_created_occurrence(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Implementation Plan Phase 12's explicit instruction: each
    occurrence gets its own audit row, correctly attributed. This does
    NOT by itself prove independent COMMITS — see the correction below and
    REC-05 (test_rec_05_per_occurrence_transaction_isolation) for that
    proof, which is airtight; this test's job is the attribution half.
    """
    start_date = (timezone.now() + timedelta(days=7)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=5),
        format="json",
        **_headers(app_user),
    ).json()

    response = _confirm(client, app_user, preview["preview_token"])
    assert response.status_code == 207
    created = response.json()["created"]
    assert len(created) == 5
    booking_ids = [c["booking_id"] for c in created]

    audit_rows = list(
        AuditLog.objects.filter(
            entity_type="booking", entity_id__in=booking_ids, action="insert"
        ).order_by("occurred_at")
    )
    assert len(audit_rows) == 5
    assert {str(r.entity_id) for r in audit_rows} == set(booking_ids)
    assert all(r.actor_id == app_user.id for r in audit_rows)
    assert all(r.actor_type == "user" for r in audit_rows)
    # NOT asserted here: that distinct occurred_at values prove independent
    # transactions. Verified directly against a real connection (see the
    # investigation this correction is based on): Django's Now() compiles
    # to Postgres's statement_timestamp(), which advances on every
    # cur.execute() call regardless of transaction boundaries — five
    # separate INSERT statements inside ONE shared transaction would ALSO
    # produce five distinct timestamps. An earlier version of this test's
    # docstring claimed otherwise; that claim was wrong and has been
    # removed rather than left uncorrected. REC-05
    # (test_rec_05_per_occurrence_transaction_isolation) is the test that
    # actually proves independent commits: occurrence 6 survives
    # occurrence 7's real, induced conflict, which is impossible if they
    # shared a transaction — any statement failure aborts the WHOLE
    # transaction in Postgres unless a savepoint catches it, and none does
    # here (create_booking's `except DatabaseError` only translates the
    # error and re-raises).


@pytest.mark.django_db
def test_rec_06_occurrence_count_100_is_valid(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    # 100 WEEKLY occurrences always span 99*7 = 693 days end to end — there
    # is no start date from "now" forward that keeps all 100 within the
    # 365-day horizon (that's REC-06's OTHER, independent bound, tested
    # separately below). Starting far enough in the past isolates the
    # occurrence_count=100 boundary on its own, without also tripping the
    # horizon check this endpoint does not implement past-dating rejection
    # for (Phase 12 flags that gap explicitly — see CLAUDE.md).
    start_date = (timezone.now() - timedelta(days=350)).date()
    response = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=100),
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_rec_06_occurrence_count_101_is_invalid(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    response = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=101),
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.django_db
def test_rec_06_occurrence_count_0_is_invalid(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    response = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=0),
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_rec_06_series_within_365_day_horizon_is_valid(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=350)).date()
    response = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=1),
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_rec_06_series_beyond_365_day_horizon_is_invalid(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=370)).date()
    response = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=1),
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.django_db
def test_rec_07_dst_series_preview_and_confirm_produce_identical_instants(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """TZ-01's exact series (Phase 11), run through the full preview ->
    confirm flow instead of calling expand_occurrences directly.
    """
    body = _preview_body(
        active_resource,
        series_start_date=date(2026, 10, 25),
        occurrence_count=4,
        tz="America/New_York",
    )
    preview_response = client.post(PREVIEW_URL, body, format="json", **_headers(app_user))
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["conflicts"] == []
    assert preview["time_adjustments"] == []
    preview_instants = [o["start"] for o in preview["would_create"]]
    assert preview_instants == [
        "2026-10-25T14:00:00Z",
        "2026-11-01T15:00:00Z",
        "2026-11-08T15:00:00Z",
        "2026-11-15T15:00:00Z",
    ]

    confirm_response = _confirm(client, app_user, preview["preview_token"])
    assert confirm_response.status_code == 207
    created = confirm_response.json()["created"]
    confirm_instants = [c["start"] for c in created]

    # The actual proof REC-07 asks for: preview and confirm produce
    # BYTE-IDENTICAL instants because both call the exact same
    # expand_occurrences function with the exact same inputs.
    assert confirm_instants == preview_instants


@pytest.mark.django_db
def test_idem_05_recurring_confirm_replay_returns_identical_arrays(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    conflict_date = start_date + timedelta(days=7)
    _book(active_resource, app_user, conflict_date)

    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=4),
        format="json",
        **_headers(app_user),
    ).json()
    ack = [c["occurrence_date"] for c in preview["conflicts"]]
    idem_key = uuid.uuid4()

    first = _confirm(
        client,
        app_user,
        preview["preview_token"],
        acknowledged_conflicts=ack,
        idempotency_key=idem_key,
    )
    assert first.status_code == 207
    first_body = first.json()

    second = _confirm(
        client,
        app_user,
        preview["preview_token"],
        acknowledged_conflicts=ack,
        idempotency_key=idem_key,
    )
    assert second.status_code == 207
    assert second.json() == first_body  # identical, not a fresh re-evaluation
    assert second["Idempotent-Replay"] == "true"

    # Ground truth: the replay did not create additional bookings.
    assert Booking.objects.filter(series_id=first_body["series_id"]).count() == len(
        first_body["created"]
    )


@pytest.mark.django_db
def test_confirm_rejects_a_token_issued_to_a_different_user(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    other_user = AppUser.objects.create(email="wrong-user@example.com", display_name="Wrong User")
    start_date = (timezone.now() + timedelta(days=7)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=1),
        format="json",
        **_headers(app_user),
    ).json()

    response = _confirm(client, other_user, preview["preview_token"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "preview_expired"


@pytest.mark.django_db
def test_preview_rejects_a_fixed_offset_timezone(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    response = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, tz="+01:00"),
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_recurring_series_cancel_only_cancels_future_confirmed_occurrences(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    # Occurrences at -10d, -3d (past) and +4d (future) relative to now.
    start_date = (timezone.now() - timedelta(days=10)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=3),
        format="json",
        **_headers(app_user),
    ).json()
    confirm = _confirm(client, app_user, preview["preview_token"]).json()
    assert len(confirm["created"]) == 3
    series_id = confirm["series_id"]

    response = client.post(_cancel_url(series_id), {}, format="json", **_confirm_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    assert body["occurrences_already_past"] == 2
    assert len(body["cancelled_booking_ids"]) == 1

    statuses = list(
        Booking.objects.filter(series_id=series_id)
        .order_by("starts_at")
        .values_list("status", flat=True)
    )
    assert statuses == ["confirmed", "confirmed", "cancelled"]


@pytest.mark.django_db
def test_recurring_series_cancel_with_no_future_occurrences_returns_200_and_empty_array(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """§10 row 48 (Implementation Plan Phase 28 audit): a series entirely
    in the past — every occurrence already `starts_at < now()` — must
    still be a real 200, with `cancelled_booking_ids == []` (nothing left
    to cancel, not an error) and `occurrences_already_past` covering every
    occurrence in the series. Every other series-cancel test has at least
    one future/uncancelled occurrence; this is the zero-future case none
    of them exercise.
    """
    start_date = (timezone.now() - timedelta(days=30)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=3),
        format="json",
        **_headers(app_user),
    ).json()
    confirm = _confirm(client, app_user, preview["preview_token"]).json()
    assert len(confirm["created"]) == 3
    series_id = confirm["series_id"]

    response = client.post(_cancel_url(series_id), {}, format="json", **_confirm_headers(app_user))
    assert response.status_code == 200
    body = response.json()
    assert body["cancelled_booking_ids"] == []
    assert body["occurrences_already_past"] == 3

    # Ground truth: nothing changed status.
    assert Booking.objects.filter(series_id=series_id, status="confirmed").count() == 3


@pytest.mark.django_db
def test_recurring_series_cancel_admin_override_without_reason_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    # PRD FR47: administrative override of another user's booking requires
    # a recorded reason — the identical rule single-booking cancel already
    # enforces, applied here since a series-cancel-by-admin is exactly
    # such an override.
    start_date = (timezone.now() + timedelta(days=7)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=2),
        format="json",
        **_headers(app_user),
    ).json()
    confirm = _confirm(client, app_user, preview["preview_token"]).json()

    admin = AppUser.objects.create(email="rec-admin@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    response = client.post(
        _cancel_url(confirm["series_id"]), {}, format="json", **_confirm_headers(admin)
    )
    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == "reason"
    # Ground truth: nothing was cancelled by the rejected attempt.
    assert Booking.objects.filter(series_id=confirm["series_id"], status="confirmed").count() == 2


@pytest.mark.django_db
def test_recurring_series_cancel_admin_override_with_reason_succeeds_and_is_recorded(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=2),
        format="json",
        **_headers(app_user),
    ).json()
    confirm = _confirm(client, app_user, preview["preview_token"]).json()

    admin = AppUser.objects.create(email="rec-admin2@example.com", display_name="Admin Two")
    ResourceAdmin.objects.create(resource=active_resource, user=admin, granted_by=app_user)

    response = client.post(
        _cancel_url(confirm["series_id"]),
        {"reason": "Room offline for maintenance"},
        format="json",
        **_confirm_headers(admin),
    )
    assert response.status_code == 200
    assert len(response.json()["cancelled_booking_ids"]) == 2

    reasons = set(
        Booking.objects.filter(series_id=confirm["series_id"]).values_list(
            "cancellation_reason", flat=True
        )
    )
    assert reasons == {"Room offline for maintenance"}


@pytest.mark.django_db
def test_recurring_series_cancel_by_non_owner_non_admin_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start_date = (timezone.now() + timedelta(days=7)).date()
    preview = client.post(
        PREVIEW_URL,
        _preview_body(active_resource, series_start_date=start_date, occurrence_count=1),
        format="json",
        **_headers(app_user),
    ).json()
    confirm = _confirm(client, app_user, preview["preview_token"]).json()

    stranger = AppUser.objects.create(email="stranger-rec@example.com", display_name="Stranger")
    response = client.post(
        _cancel_url(confirm["series_id"]), {}, format="json", **_confirm_headers(stranger)
    )
    assert response.status_code == 404
