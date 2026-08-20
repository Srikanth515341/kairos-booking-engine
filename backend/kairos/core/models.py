"""The transaction boundary (RFC v1.0 §11.2) — the entire idempotency
mechanism lives in how this table is written to, not in this schema alone.
Lives in `core`, not `bookings`, because every future write path (edit,
cancel, waitlist join, offer confirm, admin deactivate — Phases 7, 14, 16,
19) needs the identical mechanism, not a booking-specific one.
"""

from __future__ import annotations

from django.core.serializers.json import DjangoJSONEncoder
from django.db import models

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
