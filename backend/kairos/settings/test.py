import dj_database_url

from .base import *  # noqa: F403

DEBUG = False

ALLOWED_HOSTS: list[str] = ["localhost", "127.0.0.1"]

# A dedicated database (RFC v1.0 §4.1; infra/init-test-db.sql) so the test
# suite never touches development data.
DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL_TEST",
        default="postgresql://kairos:kairos@localhost:5432/kairos_test",
    )
}

# The ONLY settings module where the X-Dev-User-Id stub is reachable
# (Phase 9; kairos/identity/authentication.py) — the existing test suite's
# `_headers(user)` convenience relies on it, and the real authentication
# LAYER (OIDCSessionAuthentication) is proven separately, on its own terms,
# by tests/identity/test_authentication.py, not by rewriting every prior
# call site. The mock OIDC issuer is available here too, for that suite.
KAIROS_DEV_AUTH_STUB_ENABLED = True
KAIROS_OIDC_MOCK_ENABLED = True

# Phase 16: cancellation/decline dispatch `create_offer_for_freed_range_
# task.delay(...)` from inside a `transaction.on_commit()` hook. Eager mode
# runs it synchronously in-process instead of publishing to Redis — the
# test suite must never depend on a broker being reachable (RFC v1.0 §4.3:
# Celery/Redis is a liveness dependency, not a correctness one, and CI's
# `test` job doesn't run Redis at all, matching the `concurrency` job's own
# Postgres-only-via-plain-docker-run precedent). Combined with Django's
# `TestCase.captureOnCommitCallbacks(execute=True)` (which manually fires
# on_commit hooks without needing a real, non-rolled-back transaction),
# this lets WL-01/WL-03 exercise the REAL dispatch path end to end.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Django's own "capturing backend for tests" (Implementation Plan Phase
# 18) — every send_mail() call appends to `django.core.mail.outbox`
# in-process instead of touching a real transport, the identical
# capture-and-assert shape this test settings module already gets for
# free from CELERY_TASK_ALWAYS_EAGER above.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
