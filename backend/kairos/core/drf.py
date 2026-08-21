"""The single place every error response gets translated into the Spec
v1.0 §6 envelope: {"error": {"code", "message", "details", "request_id"}}.
Domain exceptions (kairos.core.exceptions) are handled first; anything else
falls through to DRF's default handler and is re-wrapped in the same
envelope, so no code path can produce a bare, unenveloped error body.
"""

from __future__ import annotations

from typing import Any

from rest_framework import exceptions as drf_exceptions
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_exception_handler

from kairos.core.exceptions import (
    AlreadyResourceAdminError,
    IdempotencyKeyConflictError,
    NotFoundError,
    PolicyValidationError,
    PreviewExpiredError,
    RecordableConflictError,
    RequestInProgressError,
    ServiceUnavailableError,
    SlotAlreadyAvailableError,
    UnacknowledgedConflictsError,
)


def request_id_from_context(context: dict[str, Any]) -> str | None:
    request = context.get("request")
    # DRF's Request proxies unknown attributes to the underlying Django
    # HttpRequest, so this reads the value RequestIdMiddleware attached —
    # but reach for the underlying request explicitly rather than relying
    # on that proxying, so this keeps working even if that changes.
    django_request = getattr(request, "_request", request)
    request_id = getattr(django_request, "request_id", None)
    return str(request_id) if request_id is not None else None


def build_error_envelope(
    code: str,
    message: str,
    details: dict[str, Any] | list[Any] | None,
    request_id: str | None,
) -> dict[str, Any]:
    """The Spec v1.0 §6 shape, built in exactly one place. Also used by
    kairos.core.idempotency to record a 409 slot_unavailable outcome with
    the identical body a live request would have received (Spec v1.0 §7:
    "conflict outcomes are recorded too").
    """
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details if details is not None else {},
            "request_id": request_id,
        }
    }


def _auth_failure_shape(exc: Exception) -> str:
    """Rollout v1.0 §6.2 / Implementation Plan Phase 21: "auth failure
    rate by shape (narrow = probing, broad = provider issue)." Each raise
    site in kairos.identity.authentication now passes an explicit `code=`
    on its `AuthenticationFailed(...)` — DRF wraps a single string
    `detail` in an `ErrorDetail`, which carries that code as `.code`. A
    bare `NotAuthenticated` (no credentials offered at all — no
    authenticator matched) has no such site-supplied code; DRF itself
    defaults it to `"not_authenticated"`, which is itself a meaningful,
    distinct shape (a client that never sent an Authorization header at
    all, as opposed to one that sent a bad one).
    """
    detail = getattr(exc, "detail", None)
    code = getattr(detail, "code", None)
    return str(code) if code else "unknown"


def _map_drf_exception(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, drf_exceptions.ValidationError):
        return "validation_error", "The request failed validation."
    if isinstance(exc, (drf_exceptions.NotAuthenticated, drf_exceptions.AuthenticationFailed)):
        return "unauthorized", "Authentication is required."
    if isinstance(exc, drf_exceptions.PermissionDenied):
        return "permission_denied", "You do not have permission to perform this action."
    if isinstance(exc, drf_exceptions.NotFound):
        return "not_found", "The requested resource was not found."
    if isinstance(exc, drf_exceptions.Throttled):
        return "rate_limited", "Too many requests."
    return "validation_error", str(exc)


def kairos_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    request_id = request_id_from_context(context)

    if isinstance(exc, RecordableConflictError):
        # Generic across every "legitimate final 409/422 outcome" domain
        # exception (SlotUnavailableError, AlreadyOnWaitlistError,
        # OfferExpiredError, OfferAlreadyResolvedError, Phase 14/16) — one
        # branch, not one isinstance check per exception type.
        return Response(
            build_error_envelope(exc.code, exc.message, None, request_id),
            status=exc.http_status,
        )

    if isinstance(exc, ServiceUnavailableError):
        response = Response(
            build_error_envelope(
                "service_unavailable",
                "The outcome of this request is unknown. Retry the same request.",
                None,
                request_id,
            ),
            status=503,
        )
        response["Retry-After"] = str(exc.retry_after_seconds)
        # Rollout v1.0 §6.2 / Implementation Plan Phase 21: "503 rate split
        # by cause (lock timeout vs. failover)." Not a header or body field
        # (neither is part of Spec v1.0 §6's envelope) — a plain attribute
        # kairos.core.middleware.MetricsMiddleware reads back off the
        # response object itself, after this handler returns, to populate
        # the one RequestMetric row it writes per request.
        response.kairos_error_cause = exc.cause  # type: ignore[attr-defined]
        return response

    if isinstance(exc, RequestInProgressError):
        # Same 409 status as slot_unavailable, opposite meaning — never the
        # same code. Distinct branch so the two can never be conflated
        # (Spec v1.0 §6.1, IDEM-09).
        return Response(
            build_error_envelope(
                "request_in_progress",
                "An earlier request with this idempotency key is still executing.",
                None,
                request_id,
            ),
            status=409,
        )

    if isinstance(exc, IdempotencyKeyConflictError):
        return Response(
            build_error_envelope(
                "idempotency_key_conflict",
                "This idempotency key was already used with a different request body.",
                None,
                request_id,
            ),
            status=422,
        )

    if isinstance(exc, PreviewExpiredError):
        return Response(
            build_error_envelope(
                "preview_expired",
                "This preview has expired or is invalid. Request a new preview.",
                None,
                request_id,
            ),
            status=409,
        )

    if isinstance(exc, UnacknowledgedConflictsError):
        return Response(
            build_error_envelope(
                "unacknowledged_conflicts",
                "The preview reported conflicts or time adjustments this request did not "
                "acknowledge.",
                None,
                request_id,
            ),
            status=409,
        )

    if isinstance(exc, AlreadyResourceAdminError):
        return Response(
            build_error_envelope(
                "already_resource_admin",
                "This user already administers this resource.",
                None,
                request_id,
            ),
            status=409,
        )

    if isinstance(exc, SlotAlreadyAvailableError):
        return Response(
            build_error_envelope(
                "slot_already_available",
                "This time range is currently available — book it directly.",
                None,
                request_id,
            ),
            status=422,
        )

    if isinstance(exc, NotFoundError):
        return Response(
            build_error_envelope(
                "not_found", "The requested resource was not found.", None, request_id
            ),
            status=404,
        )

    if isinstance(exc, PolicyValidationError):
        return Response(
            build_error_envelope(
                "validation_error",
                "The request failed validation.",
                {"field": exc.field, "issue": exc.issue},
                request_id,
            ),
            status=400,
        )

    default_response = drf_default_exception_handler(exc, context)
    if default_response is None:
        return None  # genuinely unhandled — surfaces as a 500, same as Django's default

    code, message = _map_drf_exception(exc)
    details = default_response.data if isinstance(exc, drf_exceptions.ValidationError) else None
    default_response.data = build_error_envelope(code, message, details, request_id)
    if isinstance(exc, (drf_exceptions.NotAuthenticated, drf_exceptions.AuthenticationFailed)):
        # Same mechanism as the ServiceUnavailableError branch above, for
        # the same reason: MetricsMiddleware reads this back to populate
        # RequestMetric.cause for "auth failure rate by shape."
        default_response.kairos_error_cause = _auth_failure_shape(exc)  # type: ignore[attr-defined]
    if isinstance(exc, drf_exceptions.Throttled):
        # Implementation Plan Phase 22: `.cause` is set by `kairos.core.
        # views.KairosAPIView.throttled()`, which reads it off whichever
        # `kairos.core.rate_limit` throttle actually rejected the request
        # ("per_principal_token_bucket" / "per_ip_token_bucket") — the
        # SAME cause-stashing mechanism the two branches above already use
        # for MetricsMiddleware's benefit, not a third one invented here.
        default_response.kairos_error_cause = getattr(exc, "cause", "rate_limited")  # type: ignore[attr-defined]
    return default_response
