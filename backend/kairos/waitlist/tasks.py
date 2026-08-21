"""Registered here, not inside `services.py` — Celery's `autodiscover_tasks()`
(`kairos/celery.py`) only scans a module literally named `tasks.py` per
installed app (Phase 13's own hard-learned lesson, `kairos/core/tasks.py`):
a `@shared_task` defined anywhere else is silently absent from a worker's
registered task list, with no error anywhere.
"""

from __future__ import annotations

from datetime import datetime

from celery import shared_task

from kairos.resources.models import Resource


@shared_task
def create_offer_for_freed_range_task(
    resource_id: str, freed_start_iso: str, freed_end_iso: str, request_id: str
) -> None:
    """RFC v1.0 §5c step 4 / §10.2: the worker `transaction.on_commit()`
    enqueues after a cancellation (`kairos.bookings.services.
    cancel_booking`) or an offer decline (`kairos.waitlist.services.
    decline_offer`) frees a range.

    Deferred import, not a module-level one: `kairos.waitlist.services`
    imports `kairos.bookings.services` (for `create_booking`, to create
    the hold), and `kairos.bookings.services` imports THIS module (to
    dispatch cascade after a cancellation) — importing `kairos.waitlist.
    services` at module load time here would close that into a genuine
    circular import. Importing it only when the task actually RUNS breaks
    the cycle without restructuring either app; by call time both modules
    are already fully loaded regardless of which one happened to import
    first.
    """
    from kairos.waitlist.services import create_offer_for_freed_range

    resource = Resource.objects.get(id=resource_id)
    create_offer_for_freed_range(
        resource,
        datetime.fromisoformat(freed_start_iso),
        datetime.fromisoformat(freed_end_iso),
        request_id,
    )
