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
    "kairos.identity",
    "kairos.resources",
    "kairos.bookings",
]

MIDDLEWARE: list[str] = []

ROOT_URLCONF = "kairos.urls"

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
