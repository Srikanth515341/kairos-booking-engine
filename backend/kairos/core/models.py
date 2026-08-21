"""The transaction boundary (RFC v1.0 §11.2) — the entire idempotency
mechanism lives in how this table is written to, not in this schema alone.
Lives in `core`, not `bookings`, because every future write path (edit,
cancel, waitlist join, offer confirm, admin deactivate — Phases 7, 14, 16,
19) needs the identical mechanism, not a booking-specific one.
"""

from __future__ import annotations

import uuid

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.db.models.functions import Now

from kairos.identity.models import AppUser


class IdempotencyKeyStatus(models.TextChoices):
    # 'in_progress' is written in the SAME transaction as the protected
    # operation, before the outcome is known — this is what makes
    # concurrent replay (PRD FR36) implementable: a retry arriving
    # mid-flight blocks on this row's primary key rather than executing a
    # second time (RFC v1.0 §11.3).
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed"


class IdempotencyKey(models.Model):
    # Scoped (user_id, key), never key alone: two users independently
    # generating the same UUID must not collide, and a key presented by a
    # different principal must be treated as unseen — this also closes the
    # key-harvesting threat in RFC v1.0 §8.2.
    pk = models.CompositePrimaryKey("user_id", "key")
    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, db_column="user_id")
    key = models.UUIDField()

    endpoint = models.TextField()  # e.g. 'POST /api/v1/bookings'
    request_body_hash = models.TextField()  # sha256 of the normalized request body

    status = models.TextField(choices=IdempotencyKeyStatus.choices)
    # Response columns are nullable — you cannot write a response you don't
    # have yet (Spec v1.0 §3).
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True, encoder=DjangoJSONEncoder)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "idempotency_key"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=list(IdempotencyKeyStatus.values)),
                name="idempotency_key_status_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status=IdempotencyKeyStatus.COMPLETED,
                        response_status__isnull=False,
                        completed_at__isnull=False,
                    )
                    | models.Q(
                        status=IdempotencyKeyStatus.IN_PROGRESS, response_status__isnull=True
                    )
                ),
                name="completed_has_response",
            ),
        ]
        indexes = [
            # 24h cleanup job (Spec v1.0 §7; kairos.core.management.commands.
            # cleanup_idempotency_keys).
            models.Index(fields=["created_at"], name="idx_idempotency_created"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.key} ({self.status})"


class AuditActorType(models.TextChoices):
    # A write arriving with no actor set is a bug, not a valid state — the
    # trigger records it as UNKNOWN rather than failing the write (RFC
    # v1.0 §12), and the reconciliation job (Phase 21) alerts on any such
    # row. It is a value this system defends AGAINST, not a normal choice
    # application code ever passes deliberately.
    USER = "user", "User"
    ADMIN = "admin", "Admin"
    SYSTEM = "system", "System"
    UNKNOWN = "unknown", "Unknown"


class AuditLog(models.Model):
    """Written EXCLUSIVELY by the `write_audit_log()` trigger (Spec v1.0
    §3; RFC v1.0 §12) — never by application code. That is the entire
    argument for a trigger over an application-level audit call: a future
    bulk-import script bypassing the service layer entirely still produces
    a row here, because the trigger fires on the table write itself, not
    on any particular code path calling it (AUD-02).

    No foreign keys to `app_user`/`booking`/etc — deliberate, matching
    Spec v1.0 §3's DDL exactly. `entity_id` is polymorphic (booking,
    resource, resource_admin, and later waitlist_entry/waitlist_offer all
    share this one table), so no single FK target exists; `actor_id` has
    no FK either, since a `RESTRICT`-on-delete FK to `app_user` would let
    a booking's own audit history block that user's eventual offboarding
    (Phase 19) — the audit trail must survive independently of whether the
    referenced principal still exists.
    """

    ActorType = AuditActorType

    id = models.BigAutoField(primary_key=True)
    entity_type = models.TextField()  # TG_TABLE_NAME — 'booking' | 'resource' | 'resource_admin'
    entity_id = models.UUIDField()
    action = models.TextField()  # 'insert' | 'update' | 'delete'
    actor_id = models.UUIDField(null=True, blank=True)
    actor_type = models.TextField(choices=AuditActorType.choices, default=AuditActorType.UNKNOWN)
    # REQUIRED for administrative overrides (PRD FR40) — enforced at the API
    # layer (BookingCancelSerializer), not by a DB-level NOT NULL here: a
    # self-cancel or a create/edit legitimately has no reason at all.
    reason = models.TextField(null=True, blank=True)  # noqa: DJ001
    request_id = models.TextField(null=True, blank=True)  # noqa: DJ001
    before_state = models.JSONField(null=True, blank=True)
    after_state = models.JSONField(null=True, blank=True)
    # `db_default`, NOT `auto_now_add` — `auto_now_add` is Python-side only
    # (Django sets the value before INSERT), so it does nothing for a row
    # the ORM never writes. Every audit_log row is written by the
    # write_audit_log() trigger via raw SQL that never mentions
    # `occurred_at` at all (matching Spec v1.0 §3's trigger body exactly),
    # relying entirely on a genuine column-level `DEFAULT now()` — which is
    # exactly what `db_default` creates and `auto_now_add` does not.
    # Caught empirically: a raw SQL insert into `resource` failed with
    # "null value in column occurred_at violates not-null constraint"
    # until this was fixed.
    occurred_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "audit_log"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(actor_type__in=list(AuditActorType.values)),
                name="audit_log_actor_type_check",
            ),
        ]
        indexes = [
            models.Index(
                fields=["entity_type", "entity_id", "-occurred_at"], name="idx_audit_entity"
            ),
            models.Index(fields=["actor_id", "-occurred_at"], name="idx_audit_actor"),
            models.Index(fields=["request_id"], name="idx_audit_request"),
        ]

    def __str__(self) -> str:
        return f"{self.entity_type}:{self.entity_id} {self.action} @ {self.occurred_at}"


class SystemCheckName(models.TextChoices):
    # The full six-name set Spec v1.0 §3 defines up front, even though
    # only the last two get a real writer in Phase 13 — RECONCILIATION/
    # SCHEMA_ASSERTION (Phase 20), HOLD_REAPER (Phase 17), OFFER_CASCADE
    # (Phase 16) exist here as CHECK-constraint values a future phase's
    # migration doesn't need to widen, matching how Spec's DDL states the
    # full CHECK (...) list once rather than growing it per phase.
    RECONCILIATION = "reconciliation", "Reconciliation"
    SCHEMA_ASSERTION = "schema_assertion", "Schema assertion"
    HOLD_REAPER = "hold_reaper", "Hold reaper"
    OFFER_CASCADE = "offer_cascade", "Offer cascade"
    SERIES_MATERIALIZATION = "series_materialization", "Series materialization"
    TZDATA_REMATERIALIZATION = "tzdata_rematerialization", "Tzdata re-materialization"


class SystemCheckStatus(models.TextChoices):
    PASS = "pass", "Pass"
    FAIL = "fail", "Fail"


class SystemCheckRun(models.Model):
    """Results surface of the correctness monitors (PRD M2/M3; RFC v1.0
    §14) — Phase 13 is the first WRITER (`series_materialization`,
    `tzdata_rematerialization`), not the phase that finishes this table.
    `status='fail'` means THIS run found something (a conflict, a stale
    version) — it has no relationship to whether a CI build passes; that's
    `test_schema_assertion.py`'s job (RECON-05), entirely separate.
    """

    CheckName = SystemCheckName
    Status = SystemCheckStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    check_name = models.TextField(choices=SystemCheckName.choices)
    run_at = models.DateTimeField(db_default=Now())
    status = models.TextField(choices=SystemCheckStatus.choices)
    findings = models.JSONField(default=dict, encoder=DjangoJSONEncoder)

    class Meta:
        db_table = "system_check_run"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(check_name__in=list(SystemCheckName.values)),
                name="system_check_run_check_name_check",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=list(SystemCheckStatus.values)),
                name="system_check_run_status_check",
            ),
        ]
        indexes = [
            models.Index(fields=["check_name", "-run_at"], name="idx_check_run_latest"),
        ]

    def __str__(self) -> str:
        return f"{self.check_name} @ {self.run_at} ({self.status})"


class NotificationType(models.TextChoices):
    # The four notification points Implementation Plan Phase 18 / PRD
    # FR52-55 name. No corresponding schema exists in Spec v1.0 §3 at
    # all (zero occurrences of "notification" in that document) — the
    # same kind of gap Phase 9's user_group table filled; this is the
    # minimal schema PRD FR55's "recorded and retried" concretely needs.
    OFFER_CREATED = "offer_created", "Offer created"
    ADMIN_CANCELLATION = "admin_cancellation", "Admin cancellation"
    TZDATA_REMATERIALIZATION = "tzdata_rematerialization", "Tzdata re-materialization"
    # Not literally the same PRD FR as the row above — a re-materialization
    # CONFLICT (occurrence could not be recomputed, human decision needed,
    # PRD FR13b) is a different audience (owner AND resource admin) and a
    # different message than a successful, silent time change. kairos.
    # bookings.tasks.rematerialize_stale_series already recorded conflicts
    # as "needing notification" since Phase 13 — this type is what fulfills
    # that promise, not new scope invented this phase.
    TZDATA_REMATERIALIZATION_CONFLICT = (
        "tzdata_rematerialization_conflict",
        "Tzdata re-materialization conflict",
    )
    # No real caller yet — Rollout v1.0 §4.5's hold-release procedure is a
    # manual operational runbook (SQL an operator runs during an incident),
    # not application code any phase has built. Built standalone this
    # phase per its own explicit instruction: the TEMPLATE and send
    # mechanism must exist and be independently testable, but inventing a
    # fake trigger path just to have a caller would be scope beyond this
    # phase's actual job. See CLAUDE.md Open Questions.
    ROLLBACK_HOLD_RELEASED = "rollback_hold_released", "Rollback hold released"
    # PRD FR51 / Implementation Plan Phase 19 — a recurring series whose
    # owner was just deactivated is FLAGGED to the resource administrator
    # ("with the option to transfer ownership or terminate the series"),
    # never silently left to keep materializing for a principal who no
    # longer exists as an active user.
    SERIES_OWNER_DEACTIVATED = "series_owner_deactivated", "Series owner deactivated"


class NotificationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class NotificationLog(models.Model):
    """PRD FR55's actual mechanism: "delivery failure must not roll back
    or block the underlying state transition, but must be recorded and
    retried." Written EXCLUSIVELY by `send_notification_task`
    (kairos.core.tasks) INSIDE the worker that attempts delivery — never
    by the service-layer functions that trigger a notification
    (kairos.core.notifications' `notify_*` functions), which only build
    the message and enqueue. That split is what keeps every notification
    dispatch call sync-free from the request path (RFC v1.0 §15a): the
    request thread (or the on_commit callback it registers) only ever
    calls `.delay()`, never touches this table.

    No FK to `app_user` for `recipient_user_id` — the SAME reasoning as
    `AuditLog.actor_id` (Phase 8): a `RESTRICT`-on-delete FK would let a
    notification's own historical record block a user's eventual
    offboarding (Phase 19), and this table must survive independently of
    whether the recipient still exists.
    """

    Type = NotificationType
    Status = NotificationStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    notification_type = models.TextField(choices=NotificationType.choices)
    recipient_user_id = models.UUIDField()
    recipient_email = models.TextField()
    subject = models.TextField()
    body = models.TextField()
    # Structured payload behind the rendered subject/body — lets tests
    # assert on specific facts (e.g. the exact expiry instant, PRD FR52)
    # without parsing prose, and gives a future retry/replay tool
    # something machine-readable to work from.
    context = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    status = models.TextField(
        choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    attempts = models.IntegerField(default=0)
    last_error = models.TextField(null=True, blank=True)  # noqa: DJ001
    request_id = models.TextField(null=True, blank=True)  # noqa: DJ001
    created_at = models.DateTimeField(db_default=Now())
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "notification_log"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(notification_type__in=list(NotificationType.values)),
                name="notification_log_type_check",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=list(NotificationStatus.values)),
                name="notification_log_status_check",
            ),
        ]
        indexes = [
            models.Index(
                fields=["recipient_user_id", "-created_at"], name="idx_notification_recipient"
            ),
            models.Index(fields=["notification_type", "-created_at"], name="idx_notification_type"),
        ]

    def __str__(self) -> str:
        return f"{self.notification_type} -> {self.recipient_email} ({self.status})"


# ============================================================
# Alerting (Implementation Plan Phase 21; Rollout v1.0 §6.1)
# ============================================================
class AlertSeverity(models.TextChoices):
    SEV_1 = "sev_1", "SEV-1"
    SEV_2 = "sev_2", "SEV-2"
    SEV_3 = "sev_3", "SEV-3"


class AlertKey(models.TextChoices):
    # The six checks Rollout v1.0 §6.1's table names, plus the
    # actor_type='unknown' SEV-3 signal (audit trail, not a background
    # job) — see kairos.core.alerting.evaluate_alerts, the ONE function
    # that evaluates all seven.
    SCHEMA_ASSERTION = "schema_assertion", "Schema assertion"
    RECONCILIATION = "reconciliation", "Reconciliation"
    HOLD_REAPER = "hold_reaper", "Hold reaper"
    OFFER_CASCADE = "offer_cascade", "Offer cascade"
    SERIES_MATERIALIZATION = "series_materialization", "Series materialization"
    TZDATA_REMATERIALIZATION = "tzdata_rematerialization", "Tzdata re-materialization"
    AUDIT_ACTOR_UNKNOWN = "audit_actor_unknown", "Audit actor unknown"


class AlertDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class AlertEvent(models.Model):
    """One row per alert CONDITION firing, not per evaluation tick —
    `kairos.core.alerting.fire_or_resolve` is the only writer, and it is
    edge-triggered: `uniq_open_alert_per_key` (a partial unique index on
    `resolved_at IS NULL`) makes it structurally impossible for two OPEN
    alerts to exist for the same `alert_key` at once, so re-evaluating an
    already-firing condition on the next `ALERT_EVALUATION_INTERVAL_
    SECONDS` tick is a no-op rather than a second email. The condition
    clearing sets `resolved_at`; a later re-firing opens a NEW row (never
    reuses the old one), so the full history of when each alert fired and
    cleared survives, the same "append, don't overwrite" instinct
    `audit_log`/`system_check_run` already apply to different tables.

    Delivery fields (`email_status`/`email_attempts`/`email_last_error`/
    `email_sent_at`) mirror `NotificationLog`'s shape exactly, reused here
    directly rather than routed through `NotificationLog` itself — an
    alert's recipient is a fixed operator mailbox (`settings.
    ALERT_RECIPIENT_EMAIL`), not an `AppUser`, and `NotificationLog.
    recipient_user_id` is a real user id by that table's own docstring.
    """

    Severity = AlertSeverity
    Key = AlertKey
    DeliveryStatus = AlertDeliveryStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert_key = models.TextField(choices=AlertKey.choices)
    severity = models.TextField(choices=AlertSeverity.choices)
    message = models.TextField()
    context = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    fired_at = models.DateTimeField(db_default=Now())
    resolved_at = models.DateTimeField(null=True, blank=True)
    email_status = models.TextField(
        choices=AlertDeliveryStatus.choices, default=AlertDeliveryStatus.PENDING
    )
    email_attempts = models.IntegerField(default=0)
    email_last_error = models.TextField(null=True, blank=True)  # noqa: DJ001
    email_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "alert_event"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(alert_key__in=list(AlertKey.values)),
                name="alert_event_key_check",
            ),
            models.CheckConstraint(
                condition=models.Q(severity__in=list(AlertSeverity.values)),
                name="alert_event_severity_check",
            ),
            models.UniqueConstraint(
                fields=["alert_key"],
                condition=models.Q(resolved_at__isnull=True),
                name="uniq_open_alert_per_key",
            ),
        ]
        indexes = [
            models.Index(fields=["alert_key", "-fired_at"], name="idx_alert_event_key"),
        ]

    def __str__(self) -> str:
        return f"{self.alert_key} ({self.severity}) fired {self.fired_at}"


# ============================================================
# Metrics (Implementation Plan Phase 21; Rollout v1.0 §6.2)
# ============================================================
class RequestMetricType(models.TextChoices):
    BOOKING_WRITE = "booking_write", "Booking write"
    AVAILABILITY_READ = "availability_read", "Availability read"
    AUTH_FAILURE = "auth_failure", "Auth failure"
    # Implementation Plan Phase 22 — a 429 gets its own type (checked
    # first in classify_metric_type, ahead of the path-based rules) so
    # kairos.core.metrics.rate_limit_metric_slot can query it directly
    # rather than inferring "rate limited" from status_code alone mixed
    # in with every other metric_type.
    RATE_LIMITED = "rate_limited", "Rate limited"
    OTHER = "other", "Other"


class RequestMetric(models.Model):
    """One row per HTTP request (`kairos.core.middleware.
    MetricsMiddleware`), read back by `kairos.core.metrics` for every
    rolling-window rate/percentile the dashboard shows — booking-write and
    availability-read P95 latency, the 503 rate split by cause, and the
    auth-failure rate by shape. No metrics/time-series library exists in
    this project (pyproject.toml has none); every aggregate is computed
    live, by query, over `METRICS_WINDOW_SECONDS`, the same "derive from
    real stored data, don't precompute" choice `heartbeat_is_stale`/
    `GET /admin/checks/latest` already made for staleness.

    `cause` is null for an ordinary request; populated for a 503
    (`ServiceUnavailableError.cause`: "lock_contention" or "failover"), an
    `auth_failure`-typed row (the failure shape — see `kairos.core.drf.
    _auth_failure_shape`), or (Implementation Plan Phase 22) a
    `rate_limited`-typed row (`"per_principal_token_bucket"` or
    `"per_ip_token_bucket"` — see `kairos.core.rate_limit`).
    `principal_id` (Phase 22) is the authenticated `AppUser.id` as text
    when one exists, null for an unauthenticated request — populated on
    EVERY row (not just 429s) since it costs nothing extra to record and
    "per-principal breakdown" (Rollout v1.0 §6.2) needs it specifically on
    rate-limited rows. `BigAutoField`, not a UUID: this table is
    request-volume-scale and gets pruned on
    `REQUEST_METRIC_RETENTION_HOURS` — no downstream reference ever needs
    a row's id to be globally unique or non-guessable, unlike an audit
    entity.
    """

    Type = RequestMetricType

    id = models.BigAutoField(primary_key=True)
    metric_type = models.TextField(choices=RequestMetricType.choices)
    method = models.TextField()
    path = models.TextField()
    status_code = models.IntegerField()
    duration_ms = models.IntegerField(null=True, blank=True)
    cause = models.TextField(null=True, blank=True)  # noqa: DJ001
    principal_id = models.TextField(null=True, blank=True)  # noqa: DJ001
    recorded_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "request_metric"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(metric_type__in=list(RequestMetricType.values)),
                name="request_metric_type_check",
            ),
        ]
        indexes = [
            models.Index(fields=["metric_type", "-recorded_at"], name="idx_request_metric_type"),
            models.Index(fields=["status_code", "-recorded_at"], name="idx_request_metric_status"),
            # Phase 22 — backs rate_limit_metric_slot's "top principals"
            # breakdown query (filter status_code=429, group by
            # principal_id).
            models.Index(
                fields=["principal_id", "-recorded_at"], name="idx_request_metric_principal"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.method} {self.path} -> {self.status_code}"


class OperationalHeartbeat(models.Model):
    """A generic heartbeat slot for an operational job that is NOT one of
    the six `system_check_run.check_name` values — that table's own CHECK
    constraint is deliberately closed to exactly six (Implementation Plan
    Phase 21 Scope IN names "all six checks," not a seventh). Used today
    for `kairos.core.management.commands.cleanup_idempotency_keys`
    (scheduled for the first time this phase — see that command's own
    docstring, written in Phase 5, naming Phase 21 explicitly). `name` is
    the primary key: one row per named job, upserted via `update_or_
    create`, not one row per run — there is no history requirement here
    the way `system_check_run`'s append-only shape serves; only "when did
    this last succeed" is ever asked.
    """

    name = models.TextField(primary_key=True)
    last_run_at = models.DateTimeField()
    findings = models.JSONField(default=dict, encoder=DjangoJSONEncoder)

    class Meta:
        db_table = "operational_heartbeat"

    def __str__(self) -> str:
        return f"{self.name} @ {self.last_run_at}"
