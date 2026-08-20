import os

from .base import *  # noqa: F403
from .base import SECRET_KEY

DEBUG = False

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

if not SECRET_KEY:
    raise RuntimeError("DJANGO_SECRET_KEY must be set in production")
