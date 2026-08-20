from __future__ import annotations

import uuid
from typing import Any

from django.contrib.postgres.fields import DateTimeRangeField
from django.db import models
from django.db.models import Func

from kairos.core.timezones import validate_iana_zone
from kairos.identity.models import AppUser
from kairos.resources.models import Resource


class RecurringSeriesStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    CANCELLED = "cancelled", "Cancelled"


class RecurringSeries(models.Model):
    """The series definition is AUTHORITATIVE; materialized occurrences in
    `booking` are DERIVED from it (RFC v1.0 §9.2) — every field needed to
    recompute the full occurrence set from scratch lives here, or
    re-materialization (PRD FR13, Phase 13) is impossible. No write path
    creates these rows yet (Phase 12); the schema and the expansion engine
    that consumes it (`kairos/bookings/recurrence.py`) exist first, on
    their own terms (Implementation Plan Phase 11's "why this is its own
    phase").
    """

    Status = RecurringSeriesStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.RESTRICT, db_column="resource_id")
    created_by = models.ForeignKey(AppUser, on_delete=models.RESTRICT, db_column="created_by")
    # IANA identifier, validated the same way as Resource.timezone (Phase
    # 10) — a fixed offset is rejected (PRD FR8) since it cannot express
    # when the underlying rules change, which is the entire reason a
    # series stores a zone identifier instead of a UTC offset at all.
    timezone = models.TextField()
    local_start_time = models.TimeField()  # wall-clock, NOT UTC
    local_end_time = models.TimeField()
    weekday = models.SmallIntegerField()  # 0 = Sunday ... 6 = Saturday
    series_start_date = models.DateField()  # local calendar date of occurrence #1
    occurrence_count = models.SmallIntegerField()  # PRD FR14a: 1-100
    # Which IANA tzdata release the current occurrences were materialized
    # under. When the deployed version differs from this, occurrences for
    # this series may be wrong and must be re-materialized (RFC v1.0 §9.4)
    # — without this column there is no way to know which series need
    # recomputation after a rule change.
    tzdata_version = models.TextField()
    materialized_through = models.DateField()  # rolling horizon watermark (PRD FR14c)
    status = models.TextField(
        choices=RecurringSeriesStatus.choices, default=RecurringSeriesStatus.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "recurring_series"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=list(RecurringSeriesStatus.values)),
                name="recurring_series_status_check",
            ),
            models.CheckConstraint(
                condition=models.Q(weekday__gte=0, weekday__lte=6),
                name="recurring_series_weekday_check",
            ),
            models.CheckConstraint(
                condition=models.Q(occurrence_count__gte=1, occurrence_count__lte=100),
                name="recurring_series_occurrence_count_check",
            ),
            models.CheckConstraint(
                condition=models.Q(local_end_time__gt=models.F("local_start_time")),
                name="series_window_valid",
            ),
        ]
        indexes = [
            # "Which series need re-materialization after a tzdata change"
            # (RFC v1.0 §9.4).
            models.Index(
                fields=["tzdata_version", "timezone"],
                name="idx_series_tzdata",
                condition=models.Q(status=RecurringSeriesStatus.ACTIVE),
            ),
            # idx_series_materialized_through (rolling materialization job,
            # PRD FR14c, Phase 13) is added via raw SQL in
            # 0003_series_materialized_through_index — Spec v1.0 §3's own
            # name for it is 32 characters, past Django's 30-character
            # portability limit (models.E034) for a models.Index. Postgres
            # itself allows up to 63, and this project is Postgres-only by
            # design, so the raw name is kept verbatim rather than shortened.
        ]

    def __str__(self) -> str:
        return f"{self.resource_id} series ({self.status})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        validate_iana_zone(self.timezone)
        super().save(*args, **kwargs)


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

    `series` (the recurring_series FK) is nullable — NULL for every one-off
    booking, set for a materialized recurring occurrence (Phase 11 adds the
    column; no write path sets it yet — Phase 12 is the first to actually
    create occurrence rows through a real endpoint).
    """

    Status = BookingStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.RESTRICT, db_column="resource_id")
    # NULL for a one-off booking. RFC v1.0 §15b: a rule-plus-runtime-
    # expansion model is fundamentally incompatible with the exclusion
    # constraint — it operates on concrete rows with concrete ranges, and
    # cannot enforce non-overlap against a booking that exists only as an
    # unexpanded rule. Every occurrence of a series is therefore its own
    # real `booking` row, sharing this FK rather than a rule ID.
    series = models.ForeignKey(
        RecurringSeries,
        on_delete=models.RESTRICT,
        db_column="series_id",
        null=True,
        blank=True,
        related_name="bookings",
    )
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
