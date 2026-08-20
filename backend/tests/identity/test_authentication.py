"""Real OIDC authentication (Implementation Plan Phase 9; RFC v1.0 §4).

Covers: the end-to-end login flow against the local mock provider (DoD:
"Real OIDC login works end-to-end against the local mock provider"),
rejection of invalid/expired credentials, and two specific, executable
verifications called out explicitly for this phase rather than left to
"should work":

1. `app.actor_id` reflects the REAL authenticated principal (not a stub)
   at the point of the key-claim INSERT — the same spy style Phase 7/8
   used on the write-path session settings, now proving Phase 9 didn't
   introduce a second mechanism for it (see kairos.core.db's shared
   `apply_write_path_session_settings` — Phase 9 changes WHERE actor_id
   comes from, not WHEN/WHERE it's applied).
2. `X-Dev-User-Id` genuinely does nothing outside the test settings module
   — proven by actually starting the app under `kairos.settings.dev` in a
   real subprocess and making a real HTTP request against it, not by
   inspecting settings.py and trusting the flag is wired up correctly.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.core.models import AuditLog, IdempotencyKey
from kairos.identity.models import AppUser
from kairos.identity.oidc import issue_session_token
from kairos.resources.models import Resource

AUTH_TOKEN_URL = "/api/v1/auth/token"
AUTH_MOCK_LOGIN_URL = "/api/v1/auth/dev-mock-login"
BOOKINGS_URL = "/api/v1/bookings"
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------------
# End-to-end login via the local mock provider
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_oidc_login_end_to_end_via_mock_provider(client: APIClient) -> None:
    mock_response = client.post(
        AUTH_MOCK_LOGIN_URL,
        data={"email": "oidc-e2e@example.com", "display_name": "OIDC E2E"},
        format="json",
    )
    assert mock_response.status_code == 200
    id_token = mock_response.json()["id_token"]

    token_response = client.post(AUTH_TOKEN_URL, data={"id_token": id_token}, format="json")
    assert token_response.status_code == 200
    body = token_response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] > 0
    access_token = body["access_token"]

    # JIT-provisioned exactly once, by the verified email claim.
    user = AppUser.objects.get(email="oidc-e2e@example.com")
    assert user.display_name == "OIDC E2E"

    # The session token — not the ID token — is what authenticates
    # subsequent requests (RFC v1.0 §4: "issues its own short-lived
    # internal session token").
    authed_response = client.get("/api/v1/resources", HTTP_AUTHORIZATION=f"Bearer {access_token}")
    assert authed_response.status_code == 200

    # Logging in again with the SAME email resolves the SAME user, not a
    # duplicate — JIT provisioning is get-or-create, not create-always.
    second_mock = client.post(
        AUTH_MOCK_LOGIN_URL, data={"email": "oidc-e2e@example.com"}, format="json"
    )
    second_token = client.post(
        AUTH_TOKEN_URL, data={"id_token": second_mock.json()["id_token"]}, format="json"
    )
    assert AppUser.objects.filter(email="oidc-e2e@example.com").count() == 1
    assert second_token.status_code == 200


@pytest.mark.django_db
def test_mock_login_disabled_outside_dev_and_test_is_404(client: APIClient) -> None:
    with override_settings(KAIROS_OIDC_MOCK_ENABLED=False):
        response = client.post(
            AUTH_MOCK_LOGIN_URL, data={"email": "should-not-work@example.com"}, format="json"
        )
    assert response.status_code == 404


# --------------------------------------------------------------------
# Rejection of invalid credentials
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_token_exchange_rejects_malformed_id_token(client: APIClient) -> None:
    response = client.post(AUTH_TOKEN_URL, data={"id_token": "not-a-jwt"}, format="json")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.django_db
def test_token_exchange_rejects_wrong_signature(client: APIClient) -> None:
    # A token that LOOKS like a mock-issuer token (right issuer/audience
    # claims) but was never actually signed by the mock issuer's key —
    # simulates a forged/tampered credential, not just a garbage string.
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = pyjwt.encode(
        {
            "iss": "https://mock-oidc.kairos.local",
            "sub": "attacker",
            "aud": "kairos-api",
            "email": "attacker@example.com",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        attacker_key,
        algorithm="RS256",
    )
    response = client.post(AUTH_TOKEN_URL, data={"id_token": forged}, format="json")
    assert response.status_code == 401
    assert not AppUser.objects.filter(email="attacker@example.com").exists()


@pytest.mark.django_db
def test_token_exchange_rejects_expired_id_token(client: APIClient) -> None:
    # mint_mock_id_token's own 5-minute expiry hasn't elapsed in a fast
    # test run — force genuine expiry via a real, independently-signed,
    # past-dated token instead of waiting or monkeypatching time itself.
    import jwt as pyjwt

    from kairos.identity.oidc import _MOCK_PRIVATE_KEY, MOCK_AUDIENCE, MOCK_ISSUER

    now = int(time.time())
    already_expired = pyjwt.encode(
        {
            "iss": MOCK_ISSUER,
            "sub": "expired@example.com",
            "aud": MOCK_AUDIENCE,
            "email": "expired@example.com",
            "iat": now - 600,
            "exp": now - 1,
        },
        _MOCK_PRIVATE_KEY,
        algorithm="RS256",
    )
    response = client.post(AUTH_TOKEN_URL, data={"id_token": already_expired}, format="json")
    assert response.status_code == 401
    assert not AppUser.objects.filter(email="expired@example.com").exists()


@pytest.mark.django_db
def test_session_token_rejected_when_expired(client: APIClient, app_user: AppUser) -> None:
    with override_settings(KAIROS_SESSION_TOKEN_TTL_SECONDS=-1):
        expired_token, _ = issue_session_token(app_user.id)

    response = client.get("/api/v1/resources", HTTP_AUTHORIZATION=f"Bearer {expired_token}")
    assert response.status_code == 401


@pytest.mark.django_db
def test_session_token_rejected_when_subject_unknown(client: APIClient) -> None:
    token, _ = issue_session_token(uuid.uuid4())
    response = client.get("/api/v1/resources", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 401


# --------------------------------------------------------------------
# app.actor_id reflects the REAL authenticated principal — same spy
# style as Phase 7's test_session_settings_are_active_at_the_key_claim_
# insert_itself, now under real OIDC auth instead of X-Dev-User-Id.
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_real_oidc_principal_reaches_app_actor_id_at_key_claim_insert(
    monkeypatch: pytest.MonkeyPatch, client: APIClient, active_resource: Resource
) -> None:
    mock_response = client.post(
        AUTH_MOCK_LOGIN_URL,
        data={"email": "spy-check@example.com", "display_name": "Spy Check"},
        format="json",
    )
    token_response = client.post(
        AUTH_TOKEN_URL, data={"id_token": mock_response.json()["id_token"]}, format="json"
    )
    access_token = token_response.json()["access_token"]
    user = AppUser.objects.get(email="spy-check@example.com")

    observed: dict[str, str] = {}
    original_create = IdempotencyKey.objects.create

    def _spy_create(*args: object, **kwargs: object) -> IdempotencyKey:
        with connection.cursor() as cur:
            cur.execute("SELECT current_setting('app.actor_id', true)")
            observed["actor_id"] = cur.fetchone()[0]  # type: ignore[index]
        return original_create(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(IdempotencyKey.objects, "create", _spy_create)

    start = timezone.now() + timedelta(hours=1)
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {access_token}",
        HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
    )

    assert response.status_code == 201
    # NOT a stub value, NOT empty — the real, authenticated principal's id.
    assert observed["actor_id"] == str(user.id)

    audit_row = AuditLog.objects.get(entity_type="booking", entity_id=response.json()["id"])
    assert audit_row.actor_id == user.id
    assert audit_row.actor_type == "user"


# --------------------------------------------------------------------
# X-Dev-User-Id must not work outside the test environment — verified by
# actually starting the app under kairos.settings.dev in a real subprocess.
# --------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.django_db(transaction=True)
def test_x_dev_user_id_is_rejected_under_dev_settings(transactional_db: None) -> None:
    """Not a settings-flag unit test — a real HTTP request against a real
    process started with DJANGO_SETTINGS_MODULE=kairos.settings.dev, the
    actual module `manage.py runserver` uses, not a simulation of it. A
    bug where dev.py itself accidentally set KAIROS_DEV_AUTH_STUB_ENABLED
    would NOT be caught by exercising the authenticator class in isolation
    under test settings — only this catches that.
    """
    import os

    cfg = connection.settings_dict
    dsn = (
        f"postgresql://kairos_app:kairos_app@{cfg['HOST'] or 'localhost'}:"
        f"{cfg['PORT'] or 5432}/{cfg['NAME']}"
    )
    port = _free_port()
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "kairos.settings.dev",
        "DATABASE_URL": dsn,
    }

    proc = subprocess.Popen(
        [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{port}/api/v1/resources"
        deadline = time.time() + 20
        ready = False
        while time.time() < deadline:
            try:
                urllib.request.urlopen(base_url, timeout=1)
                ready = True
                break
            except urllib.error.HTTPError:
                ready = True  # server responded — any status means it's up
                break
            except Exception:
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    pytest.fail(f"dev-settings server exited early:\n{output}")
                time.sleep(0.3)
        assert ready, "dev-settings server did not become reachable in time"

        # A random UUID is enough — the stub, if reachable, would attempt
        # a lookup and 401 with "does not match a known user"; what this
        # test actually distinguishes is THAT path never being reached at
        # all (the stub authenticator returns None immediately when
        # disabled, falling through to a bare unauthenticated 401 with no
        # X-Dev-User-Id-specific handling).
        req = urllib.request.Request(base_url, headers={"X-Dev-User-Id": str(uuid.uuid4())})
        try:
            urllib.request.urlopen(req, timeout=5)
            status = 200
            challenge = None
        except urllib.error.HTTPError as exc:
            status = exc.code
            challenge = exc.headers.get("WWW-Authenticate")

        assert status == 401
        # The challenge is the OIDC "Bearer" one, not "X-Dev-User-Id" —
        # confirming the stub authenticator never even offered its own
        # challenge, i.e. it was never in play for this request at all.
        assert challenge == "Bearer"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
