"""Shared view infrastructure. Every endpoint requires authentication
(Spec v1.0 §1: "No anonymous endpoints") — declared once here rather than
repeated on every view.
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.views import APIView

from kairos.identity.authentication import OIDCSessionAuthentication, StubUserIdAuthentication


class KairosAPIView(APIView):
    # OIDC first (Phase 9) — it's the real mechanism and, per DRF's
    # get_authenticate_header(), the FIRST authenticator's challenge is
    # what an unauthenticated 401 carries, so a `Bearer` challenge (not
    # `X-Dev-User-Id`) is the correct default now. StubUserIdAuthentication
    # is a no-op everywhere except kairos.settings.test regardless of its
    # position here — see that class's docstring.
    authentication_classes = [OIDCSessionAuthentication, StubUserIdAuthentication]
    permission_classes = [IsAuthenticated]


def request_id(request: Request) -> str:
    """Extracted here (Implementation Plan Phase 19) once a fourth view
    module (kairos.identity, for the deactivate endpoint) needed the
    identical helper `bookings`/`waitlist`/`resources` views.py already
    each defined locally — this project's own "worth extracting once
    real duplication exists, not before" threshold (see
    RecordableConflictError, Phase 16).
    """
    django_request = getattr(request, "_request", request)
    return getattr(django_request, "request_id", "") or ""
