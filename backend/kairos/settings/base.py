from __future__ import annotations

import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")

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

# DATABASE_URL / DATABASE_URL_TEST — see .env.example.
DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        default="postgresql://kairos:kairos@localhost:5432/kairos_dev",
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"

LANGUAGE_CODE = "en-us"
