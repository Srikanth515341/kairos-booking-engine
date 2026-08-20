"""Policy validation lives here, never availability (Implementation Plan
Phase 4 scope). Range well-formedness, bookable hours, max duration,
past-dating, and the advance horizon are all checkable without touching
`booking` — availability is deliberately not checked, because that check-
then-insert step is exactly what RFC v1.0 §3 eliminates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone as django_timezone
from rest_framework import serializers

from kairos.bookings.models import Booking
from kairos.core.constants import MAX_ADVANCE_HORIZON_DAYS
from kairos.core.exceptions import PolicyValidationError, ResourceNotFoundError
from kairos.resources.models import Resource, ResourceStatus


class BookingCreateSerializer(serializers.Serializer[None]):
    resource_id = serializers.UUIDField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            resource = Resource.objects.get(id=attrs["resource_id"])
        except Resource.DoesNotExist as exc:
            raise ResourceNotFoundError from exc
        if resource.status != ResourceStatus.ACTIVE:
            # An inactive resource is treated identically to a nonexistent
            # one (Spec v1.0 §5.1) — not a distinct error, so its existence
            # isn't leaked.
            raise ResourceNotFoundError

        start: datetime = attrs["start"]
        end: datetime = attrs["end"]

        if end <= start:
            raise PolicyValidationError("end", "must be after start")

        local_tz = ZoneInfo(resource.timezone)
        local_start_time = start.astimezone(local_tz).time()
        local_end_time = end.astimezone(local_tz).time()
        if not (
            resource.bookable_start_time <= local_start_time
            and local_end_time <= resource.bookable_end_time
        ):
            raise PolicyValidationError("start", "outside the resource's bookable hours")

        if resource.max_booking_duration_minutes is not None and end - start > timedelta(
            minutes=resource.max_booking_duration_minutes
        ):
            raise PolicyValidationError("end", "exceeds the resource's maximum booking duration")

        now = django_timezone.now()
        if start < now:
            raise PolicyValidationError("start", "must not be in the past")

        if start > now + timedelta(days=MAX_ADVANCE_HORIZON_DAYS):
            raise PolicyValidationError("start", f"must be within {MAX_ADVANCE_HORIZON_DAYS} days")

        attrs["resource"] = resource
        return attrs


class BookingResponseSerializer(serializers.Serializer[Booking]):
    """Spec v1.0 §5.1's 201 response body, exactly."""

    id = serializers.UUIDField()
    resource_id = serializers.SerializerMethodField()
    user_id = serializers.SerializerMethodField()
    start = serializers.SerializerMethodField()
    end = serializers.SerializerMethodField()
    status = serializers.CharField()
    series_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()

    def get_resource_id(self, obj: Booking) -> str:
        return str(obj.resource_id)

    def get_user_id(self, obj: Booking) -> str:
        return str(obj.user_id)

    def get_start(self, obj: Booking) -> datetime:
        return obj.time_range.lower  # type: ignore[no-any-return]

    def get_end(self, obj: Booking) -> datetime:
        return obj.time_range.upper  # type: ignore[no-any-return]

    def get_series_id(self, obj: Booking) -> None:
        # `booking.series_id` doesn't exist until Phase 11 (recurring_series).
        return None
