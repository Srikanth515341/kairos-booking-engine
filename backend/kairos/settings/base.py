from __future__ import annotations

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")

# ============================================================
# Authentication (Phase 9; RFC v1.0 §4, §8.1)
# ============================================================
# The dev-only X-Dev-User-Id header (kairos/identity/authentication.py,
# StubUserIdAuthentication) is a REAL security boundary, not a convention —
# gated by this flag, checked at request time, not just by which classes
# happen to be wired up. False here means every environment gets it OFF
# unless a settings module explicitly turns it on. Only kairos.settings.test
# does (tests need it as a synchronous, network-free convenience — the
# alternative is minting a signed session token in every one of the ~150
# existing test call sites for zero additional coverage, since the
# authentication LAYER itself is what Phase 9 needs proven, not that every
# prior test happens to exercise it redundantly).
KAIROS_DEV_AUTH_STUB_ENABLED = False

# The local mock OIDC issuer (kairos/identity/oidc.py) stands in for a real
# identity provider so `dev`/`test` never need one running — but it must
# never be reachable in `prod`. Also off by default; dev.py and test.py
# turn it on explicitly.
KAIROS_OIDC_MOCK_ENABLED = False

# Real OIDC provider config (RFC v1.0 §4) — unset in local dev/test, where
# KAIROS_OIDC_MOCK_ENABLED covers the identical code path against a fixed
# dev keypair instead of a live IdP's JWKS endpoint.
OIDC_ISSUER = os.environ.get("OIDC_ISSUER", "")
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")

# The backend's OWN short-lived session token (RFC v1.0 §4: "issues its own
# short-lived internal session token") — HS256, not RS256: unlike the OIDC
# ID token (issued by an external party a relying party must verify without
# sharing a secret), this token is issued AND verified by this same
# service, so a shared HMAC secret is the right tool, not a keypair.
# Falls back to SECRET_KEY, THEN to a dev-only literal — not SECRET_KEY
# alone, which is itself commonly empty in dev/test (no DJANGO_SECRET_KEY
# env var set locally; prod.py raises if it's empty, but dev/test don't).
# An empty HMAC key isn't a quiet insecure default, it's a hard failure
# (PyJWT itself refuses to sign with one) — caught empirically: the first
# real login attempt in a fresh test run raised InvalidKeyError. prod.py
# requires KAIROS_SESSION_SIGNING_KEY set independently, the same way it
# already requires DJANGO_SECRET_KEY, so this dev-only literal is never
# reachable there.
KAIROS_SESSION_SIGNING_KEY = (
    os.environ.get("KAIROS_SESSION_SIGNING_KEY", "")
    or SECRET_KEY
    or "kairos-dev-session-signing-key-insecure"
)
KAIROS_SESSION_TOKEN_TTL_SECONDS = int(os.environ.get("KAIROS_SESSION_TOKEN_TTL_SECONDS", "900"))

# django.contrib.postgres is required by django.contrib.postgres.fields
# (DateTimeRangeField) — RangeField refuses to load without it. auth and
# contenttypes are deliberately excluded: authentication is delegated to
# SSO/OIDC (RFC v1.0 §4, Phase 9), so Django's built-in User/Permission
# machinery has no role here and would only add unused tables.
INSTALLED_APPS = [
    "django.contrib.postgres",
    "rest_framework",
    "kairos.core",
    "kairos.identity",
    "kairos.resources",
    "kairos.bookings",
]

MIDDLEWARE = [
    "kairos.core.middleware.RequestIdMiddleware",
]

ROOT_URLCONF = "kairos.urls"

# JSON only (Implementation Plan Phase 4 scope) — no browsable API, no form
# parsing. kairos_exception_handler wraps every error in the Spec v1.0 §6
# envelope; nothing produces a bare, unenveloped error body.
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "kairos.core.drf.kairos_exception_handler",
    # DRF's default is the string path to django.contrib.auth.models.
    # AnonymousUser — importing that module pulls in ContentType, which
    # fails because contenttypes isn't installed (deliberate, see
    # INSTALLED_APPS above). None makes DRF leave request.user as None for
    # an unauthenticated request instead, which IsAuthenticated already
    # handles correctly (`None and ...` is falsy without touching
    # `.is_authenticated`).
    "UNAUTHENTICATED_USER": None,
}

# Structured JSON logging (Implementation Plan Phase 4 scope) — every
# record's fields plus whatever a call site passes via extra= (request_id,
# user_id, resource_id, outcome).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "kairos.core.logging.JSONFormatter"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

TEMPLATES: list[dict[str, object]] = []

WSGI_APPLICATION = "kairos.wsgi.application"

# DATABASE_URL / DATABASE_URL_TEST — see .env.example. The default here is
# `kairos_app`, the least-privilege role Phase 8 provisions (grant-level
# append-only on audit_log; ordinary DML everywhere else) — the RUNNING
# APPLICATION must never connect as the superuser (Spec v1.0 §3; AUD-01's
# entire premise is that the app role literally *cannot* violate the
# append-only guarantee, which is only true if the app actually connects as
# that role). Migrations need DDL privileges `kairos_app` deliberately
# doesn't have — override DATABASE_URL to the superuser DSN for
# `manage.py migrate` specifically (see README.md "Running Locally"), not
# by widening this default.
DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        default="postgresql://kairos_app:kairos_app@localhost:5432/kairos_dev",
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

LANGUAGE_CODE = "en-us"
