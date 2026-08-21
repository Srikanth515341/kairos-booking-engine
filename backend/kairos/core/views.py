"""Shared view infrastructure. Every endpoint requires authentication
(Spec v1.0 §1: "No anonymous endpoints") — declared once here rather than
repeated on every view.
"""

from __future__ import annotations

from typing import Any, cast

from rest_framework import exceptions as drf_exceptions
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from kairos.core.models import SystemCheckRun
from kairos.identity.authentication import OIDCSessionAuthentication, StubUserIdAuthentication
from kairos.identity.authorization import AuthorizationService
from kairos.identity.models import AppUser


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


# Spec v1.0 §5.15's own example response lists the six checks in this
# order (schema_assertion and reconciliation first, the four background-
# job heartbeats after) — deliberately NOT `SystemCheckName.values`'
# declared enum order (RECONCILIATION, SCHEMA_ASSERTION, HOLD_REAPER,
# OFFER_CASCADE, SERIES_MATERIALIZATION, TZDATA_REMATERIALIZATION), which
# differs, since this is a human-facing dashboard-style read where the
# two most load-bearing checks belong first.
_CHECKS_LATEST_ORDER = (
    "schema_assertion",
    "reconciliation",
    "hold_reaper",
    "offer_cascade",
    "series_materialization",
    "tzdata_rematerialization",
)


class AdminChecksLatestView(KairosAPIView):
    """GET /api/v1/admin/checks/latest (Spec v1.0 §5.15; Implementation
    Plan Phase 20) — read-only surface over `system_check_run`, for
    `operations` or `system_admin`. Exists so dashboards don't need
    direct database access (Spec's own stated reason) — the jobs
    themselves already query Postgres directly and don't need this
    endpoint to function.
    """

    def get(self, request: Request) -> Response:
        user = cast(AppUser, request.user)
        if not (
            AuthorizationService.is_operations(user) or AuthorizationService.is_system_admin(user)
        ):
            raise drf_exceptions.PermissionDenied()

        checks: list[dict[str, Any]] = []
        for check_name in _CHECKS_LATEST_ORDER:
            latest = (
                SystemCheckRun.objects.filter(check_name=check_name).order_by("-run_at").first()
            )
            if latest is None:
                # Absence IS the signal (RFC v1.0 §14) — reported
                # honestly as null/null rather than omitted or faked as
                # a pass, so a dashboard can't mistake "never run" for
                # "healthy."
                checks.append(
                    {"check_name": check_name, "last_run_at": None, "status": None, "findings": {}}
                )
            else:
                checks.append(
                    {
                        "check_name": check_name,
                        "last_run_at": latest.run_at.isoformat().replace("+00:00", "Z"),
                        "status": latest.status,
                        "findings": latest.findings,
                    }
                )
        return Response({"checks": checks})
