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
