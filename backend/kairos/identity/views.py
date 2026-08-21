"""Auth endpoints (Implementation Plan Phase 9; RFC v1.0 §4). Not on
Spec v1.0's original endpoint list — that document predates real
authentication and simply says "every endpoint requires
`Authorization: Bearer <JWT>`, validated against the SSO/OIDC integration"
without specifying how a client obtains one. These two endpoints are that
missing piece, added here rather than left implicit.

Deliberately NOT KairosAPIView subclasses — a login endpoint cannot itself
require the credential it's about to issue.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from django.conf import settings
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from kairos.core.exceptions import NotFoundError, PolicyValidationError
from kairos.core.idempotency import run_idempotent_write
from kairos.core.models import AuditActorType
from kairos.core.views import KairosAPIView
from kairos.core.views import request_id as _request_id
from kairos.identity.authentication import OIDCSessionAuthentication
from kairos.identity.authorization import AuthorizationService
from kairos.identity.models import AppUser
from kairos.identity.oidc import (
    OIDCError,
    issue_session_token,
    mint_mock_id_token,
    verify_id_token,
)
from kairos.identity.serializers import (
    DevMockLoginSerializer,
    TokenExchangeSerializer,
    UserDeactivateSerializer,
)
from kairos.identity.services import DeactivateUserRequest, deactivate_user

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def _require_idempotency_key(request: Request) -> uuid.UUID:
    # Spec v1.0 §7's coverage list names this endpoint explicitly.
    header_value = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if not header_value:
        raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "header is required")
    try:
        return uuid.UUID(header_value)
    except ValueError as exc:
        raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "must be a valid UUID") from exc


def _resolve_or_provision_user(*, email: str, display_name: str) -> AppUser:
    """JIT provisioning on first successful login — a verified OIDC
    identity is proof enough of who this is; there's no separate SCIM/
    directory-sync process for this project to wait on instead. Matches
    every AppUser's own docstring ("the local identity record every FK
    resolves against") — this is the ONE place that record gets created
    from a real login rather than a test fixture.
    """
    user, _created = AppUser.objects.get_or_create(
        email=email, defaults={"display_name": display_name}
    )
    return user


class TokenExchangeView(APIView):
    """POST /api/v1/auth/token — exchanges a verified OIDC ID token for
    this backend's own short-lived session token."""

    # Listed even though neither view relies on it to resolve request.user
    # (permission_classes is AllowAny) — purely so DRF's
    # get_authenticate_header() has an authenticator to ask for a
    # WWW-Authenticate challenge. Without one, APIView.handle_exception()
    # silently downgrades the AuthenticationFailed these views raise from
    # 401 to 403 — the exact same DRF behavior StubUserIdAuthentication's
    # own authenticate_header() already works around, caught here
    # empirically the same way: a real 403 where 401 was expected.
    authentication_classes: list[type[BaseAuthentication]] = [OIDCSessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = TokenExchangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            identity = verify_id_token(serializer.validated_data["id_token"])
        except OIDCError as exc:
            raise drf_exceptions.AuthenticationFailed(str(exc)) from exc

        user = _resolve_or_provision_user(email=identity.email, display_name=identity.display_name)
        access_token, expires_in = issue_session_token(user.id)

        return Response(
            {"access_token": access_token, "token_type": "Bearer", "expires_in": expires_in}
        )


class DevMockLoginView(APIView):
    """POST /api/v1/auth/dev-mock-login — Implementation Plan Phase 9's
    local mock provider: stands in for "the user completed the OIDC flow
    with a real IdP." Returns an ID token for `POST /api/v1/auth/token`,
    not a session token directly — so the SAME verification code path a
    real IdP's token would go through is what a caller using this actually
    exercises end-to-end, not a shortcut around it.
    """

    # Listed even though neither view relies on it to resolve request.user
    # (permission_classes is AllowAny) — purely so DRF's
    # get_authenticate_header() has an authenticator to ask for a
    # WWW-Authenticate challenge. Without one, APIView.handle_exception()
    # silently downgrades the AuthenticationFailed these views raise from
    # 401 to 403 — the exact same DRF behavior StubUserIdAuthentication's
    # own authenticate_header() already works around, caught here
    # empirically the same way: a real 403 where 401 was expected.
    authentication_classes: list[type[BaseAuthentication]] = [OIDCSessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        if not settings.KAIROS_OIDC_MOCK_ENABLED:
            # Treated as nonexistent, not forbidden — this endpoint has no
            # business being reachable in an environment where it isn't
            # enabled, matching Spec v1.0 §1's 404-for-absence convention
            # rather than exposing that a disabled mock login exists.
            raise NotFoundError

        serializer = DevMockLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        display_name = serializer.validated_data["display_name"] or email

        if not display_name:
            raise PolicyValidationError("display_name", "must not be blank")

        id_token = mint_mock_id_token(subject=email, email=email, display_name=display_name)
        return Response({"id_token": id_token})


class UserDeactivateView(KairosAPIView):
    """POST /api/v1/admin/users/{id}/deactivate (Spec v1.0 §5.15; PRD
    FR49-51; Test Plan OFF-01/OFF-02). `system_admin`-only — checked
    BEFORE looking up the target user, so a non-admin caller gets a
    uniform 403 regardless of whether the target id exists, never
    leaking which user ids are real (unlike `PATCH /resources/{id}`,
    where existence is already public via `GET /resources`, a real user
    id is not similarly browsable anywhere in this API).
    """

    def post(self, request: Request, pk: uuid.UUID) -> Response:
        idempotency_key = _require_idempotency_key(request)
        user = cast(AppUser, request.user)
        if not AuthorizationService.is_system_admin(user):
            raise drf_exceptions.PermissionDenied()

        try:
            target = AppUser.objects.get(id=pk)
        except AppUser.DoesNotExist as exc:
            raise NotFoundError from exc

        serializer = UserDeactivateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            result = deactivate_user(
                DeactivateUserRequest(
                    target=target, actor=user, reason=reason, request_id=request_id
                )
            )
            body = {
                "user_id": str(result.user_id),
                "status": result.status,
                "bookings_transferred": result.bookings_transferred,
                "bookings_cancelled": result.bookings_cancelled,
                "bookings_retained": result.bookings_retained,
                "waitlist_entries_cancelled": result.waitlist_entries_cancelled,
                "offers_released": result.offers_released,
                "series_flagged_for_admin": result.series_flagged_for_admin,
            }
            return status.HTTP_200_OK, body

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint=f"POST /api/v1/admin/users/{pk}/deactivate",
            # target user id folded into the fingerprinted body — same
            # reasoning as every other {id}-scoped mutation in this
            # codebase (kairos.bookings.views): the body alone ({"reason":
            # ...}) never mentions WHICH user this deactivation targets.
            body={"user_id": str(pk), **dict(request.data)},
            request_id=request_id,
            actor_type=AuditActorType.ADMIN,
            reason=reason,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response
