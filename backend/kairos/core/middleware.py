"""X-Request-Id (Spec v1.0 §1): accepted from the client or generated,
returned on every response, and threaded through to BookingService for the
audit session variable (Phase 8) and structured logging (Phase 4 scope).

`MetricsMiddleware` (Implementation Plan Phase 21; Rollout v1.0 §6.2) is
the second, separate middleware here — one `RequestMetric` row per
request, timing every response and reading back whatever `cause`
`kairos.core.drf.kairos_exception_handler` stashed onto it (a 503's
lock-contention-vs-failover split, or a 401's failure shape). Kept
distinct from `RequestIdMiddleware` rather than folded into it: request-id
correlation is a Phase 4 concern with nothing to do with metrics, and
Django processes `MIDDLEWARE` in list order — `RequestIdMiddleware` runs
first specifically so `request.request_id` exists before anything else
(including this one) could want it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.utils import timezone as django_timezone

REQUEST_ID_HEADER = "X-Request-Id"


class RequestIdMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        # Not declared on HttpRequest — attached here and read back via
        # getattr() everywhere it's consumed (kairos.core.drf, views).
        request.request_id = request_id  # type: ignore[attr-defined]
        response = self.get_response(request)
        response[REQUEST_ID_HEADER] = request_id
        return response


class MetricsMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        started = django_timezone.now()
        response = self.get_response(request)
        duration_ms = int((django_timezone.now() - started).total_seconds() * 1000)

        # Deferred import: kairos.core.metrics reads kairos.core.models,
        # which is safe at module load time, but importing Django models
        # before the app registry is ready (this middleware class is
        # instantiated at settings-load time) raises AppRegistryNotReady —
        # the same reason every deferred-import precedent elsewhere in
        # this project exists, applied here for a different root cause.
        from kairos.core.metrics import classify_metric_type, record_request_metric

        # django-stubs types HttpRequest.method as `str | None` (WSGI
        # theoretically allows an absent REQUEST_METHOD); every real
        # request Django itself routes has one.
        method = request.method or ""
        record_request_metric(
            metric_type=classify_metric_type(
                path=request.path, method=method, status_code=response.status_code
            ),
            method=method,
            path=request.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            cause=getattr(response, "kairos_error_cause", None),
        )
        return response
