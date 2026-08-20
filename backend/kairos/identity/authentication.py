"""Authentication classes (Implementation Plan Phase 9; RFC v1.0 §4).

`OIDCSessionAuthentication` is the real mechanism — it validates the
backend's own short-lived session token (kairos/identity/oidc.py), issued
after a real OIDC login. `StubUserIdAuthentication` is the dev-only
`X-Dev-User-Id` header this project has used since Phase 4, now gated
behind `settings.KAIROS_DEV_AUTH_STUB_ENABLED` — checked at request time,
not by which classes happen to be registered, so it is a real,
environment-scoped security boundary rather than a convention. Only
`kairos.settings.test` turns that flag on; see that module's comment.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from rest_framework import authentication, exceptions
from rest_framework.request import Request

from kairos.identity.models import AppUser
from kairos.identity.oidc import SessionTokenError, verify_session_token

DEV_USER_ID_HEADER = "X-Dev-User-Id"


class OIDCSessionAuthentication(authentication.BaseAuthentication):
    """Validates `Authorization: Bearer <session-token>` — the token
    `POST /api/v1/auth/token` issues after verifying a real (or, in
    dev/test, mock) OIDC ID token. Spec v1.0 §1: "Every endpoint requires
    Authorization: Bearer <JWT>."
    """

    def authenticate(self, request: Request) -> tuple[AppUser, None] | None:
        header_value = request.headers.get("Authorization")
        if not header_value or not header_value.startswith("Bearer "):
            return None  # no credentials offered — DRF treats this as anonymous

        token = header_value[len("Bearer ") :].strip()
        try:
            user_id = verify_session_token(token)
        except SessionTokenError as exc:
            raise exceptions.AuthenticationFailed(str(exc)) from exc

        try:
            user = AppUser.objects.get(id=user_id)
        except AppUser.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed(
                "session token does not match a known user"
            ) from exc

        return user, None

    def authenticate_header(self, request: Request) -> str:
        return "Bearer"


class StubUserIdAuthentication(authentication.BaseAuthentication):
    """# STUB — dev-only convenience predating real auth (Phase 4). Kept
    for the existing test suite's sake (see kairos.settings.test's
    comment) rather than requiring every prior test to mint a signed
    session token for coverage the authentication LAYER itself already
    gets from tests/identity/test_authentication.py. Structurally unable
    to authenticate anything unless `KAIROS_DEV_AUTH_STUB_ENABLED` is
    True — checked on every call, not just at import/class-definition
    time, so it cannot silently become reachable by refactoring class
    lists without also flipping a setting no environment but test sets.
    """

    def authenticate(self, request: Request) -> tuple[AppUser, None] | None:
        if not settings.KAIROS_DEV_AUTH_STUB_ENABLED:
            return None  # ignored outside test, per Implementation Plan Phase 9 DoD

        header_value = request.headers.get(DEV_USER_ID_HEADER)
        if header_value is None:
            return None  # no credentials offered — DRF treats this as anonymous

        try:
            user_id = uuid.UUID(header_value)
        except ValueError as exc:
            raise exceptions.AuthenticationFailed(
                f"{DEV_USER_ID_HEADER} is not a valid UUID"
            ) from exc

        try:
            user = AppUser.objects.get(id=user_id)
        except AppUser.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed(
                f"{DEV_USER_ID_HEADER} does not match a known user"
            ) from exc

        return user, None

    def authenticate_header(self, request: Request) -> str:
        # Without this, DRF's APIView.handle_exception() downgrades
        # NotAuthenticated from 401 to 403 — a 401 is only spec-compliant
        # alongside a WWW-Authenticate challenge, and BaseAuthentication's
        # default authenticate_header() returns None, meaning "no challenge
        # available." Spec v1.0 §5.1 documents 401 specifically, so this
        # stub supplies one. Returned unconditionally (even when the stub
        # itself is disabled) so a request using ONLY this header still
        # gets a clean 401, not the generic "Bearer" challenge that would
        # look like the request should have used a session token instead.
        return DEV_USER_ID_HEADER
