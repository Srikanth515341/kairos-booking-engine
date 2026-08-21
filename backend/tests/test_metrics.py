"""kairos.core.metrics + kairos.core.middleware.MetricsMiddleware
(Implementation Plan Phase 21; Rollout v1.0 §6.2): booking-write/
availability-read P95, 503 rate split by cause, auth failure rate by
shape, Redis availability, idempotency-key growth + cleanup heartbeat,
audit actor_type='unknown' count, and the rate-limit metric slot.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.db import DatabaseError
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient

from kairos.bookings.services import (
    FAILOVER_SQLSTATES,
    RETRYABLE_SQLSTATES,
    _handle_write_database_error,
)
from kairos.core.drf import kairos_exception_handler
from kairos.core.exceptions import ServiceUnavailableError
from kairos.core.metrics import (
    audit_actor_unknown_count,
    auth_failure_rate_by_shape,
    classify_metric_type,
    error_rate_by_cause,
    idempotency_key_stats,
    p95_duration_ms,
    prune_old_request_metrics,
    rate_limit_metric_slot,
    record_request_metric,
    redis_availability,
)
from kairos.core.models import (
    AuditActorType,
    AuditLog,
    IdempotencyKeyStatus,
    OperationalHeartbeat,
    RequestMetric,
)
from kairos.identity.models import AppUser, AppUserStatus
from kairos.identity.oidc import issue_session_token

CHECKS_URL = "/api/v1/admin/checks/latest"


class _FakeSqlstateCause(Exception):
    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _db_error(sqlstate: str | None) -> DatabaseError:
    exc = DatabaseError("boom")
    exc.__cause__ = _FakeSqlstateCause(sqlstate) if sqlstate is not None else None
    return exc


# --------------------------------------------------------------------
# classify_metric_type
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method", "status_code", "expected"),
    [
        ("/api/v1/bookings", "POST", 201, RequestMetric.Type.BOOKING_WRITE),
        ("/api/v1/bookings/abc/cancel", "POST", 200, RequestMetric.Type.BOOKING_WRITE),
        ("/api/v1/recurring-series/abc/cancel", "POST", 200, RequestMetric.Type.BOOKING_WRITE),
        ("/api/v1/waitlist-offers/abc/confirm", "POST", 200, RequestMetric.Type.BOOKING_WRITE),
        ("/api/v1/resources/abc/availability", "GET", 200, RequestMetric.Type.AVAILABILITY_READ),
        ("/api/v1/resources", "GET", 200, RequestMetric.Type.OTHER),
        ("/api/v1/bookings", "GET", 200, RequestMetric.Type.OTHER),
        ("/api/v1/bookings", "POST", 401, RequestMetric.Type.AUTH_FAILURE),
        ("/api/v1/resources/abc/availability", "GET", 401, RequestMetric.Type.AUTH_FAILURE),
    ],
)
def test_classify_metric_type(path: str, method: str, status_code: int, expected: str) -> None:
    assert classify_metric_type(path=path, method=method, status_code=status_code) == expected


# --------------------------------------------------------------------
# 503 cause: lock_contention vs. failover (Rollout v1.0 §6.2)
# --------------------------------------------------------------------


@pytest.mark.parametrize("sqlstate", sorted(RETRYABLE_SQLSTATES))
def test_retryable_sqlstates_map_to_lock_contention_cause(sqlstate: str) -> None:
    with pytest.raises(ServiceUnavailableError) as exc_info:
        _handle_write_database_error(_db_error(sqlstate), {})
    assert exc_info.value.cause == "lock_contention"


@pytest.mark.parametrize("sqlstate", sorted(FAILOVER_SQLSTATES))
def test_failover_sqlstates_map_to_failover_cause(sqlstate: str) -> None:
    with pytest.raises(ServiceUnavailableError) as exc_info:
        _handle_write_database_error(_db_error(sqlstate), {})
    assert exc_info.value.cause == "failover"


def test_a_connection_level_failure_with_no_sqlstate_maps_to_failover_cause() -> None:
    """A genuine connection-level failure (server unreachable, mid-
    failover) carries NO sqlstate at all — SQLSTATEs come from a server
    response, and a connection that never reached the server has none.
    """
    with pytest.raises(ServiceUnavailableError) as exc_info:
        _handle_write_database_error(_db_error(None), {})
    assert exc_info.value.cause == "failover"
    assert exc_info.value.retry_after_seconds > 1  # longer than the default lock-contention wait


def test_kairos_exception_handler_stashes_503_cause_onto_the_response() -> None:
    response = kairos_exception_handler(
        ServiceUnavailableError(cause="failover"), {"request": None}
    )
    assert response is not None
    assert response.kairos_error_cause == "failover"  # type: ignore[attr-defined]


def test_kairos_exception_handler_stashes_auth_failure_shape_onto_the_response() -> None:
    exc = AuthenticationFailed("bad token", code="invalid_session_token")
    response = kairos_exception_handler(exc, {"request": None})
    assert response is not None
    assert response.kairos_error_cause == "invalid_session_token"  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_error_rate_by_cause_splits_lock_contention_from_failover() -> None:
    record_request_metric(
        metric_type=RequestMetric.Type.BOOKING_WRITE,
        method="POST",
        path="/api/v1/bookings",
        status_code=503,
        duration_ms=10,
        cause="lock_contention",
    )
    record_request_metric(
        metric_type=RequestMetric.Type.BOOKING_WRITE,
        method="POST",
        path="/api/v1/bookings",
        status_code=503,
        duration_ms=10,
        cause="failover",
    )
    record_request_metric(
        metric_type=RequestMetric.Type.BOOKING_WRITE,
        method="POST",
        path="/api/v1/bookings",
        status_code=201,
        duration_ms=10,
    )

    result = error_rate_by_cause()
    assert result["total_requests"] == 3
    assert result["total_503"] == 2
    assert result["by_cause"] == {"lock_contention": 1, "failover": 1}
    assert result["rate"] == pytest.approx(2 / 3)


# --------------------------------------------------------------------
# Auth failure rate by shape — real HTTP, real authenticator
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_a_malformed_bearer_token_is_recorded_as_invalid_session_token_shape() -> None:
    client = APIClient()
    response = client.get(CHECKS_URL, HTTP_AUTHORIZATION="Bearer not-a-real-token")
    assert response.status_code == 401

    row = RequestMetric.objects.filter(metric_type=RequestMetric.Type.AUTH_FAILURE).latest(
        "recorded_at"
    )
    assert row.cause == "invalid_session_token"
    assert row.status_code == 401


@pytest.mark.django_db
def test_a_deactivated_users_still_valid_token_is_recorded_as_deactivated_account_shape() -> None:
    user = AppUser.objects.create(
        email="deactivated-metrics@example.com",
        display_name="D",
        status=AppUserStatus.DEACTIVATED,
        deactivated_at=timezone.now(),
    )
    token, _ = issue_session_token(user.id)
    client = APIClient()
    response = client.get(CHECKS_URL, HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 401

    row = RequestMetric.objects.filter(metric_type=RequestMetric.Type.AUTH_FAILURE).latest(
        "recorded_at"
    )
    assert row.cause == "deactivated_account"


@pytest.mark.django_db
def test_auth_failure_rate_by_shape_aggregates_distinct_shapes() -> None:
    client = APIClient()
    client.get(CHECKS_URL, HTTP_AUTHORIZATION="Bearer garbage-one")
    client.get(CHECKS_URL, HTTP_AUTHORIZATION="Bearer garbage-two")
    client.get(CHECKS_URL)  # no Authorization header at all -> not_authenticated shape

    result = auth_failure_rate_by_shape()
    assert result["total"] == 3
    assert result["by_shape"]["invalid_session_token"] == 2
    assert result["by_shape"]["not_authenticated"] == 1


# --------------------------------------------------------------------
# P95 latency
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_p95_duration_ms_computes_over_the_correct_metric_type_only() -> None:
    for ms in [10, 20, 30, 40, 1000]:
        record_request_metric(
            metric_type=RequestMetric.Type.BOOKING_WRITE,
            method="POST",
            path="/api/v1/bookings",
            status_code=201,
            duration_ms=ms,
        )
    # A different metric_type must not pollute the booking_write P95.
    record_request_metric(
        metric_type=RequestMetric.Type.AVAILABILITY_READ,
        method="GET",
        path="/api/v1/resources/x/availability",
        status_code=200,
        duration_ms=99999,
    )

    p95 = p95_duration_ms(metric_type=RequestMetric.Type.BOOKING_WRITE)
    assert p95 is not None
    assert (
        p95 < 1000
    )  # the outlier from BOOKING_WRITE's own set pulls it up, but nowhere near AVAILABILITY_READ's


@pytest.mark.django_db
def test_p95_duration_ms_returns_none_with_no_data() -> None:
    assert p95_duration_ms(metric_type=RequestMetric.Type.BOOKING_WRITE) is None


# --------------------------------------------------------------------
# Point-in-time gauges
# --------------------------------------------------------------------


def test_redis_availability_is_true_against_the_real_running_broker() -> None:
    # infra/docker-compose.yml's redis service is expected to be up for
    # the local/CI test run, same assumption CELERY_TASK_ALWAYS_EAGER's
    # own settings comment makes about Postgres.
    assert redis_availability() is True


def test_redis_availability_is_false_against_an_unreachable_broker(settings) -> None:
    settings.CELERY_BROKER_URL = "redis://localhost:1/0"  # nothing listens here
    assert redis_availability() is False


@pytest.mark.django_db
def test_idempotency_key_stats_reports_count_and_cleanup_heartbeat() -> None:
    from kairos.core.models import IdempotencyKey
    from kairos.identity.models import AppUser

    user = AppUser.objects.create(email="idem-stats@example.com", display_name="U")
    IdempotencyKey.objects.create(
        user=user,
        key=uuid.uuid4(),
        endpoint="POST /bookings",
        request_body_hash="x",
        status=IdempotencyKeyStatus.COMPLETED,
        response_status=201,
        response_body={},
        completed_at=timezone.now(),
    )

    stats = idempotency_key_stats()
    assert stats["total_keys"] == 1
    assert stats["cleanup_last_run_at"] is None  # cleanup has never run in this test

    OperationalHeartbeat.objects.create(
        name="idempotency_cleanup",
        last_run_at=timezone.now(),
        findings={"deleted_count": 3},
    )
    stats = idempotency_key_stats()
    assert stats["cleanup_last_run_at"] is not None
    assert stats["cleanup_findings"] == {"deleted_count": 3}


@pytest.mark.django_db
def test_cleanup_expired_idempotency_keys_writes_the_heartbeat() -> None:
    from kairos.core.idempotency import cleanup_expired_idempotency_keys

    findings = cleanup_expired_idempotency_keys()
    assert findings == {"deleted_count": 0}
    heartbeat = OperationalHeartbeat.objects.get(name="idempotency_cleanup")
    assert heartbeat.findings == {"deleted_count": 0}


@pytest.mark.django_db
def test_audit_actor_unknown_count_only_counts_within_the_window() -> None:
    now = timezone.now()
    AuditLog.objects.create(
        entity_type="booking",
        entity_id=uuid.uuid4(),
        action="insert",
        actor_type=AuditActorType.UNKNOWN,
    )
    old = AuditLog.objects.create(
        entity_type="booking",
        entity_id=uuid.uuid4(),
        action="insert",
        actor_type=AuditActorType.UNKNOWN,
    )
    AuditLog.objects.filter(id=old.id).update(occurred_at=now - timedelta(days=1))
    AuditLog.objects.create(
        entity_type="booking",
        entity_id=uuid.uuid4(),
        action="insert",
        actor_type=AuditActorType.SYSTEM,
    )

    assert audit_actor_unknown_count(window_seconds=900) == 1


@pytest.mark.django_db
def test_rate_limit_metric_slot_reports_real_data_now_that_rate_limiting_exists() -> None:
    """Implementation Plan Phase 21 left this an honest `{"available":
    False, ...}` slot; Phase 22 built the real rate limiter
    (`kairos.core.rate_limit`) and this function now reports genuine
    counts from `RequestMetric` — never fabricated, but no longer a
    placeholder either. See tests/test_rate_limit.py for the mechanism
    that actually produces 429s this function reads back.
    """
    record_request_metric(
        metric_type=RequestMetric.Type.RATE_LIMITED,
        method="POST",
        path="/api/v1/bookings",
        status_code=429,
        duration_ms=1,
        cause="per_principal_token_bucket",
        principal_id="11111111-1111-1111-1111-111111111111",
    )
    record_request_metric(
        metric_type=RequestMetric.Type.RATE_LIMITED,
        method="POST",
        path="/api/v1/bookings",
        status_code=429,
        duration_ms=1,
        cause="per_ip_token_bucket",
        principal_id=None,
    )
    record_request_metric(
        metric_type=RequestMetric.Type.BOOKING_WRITE,
        method="POST",
        path="/api/v1/bookings",
        status_code=201,
        duration_ms=5,
    )

    slot = rate_limit_metric_slot()
    assert slot["available"] is True
    assert slot["total_requests"] == 3
    assert slot["total_429"] == 2
    assert slot["rate"] == pytest.approx(2 / 3)
    assert slot["by_cause"] == {
        "per_principal_token_bucket": 1,
        "per_ip_token_bucket": 1,
    }
    assert slot["top_principals"] == [
        {"principal_id": "11111111-1111-1111-1111-111111111111", "count": 1}
    ]


@pytest.mark.django_db
def test_prune_old_request_metrics_deletes_only_rows_past_retention() -> None:
    fresh = RequestMetric.objects.create(
        metric_type=RequestMetric.Type.OTHER, method="GET", path="/x", status_code=200
    )
    stale = RequestMetric.objects.create(
        metric_type=RequestMetric.Type.OTHER, method="GET", path="/x", status_code=200
    )
    RequestMetric.objects.filter(id=stale.id).update(
        recorded_at=timezone.now() - timedelta(hours=999)
    )

    deleted = prune_old_request_metrics()
    assert deleted == 1
    assert RequestMetric.objects.filter(id=fresh.id).exists()
    assert not RequestMetric.objects.filter(id=stale.id).exists()


# --------------------------------------------------------------------
# MetricsMiddleware — one row per real HTTP request
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_a_real_request_is_recorded_by_the_middleware() -> None:
    client = APIClient()
    before = RequestMetric.objects.count()
    client.get(CHECKS_URL)  # unauthenticated -> 401, still recorded
    after = RequestMetric.objects.count()
    assert after == before + 1
    row = RequestMetric.objects.latest("recorded_at")
    assert row.path == "/api/v1/admin/checks/latest"
    assert row.method == "GET"
    assert row.status_code == 401
    assert row.duration_ms is not None and row.duration_ms >= 0
