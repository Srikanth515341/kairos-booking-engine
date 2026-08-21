"""Alert evaluation and delivery (Implementation Plan Phase 21; Rollout
v1.0 §6.1, RUNBOOK-02/06/07/09). One periodic function, `evaluate_alerts`
(scheduled via `kairos.core.tasks.evaluate_alerts_task`), reads the SAME
data every check already writes (`system_check_run`, `audit_log`) and
decides whether each of Rollout's six named alert conditions — plus
`audit_actor_unknown` (SEV-3, RFC v1.0 §12) — is currently active. No new
monitoring pipeline, no new dependency: this is a read over existing
tables, not a second copy of any check's own logic.

Delivery reuses `kairos.core.notifications.NotificationService` (Phase
18) directly — the SAME `EMAIL_BACKEND`-based send, console in dev,
`locmem` in test, real SMTP in prod — to a fixed operator mailbox
(`settings.ALERT_RECIPIENT_EMAIL`) rather than an `AppUser`, which is why
alert delivery state lives on `AlertEvent` itself (see that model's own
docstring) instead of `NotificationLog`.

Edge-triggered, not level-triggered: `fire_or_resolve` only creates a new
`AlertEvent` (and sends exactly one email) on the transition from "not
firing" to "firing" for a given `alert_key` — `uniq_open_alert_per_key`
makes a second OPEN row for the same key structurally impossible, so
re-evaluating an already-firing condition on the next tick is a no-op,
not a second email every `ALERT_EVALUATION_INTERVAL_SECONDS`. This is
what keeps RECON-07 ("every alert deliberately fired once... confirmed to
reach its target") meaningful evidence of the CONDITION firing, not proof
that a poller sends duplicate mail.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import F
from django.utils import timezone as django_timezone

from kairos.core.constants import (
    AUDIT_ACTOR_UNKNOWN_LOOKBACK_SECONDS,
    SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS,
)
from kairos.core.models import (
    AlertDeliveryStatus,
    AlertEvent,
    AlertKey,
    AlertSeverity,
    AuditActorType,
    AuditLog,
    SystemCheckRun,
)
from kairos.core.notifications import NotificationService
from kairos.core.reconciliation import RECONCILIATION_FAILURE_MESSAGE
from kairos.core.schema_assertion import (
    SCHEMA_ASSERTION_FAILURE_MESSAGE,
    schema_assertion_heartbeat_is_stale,
)

logger = logging.getLogger(__name__)

ALERT_SEVERITY: dict[str, str] = {
    AlertKey.SCHEMA_ASSERTION: AlertSeverity.SEV_1,
    AlertKey.RECONCILIATION: AlertSeverity.SEV_1,
    AlertKey.HOLD_REAPER: AlertSeverity.SEV_2,
    AlertKey.OFFER_CASCADE: AlertSeverity.SEV_2,
    AlertKey.SERIES_MATERIALIZATION: AlertSeverity.SEV_2,
    AlertKey.TZDATA_REMATERIALIZATION: AlertSeverity.SEV_2,
    AlertKey.AUDIT_ACTOR_UNKNOWN: AlertSeverity.SEV_3,
}


def _latest_check(check_name: str) -> SystemCheckRun | None:
    return SystemCheckRun.objects.filter(check_name=check_name).order_by("-run_at").first()


def fire_or_resolve(
    *, alert_key: str, active: bool, message: str, context: dict[str, Any]
) -> AlertEvent | None:
    """Returns the newly-created `AlertEvent` on a fresh firing, `None`
    otherwise (already firing, or not active). Callers/tests use the
    return value to assert "this evaluation actually fired a NEW alert,"
    not merely "the condition was active."
    """
    open_event = AlertEvent.objects.filter(alert_key=alert_key, resolved_at__isnull=True).first()

    if not active:
        if open_event is not None:
            AlertEvent.objects.filter(id=open_event.id).update(resolved_at=django_timezone.now())
            logger.info("alert_resolved", extra={"alert_key": alert_key})
        return None

    if open_event is not None:
        return None  # already firing — edge-triggered, no repeat dispatch

    event = AlertEvent.objects.create(
        alert_key=alert_key,
        severity=ALERT_SEVERITY[alert_key],
        message=message,
        context=context,
    )
    logger.critical(
        "alert_fired",
        extra={"alert_key": alert_key, "severity": event.severity, "alert_id": str(event.id)},
    )
    _dispatch_alert_email(event)
    return event


def _dispatch_alert_email(event: AlertEvent) -> None:
    """Mirrors `kairos.waitlist.tasks.dispatch_cascade` / `kairos.core.
    tasks.dispatch_notification` exactly: a broker outage must degrade
    (logged) rather than raise — an alert that fails to SEND because
    Redis is down must not itself crash the alert-evaluation task that
    would otherwise keep firing on every subsequent tick.
    """
    from kairos.core.tasks import send_alert_email_task

    try:
        send_alert_email_task.delay(str(event.id))
    except Exception:
        logger.error(
            "alert_dispatch_failed_broker_unavailable",
            extra={"alert_id": str(event.id), "alert_key": event.alert_key},
            exc_info=True,
        )


def _execute_alert_email_delivery(alert_event_id: str) -> None:
    """One call per Celery attempt — mirrors `kairos.core.notifications.
    _execute_notification_delivery`'s own shape exactly (attempts
    accumulate via `F()`, status reflects the latest outcome, a failure
    re-raises so `send_alert_email_task`'s `autoretry_for` schedules the
    next attempt).
    """
    event = AlertEvent.objects.get(id=alert_event_id)
    AlertEvent.objects.filter(id=event.id).update(email_attempts=F("email_attempts") + 1)

    subject = f"[{event.severity.upper()}] {event.alert_key}: alert fired"
    body = (
        f"{event.message}\n\n"
        f"Context: {json.dumps(event.context, indent=2, default=str)}\n\n"
        f"Fired at: {event.fired_at.isoformat()}"
    )
    try:
        NotificationService.send(
            recipient_email=settings.ALERT_RECIPIENT_EMAIL, subject=subject, body=body
        )
    except Exception as exc:
        AlertEvent.objects.filter(id=event.id).update(
            email_status=AlertDeliveryStatus.FAILED, email_last_error=str(exc)
        )
        logger.warning(
            "alert_email_delivery_failed", extra={"alert_id": alert_event_id}, exc_info=True
        )
        raise

    AlertEvent.objects.filter(id=event.id).update(
        email_status=AlertDeliveryStatus.SENT, email_sent_at=django_timezone.now()
    )
    logger.info(
        "alert_email_delivered",
        extra={"alert_id": alert_event_id, "alert_key": event.alert_key},
    )


def evaluate_alerts(*, now: datetime | None = None) -> list[AlertEvent]:
    """Reads all seven signals and fires/resolves each independently.
    Deferred imports throughout for `kairos.waitlist.services`/`kairos.
    bookings.tasks` functions — those modules don't import this one, so
    there's no genuine cycle, but keeping every cross-app read deferred
    here (rather than at module top) matches this project's own
    established discipline for any import crossing an app boundary in a
    module Celery's `autodiscover_tasks()` or the app registry might load
    early.
    """
    now = now or django_timezone.now()
    fired: list[AlertEvent] = []

    # --- schema_assertion (SEV-1): fails, or no run in 2x interval -----
    latest = _latest_check(SystemCheckRun.CheckName.SCHEMA_ASSERTION)
    failing = latest is not None and latest.status == SystemCheckRun.Status.FAIL
    stale = schema_assertion_heartbeat_is_stale(now=now)
    event = fire_or_resolve(
        alert_key=AlertKey.SCHEMA_ASSERTION,
        active=failing or stale,
        message=(
            SCHEMA_ASSERTION_FAILURE_MESSAGE
            if failing
            else (
                f"schema_assertion has not had a successful run in "
                f"{SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS}s."
            )
        ),
        context={
            "failing": failing,
            "stale": stale,
            "findings": latest.findings if latest else None,
        },
    )
    if event is not None:
        fired.append(event)

    # --- reconciliation (SEV-1): overlaps_found > 0, any run -----------
    latest = _latest_check(SystemCheckRun.CheckName.RECONCILIATION)
    failing = latest is not None and latest.status == SystemCheckRun.Status.FAIL
    event = fire_or_resolve(
        alert_key=AlertKey.RECONCILIATION,
        active=failing,
        message=RECONCILIATION_FAILURE_MESSAGE,
        context={
            "overlaps_found": (latest.findings.get("overlaps_found") if latest else None),
        },
    )
    if event is not None:
        fired.append(event)

    # --- hold_reaper (SEV-2): no successful run in 90s ------------------
    from kairos.waitlist.services import hold_reaper_heartbeat_is_stale

    stale = hold_reaper_heartbeat_is_stale(now=now)
    event = fire_or_resolve(
        alert_key=AlertKey.HOLD_REAPER,
        active=stale,
        message="hold_reaper has not had a successful run in 90s.",
        context={"stale": stale},
    )
    if event is not None:
        fired.append(event)

    # --- offer_cascade (SEV-2): stuck-holds count, NOT time-since-run --
    # Implementation Plan Phase 21's own clarification #1 (CLAUDE.md Key
    # Technical Decisions): the naive "no run in 90s" false-alarms during
    # ordinary quiet periods for this event-triggered job, so the PRIMARY
    # trigger is Rollout v1.0 §6.1's own second condition instead.
    from kairos.waitlist.services import count_stuck_held_bookings

    stuck = count_stuck_held_bookings(now=now)
    event = fire_or_resolve(
        alert_key=AlertKey.OFFER_CASCADE,
        active=stuck["stuck_held_bookings"] > 0,
        message=(
            "One or more holds are sitting past expires_at plus grace with no "
            "corresponding offer cascade — both reclamation mechanisms "
            "(cleanup-on-write, the reaper) appear to have failed to reach them."
        ),
        context=stuck,
    )
    if event is not None:
        fired.append(event)

    # --- series_materialization (SEV-2): no run in 2x interval ---------
    from kairos.bookings.tasks import series_materialization_heartbeat_is_stale

    stale = series_materialization_heartbeat_is_stale(now=now)
    event = fire_or_resolve(
        alert_key=AlertKey.SERIES_MATERIALIZATION,
        active=stale,
        message="series_materialization has not had a successful run in 2x its interval.",
        context={"stale": stale},
    )
    if event is not None:
        fired.append(event)

    # --- tzdata_rematerialization (SEV-2): fails, or stale --------------
    from kairos.bookings.tasks import tzdata_rematerialization_heartbeat_is_stale

    latest = _latest_check(SystemCheckRun.CheckName.TZDATA_REMATERIALIZATION)
    failing = latest is not None and latest.status == SystemCheckRun.Status.FAIL
    stale = tzdata_rematerialization_heartbeat_is_stale(now=now)
    event = fire_or_resolve(
        alert_key=AlertKey.TZDATA_REMATERIALIZATION,
        active=failing or stale,
        message=(
            "tzdata_rematerialization failed on its last run, or has not run since "
            "the installed tzdata version changed."
        ),
        context={
            "failing": failing,
            "stale": stale,
            "findings": latest.findings if latest else None,
        },
    )
    if event is not None:
        fired.append(event)

    # --- audit_actor_unknown (SEV-3): any recent row --------------------
    lookback = now - timedelta(seconds=AUDIT_ACTOR_UNKNOWN_LOOKBACK_SECONDS)
    unknown_qs = AuditLog.objects.filter(
        actor_type=AuditActorType.UNKNOWN, occurred_at__gte=lookback
    )
    count = unknown_qs.count()
    event = fire_or_resolve(
        alert_key=AlertKey.AUDIT_ACTOR_UNKNOWN,
        active=count > 0,
        message=(
            "One or more audit_log rows were written with actor_type='unknown' — a "
            "write reached the database with no app.actor_type session variable set "
            "(kairos.core.models.AuditActorType docstring: 'a write arriving with no "
            "actor set is a bug'). Find the writer that bypassed apply_write_path_"
            "session_settings."
        ),
        context={
            "count": count,
            "sample_audit_log_ids": [str(i) for i in unknown_qs.values_list("id", flat=True)[:20]],
        },
    )
    if event is not None:
        fired.append(event)

    return fired
