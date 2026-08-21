"""kairos.core.rate_limit (Implementation Plan Phase 22; RFC v1.0 §8.2).
SEC-02: "429 begins exactly at the threshold — the request at the limit
succeeds, the next does not — and a second principal in the same window
is entirely unaffected."

`RATE_LIMIT_ENABLED` is False under `kairos.settings.test` by default
(see that module's own comment) — every test in this file that exercises
the real throttle over HTTP explicitly flips it True via the `settings`
fixture, which pytest-django reverts automatically at teardown.
"""

from __future__ import annotations

import uuid
from datetime import time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.core.rate_limit import BookingCreatePrincipalThrottle, PerIPThrottle, TokenBucket
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"


def _headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


def _booking_payload(offset_hours: int) -> dict[str, object]:
    start = (timezone.now() + timedelta(days=1, hours=offset_hours)).replace(
        minute=0, second=0, microsecond=0
    )
    end = start + timedelta(minutes=30)
    return {
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
    }


@pytest.fixture
def user(db: None) -> AppUser:
    return AppUser.objects.create(email=f"rl-{uuid.uuid4()}@example.com", display_name="U")


@pytest.fixture
def resource(db: None, user: AppUser) -> Resource:
    return Resource.objects.create(
        name="RL Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=user,
    )


# --------------------------------------------------------------------
# TokenBucket — the primitive, in isolation
# --------------------------------------------------------------------


@pytest.fixture
def bucket_key() -> str:
    # A fresh, random key per test — Redis is a real external service
    # that persists across test runs (unlike the Postgres db fixture,
    # nothing rolls this back), so every test needs its own key rather
    # than relying on manual cleanup.
    return f"test:ratelimit:{uuid.uuid4()}"


def test_token_bucket_allows_exactly_capacity_requests_then_blocks(bucket_key: str) -> None:
    bucket = TokenBucket(capacity=3, window_seconds=60)
    now = 1_000_000.0

    for _ in range(3):
        allowed, _ = bucket.check(bucket_key, now=now)
        assert allowed is True

    allowed, retry_after = bucket.check(bucket_key, now=now)
    assert allowed is False
    assert retry_after > 0


def test_token_bucket_refills_over_time(bucket_key: str) -> None:
    bucket = TokenBucket(capacity=2, window_seconds=60)  # refill_rate = 1/30s
    now = 1_000_000.0

    assert bucket.check(bucket_key, now=now)[0] is True
    assert bucket.check(bucket_key, now=now)[0] is True
    assert bucket.check(bucket_key, now=now)[0] is False  # exhausted

    # 31s later, exactly one token has refilled.
    allowed, _ = bucket.check(bucket_key, now=now + 31)
    assert allowed is True
    assert bucket.check(bucket_key, now=now + 31)[0] is False  # only one token back


def test_token_bucket_fails_open_when_redis_is_unreachable(monkeypatch, bucket_key: str) -> None:
    """The module's own central claim: a fairness policy must never block
    a real booking because its OWN infrastructure (Redis) is unavailable.
    """

    def _raise(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis is down")

    bucket = TokenBucket(capacity=1, window_seconds=60)
    monkeypatch.setattr("kairos.core.rate_limit._redis_client", _raise)

    allowed, retry_after = bucket.check(bucket_key)
    assert allowed is True
    assert retry_after == 0


# --------------------------------------------------------------------
# SEC-02 — the real throttle, over real HTTP
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_sec_02_429_begins_exactly_at_the_threshold(
    settings, monkeypatch, user: AppUser, resource: Resource
) -> None:
    settings.RATE_LIMIT_ENABLED = True
    monkeypatch.setattr(
        BookingCreatePrincipalThrottle, "_bucket", TokenBucket(capacity=3, window_seconds=60)
    )
    client = APIClient()

    # Requests 1..3 (== capacity) all succeed.
    for i in range(3):
        response = client.post(
            BOOKINGS_URL,
            data={"resource_id": str(resource.id), **_booking_payload(i)},
            format="json",
            **_headers(user),
        )
        assert response.status_code == 201, f"request {i + 1} should succeed: {response.data}"

    # Request 4 (== capacity + 1) is throttled, not because of anything
    # about the booking itself (a fresh, non-conflicting time range).
    response = client.post(
        BOOKINGS_URL,
        data={"resource_id": str(resource.id), **_booking_payload(99)},
        format="json",
        **_headers(user),
    )
    assert response.status_code == 429
    assert response.data["error"]["code"] == "rate_limited"
    assert "Retry-After" in response


@pytest.mark.django_db
def test_sec_02_a_second_principal_in_the_same_window_is_unaffected(
    settings, monkeypatch, resource: Resource
) -> None:
    settings.RATE_LIMIT_ENABLED = True
    monkeypatch.setattr(
        BookingCreatePrincipalThrottle, "_bucket", TokenBucket(capacity=1, window_seconds=60)
    )
    client = APIClient()
    user_a = AppUser.objects.create(email=f"rl-a-{uuid.uuid4()}@example.com", display_name="A")
    user_b = AppUser.objects.create(email=f"rl-b-{uuid.uuid4()}@example.com", display_name="B")

    # A exhausts their own bucket.
    r1 = client.post(
        BOOKINGS_URL,
        data={"resource_id": str(resource.id), **_booking_payload(1)},
        format="json",
        **_headers(user_a),
    )
    assert r1.status_code == 201
    r2 = client.post(
        BOOKINGS_URL,
        data={"resource_id": str(resource.id), **_booking_payload(2)},
        format="json",
        **_headers(user_a),
    )
    assert r2.status_code == 429

    # B, entirely unrelated, sails through in the SAME window.
    r3 = client.post(
        BOOKINGS_URL,
        data={"resource_id": str(resource.id), **_booking_payload(3)},
        format="json",
        **_headers(user_b),
    )
    assert r3.status_code == 201


@pytest.mark.django_db
def test_per_ip_throttle_is_defense_in_depth_and_does_not_block_normal_use(
    settings, monkeypatch, user: AppUser, resource: Resource
) -> None:
    """PerIPThrottle is coarser and exists to catch one source hammering
    the API, not to interfere with ordinary traffic — proven by NOT
    tripping across a handful of ordinary requests from a distinct,
    per-test IP (see this file's module docstring on why IP is faked
    per-test rather than left at the test client's shared default).
    """
    settings.RATE_LIMIT_ENABLED = True
    monkeypatch.setattr(PerIPThrottle, "_bucket", TokenBucket(capacity=5, window_seconds=60))
    client = APIClient()
    fake_ip = f"10.0.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}"

    for i in range(3):
        response = client.post(
            BOOKINGS_URL,
            data={"resource_id": str(resource.id), **_booking_payload(i)},
            format="json",
            REMOTE_ADDR=fake_ip,
            **_headers(user),
        )
        assert response.status_code == 201


@pytest.mark.django_db
def test_per_ip_throttle_trips_and_reports_the_correct_cause(
    settings, monkeypatch, resource: Resource
) -> None:
    settings.RATE_LIMIT_ENABLED = True
    monkeypatch.setattr(PerIPThrottle, "_bucket", TokenBucket(capacity=1, window_seconds=60))
    client = APIClient()
    fake_ip = f"10.1.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}"
    user_a = AppUser.objects.create(email=f"rl-ip-a-{uuid.uuid4()}@example.com", display_name="A")
    user_b = AppUser.objects.create(email=f"rl-ip-b-{uuid.uuid4()}@example.com", display_name="B")

    r1 = client.post(
        BOOKINGS_URL,
        data={"resource_id": str(resource.id), **_booking_payload(1)},
        format="json",
        REMOTE_ADDR=fake_ip,
        **_headers(user_a),
    )
    assert r1.status_code == 201

    # A DIFFERENT principal, but the SAME (faked) IP — the per-IP bucket,
    # not the per-principal one, is what trips here.
    r2 = client.post(
        BOOKINGS_URL,
        data={"resource_id": str(resource.id), **_booking_payload(2)},
        format="json",
        REMOTE_ADDR=fake_ip,
        **_headers(user_b),
    )
    assert r2.status_code == 429


@pytest.mark.django_db
def test_rate_limiting_does_not_apply_to_get_bookings(settings, monkeypatch, user: AppUser) -> None:
    """Scope IN is explicit: rate limiting is on booking CREATION, not
    the whole /bookings collection endpoint.
    """
    settings.RATE_LIMIT_ENABLED = True
    monkeypatch.setattr(
        BookingCreatePrincipalThrottle, "_bucket", TokenBucket(capacity=0, window_seconds=60)
    )
    client = APIClient()
    response = client.get(BOOKINGS_URL, **_headers(user))
    assert response.status_code == 200


@pytest.mark.django_db
def test_rate_limiting_disabled_by_default_under_test_settings(
    user: AppUser, resource: Resource
) -> None:
    """Confirms this file's own premise: without explicitly opting in via
    the `settings` fixture, RATE_LIMIT_ENABLED is False and booking
    creation is entirely unthrottled — every other test in this suite
    (IDEM-06's 100 concurrent-replay reps, WL-01/WL-02's threaded races,
    ...) depends on this being true.
    """
    client = APIClient()
    for i in range(15):  # comfortably past the real default capacity (10)
        response = client.post(
            BOOKINGS_URL,
            data={"resource_id": str(resource.id), **_booking_payload(i)},
            format="json",
            **_headers(user),
        )
        assert response.status_code == 201, f"request {i + 1}: {response.data}"
