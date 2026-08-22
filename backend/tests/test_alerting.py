"""kairos.core.alerting (Implementation Plan Phase 21; Rollout v1.0
§6.1). RECON-07: "every alert deliberately fired once and confirmed to
reach its target... a release gate, not a recommendation." Each of the
seven `test_*_alert_fires_and_reaches_its_target` tests below is that
evidence — seeds the exact condition Rollout v1.0 §6.1's table names,
calls `evaluate_alerts()` (the same function `evaluate_alerts_task` calls
on schedule), and asserts BOTH an `AlertEvent` row with the correct
`alert_key`/`severity` AND a real email landed in `mail.outbox` (the
locmem `EMAIL_BACKEND`, `kairos.settings.test`) — "reaching its target"
made into something CI actually checks, not a manual one-off.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.db import connection
from django.utils import timezone

from kairos.bookings.models import Booking, BookingStatus
from kairos.core.alerting import evaluate_alerts, fire_or_resolve
from kairos.core.constants import (
    HOLD_REAPER_INTERVAL_SECONDS,
    OFFER_CASCADE_STUCK_HOLD_GRACE_SECONDS,
    SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS,
    SERIES_MATERIALIZATION_STALE_THRESHOLD_SECONDS,
)
from kairos.core.models import (
    AlertEvent,
    AlertKey,
    AlertSeverity,
    AuditActorType,
    AuditLog,
    SystemCheckRun,
)
from kairos.core.reconciliation import check_reconciliation
from kairos.core.schema_assertion import check_schema_assertion
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

DROP_CONSTRAINT_SQL = "ALTER TABLE booking DROP CONSTRAINT no_overlapping_bookings;"
RESTORE_CONSTRAINT_SQL = """
ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        resource_id WITH =,
        time_range  WITH &&
    )
    WHERE (status IN ('confirmed', 'held'));
"""
NARROW_CONSTRAINT_SQL = """
ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        resource_id WITH =,
        time_range  WITH &&
    )
    WHERE (status IN ('confirmed'));
"""


@pytest.fixture
def user(db: None) -> AppUser:
    return AppUser.objects.create(email="alert-test-user@example.com", display_name="U")


@pytest.fixture
def resource(db: None, user: AppUser) -> Resource:
    return Resource.objects.create(
        name="Alert Test Room",
        timezone="UTC",
        bookable_start_time="00:00:00",
        bookable_end_time="23:59:00",
        created_by=user,
    )


# --------------------------------------------------------------------
# fire_or_resolve mechanics: edge-triggered, not level-triggered
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_fire_or_resolve_creates_exactly_one_open_event_while_active() -> None:
    first = fire_or_resolve(alert_key=AlertKey.HOLD_REAPER, active=True, message="m", context={})
    assert first is not None
    assert first.severity == AlertSeverity.SEV_2
    assert first.resolved_at is None

    # Re-evaluating while still active is a no-op — no second row, no
    # second email — this is the mechanism RECON-07's "at least once" is
    # measured against, not a flood of duplicates.
    second = fire_or_resolve(alert_key=AlertKey.HOLD_REAPER, active=True, message="m", context={})
    assert second is None
    assert (
        AlertEvent.objects.filter(alert_key=AlertKey.HOLD_REAPER, resolved_at__isnull=True).count()
        == 1
    )


@pytest.mark.django_db
def test_fire_or_resolve_resolves_when_condition_clears() -> None:
    fire_or_resolve(alert_key=AlertKey.HOLD_REAPER, active=True, message="m", context={})
    resolved = fire_or_resolve(
        alert_key=AlertKey.HOLD_REAPER, active=False, message="m", context={}
    )
    assert resolved is None  # resolve doesn't return the event, only a fresh firing does
    event = AlertEvent.objects.get(alert_key=AlertKey.HOLD_REAPER)
    assert event.resolved_at is not None

    # The condition firing AGAIN opens a brand-new row rather than
    # reusing the resolved one — full history survives.
    refired = fire_or_resolve(alert_key=AlertKey.HOLD_REAPER, active=True, message="m", context={})
    assert refired is not None
    assert refired.id != event.id
    assert AlertEvent.objects.filter(alert_key=AlertKey.HOLD_REAPER).count() == 2


@pytest.mark.django_db
def test_a_fired_alert_sends_a_real_email_to_the_configured_recipient(settings) -> None:
    settings.ALERT_RECIPIENT_EMAIL = "oncall-test@example.com"
    event = fire_or_resolve(
        alert_key=AlertKey.HOLD_REAPER, active=True, message="test message", context={}
    )
    assert event is not None
    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert sent.to == ["oncall-test@example.com"]
    assert "hold_reaper" in sent.subject
    assert "SEV_2".lower() in sent.subject.lower() or "sev_2" in sent.subject.lower()

    event.refresh_from_db()
    assert event.email_status == AlertEvent.DeliveryStatus.SENT
    assert event.email_attempts == 1
    assert event.email_sent_at is not None


# --------------------------------------------------------------------
# RECON-07 — every alert fired by deliberate injection, one at a time
# --------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_schema_assertion_fail_alert_fires_and_reaches_its_target() -> None:
    with connection.cursor() as cur:
        cur.execute(DROP_CONSTRAINT_SQL)
    try:
        check_schema_assertion()  # writes a FAIL system_check_run
        fired = evaluate_alerts()
    finally:
        with connection.cursor() as cur:
            cur.execute(RESTORE_CONSTRAINT_SQL)

    fired_keys = [e.alert_key for e in fired]
    assert AlertKey.SCHEMA_ASSERTION in fired_keys
    event = AlertEvent.objects.get(alert_key=AlertKey.SCHEMA_ASSERTION, resolved_at__isnull=True)
    assert event.severity == AlertSeverity.SEV_1
    assert any("schema_assertion" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_schema_assertion_stale_alert_fires_on_no_run_at_all() -> None:
    # No SystemCheckRun has ever been written for schema_assertion in
    # this test's isolated transaction — absence itself is the signal.
    fired = evaluate_alerts()
    fired_keys = [e.alert_key for e in fired]
    assert AlertKey.SCHEMA_ASSERTION in fired_keys
    event = AlertEvent.objects.get(alert_key=AlertKey.SCHEMA_ASSERTION, resolved_at__isnull=True)
    assert event.context["stale"] is True
    assert event.context["failing"] is False


@pytest.mark.django_db
def test_schema_assertion_stale_alert_fires_past_2x_interval_and_resolves_on_fresh_run() -> None:
    now = timezone.now()
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.SCHEMA_ASSERTION,
        status=SystemCheckRun.Status.PASS,
        findings={},
    )
    SystemCheckRun.objects.filter(check_name=SystemCheckRun.CheckName.SCHEMA_ASSERTION).update(
        run_at=now - timedelta(seconds=SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS + 1)
    )
    fired = evaluate_alerts(now=now)
    assert AlertKey.SCHEMA_ASSERTION in [e.alert_key for e in fired]

    # A fresh run resolves it.
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.SCHEMA_ASSERTION,
        status=SystemCheckRun.Status.PASS,
        findings={},
    )
    evaluate_alerts(now=now)
    event = AlertEvent.objects.get(alert_key=AlertKey.SCHEMA_ASSERTION)
    assert event.resolved_at is not None


@pytest.mark.django_db(transaction=True)
def test_reconciliation_overlap_alert_fires_and_reaches_its_target(
    user: AppUser, resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    overlap_start = start + timedelta(minutes=30)
    overlap_end = overlap_start + timedelta(hours=1)

    with connection.cursor() as cur:
        cur.execute(DROP_CONSTRAINT_SQL)
    try:
        Booking.objects.create(resource=resource, user=user, time_range=(start, end))
        Booking.objects.create(
            resource=resource, user=user, time_range=(overlap_start, overlap_end)
        )
        check_reconciliation()  # writes a FAIL system_check_run
        fired = evaluate_alerts()
    finally:
        Booking.objects.filter(resource=resource).delete()
        with connection.cursor() as cur:
            cur.execute(RESTORE_CONSTRAINT_SQL)

    assert AlertKey.RECONCILIATION in [e.alert_key for e in fired]
    event = AlertEvent.objects.get(alert_key=AlertKey.RECONCILIATION, resolved_at__isnull=True)
    assert event.severity == AlertSeverity.SEV_1
    assert event.context["overlaps_found"] == 1
    assert any("reconciliation" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_hold_reaper_stale_alert_fires_within_90s_threshold_and_reaches_its_target() -> None:
    now = timezone.now()
    threshold = HOLD_REAPER_INTERVAL_SECONDS * 3  # 90s by default
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.HOLD_REAPER,
        status=SystemCheckRun.Status.PASS,
        findings={},
    )
    SystemCheckRun.objects.filter(check_name=SystemCheckRun.CheckName.HOLD_REAPER).update(
        run_at=now - timedelta(seconds=threshold + 1)
    )
    fired = evaluate_alerts(now=now)
    assert AlertKey.HOLD_REAPER in [e.alert_key for e in fired]
    event = AlertEvent.objects.get(alert_key=AlertKey.HOLD_REAPER, resolved_at__isnull=True)
    assert event.severity == AlertSeverity.SEV_2
    assert any("hold_reaper" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_offer_cascade_stuck_holds_alert_fires_not_staleness(
    user: AppUser, resource: Resource
) -> None:
    """Implementation Plan Phase 21's own clarification #1: the PRIMARY
    trigger is a live stuck-holds count, not time-since-last-run — proven
    here by writing a FRESH offer_cascade heartbeat (so staleness alone
    would NOT fire) while a hold sits well past its expiry plus grace.
    """
    now = timezone.now()
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.OFFER_CASCADE,
        status=SystemCheckRun.Status.PASS,
        findings={"offer_created": False, "candidates_tried": 0},
    )  # fresh — not stale

    stuck_expiry = now - timedelta(seconds=OFFER_CASCADE_STUCK_HOLD_GRACE_SECONDS + 60)
    Booking.objects.create(
        resource=resource,
        user=user,
        time_range=(now - timedelta(hours=2), now - timedelta(hours=1, minutes=45)),
        status=BookingStatus.HELD,
        expires_at=stuck_expiry,
    )

    fired = evaluate_alerts(now=now)
    assert AlertKey.OFFER_CASCADE in [e.alert_key for e in fired]
    event = AlertEvent.objects.get(alert_key=AlertKey.OFFER_CASCADE, resolved_at__isnull=True)
    assert event.severity == AlertSeverity.SEV_2
    assert event.context["stuck_held_bookings"] == 1
    assert any("offer_cascade" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_offer_cascade_does_not_false_alarm_during_an_ordinary_quiet_period() -> None:
    """The false-alarm this phase's clarification exists to avoid: no
    cascade traffic at all (no heartbeat, no stuck holds) must NOT fire
    offer_cascade — a naive "no run in 90s" check would have fired here.
    """
    now = timezone.now()
    evaluate_alerts(now=now)
    assert not AlertEvent.objects.filter(
        alert_key=AlertKey.OFFER_CASCADE, resolved_at__isnull=True
    ).exists()


@pytest.mark.django_db
def test_series_materialization_stale_alert_fires_and_reaches_its_target() -> None:
    now = timezone.now()
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.SERIES_MATERIALIZATION,
        status=SystemCheckRun.Status.PASS,
        findings={},
    )
    SystemCheckRun.objects.filter(
        check_name=SystemCheckRun.CheckName.SERIES_MATERIALIZATION
    ).update(run_at=now - timedelta(seconds=SERIES_MATERIALIZATION_STALE_THRESHOLD_SECONDS + 1))

    fired = evaluate_alerts(now=now)
    assert AlertKey.SERIES_MATERIALIZATION in [e.alert_key for e in fired]
    event = AlertEvent.objects.get(
        alert_key=AlertKey.SERIES_MATERIALIZATION, resolved_at__isnull=True
    )
    assert event.severity == AlertSeverity.SEV_2
    assert any("series_materialization" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_tzdata_rematerialization_fail_alert_fires_and_reaches_its_target() -> None:
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.TZDATA_REMATERIALIZATION,
        status=SystemCheckRun.Status.FAIL,
        findings={"conflicts": [{"series_id": str(uuid.uuid4())}]},
    )
    fired = evaluate_alerts()
    assert AlertKey.TZDATA_REMATERIALIZATION in [e.alert_key for e in fired]
    event = AlertEvent.objects.get(
        alert_key=AlertKey.TZDATA_REMATERIALIZATION, resolved_at__isnull=True
    )
    assert event.severity == AlertSeverity.SEV_2
    assert event.context["failing"] is True
    assert any("tzdata_rematerialization" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_audit_actor_unknown_alert_fires_sev3_and_reaches_its_target() -> None:
    # No `resource`/`user` fixture here deliberately: a plain
    # Resource.objects.create() bypasses apply_write_path_session_
    # settings and would ITSELF produce a second actor_type='unknown'
    # audit_log row via the trigger — exactly what this alert exists to
    # catch, but not what THIS test is isolating. One explicit row only.
    AuditLog.objects.create(
        entity_type="booking",
        entity_id=uuid.uuid4(),
        action="insert",
        actor_type=AuditActorType.UNKNOWN,
    )
    fired = evaluate_alerts()
    assert AlertKey.AUDIT_ACTOR_UNKNOWN in [e.alert_key for e in fired]
    event = AlertEvent.objects.get(alert_key=AlertKey.AUDIT_ACTOR_UNKNOWN, resolved_at__isnull=True)
    assert event.severity == AlertSeverity.SEV_3
    assert event.context["count"] == 1
    assert any("audit_actor_unknown" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_audit_actor_unknown_resolves_once_no_recent_row_remains() -> None:
    now = timezone.now()
    old = AuditLog.objects.create(
        entity_type="booking",
        entity_id=uuid.uuid4(),
        action="insert",
        actor_type=AuditActorType.UNKNOWN,
    )
    AuditLog.objects.filter(id=old.id).update(
        occurred_at=now - timedelta(seconds=999999)  # long past the lookback window
    )
    evaluate_alerts(now=now)
    assert not AlertEvent.objects.filter(
        alert_key=AlertKey.AUDIT_ACTOR_UNKNOWN, resolved_at__isnull=True
    ).exists()


@pytest.mark.django_db
def test_gist_write_throughput_alert_fires_sev3_and_reaches_its_target() -> None:
    """Implementation Plan Phase 29 — Rollout v1.0 §6's "GiST write
    throughput on booking" row, given a real writer (and a real,
    CONC-06-derived threshold) for the first time. Reads the SAME live
    `p95_duration_ms(metric_type=BOOKING_WRITE)` the admin dashboard
    already computes (Phase 21) — no second aggregation mechanism.
    """
    from kairos.core.constants import BOOKING_WRITE_P95_ALERT_THRESHOLD_MS
    from kairos.core.models import RequestMetric, RequestMetricType

    RequestMetric.objects.create(
        metric_type=RequestMetricType.BOOKING_WRITE,
        method="POST",
        path="/api/v1/bookings",
        status_code=201,
        duration_ms=BOOKING_WRITE_P95_ALERT_THRESHOLD_MS + 500,
    )
    fired = evaluate_alerts()
    assert AlertKey.GIST_WRITE_THROUGHPUT in [e.alert_key for e in fired]
    event = AlertEvent.objects.get(
        alert_key=AlertKey.GIST_WRITE_THROUGHPUT, resolved_at__isnull=True
    )
    assert event.severity == AlertSeverity.SEV_3
    assert event.context["threshold_ms"] == BOOKING_WRITE_P95_ALERT_THRESHOLD_MS
    assert any("gist_write_throughput" in m.subject for m in mail.outbox)


@pytest.mark.django_db
def test_gist_write_throughput_does_not_false_alarm_under_the_threshold() -> None:
    """A booking-write P95 comfortably under threshold — PERF-01's own
    steady-load target, in spirit — must never fire this alert. Proven
    directly, the same "prove the negative, not just the positive"
    discipline `test_offer_cascade_does_not_false_alarm_during_an_
    ordinary_quiet_period` already established for this module.
    """
    from kairos.core.constants import BOOKING_WRITE_P95_ALERT_THRESHOLD_MS
    from kairos.core.models import RequestMetric, RequestMetricType

    RequestMetric.objects.create(
        metric_type=RequestMetricType.BOOKING_WRITE,
        method="POST",
        path="/api/v1/bookings",
        status_code=201,
        duration_ms=BOOKING_WRITE_P95_ALERT_THRESHOLD_MS - 500,
    )
    evaluate_alerts()
    assert not AlertEvent.objects.filter(
        alert_key=AlertKey.GIST_WRITE_THROUGHPUT, resolved_at__isnull=True
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_recon05_narrowed_predicate_also_fires_schema_assertion_sev1() -> None:
    """RECON-05's second case (RUNBOOK-01 cause #1: the constraint still
    exists, but its predicate no longer covers 'held') must alert exactly
    like a fully-dropped constraint — a bare existence check would miss
    it, but check_schema_assertion's own full-definition comparison
    catches it, and this phase's alerting must too.
    """
    with connection.cursor() as cur:
        cur.execute(DROP_CONSTRAINT_SQL)
        cur.execute(NARROW_CONSTRAINT_SQL)
    try:
        findings = check_schema_assertion()
        assert findings["covers_held"] is False
        fired = evaluate_alerts()
    finally:
        with connection.cursor() as cur:
            cur.execute(DROP_CONSTRAINT_SQL)
            cur.execute(RESTORE_CONSTRAINT_SQL)

    assert AlertKey.SCHEMA_ASSERTION in [e.alert_key for e in fired]
