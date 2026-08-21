"""Celery task registration for `core`. Celery's `autodiscover_tasks()`
(kairos/celery.py) only looks for a module literally named `tasks.py`
inside each installed app — `kairos/core/tzdata_check.py`'s own name
doesn't match that convention, so its task wrapper lives here instead,
thin, importing the real logic rather than duplicating it. Caught live:
the first `docker compose up` of the worker registered
`kairos.bookings.tasks.*` but NOT a tzdata-drift task at all, silently,
until `celery -A kairos worker` was actually run and its own startup
banner was read — exactly the kind of "no errors, just absence" failure
mode RFC v1.0 §14 warns background jobs are prone to.
"""

from __future__ import annotations

from celery import shared_task

from kairos.core.tzdata_check import check_tzdata_drift


@shared_task(name="kairos.core.tasks.check_tzdata_drift_task")
def check_tzdata_drift_task() -> dict[str, str | bool | None]:
    return check_tzdata_drift()
