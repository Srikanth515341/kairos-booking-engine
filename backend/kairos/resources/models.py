from __future__ import annotations

import uuid

from django.db import models

from kairos.identity.models import AppUser


class ResourceOffboardingPolicy(models.TextChoices):
    # PRD FR49. Applied to this resource's bookings when their owner is
    # deactivated.
    TRANSFER = "transfer", "Transfer"
    CANCEL_AND_NOTIFY = "cancel_and_notify", "Cancel and notify"
    RETAIN = "retain", "Retain"


class ResourceStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"


class Resource(models.Model):
    OffboardingPolicy = ResourceOffboardingPolicy
    Status = ResourceStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.TextField()
    category = models.TextField(default="meeting_room")
    # IANA identifier, e.g. 'Europe/Paris'. A fixed offset ('+01:00') is
    # rejected at the API boundary (PRD FR8) because an offset cannot express
    # when DST rules change.
    timezone = models.TextField()
    bookable_start_time = models.TimeField()
    bookable_end_time = models.TimeField()
    max_booking_duration_minutes = models.IntegerField(null=True, blank=True)
    offboarding_policy = models.TextField(
        choices=ResourceOffboardingPolicy.choices, default=ResourceOffboardingPolicy.TRANSFER
    )
    status = models.TextField(choices=ResourceStatus.choices, default=ResourceStatus.ACTIVE)
    created_by = models.ForeignKey(AppUser, on_delete=models.RESTRICT, db_column="created_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "resource"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(offboarding_policy__in=list(ResourceOffboardingPolicy.values)),
                name="resource_offboarding_policy_check",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=list(ResourceStatus.values)),
                name="resource_status_check",
            ),
            models.CheckConstraint(
                condition=models.Q(bookable_end_time__gt=models.F("bookable_start_time")),
                name="bookable_window_valid",
            ),
        ]

    def __str__(self) -> str:
        return self.name
