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

import logging
from typing import Any

from celery import shared_task

from kairos.core.constants import (
    ALERT_EMAIL_MAX_RETRIES,
    ALERT_EMAIL_RETRY_BACKOFF_MAX_SECONDS,
    NOTIFICATION_MAX_RETRIES,
    NOTIFICATION_RETRY_BACKOFF_MAX_SECONDS,
)
from kairos.core.reconciliation import check_reconciliation
from kairos.core.schema_assertion import check_schema_assertion
from kairos.core.tzdata_check import check_tzdata_drift

logger = logging.getLogger(__name__)


@shared_task(name="kairos.core.tasks.check_tzdata_drift_task")
def check_tzdata_drift_task() -> dict[str, str | bool | None]:
    return check_tzdata_drift()


@shared_task(
    bind=True,
    name="kairos.core.tasks.send_notification_task",
    # PRD v1.0 FR55: "must be recorded and retried." autoretry_for=
    # (Exception,) is deliberately broad, mirroring kairos.waitlist.tasks.
    # dispatch_cascade's own reasoning: enumerating exact SMTP/network
    # exception classes risks missing one and silently reintroducing the
    # exact "one failure mode falls through unretried" bug this decorator
    # exists to prevent. retry_backoff=True gives exponential spacing
    # between attempts rather than hammering an outage immediately.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=NOTIFICATION_RETRY_BACKOFF_MAX_SECONDS,
    retry_jitter=True,
    max_retries=NOTIFICATION_MAX_RETRIES,
)
def send_notification_task(
    self: Any,
    notification_id: str,
    notification_type: str,
    recipient_user_id: str,
    recipient_email: str,
    subject: str,
    body: str,
    context: dict[str, Any],
    request_id: str,
) -> None:
    # Deferred import — same circular-import shape as
    # kairos.waitlist.tasks.create_offer_for_freed_range_task: kairos.core.
    # notifications' notify_* builders call THIS module's
    # dispatch_notification (below), so importing kairos.core.notifications
    # at this module's top level would close the cycle at load time.
    from kairos.core.notifications import _execute_notification_delivery

    _execute_notification_delivery(
        notification_id=notification_id,
        notification_type=notification_type,
        recipient_user_id=recipient_user_id,
        recipient_email=recipient_email,
        subject=subject,
        body=body,
        context=context,
        request_id=request_id,
    )


def dispatch_notification(
    *,
    notification_id: str,
    notification_type: str,
    recipient_user_id: str,
    recipient_email: str,
    subject: str,
    body: str,
    context: dict[str, Any],
    request_id: str,
) -> None:
    """The ONE place `send_notification_task.delay(...)` is called from —
    mirrors `kairos.waitlist.tasks.dispatch_cascade` (Phase 17) exactly,
    for the identical reason: this is frequently called from inside a
    `transaction.on_commit()` callback (kairos.bookings.services.
    cancel_booking's admin-cancellation path), which runs synchronously
    right after a real, already-successful commit. An exception escaping
    here would propagate into that already-succeeded caller, turning a
    successful cancellation into an apparent request failure over a
    notification provider's availability — precisely what RFC v1.0 §15a
    /PRD FR55 forbid. A broker outage degrades (logged) rather than
    raises; a PROVIDER (SMTP) outage never even reaches this function —
    it surfaces inside the worker, where send_notification_task's own
    retry/record logic (not this wrapper) is what handles it.
    """
    try:
        send_notification_task.delay(
            notification_id,
            notification_type,
            recipient_user_id,
            recipient_email,
            subject,
            body,
            context,
            request_id,
        )
    except Exception:
        logger.error(
            "notification_dispatch_failed_broker_unavailable",
            extra={"request_id": request_id, "notification_type": notification_type},
            exc_info=True,
        )


@shared_task(name="kairos.core.tasks.run_reconciliation_task")
def run_reconciliation_task() -> dict[str, Any]:
    return check_reconciliation()


@shared_task(name="kairos.core.tasks.run_schema_assertion_task")
def run_schema_assertion_task() -> dict[str, Any]:
    return check_schema_assertion()


@shared_task(name="kairos.core.tasks.evaluate_alerts_task")
def evaluate_alerts_task() -> dict[str, Any]:
    """Implementation Plan Phase 21 — the alert-evaluation heartbeat
    (`ALERT_EVALUATION_INTERVAL_SECONDS`). Also prunes expired
    `RequestMetric` rows on the same tick rather than a second Beat entry
    (see `kairos.core.metrics.prune_old_request_metrics`'s own docstring).
    """
    from kairos.core.alerting import evaluate_alerts
    from kairos.core.metrics import prune_old_request_metrics

    fired = evaluate_alerts()
    pruned = prune_old_request_metrics()
    return {"alerts_fired": [e.alert_key for e in fired], "metrics_pruned": pruned}


@shared_task(
    bind=True,
    name="kairos.core.tasks.send_alert_email_task",
    # Mirrors send_notification_task's own retry configuration exactly —
    # an alert email is delivered through the identical EMAIL_BACKEND
    # mechanism and deserves the identical "recorded and retried"
    # treatment PRD FR55 requires of a user-facing notification.
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=ALERT_EMAIL_RETRY_BACKOFF_MAX_SECONDS,
    retry_jitter=True,
    max_retries=ALERT_EMAIL_MAX_RETRIES,
)
def send_alert_email_task(self: Any, alert_event_id: str) -> None:
    # Deferred import — same circular-import shape as send_notification_
    # task's own deferred import of _execute_notification_delivery.
    from kairos.core.alerting import _execute_alert_email_delivery

    _execute_alert_email_delivery(alert_event_id)


@shared_task(name="kairos.core.tasks.cleanup_idempotency_keys_task")
def cleanup_idempotency_keys_task() -> dict[str, int]:
    """The schedule `kairos.core.management.commands.
    cleanup_idempotency_keys`'s own docstring deferred to "Phase 21's
    job" since Phase 5 — calls the SAME `cleanup_expired_idempotency_keys`
    function that command now calls too.
    """
    from kairos.core.idempotency import cleanup_expired_idempotency_keys

    return cleanup_expired_idempotency_keys()
