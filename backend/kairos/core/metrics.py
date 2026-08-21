"""Request-level metrics (Implementation Plan Phase 21; Rollout v1.0
§6.2) — booking-write/availability-read P95 latency, 503 rate split by
cause, auth failure rate by shape, plus the point-in-time gauges the
dashboard shows (Redis availability, idempotency-key growth + cleanup
heartbeat, audit rows with actor_type='unknown', the rate-limit slot).

No metrics/time-series library exists in this project (pyproject.toml has
none — pure Django/DRF/psycopg/Celery). Every rate/percentile here is
computed LIVE, by query, over a rolling window (`METRICS_WINDOW_SECONDS`)
against `RequestMetric` rows `kairos.core.middleware.MetricsMiddleware`
writes — the same "derive from real stored data by query, don't
precompute into a store nothing else needs" choice `heartbeat_is_stale`/
`GET /admin/checks/latest` already made for staleness, applied here to
rates instead.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import connection
from django.db.models import Count
from django.utils import timezone as django_timezone

from kairos.core.constants import METRICS_WINDOW_SECONDS, REQUEST_METRIC_RETENTION_HOURS
from kairos.core.models import (
    AuditActorType,
    AuditLog,
    IdempotencyKey,
    OperationalHeartbeat,
    RequestMetric,
)

logger = logging.getLogger(__name__)

# Path fragments that classify a request as the two Rollout v1.0 §6.2
# names explicitly ("booking-write P95, availability-read P95"). Method
# is checked alongside path since GET /bookings (a read) and POST
# /bookings (a write) share a path prefix.
_BOOKING_WRITE_PATH_MARKERS = ("/bookings", "/recurring-series", "/waitlist-offers")


def classify_metric_type(*, path: str, method: str, status_code: int) -> str:
    """A 401 or 429 is classified by status code alone, regardless of
    which endpoint it hit — "auth failure rate by shape" and "rate-limit
    trigger rate" both need to aggregate across every endpoint that could
    produce one, not just the ones this function would otherwise call
    booking-write/availability-read. Both are checked FIRST, ahead of the
    path-based rules below, for exactly that reason.
    """
    if status_code == 429:
        return RequestMetric.Type.RATE_LIMITED
    if status_code == 401:
        return RequestMetric.Type.AUTH_FAILURE
    if method == "GET" and "/availability" in path:
        return RequestMetric.Type.AVAILABILITY_READ
    if method in ("POST", "PATCH") and any(
        marker in path for marker in _BOOKING_WRITE_PATH_MARKERS
    ):
        return RequestMetric.Type.BOOKING_WRITE
    return RequestMetric.Type.OTHER


def record_request_metric(
    *,
    metric_type: str,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int | None,
    cause: str | None = None,
    principal_id: str | None = None,
) -> None:
    RequestMetric.objects.create(
        metric_type=metric_type,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        cause=cause,
        principal_id=principal_id,
    )


def p95_duration_ms(
    *, metric_type: str, window_seconds: int = METRICS_WINDOW_SECONDS
) -> float | None:
    """Postgres's own `percentile_cont` — no Python-side sorting of
    however many rows the window holds, and no approximation library this
    project doesn't depend on.
    """
    cutoff = django_timezone.now() - timedelta(seconds=window_seconds)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
              FROM request_metric
             WHERE metric_type = %s AND recorded_at >= %s AND duration_ms IS NOT NULL
            """,
            [metric_type, cutoff],
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def error_rate_by_cause(*, window_seconds: int = METRICS_WINDOW_SECONDS) -> dict[str, Any]:
    """Rollout v1.0 §6.2: "503 rate split by cause (lock timeout vs.
    failover — they need opposite responses)." `by_cause` keys are
    `ServiceUnavailableError.cause` values ("lock_contention"/"failover");
    a 503 somehow reaching a client without a recorded cause (should not
    happen — every raise site in this codebase sets one) surfaces as
    "unknown" rather than silently vanishing from the split.
    """
    cutoff = django_timezone.now() - timedelta(seconds=window_seconds)
    total = RequestMetric.objects.filter(recorded_at__gte=cutoff).count()
    rows = (
        RequestMetric.objects.filter(recorded_at__gte=cutoff, status_code=503)
        .values("cause")
        .annotate(count=Count("id"))
    )
    by_cause: dict[str, int] = {(row["cause"] or "unknown"): row["count"] for row in rows}
    total_503 = sum(by_cause.values())
    return {
        "window_seconds": window_seconds,
        "total_requests": total,
        "total_503": total_503,
        "rate": (total_503 / total) if total else 0.0,
        "by_cause": by_cause,
    }


def auth_failure_rate_by_shape(*, window_seconds: int = METRICS_WINDOW_SECONDS) -> dict[str, Any]:
    """Rollout v1.0 §6.2: "auth failure rate by shape (narrow = probing,
    broad = provider issue)." `by_shape` is the raw distribution — how
    many distinct shapes are firing and in what proportion IS the signal
    a human reads (a narrow spike is one shape dominating; a broad one is
    many shapes roughly evenly), not something this function collapses
    into a single verdict.
    """
    cutoff = django_timezone.now() - timedelta(seconds=window_seconds)
    rows = (
        RequestMetric.objects.filter(
            recorded_at__gte=cutoff, metric_type=RequestMetric.Type.AUTH_FAILURE
        )
        .values("cause")
        .annotate(count=Count("id"))
    )
    by_shape: dict[str, int] = {(row["cause"] or "unknown"): row["count"] for row in rows}
    return {"window_seconds": window_seconds, "total": sum(by_shape.values()), "by_shape": by_shape}


def redis_availability() -> bool:
    """A live PING against `CELERY_BROKER_URL` — independent of Celery
    itself (a scheduled Celery task checking Redis availability is
    circular: if Redis is down, Beat can't reliably dispatch the task
    that would report it). `redis-py` is already an installed dependency
    (`celery[redis]`, pyproject.toml) — no new package needed.
    """
    from django.conf import settings

    try:
        import redis

        # redis-py ships no inline types recognized under this project's
        # mypy strict config (no `types-redis` dev dependency either) —
        # a real dependency, just an untyped one at the boundary.
        client = redis.from_url(  # type: ignore[no-untyped-call]
            settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1
        )
        return bool(client.ping())
    except Exception:
        logger.warning("redis_availability_check_failed", exc_info=True)
        return False


def idempotency_key_stats() -> dict[str, Any]:
    total = IdempotencyKey.objects.count()
    heartbeat = OperationalHeartbeat.objects.filter(name="idempotency_cleanup").first()
    return {
        "total_keys": total,
        "cleanup_last_run_at": (
            heartbeat.last_run_at.isoformat().replace("+00:00", "Z") if heartbeat else None
        ),
        "cleanup_findings": heartbeat.findings if heartbeat else None,
    }


def audit_actor_unknown_count(*, window_seconds: int = METRICS_WINDOW_SECONDS) -> int:
    cutoff = django_timezone.now() - timedelta(seconds=window_seconds)
    return AuditLog.objects.filter(
        actor_type=AuditActorType.UNKNOWN, occurred_at__gte=cutoff
    ).count()


def rate_limit_metric_slot(*, window_seconds: int = METRICS_WINDOW_SECONDS) -> dict[str, Any]:
    """Implementation Plan Phase 21 left this as an honest `{"available":
    False, ...}` slot, since rate limiting didn't exist yet. Phase 22
    built it (`kairos.core.rate_limit`) — this now reports REAL data:
    total requests that hit a 429 in the window, split by which limiter
    tripped (`by_cause`: `"per_principal_token_bucket"` /
    `"per_ip_token_bucket"`), and the top principals by 429 count
    (Rollout v1.0 §6.2's "per-principal breakdown"). Still reads live from
    `RequestMetric`, never a separate precomputed store — the same choice
    every other metric in this module already makes.
    """
    cutoff = django_timezone.now() - timedelta(seconds=window_seconds)
    total_requests = RequestMetric.objects.filter(recorded_at__gte=cutoff).count()
    limited_qs = RequestMetric.objects.filter(recorded_at__gte=cutoff, status_code=429)
    total_429 = limited_qs.count()
    by_cause: dict[str, int] = {
        (row["cause"] or "unknown"): row["count"]
        for row in limited_qs.values("cause").annotate(count=Count("id"))
    }
    top_principals = [
        {"principal_id": row["principal_id"], "count": row["count"]}
        for row in limited_qs.exclude(principal_id__isnull=True)
        .values("principal_id")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    ]
    return {
        "available": True,
        "window_seconds": window_seconds,
        "total_requests": total_requests,
        "total_429": total_429,
        "rate": (total_429 / total_requests) if total_requests else 0.0,
        "by_cause": by_cause,
        "top_principals": top_principals,
    }


def prune_old_request_metrics() -> int:
    """`RequestMetric` grows one row per HTTP request — pruned on the same
    tick as `kairos.core.alerting.evaluate_alerts_task` rather than a
    second Beat entry, since nothing ever queries past `METRICS_WINDOW_
    SECONDS` anyway (mirrors `IDEMPOTENCY_RETENTION_HOURS`'s own "nothing
    needs it past its own window" reasoning).
    """
    cutoff = django_timezone.now() - timedelta(hours=REQUEST_METRIC_RETENTION_HOURS)
    deleted, _ = RequestMetric.objects.filter(recorded_at__lt=cutoff).delete()
    return deleted
