import os

from .base import *  # noqa: F403
from .base import CORS_ALLOWED_ORIGINS, EMAIL_HOST, KAIROS_SESSION_SIGNING_KEY, SECRET_KEY

DEBUG = False

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set in production")

# base.py's dev-only literal fallback must never be reachable here — every
# session token this backend issues is only as trustworthy as this key.
if KAIROS_SESSION_SIGNING_KEY == "kairos-dev-session-signing-key-insecure":
    raise RuntimeError(
        "KAIROS_SESSION_SIGNING_KEY (or DJANGO_SECRET_KEY) must be set in production"
    )

# Real transactional delivery (Implementation Plan Phase 18) — the same
# "refuse to start with a dev-shaped default" discipline as the two checks
# above, applied to SMTP_HOST/PORT/USER/PASSWORD (base.py). An unset
# EMAIL_HOST would silently mean every notification (PRD FR52-54) fails
# on its first delivery attempt in production, retried into the ground by
# send_notification_task rather than caught at startup.
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
if not EMAIL_HOST:
    raise RuntimeError("SMTP_HOST must be set in production")

# Implementation Plan Phase 22: "CORS configured explicitly, no wildcard
# in production" — enforced here, not merely documented, the identical
# "refuse a dev-shaped default" discipline as the three checks above.
# kairos.core.middleware.CorsMiddleware never treats "*" as meaningful
# anyway (it only ever reflects an exact allowlisted Origin back), so this
# check is defense-in-depth against someone setting CORS_ALLOWED_ORIGINS=*
# expecting it to work the way many other frameworks' wildcard does.
if "*" in CORS_ALLOWED_ORIGINS:
    raise RuntimeError("CORS_ALLOWED_ORIGINS must not contain '*' in production")

# HTTPS-only security headers (Implementation Plan Phase 22) — meaningless
# against a local, non-TLS dev server, so these are prod-only rather than
# in base.py alongside the always-on headers.
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000  # one year — the standard HSTS preload minimum
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
