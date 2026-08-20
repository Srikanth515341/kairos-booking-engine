import os

from .base import *  # noqa: F403
from .base import KAIROS_SESSION_SIGNING_KEY, SECRET_KEY

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
