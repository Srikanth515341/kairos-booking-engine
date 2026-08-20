"""X-Request-Id (Spec v1.0 §1): accepted from the client or generated,
returned on every response, and threaded through to BookingService for the
audit session variable (Phase 8) and structured logging (Phase 4 scope).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

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
