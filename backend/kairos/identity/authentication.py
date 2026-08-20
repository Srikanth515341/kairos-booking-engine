"""# STUB — replaced in Phase 9 by real OIDC/SSO JWT validation (RFC v1.0 §4).

Resolves the authenticated principal from a dev-only X-Dev-User-Id header
instead of a verified token. Blocking core correctness work on SSO
integration would be a sequencing mistake (Implementation Plan Phase 4) —
identity is trivially stubbed here so every endpoint built from Phase 4
onward has a real, if temporary, notion of "who is making this request."
"""

from __future__ import annotations

import uuid

from rest_framework import authentication, exceptions
from rest_framework.request import Request

from kairos.identity.models import AppUser

DEV_USER_ID_HEADER = "X-Dev-User-Id"


class StubUserIdAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request: Request) -> tuple[AppUser, None] | None:
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
        # stub supplies one.
        return DEV_USER_ID_HEADER
