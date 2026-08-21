import os

from .base import *  # noqa: F403
from .base import EMAIL_HOST, KAIROS_SESSION_SIGNING_KEY, SECRET_KEY

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
