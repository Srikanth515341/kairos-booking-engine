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
