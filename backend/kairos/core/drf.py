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
    PolicyValidationError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    SlotUnavailableError,
)


def _request_id(context: dict[str, Any]) -> str | None:
    request = context.get("request")
    # DRF's Request proxies unknown attributes to the underlying Django
    # HttpRequest, so this reads the value RequestIdMiddleware attached —
    # but reach for the underlying request explicitly rather than relying
    # on that proxying, so this keeps working even if that changes.
    django_request = getattr(request, "_request", request)
    request_id = getattr(django_request, "request_id", None)
    return str(request_id) if request_id is not None else None


def _envelope(
    code: str,
    message: str,
    details: dict[str, Any] | list[Any] | None,
    request_id: str | None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details if details is not None else {},
            "request_id": request_id,
        }
    }


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
    request_id = _request_id(context)

    if isinstance(exc, SlotUnavailableError):
        return Response(
            _envelope(
                "slot_unavailable",
                "This time slot is no longer available.",
                None,
                request_id,
            ),
            status=409,
        )

    if isinstance(exc, ServiceUnavailableError):
        response = Response(
            _envelope(
                "service_unavailable",
                "The outcome of this request is unknown. Retry the same request.",
                None,
                request_id,
            ),
            status=503,
        )
        response["Retry-After"] = str(exc.retry_after_seconds)
        return response

    if isinstance(exc, ResourceNotFoundError):
        return Response(
            _envelope("not_found", "The requested resource was not found.", None, request_id),
            status=404,
        )

    if isinstance(exc, PolicyValidationError):
        return Response(
            _envelope(
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
    default_response.data = _envelope(code, message, details, request_id)
    return default_response
