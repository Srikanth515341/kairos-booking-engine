from .base import *  # noqa: F403

DEBUG = True

ALLOWED_HOSTS: list[str] = ["localhost", "127.0.0.1"]

# The mock OIDC issuer is available in dev (no real IdP required locally —
# Implementation Plan Phase 9 scope) but the X-Dev-User-Id stub is NOT:
# dev is meant to exercise the real authentication path end-to-end, the
# same one prod uses, just against a local stand-in identity provider
# instead of a real one.
KAIROS_OIDC_MOCK_ENABLED = True
