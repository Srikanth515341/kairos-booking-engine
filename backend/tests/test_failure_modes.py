"""FAIL-01, FAIL-02 (Test Plan v1.0 §12; Implementation Plan Phase 28) —
real HTTP-level proofs that a failover-shaped and a lock-contention-shaped
database failure both degrade correctly through the ACTUAL `POST
/api/v1/bookings` endpoint: a 503 with `Retry-After`, never a hang, never a
500, never a 409, and a retry with the identical `Idempotency-Key`
resolves unambiguously afterward. `_handle_write_database_error`'s own
SQLSTATE-to-cause mapping (kairos/bookings/services.py) is already unit-
tested directly in tests/test_metrics.py and tests/bookings/test_services.py
— what's new here is proving the full request/response contract end to
end, the same escalation Phase 4's own service-layer tests got extended to
an HTTP boundary by Phase 9 (see tests/bookings/test_views.py). FAIL-03
(replica lag) is tests/test_replica.py + tests/resources/test_views.py —
kept separate since it's a read-path concern, not a write-path failure.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import psycopg
import pytest
from django.db import OperationalError
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.core import db as core_db
from kairos.core.constants import FAILOVER_RETRY_AFTER_SECONDS
from kairos.core.models import IdempotencyKey
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from tests.concurrency.harness import django_test_dsn, range_literal

BOOKINGS_URL = "/api/v1/bookings"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser, idempotency_key: uuid.UUID) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key),
    }


# --------------------------------------------------------------------
# FAIL-01 — primary failover mid-write
# --------------------------------------------------------------------


class _FakeConnectionLevelFailureCause(Exception):
    # A genuine connection-level failure (server unreachable, mid-election)
    # carries NO sqlstate at all — SQLSTATEs come from a server response,
    # and a connection that never reached one has none
    # (kairos.bookings.services._handle_write_database_error's own
    # docstring). This is the failover-shaped case, not a Class-57 code.
    sqlstate = None


@pytest.mark.django_db
def test_fail_01_primary_failover_mid_write_returns_503_then_retry_resolves_unambiguously(
    app_user: AppUser, active_resource: Resource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the primary failing over WHILE the booking INSERT is in
    flight: the very first `Booking.objects.create(...)` call raises a
    connection-level failure; every subsequent call (the retry, once the
    "election" is simulated as over) behaves normally. Asserts the full
    FAIL-01 contract: 503 (never a hang, never a 500), `Retry-After`
    present, `cause="failover"` (the longer wait Rollout v1.0 §6.2 asks
    for, distinct from ordinary lock contention), nothing recorded under
    the idempotency key (the outcome was genuinely unknown), and a retry
    presenting the SAME key resolves unambiguously — a real 201, exactly
    one booking, never a second attempt confused about what happened to
    the first.
    """
    client = APIClient()
    original_create = Booking.objects.create
    call_count = {"n": 0}

    def _fail_once_then_succeed(*args: object, **kwargs: object) -> Booking:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OperationalError("simulated primary failover") from (
                _FakeConnectionLevelFailureCause()
            )
        return original_create(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(Booking.objects, "create", _fail_once_then_succeed)

    start = timezone.now() + timedelta(hours=1)
    key = uuid.uuid4()
    payload = {
        "resource_id": str(active_resource.id),
        "start": _iso(start),
        "end": _iso(start + timedelta(hours=1)),
    }

    first = client.post(BOOKINGS_URL, data=payload, format="json", **_headers(app_user, key))
    assert first.status_code == 503
    assert first.json()["error"]["code"] == "service_unavailable"
    assert first.headers["Retry-After"] == str(FAILOVER_RETRY_AFTER_SECONDS)
    assert getattr(first, "kairos_error_cause", None) == "failover"

    # Spec v1.0 §5.1: the outcome of a 503 is unknown, not decided —
    # nothing is recorded under this key at all. Ground truth, not just
    # the absence of a 201.
    assert Booking.objects.filter(resource=active_resource).count() == 0
    assert not IdempotencyKey.objects.filter(user=app_user, key=key).exists()

    retry = client.post(BOOKINGS_URL, data=payload, format="json", **_headers(app_user, key))
    assert retry.status_code == 201
    assert "Idempotent-Replay" not in retry.headers  # a genuinely fresh attempt, not a replay
    assert Booking.objects.filter(resource=active_resource).count() == 1


# --------------------------------------------------------------------
# FAIL-02 — lock timeout returns 503, not 500, not 409
# --------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_fail_02_lock_timeout_returns_503_then_retry_resolves_unambiguously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine reproduction, not simulation — the same technique
    tests/bookings/test_services.py's `test_lock_timeout_maps_to_service_
    unavailable` established, extended here through the REAL HTTP
    endpoint rather than calling `create_booking` directly: a real
    conflicting row held open, uncommitted, on an independent connection,
    with `lock_timeout` forced very short so the write path's own INSERT
    genuinely times out waiting on it. FAIL-02's full contract: 503 (never
    a 500 — this must not surface as an unhandled server error; never a
    409 — this is not the exclusion constraint firing, it's contention on
    an as-yet-undetermined outcome), `Retry-After` present. Once the
    blocking transaction releases the row, a retry with the identical
    `Idempotency-Key` resolves unambiguously.
    """
    monkeypatch.setattr(core_db, "WRITE_PATH_LOCK_TIMEOUT", "200ms")

    owner = AppUser.objects.create(email="fail02@example.com", display_name="Fail02 Owner")
    resource = Resource.objects.create(
        name="Fail02 Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59, 59),
        created_by=owner,
    )
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    key = uuid.uuid4()
    payload = {"resource_id": str(resource.id), "start": _iso(start), "end": _iso(end)}
    client = APIClient()

    blocking_conn = psycopg.connect(django_test_dsn())
    blocking_conn.autocommit = False
    try:
        with blocking_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO booking (id, resource_id, user_id, time_range, status, "
                "created_at) VALUES (%s, %s, %s, %s::tstzrange, 'confirmed', now())",
                (str(uuid.uuid4()), str(resource.id), str(owner.id), range_literal(start, end)),
            )
            # Deliberately not committed — held open for the duration of
            # the real HTTP request below, forcing its own INSERT to wait
            # on this row until lock_timeout fires.
            response = client.post(
                BOOKINGS_URL, data=payload, format="json", **_headers(owner, key)
            )
    finally:
        blocking_conn.rollback()
        blocking_conn.close()

    assert response.status_code == 503
    assert response.status_code != 500
    assert response.status_code != 409
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "Retry-After" in response.headers
    assert getattr(response, "kairos_error_cause", None) == "lock_contention"

    assert not IdempotencyKey.objects.filter(user=owner, key=key).exists()

    retry = client.post(BOOKINGS_URL, data=payload, format="json", **_headers(owner, key))
    assert retry.status_code == 201
    assert Booking.objects.filter(resource=resource).count() == 1
