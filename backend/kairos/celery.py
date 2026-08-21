"""The Celery application (Implementation Plan Phase 13; RFC v1.0 §4.1,
§4.3). Redis is a LIVENESS dependency, never a correctness one — the
exclusion constraint holds with or without a worker running (RFC v1.0
§4.3): if Redis or every worker is down, bookings still succeed or
conflict correctly, and only asynchronous liveness (rolling
materialization, tz re-materialization, later phases' waitlist cascade
and hold reclamation) pauses.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kairos.settings.prod")  # matches wsgi.py's default

app = Celery("kairos")
# Reads every CELERY_* setting from Django settings (namespace="CELERY"
# means CELERY_BROKER_URL in settings.py becomes broker_url here) — one
# configuration source, not a parallel celeryconfig.py to keep in sync.
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
