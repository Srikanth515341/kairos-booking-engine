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

from django.conf import settings
from rest_framework import exceptions as drf_exceptions
from rest_framework.authentication import BaseAuthentication
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from kairos.core.exceptions import NotFoundError, PolicyValidationError
from kairos.identity.authentication import OIDCSessionAuthentication
from kairos.identity.models import AppUser
from kairos.identity.oidc import (
    OIDCError,
    issue_session_token,
    mint_mock_id_token,
    verify_id_token,
)
from kairos.identity.serializers import DevMockLoginSerializer, TokenExchangeSerializer


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
