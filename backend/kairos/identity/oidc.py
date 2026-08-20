"""OIDC token validation and the backend's own short-lived session token
(RFC v1.0 §4, §8.1; PRD FR44–FR48). Two distinct token types live here:

1. The ID token an OIDC provider issues after a user authenticates with it —
   RS256, asymmetric, verified against the issuer's published public key
   (real deployments: JWKS fetched from `OIDC_ISSUER`; local dev/test: a
   fixed dev keypair standing in for a real IdP, per
   Implementation Plan Phase 9's "local mock provider... so the system runs
   without external dependencies"). This service never signs one — it only
   ever verifies.
2. This backend's OWN internal session token, minted in `issue_session_token`
   after step 1 succeeds — HS256, since the same service both issues and
   verifies it (RFC v1.0 §4: "issues its own short-lived internal session
   token"). Every authenticated API request after login carries THIS token,
   not the original ID token — the API never re-validates against the IdP
   on every request.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt
from django.conf import settings

# ============================================================
# Dev-only mock issuer keypair (Implementation Plan Phase 9's "local mock
# provider" — RS256, matching what a real IdP's asymmetric signing looks
# like, so the verification code path exercised here is the SAME one a
# real IdP's tokens would go through, not a simplified stand-in). Fixed and
# checked into the repo, exactly like infra/docker-compose.yml's hardcoded
# POSTGRES_PASSWORD and Phase 8's kairos_app password — a dev-only literal,
# never reachable when KAIROS_OIDC_MOCK_ENABLED is False (prod.py never
# sets it True). Generated once for this project; there is nothing secret
# about a LOCAL mock issuer's key pair being public — it signs nothing a
# real party ever needs to trust.
# ============================================================
MOCK_ISSUER = "https://mock-oidc.kairos.local"
MOCK_AUDIENCE = "kairos-api"

_MOCK_PRIVATE_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQC67jmxO9FIDQt8
0cWhamqciJx5CU+ORpxFVb07G4zl1lzOETt6MVJM8b1ANYr/mK0VAaeUeR2ek0vQ
iykDlYm1SOROqoaIDe3F2lVKN3WtI88Xud3zhptju0JeGRdHCFghS/EGsNXhb4dI
bnQEhNiOH7EzWmQxjSQPDsgVVvn8P4WrQtvDzRNxve6FTHBs2NZIPn1+1u+jvnla
IX5g1CxsEa6d7M8ldCf+V66tAfmYH0u8RiDrBbnQmq6kgK6eqi/ySIkEevf5MFPp
0s3ZCbMdbEap3PD+6NCBVxz7VcD2k09CCKhFPMD28efZWbiMx8uvLTdBH9bgwpJr
1YK58bwhAgMBAAECggEAAiMXl0JhD+MQSs7GJOSHz/7S1SJpXa3KexNyHKMhvv3u
pS92v9yg/X61BG8oRehLsEYK0ax5zXaPIxT4NNGIl8E71PdnntYT7hNjLrxIFODo
LPQDyNU04RTCuzsrtDTw9v58hLBQXamuAQK8QlH3fNZ5auYhZ5yeuSpY2L+cD4lw
X3SWrc/cmk9rCWdT/FnJWYjd38FAwoMMnRFzV67CuoatWbskvY5T6bbbY4+gKM6+
iR6XrkOWMvDf6J/C/UCBe9XWypmLVdKl8SCtLlo3QXdqCCLWFNmENoLK7vkMoaxw
mFb77gCt/B4VefdL1IMbXH65JXGmPbAWo+LPT8ra8QKBgQDhcPh+q1berRMJzP17
pB3vYhThObI/yy6WxajPKoQSdpm7qyVER+YXEHOHGUUS39NU48Z9ZdRRt+9AMaDT
IEiJEsDCppQth438TP+2sm7QZem/JDo4Gl5VKbLjk3PzzDnJYC/Y6MyuLvUNmEQ3
+GXdWLOD9r6HP/rgOMetc0FMaQKBgQDUROVEcYojSLfCL69Cy7ZOZvxIxkDI3lmv
zRUIAIybsohOYLcm23tI3TpOjln/FkpPNxlKOKbeu8tYEcfQ5aeHfkyJUDSLR4dC
a39cKMaVQ+FYRK1TRC0u4hp2eZHQVc2vpFGAAveNQW4AE+PF1ZQddEH1hLSd14Gg
RS4NydXa+QKBgDvzYuGshslolSuCC9aZaiyClLLgOPql+Zm2rpGI6N5QE8nIVXy2
8gUoJtDCc4/1XamxeNNYBQJGO6WCjRYX+7IX/pLE4ZrJ9YQmpcnb4YQh1esyoxjQ
Sk2zbRL/31Hx+VwH7DFulx+q9RfMajfIIq6eK/7n2vr0lK6T6uXBRJghAoGAZLer
8SXOXZ+LUWA+0HrILbs/yWgIJUcbVwcAC6P3E4lKk6XgQVeyz4fouo1gtmBlMeD6
5vSqeNSyTz9giAXvz6JlvmGIDO4Lh8Bp1dijIP/sVG2BsBiRN8WguMZGIYwU5Fob
MZo2y5dYEkFduej1NmSLR2uIJ7yxjNJGX9R83EECgYBiQtCn7rjc+oYFX3FmOlAJ
tsrKAkQTCU8+HECIT1qdGAcF3P3u9SyFhGKnxJRTUBn32mkasOchT8gcZ6fV8ehP
N4rwD+1725PV//YR5A5UO4StqIZ8/syo2vzugngNL6rfpfGYsLGA0dnajKJZ6U+a
UH7G72ZghT/uKVqZpc7F+g==
-----END PRIVATE KEY-----"""

_MOCK_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuu45sTvRSA0LfNHFoWpq
nIiceQlPjkacRVW9OxuM5dZczhE7ejFSTPG9QDWK/5itFQGnlHkdnpNL0IspA5WJ
tUjkTqqGiA3txdpVSjd1rSPPF7nd84abY7tCXhkXRwhYIUvxBrDV4W+HSG50BITY
jh+xM1pkMY0kDw7IFVb5/D+Fq0Lbw80Tcb3uhUxwbNjWSD59ftbvo755WiF+YNQs
bBGunezPJXQn/leurQH5mB9LvEYg6wW50JqupICunqov8kiJBHr3+TBT6dLN2Qmz
HWxGqdzw/ujQgVcc+1XA9pNPQgioRTzA9vHn2Vm4jMfLry03QR/W4MKSa9WCufG8
IQIDAQAB
-----END PUBLIC KEY-----"""


class OIDCError(Exception):
    """The ID token failed verification — bad signature, wrong issuer,
    wrong audience, or expired. Never distinguishes WHICH beyond a message,
    matching Spec v1.0 §6.1's `unauthorized` 401 (no detail that helps an
    attacker enumerate why a specific token was rejected)."""


class SessionTokenError(Exception):
    """The internal session token failed verification."""


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str  # the IdP's own subject identifier — NOT our AppUser.id
    email: str
    display_name: str


def mint_mock_id_token(*, subject: str, email: str, display_name: str) -> str:
    """Stands in for "the user completed the OIDC flow with a real IdP and
    the browser was redirected back with this ID token" — Implementation
    Plan Phase 9's local mock provider. Only ever callable when
    `KAIROS_OIDC_MOCK_ENABLED` is True (enforced by the view that exposes
    this, never here — this function itself has no environment gate, so a
    future caller can't accidentally forget the check and rely on this
    module alone to enforce it).
    """
    now = int(time.time())
    payload = {
        "iss": MOCK_ISSUER,
        "sub": subject,
        "aud": MOCK_AUDIENCE,
        "email": email,
        "name": display_name,
        "iat": now,
        "exp": now + 300,  # 5 minutes — an ID token is presented once, immediately
    }
    return jwt.encode(payload, _MOCK_PRIVATE_KEY, algorithm="RS256")


def verify_id_token(token: str) -> VerifiedIdentity:
    """Verifies a token's signature, issuer, audience, and expiry, and
    returns the claims the caller actually needs. Raises OIDCError on any
    failure — signature, issuer, audience, and expiry are checked as one
    unit, not sequentially with different error messages, so a caller
    can't learn which check failed.

    Real-IdP path (`iss` != the mock issuer): fetches the provider's public
    key from its JWKS endpoint. Structurally complete but genuinely
    UNTESTED against a live IdP — this project has none to test against
    (the same documented-gap pattern as IDEM-07/08 needing Phase 28's fault
    injection tooling). The mock-issuer path below is the one this phase's
    tests actually exercise end-to-end.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise OIDCError("malformed token") from exc

    issuer = str(unverified.get("iss") or "")
    try:
        if issuer == MOCK_ISSUER:
            if not settings.KAIROS_OIDC_MOCK_ENABLED:
                raise OIDCError("mock issuer is not enabled in this environment")
            claims = jwt.decode(
                token, _MOCK_PUBLIC_KEY, algorithms=["RS256"], audience=MOCK_AUDIENCE
            )
        else:
            claims = jwt.decode(
                token,
                _fetch_jwks_public_key(issuer, unverified),
                algorithms=["RS256"],
                audience=settings.OIDC_CLIENT_ID,
                issuer=settings.OIDC_ISSUER,
            )
    except jwt.PyJWTError as exc:
        raise OIDCError("token failed signature/issuer/audience/expiry verification") from exc

    email = claims.get("email")
    subject = claims.get("sub")
    if not email or not subject:
        raise OIDCError("token is missing required 'sub'/'email' claims")

    return VerifiedIdentity(
        subject=str(subject), email=str(email), display_name=str(claims.get("name") or email)
    )


def _fetch_jwks_public_key(issuer: str, unverified_claims: dict[str, object]) -> str:
    """Real-IdP JWKS fetch — see verify_id_token's docstring: this path has
    no local mock/test coverage, since exercising it needs a real running
    IdP this project doesn't have. Left structurally complete (not a stub
    that silently no-ops) so Phase 9 doesn't leave a landmine for whoever
    connects a real provider first.
    """
    import urllib.request

    from jwt import PyJWKClient

    if not issuer or issuer != settings.OIDC_ISSUER:
        raise OIDCError(f"unrecognized token issuer: {issuer!r}")
    jwks_url = f"{issuer.rstrip('/')}/.well-known/jwks.json"
    client = PyJWKClient(jwks_url, cache_keys=True)
    with urllib.request.urlopen(jwks_url, timeout=5):  # fail fast if unreachable
        pass
    signing_key = client.get_signing_key_from_jwt(
        jwt.encode(unverified_claims, "unused", algorithm="none")
    )
    return signing_key.key  # type: ignore[no-any-return]


def issue_session_token(user_id: uuid.UUID) -> tuple[str, int]:
    """The backend's own short-lived session token, minted after
    `verify_id_token` succeeds and the identity has been resolved to a real
    `AppUser` (kairos/identity/views.py). Returns (token, expires_in).
    """
    ttl = settings.KAIROS_SESSION_TOKEN_TTL_SECONDS
    now = int(time.time())
    payload = {"sub": str(user_id), "iat": now, "exp": now + ttl}
    token = jwt.encode(payload, settings.KAIROS_SESSION_SIGNING_KEY, algorithm="HS256")
    return token, ttl


def verify_session_token(token: str) -> uuid.UUID:
    try:
        claims = jwt.decode(token, settings.KAIROS_SESSION_SIGNING_KEY, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise SessionTokenError("session token is invalid or expired") from exc

    try:
        return uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise SessionTokenError("session token subject is not a valid user id") from exc
