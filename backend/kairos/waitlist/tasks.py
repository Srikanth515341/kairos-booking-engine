"""Registered here, not inside `services.py` — Celery's `autodiscover_tasks()`
(`kairos/celery.py`) only scans a module literally named `tasks.py` per
installed app (Phase 13's own hard-learned lesson, `kairos/core/tasks.py`):
a `@shared_task` defined anywhere else is silently absent from a worker's
registered task list, with no error anywhere.
"""

from __future__ import annotations

import logging
from datetime import datetime

from celery import shared_task

from kairos.resources.models import Resource

logger = logging.getLogger(__name__)


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


def dispatch_cascade(resource_id: str, start: datetime, end: datetime, request_id: str) -> None:
    """The ONE place `create_offer_for_freed_range_task.delay(...)` is
    called from (`cancel_booking` and `decline_offer` both go through
    this, never `.delay()` directly) — wrapped so a broker outage
    degrades rather than raises.

    RFC v1.0 §4.3: Celery/Redis is a LIVENESS dependency, never a
    correctness one. Both callers register their dispatch inside
    `transaction.on_commit()`, which runs synchronously right after a
    real, already-successful commit — an unhandled exception here would
    propagate out of that commit and surface as a request failure for an
    operation (cancel/decline) that ALREADY SUCCEEDED at the database
    level, exactly the "an outage turns a success into an apparent
    failure" mistake WL-06 exists to catch. Test Plan v1.0 WL-06's own
    assertion is explicit: booking creation and cancellation must still
    succeed with Redis down; only the offer/cascade — genuinely
    Redis-dependent — degrades, silently from the caller's perspective,
    logged here as the alert signal Spec v1.0 §14/RFC v1.0 §4.3 call for
    (full alert-routing infrastructure is Phase 21; this is the same
    "log the signal now, wire real alerting later" precedent Phase 13's
    tzdata-drift check already established).
    """
    try:
        create_offer_for_freed_range_task.delay(
            resource_id, start.isoformat(), end.isoformat(), request_id
        )
    except Exception:
        logger.error(
            "cascade_dispatch_failed_broker_unavailable",
            extra={"request_id": request_id, "resource_id": resource_id},
            exc_info=True,
        )


@shared_task(name="kairos.waitlist.tasks.reap_expired_holds_task")
def reap_expired_holds_task() -> dict[str, object]:
    from kairos.waitlist.services import reap_expired_holds

    return reap_expired_holds()
