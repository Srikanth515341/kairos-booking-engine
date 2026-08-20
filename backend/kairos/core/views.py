"""Shared view infrastructure. Every endpoint requires authentication
(Spec v1.0 §1: "No anonymous endpoints") — declared once here rather than
repeated on every view.
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from kairos.identity.authentication import StubUserIdAuthentication


class KairosAPIView(APIView):
    authentication_classes = [StubUserIdAuthentication]
    permission_classes = [IsAuthenticated]
