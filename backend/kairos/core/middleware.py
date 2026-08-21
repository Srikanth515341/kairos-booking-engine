"""X-Request-Id (Spec v1.0 §1): accepted from the client or generated,
returned on every response, and threaded through to BookingService for the
audit session variable (Phase 8) and structured logging (Phase 4 scope).

`MetricsMiddleware` (Implementation Plan Phase 21; Rollout v1.0 §6.2) is
the second, separate middleware here — one `RequestMetric` row per
request, timing every response and reading back whatever `cause`
`kairos.core.drf.kairos_exception_handler` stashed onto it (a 503's
lock-contention-vs-failover split, a 401's failure shape, or — Phase 22 —
a 429's triggering limiter). Kept distinct from `RequestIdMiddleware`
rather than folded into it: request-id correlation is a Phase 4 concern
with nothing to do with metrics, and Django processes `MIDDLEWARE` in list
order — `RequestIdMiddleware` runs first specifically so `request.
request_id` exists before anything else (including this one) could want
it.

`CorsMiddleware` (Phase 22; RFC v1.0 §8.2's security-hardening scope) is a
small, explicit allowlist — no wildcard, ever, from this middleware's own
code (not merely "not in production": `*` is never a value this class
treats as "allow everything," see its docstring) — rather than adding
`django-cors-headers` as a new dependency for what an allowlist-and-
reflect implementation covers in a few lines.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.conf import settings
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

        # DRF's Request.user setter also assigns `self._request.user`
        # (the SAME underlying HttpRequest instance this middleware
        # holds) — so by the time get_response() above has returned, an
        # authenticated request's principal is visible here even though
        # this is plain Django, not DRF. Never set at all (not even to
        # None) if authentication itself raised before reaching that
        # assignment — getattr's default covers that case too.
        user = getattr(request, "user", None)
        principal_id = str(user.id) if user is not None else None

        record_request_metric(
            metric_type=classify_metric_type(
                path=request.path, method=method, status_code=response.status_code
            ),
            method=method,
            path=request.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            cause=getattr(response, "kairos_error_cause", None),
            principal_id=principal_id,
        )
        return response


class CorsMiddleware:
    """Explicit CORS handling (Implementation Plan Phase 22): reflects
    `Access-Control-Allow-Origin` back ONLY when the request's `Origin`
    header exactly matches an entry in `settings.CORS_ALLOWED_ORIGINS` —
    never a bare `*`, from this code, under any settings module. `prod.py`
    additionally refuses to even START if `CORS_ALLOWED_ORIGINS` somehow
    contains the literal string `"*"` (the same "refuse a dev-shaped
    default" discipline `SECRET_KEY`/`KAIROS_SESSION_SIGNING_KEY`/
    `EMAIL_HOST` already have) — belt AND suspenders, since a wildcard
    origin combined with credentialed requests is a real cross-origin data
    leak, not a theoretical one.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        origin = request.headers.get("Origin")
        allowed = origin is not None and origin in settings.CORS_ALLOWED_ORIGINS

        # A preflight request must be answered directly — this app has no
        # view mounted at every possible OPTIONS path, and even where DRF
        # would handle OPTIONS itself, that response wouldn't carry the
        # CORS headers a preflight check specifically inspects.
        if request.method == "OPTIONS" and "Access-Control-Request-Method" in request.headers:
            response = HttpResponse(status=204 if allowed else 403)
            if allowed:
                response["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
                requested_headers = request.headers.get("Access-Control-Request-Headers")
                if requested_headers:
                    response["Access-Control-Allow-Headers"] = requested_headers
                response["Access-Control-Max-Age"] = "600"
        else:
            response = self.get_response(request)

        if allowed:
            assert origin is not None  # narrowed by `allowed`'s own check above
            response["Access-Control-Allow-Origin"] = origin
            # This response's content varies by the Origin header — a
            # shared cache (or the browser's own HTTP cache) must not
            # serve one origin's CORS-approved response to a different
            # origin's request for the identical URL.
            existing_vary = response.get("Vary", "")
            vary_values = {v.strip() for v in existing_vary.split(",") if v.strip()}
            vary_values.add("Origin")
            response["Vary"] = ", ".join(sorted(vary_values))

        return response
