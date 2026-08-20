from __future__ import annotations

import uuid

from django.contrib.postgres.fields import DateTimeRangeField
from django.db import models
from django.db.models import Func

from kairos.identity.models import AppUser
from kairos.resources.models import Resource


class BookingStatus(models.TextChoices):
    # 'held' is a real reservation, not bookkeeping: it occupies the SAME
    # exclusion domain as a confirmed booking. This is what makes a
    # waitlist offer enforceable (RFC v1.0 §10.1).
    CONFIRMED = "confirmed", "Confirmed"
    HELD = "held", "Held"
    CANCELLED = "cancelled", "Cancelled"


class Booking(models.Model):
    """The table the entire correctness guarantee lives on (RFC v1.0 §3).

    The `no_overlapping_bookings` EXCLUDE constraint that makes this table
    correct is added via raw SQL in migration 0002_exclusion_constraint, not
    declared here — see that migration for the full rationale.

    `series_id` (the recurring_series FK) is deliberately absent until Phase
    11 introduces `recurring_series` — a FK cannot reference a table that
    doesn't exist yet, per Implementation Plan Phase 2's own indexing note
    ("idx_booking_series, nullable FK deferred").
    """

    Status = BookingStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.RESTRICT, db_column="resource_id")
    # For status='confirmed', the owner. For status='held', the waitlisted
    # user the slot is reserved FOR. Acceptance transitions this same row to
    # 'confirmed' with the same user_id — no new row is created (RFC §10.3).
    user = models.ForeignKey(
        AppUser, on_delete=models.RESTRICT, db_column="user_id", related_name="bookings"
    )
    time_range = DateTimeRangeField()
    # Materialized so list endpoints can sort and paginate on start time with
    # a plain btree index (Spec §8) instead of a functional sort.
    starts_at = models.GeneratedField(
        expression=Func("time_range", function="lower", output_field=models.DateTimeField()),
        output_field=models.DateTimeField(),
        db_persist=True,
    )
    status = models.TextField(choices=BookingStatus.choices, default=BookingStatus.CONFIRMED)
    expires_at = models.DateTimeField(null=True, blank=True)  # set iff status='held'
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        AppUser,
        on_delete=models.RESTRICT,
        db_column="cancelled_by",
        null=True,
        blank=True,
        related_name="cancelled_bookings",
    )
    # null=True (not the empty-string convention) to match Spec v1.0 §3's
    # nullable `TEXT` column exactly: absence of a reason is NULL, not "".
    cancellation_reason = models.TextField(null=True, blank=True)  # noqa: DJ001

    class Meta:
        db_table = "booking"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=list(BookingStatus.values)),
                name="booking_status_check",
            ),
            models.CheckConstraint(
                condition=models.Q(time_range__isempty=False),
                name="booking_time_range_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status=BookingStatus.HELD, expires_at__isnull=False)
                    | (~models.Q(status=BookingStatus.HELD) & models.Q(expires_at__isnull=True))
                ),
                name="hold_has_expiry",
            ),
        ]
        indexes = [
            # Hold reclamation sweep (RFC §10.4): "status='held' AND
            # expires_at <= now()".
            models.Index(
                fields=["expires_at"],
                name="idx_booking_hold_expiry",
                condition=models.Q(status=BookingStatus.HELD),
            ),
            # "My bookings" list + cursor pagination on start time (Spec §8).
            models.Index(fields=["user", "starts_at", "id"], name="idx_booking_user_starts"),
        ]

    def __str__(self) -> str:
        return f"{self.resource_id} {self.time_range} ({self.status})"
