"""Security headers + CORS (Implementation Plan Phase 22). Headers reuse
Django's own `SecurityMiddleware`/`XFrameOptionsMiddleware` (a framework
guarantee, not reinvented) — tested here as real response headers on a
real request, not by inspecting MIDDLEWARE/settings values in isolation,
since `X_FRAME_OPTIONS` being SET means nothing if the middleware that
reads it was never added to MIDDLEWARE (a real bug caught writing this
exact test — see kairos/settings/base.py's own comment on
XFrameOptionsMiddleware).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from rest_framework.test import APIClient

from kairos.identity.models import AppUser

BACKEND_DIR = Path(__file__).resolve().parent.parent
CHECKS_URL = "/api/v1/admin/checks/latest"


def _headers(user: AppUser) -> dict[str, str]:
    return {"HTTP_X_DEV_USER_ID": str(user.id)}


@pytest.mark.django_db
def test_security_headers_present_on_a_real_response(app_user: AppUser) -> None:
    client = APIClient()
    response = client.get(CHECKS_URL, **_headers(app_user))
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    assert response["Referrer-Policy"] == "same-origin"


# --------------------------------------------------------------------
# CORS — explicit allowlist, no wildcard, ever
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_cors_origin_not_in_allowlist_gets_no_cors_header(settings, app_user: AppUser) -> None:
    settings.CORS_ALLOWED_ORIGINS = ["https://allowed.example.com"]
    client = APIClient()
    response = client.get(CHECKS_URL, HTTP_ORIGIN="https://evil.example.com", **_headers(app_user))
    assert "Access-Control-Allow-Origin" not in response


@pytest.mark.django_db
def test_cors_allowlisted_origin_is_reflected_back_with_vary(settings, app_user: AppUser) -> None:
    settings.CORS_ALLOWED_ORIGINS = ["https://allowed.example.com"]
    client = APIClient()
    response = client.get(
        CHECKS_URL, HTTP_ORIGIN="https://allowed.example.com", **_headers(app_user)
    )
    assert response["Access-Control-Allow-Origin"] == "https://allowed.example.com"
    assert "Origin" in response["Vary"]


@pytest.mark.django_db
def test_cors_never_reflects_a_literal_wildcard_even_if_configured(
    settings, app_user: AppUser
) -> None:
    """`CorsMiddleware` never treats `"*"` as "allow everything" — if it
    somehow ended up in the allowlist (prod.py refuses to start with it
    at all, see below), an Origin header would still need to match it
    byte-for-byte, which no real Origin ever does.
    """
    settings.CORS_ALLOWED_ORIGINS = ["*"]
    client = APIClient()
    response = client.get(
        CHECKS_URL, HTTP_ORIGIN="https://anything.example.com", **_headers(app_user)
    )
    assert "Access-Control-Allow-Origin" not in response


def test_cors_preflight_from_allowed_origin_succeeds(settings) -> None:
    settings.CORS_ALLOWED_ORIGINS = ["https://allowed.example.com"]
    client = APIClient()
    response = client.options(
        CHECKS_URL,
        HTTP_ORIGIN="https://allowed.example.com",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
    )
    assert response.status_code == 204
    assert response["Access-Control-Allow-Origin"] == "https://allowed.example.com"
    assert "GET" in response["Access-Control-Allow-Methods"]


def test_cors_preflight_from_disallowed_origin_is_rejected(settings) -> None:
    settings.CORS_ALLOWED_ORIGINS = ["https://allowed.example.com"]
    client = APIClient()
    response = client.options(
        CHECKS_URL,
        HTTP_ORIGIN="https://evil.example.com",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
    )
    assert response.status_code == 403
    assert "Access-Control-Allow-Origin" not in response


# --------------------------------------------------------------------
# prod.py — "no wildcard in production," enforced, not just documented
# --------------------------------------------------------------------


def _prod_env(cors_allowed_origins: str) -> dict[str, str]:
    import os

    return {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "kairos.settings.prod",
        "DJANGO_SECRET_KEY": "a" * 50,
        "KAIROS_SESSION_SIGNING_KEY": "b" * 50,
        "SMTP_HOST": "smtp.example.com",
        "DJANGO_ALLOWED_HOSTS": "example.com",
        "CORS_ALLOWED_ORIGINS": cors_allowed_origins,
    }


def test_prod_settings_refuse_to_start_with_a_wildcard_cors_origin() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=str(BACKEND_DIR),
        env=_prod_env("*"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "CORS_ALLOWED_ORIGINS must not contain '*'" in result.stderr


def test_prod_settings_refuse_to_start_with_a_wildcard_among_other_origins() -> None:
    """Not just a bare `"*"` — the check must catch it mixed into a
    comma-separated list too, not only the single-value case.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=str(BACKEND_DIR),
        env=_prod_env("https://allowed.example.com,*"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "CORS_ALLOWED_ORIGINS must not contain '*'" in result.stderr


def test_prod_settings_start_fine_with_a_real_explicit_origin() -> None:
    """The positive control: this isn't refusing to start at all, or
    refusing for an unrelated reason — it starts cleanly with a real,
    non-wildcard origin.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import django; django.setup(); print('OK')"],
        cwd=str(BACKEND_DIR),
        env=_prod_env("https://allowed.example.com"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
