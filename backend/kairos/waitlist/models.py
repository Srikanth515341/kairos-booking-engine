"""`waitlist_entry` (Spec v1.0 §3; Implementation Plan Phase 14; PRD FR20-22,
FR27). A peer entity to `booking`, not a sub-concept of it — RFC v1.0 §4.2's
own architecture diagram lists `waitlist_entry` alongside `booking` under
PostgreSQL PRIMARY, and Spec v1.0 §2's ER diagram reaches it directly from
both `app_user` and `resource`, never through `booking` — hence its own app
rather than living inside `kairos.bookings`.
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import DateTimeRangeField
from django.contrib.postgres.indexes import GistIndex
from django.db import models
from django.db.models.functions import Now

from kairos.identity.models import AppUser
from kairos.resources.models import Resource


class WaitlistEntryStatus(models.TextChoices):
    WAITING = "waiting", "Waiting"
    OFFERED = "offered", "Offered"
    FULFILLED = "fulfilled", "Fulfilled"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"


class WaitlistEntry(models.Model):
    """`uniq_live_waitlist_per_user_slot` is added in migration 0002, not
    declared here — its Spec-literal name is 32 characters, past Django's
    30-character `UniqueConstraint`/`models.Index` portability limit
    (models.E033/E034), the identical situation Phase 11's
    `idx_series_materialized_through` already established the raw-SQL-
    migration precedent for (Postgres itself allows up to 63; the name is
    kept verbatim rather than shortened). That index is what makes
    SEC-03(b)/(c) a database guarantee rather than an application-level
    race: it covers BOTH 'waiting' and 'offered' so a user cannot re-join
    while already holding an outstanding offer (RFC v1.0 §8.2).
    """

    Status = WaitlistEntryStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    resource = models.ForeignKey(
        Resource,
        on_delete=models.RESTRICT,
        db_column="resource_id",
        related_name="waitlist_entries",
    )
    user = models.ForeignKey(
        AppUser,
        on_delete=models.RESTRICT,
        db_column="user_id",
        related_name="waitlist_entries",
    )
    time_range = DateTimeRangeField()
    status = models.TextField(
        choices=WaitlistEntryStatus.choices, default=WaitlistEntryStatus.WAITING
    )
    # `db_default`, NOT `auto_now_add` — the same fix Phase 8 needed for
    # AuditLog.occurred_at (auto_now_add is Python-side only). Here it is
    # also the entire enforcement mechanism behind SEC-03(a): a genuine
    # column-level DEFAULT means the value is set by Postgres itself
    # regardless of what a request body contains, so
    # WaitlistJoinSerializer doesn't merely choose to ignore a
    # client-supplied `joined_at` — there is no field for one to bind to
    # in the first place (RFC v1.0 §8.2: a forged queue position would
    # break FCFS, PRD FR22).
    joined_at = models.DateTimeField(db_default=Now())

    class Meta:
        db_table = "waitlist_entry"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=list(WaitlistEntryStatus.values)),
                name="waitlist_entry_status_check",
            ),
            models.CheckConstraint(
                condition=models.Q(time_range__isempty=False),
                name="wl_entry_time_range_not_empty",
            ),
        ]
        indexes = [
            # Eligibility lookup (PRD FR21): find entries whose requested
            # range is FULLY CONTAINED BY the freed range —
            # `freed_range @> time_range`. Containment, not overlap. A
            # GiST index on the range serves @> as well as && (Spec v1.0
            # §3's own comment on this index, reproduced here).
            GistIndex(
                fields=["resource", "time_range"],
                name="idx_waitlist_entry_lookup",
                condition=models.Q(status=WaitlistEntryStatus.WAITING),
            ),
            # FCFS ordering within a resource (PRD FR22).
            models.Index(
                fields=["resource", "joined_at", "id"],
                name="idx_waitlist_entry_order",
                condition=models.Q(status=WaitlistEntryStatus.WAITING),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} waiting on {self.resource_id} ({self.status})"
