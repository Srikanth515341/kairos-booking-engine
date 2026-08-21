"""Custom 404/500 handlers (Implementation Plan Phase 22; SEC-04). A URL
that never resolves to any view at all (e.g. an injection-shaped path
segment that doesn't match a `<uuid:...>` converter — Django's URL
resolution rejects it before any view, DRF's dispatch machinery, or
`kairos_exception_handler` ever runs) previously fell through to Django's
own bare, unenveloped HTML 404/500 page — the one real gap in `kairos.
core.drf.kairos_exception_handler`'s own "no code path can produce a
bare, unenveloped error body" claim (that handler is a DRF exception
handler; it is never even reached when the URL itself doesn't resolve).

This is also the honest answer to "path UUID injection payloads": a
malformed/injection-shaped value in a URL PATH segment never reaches this
application's code, the ORM, or raw SQL at all — the `<uuid:...>`
converter (used for every `{id}`-shaped path segment in this project,
confirmed by inspection) simply doesn't match it, so Django's routing
itself rejects the request before any view runs. That surfaces as 404,
not 400 — arguably a STRONGER guarantee than "safely rejected with 400
inside application code," not a weaker one — and, as of this phase, in
the same enveloped JSON shape as every other error response, not a bare
Django error page.
"""

from __future__ import annotations

from django.http import HttpRequest, JsonResponse

from kairos.core.drf import build_error_envelope


def handler404(
    request: HttpRequest, exception: Exception, template_name: str = "404.html"
) -> JsonResponse:
    request_id = getattr(request, "request_id", None)
    return JsonResponse(
        build_error_envelope(
            "not_found", "The requested resource was not found.", None, request_id
        ),
        status=404,
    )


def handler500(request: HttpRequest) -> JsonResponse:
    request_id = getattr(request, "request_id", None)
    return JsonResponse(
        build_error_envelope("internal_error", "An unexpected error occurred.", None, request_id),
        status=500,
    )
