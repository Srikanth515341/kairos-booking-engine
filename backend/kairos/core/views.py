"""Shared view infrastructure. Every endpoint requires authentication
(Spec v1.0 §1: "No anonymous endpoints") — declared once here rather than
repeated on every view.
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
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
