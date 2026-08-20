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
from kairos.core.exceptions import NotFoundError, PolicyValidationError
from kairos.core.timezones import validate_iana_zone
from kairos.identity.authorization import AuthorizationService
from kairos.resources.models import Resource, ResourceStatus


def _validate_range_policy(resource: Resource, start: datetime, end: datetime) -> None:
    """Bookable hours, max duration, past-dating, and the advance horizon —
    shared by create and edit (PRD FR5: "editing... must be evaluated
    against the overlap constraint exactly as a new booking is. Edit
    cannot be a bypass"). A range that would be rejected at creation must
    be rejected identically when it arrives via an edit.
    """
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


class BookingCreateSerializer(serializers.Serializer[None]):
    resource_id = serializers.UUIDField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            resource = Resource.objects.get(id=attrs["resource_id"])
        except Resource.DoesNotExist as exc:
            raise NotFoundError from exc
        if resource.status != ResourceStatus.ACTIVE:
            # An inactive resource is treated identically to a nonexistent
            # one (Spec v1.0 §5.1) — not a distinct error, so its existence
            # isn't leaked.
            raise NotFoundError
        # PRD FR46: a restricted resource's non-members "may neither book
        # nor join its waitlist" — same 404 as inactive/nonexistent, so a
        # booker outside the group can't distinguish "restricted" from
        # "doesn't exist" (SEC-06's premise applies here too, not just to
        # the read endpoints).
        if not AuthorizationService.can_view_resource(self.context["user"], resource):
            raise NotFoundError

        _validate_range_policy(resource, attrs["start"], attrs["end"])

        attrs["resource"] = resource
        return attrs


class BookingEditSerializer(serializers.Serializer[None]):
    """PATCH body (Spec v1.0 §5.5). Only start/end are editable —
    `resource_id` isn't a field here at all, not merely ignored if sent:
    booking a different resource means cancelling and creating instead, so
    the range is validated against the booking's EXISTING resource, passed
    in via context rather than re-looked-up from the body.
    """

    start = serializers.DateTimeField()
    end = serializers.DateTimeField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        booking: Booking = self.context["booking"]
        _validate_range_policy(booking.resource, attrs["start"], attrs["end"])
        return attrs


class BookingCancelSerializer(serializers.Serializer[None]):
    """POST .../cancel body (Spec v1.0 §5.6). `reason` is required only
    when the requester isn't the booking's owner (PRD FR40/FR47/FR53 — the
    notified owner must be able to learn why an admin cancelled their
    booking); optional for self-cancellation. A blank string is treated
    the same as absent, not as "a reason was given."
    """

    reason = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not self.context["is_owner"] and not attrs.get("reason"):
            raise PolicyValidationError("reason", "required for an administrative override")
        return attrs


class BookingResponseSerializer(serializers.Serializer[Booking]):
    """Spec v1.0 §5.1's 201 response body, plus the cancellation fields
    Spec v1.0 §5.6 requires the cancel response to populate. Phase 6's
    GET already promises "shape as §5.1" for every booking regardless of
    status, so extending the ONE shared serializer (rather than a
    cancel-only variant) keeps GET on a cancelled booking able to show why
    and when it was cancelled too, instead of silently omitting that state.
    """

    id = serializers.UUIDField()
    resource_id = serializers.SerializerMethodField()
    user_id = serializers.SerializerMethodField()
    # DateTimeField (not a SerializerMethodField returning a raw datetime)
    # so start/end are formatted through the exact same code path as
    # created_at below. This matters for idempotency (Phase 5): a
    # SerializerMethodField's raw datetime return value gets re-formatted
    # differently by Django's JSONField storage encoder (which truncates to
    # millisecond precision) than by DRF's own response renderer (which
    # keeps microseconds) — two different strings for the same instant,
    # breaking IDEM-02's "identical stored response returned verbatim."
    # Formatting to a string once, here, means both paths see the same value.
    start = serializers.DateTimeField(source="time_range.lower")
    end = serializers.DateTimeField(source="time_range.upper")
    status = serializers.CharField()
    series_id = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    cancelled_at = serializers.DateTimeField(allow_null=True)
    cancelled_by = serializers.SerializerMethodField()
    cancellation_reason = serializers.CharField(allow_null=True)

    def get_resource_id(self, obj: Booking) -> str:
        return str(obj.resource_id)

    def get_user_id(self, obj: Booking) -> str:
        return str(obj.user_id)

    def get_series_id(self, obj: Booking) -> str | None:
        # `booking.series_id` exists since Phase 11 (recurring_series) but
        # no write path sets it yet (Phase 12 is the first to create
        # occurrence rows) — every existing booking has it NULL today.
        return str(obj.series_id) if obj.series_id else None

    def get_cancelled_by(self, obj: Booking) -> str | None:
        return str(obj.cancelled_by_id) if obj.cancelled_by_id else None


class RecurringSeriesPreviewSerializer(serializers.Serializer[None]):
    """POST /bookings/recurring/preview body (Spec v1.0 §5.8). Request-shape
    and stateless policy validation only — occurrence_count bounds are
    `expand_occurrences`'s own job (Phase 11), and the 365-day horizon
    needs `resource`/expanded occurrences that don't exist until
    `kairos.bookings.recurring_series.preview_recurring_series` runs.
    """

    resource_id = serializers.UUIDField()
    timezone = serializers.CharField()
    local_start_time = serializers.TimeField()
    local_end_time = serializers.TimeField()
    weekday = serializers.IntegerField(min_value=0, max_value=6)
    series_start_date = serializers.DateField()
    occurrence_count = serializers.IntegerField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            resource = Resource.objects.get(id=attrs["resource_id"])
        except Resource.DoesNotExist as exc:
            raise NotFoundError from exc
        if resource.status != ResourceStatus.ACTIVE:
            raise NotFoundError
        if not AuthorizationService.can_view_resource(self.context["user"], resource):
            raise NotFoundError

        # PRD FR8: a fixed offset is rejected at the API boundary, the
        # same check Resource.save()/RecurringSeries.save() enforce at
        # write time (Phase 10/11) — here it must run explicitly, since no
        # RecurringSeries row exists yet for a model-level save() to
        # intercept.
        validate_iana_zone(attrs["timezone"])
        if attrs["local_end_time"] <= attrs["local_start_time"]:
            raise PolicyValidationError("local_end_time", "must be after local_start_time")

        attrs["resource"] = resource
        return attrs


class RecurringSeriesCancelSerializer(serializers.Serializer[None]):
    """POST /recurring-series/{id}/cancel body (Spec v1.0 §5.10). Spec's
    own example body is empty, but PRD FR47 ("administrative override of
    another user's booking requires a recorded reason") is unconditional
    and a series-cancel-by-admin is exactly such an override — the
    identical rule `BookingCancelSerializer` already enforces for a single
    booking, applied here rather than left as a gap Spec's example simply
    didn't happen to show.
    """

    reason = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not self.context["is_owner"] and not attrs.get("reason"):
            raise PolicyValidationError("reason", "required for an administrative override")
        return attrs


class RecurringSeriesConfirmSerializer(serializers.Serializer[None]):
    """POST /bookings/recurring body (Spec v1.0 §5.9). `preview_token`
    verification (expiry, signature, ownership) is deliberately NOT done
    here — PreviewExpiredError is a 409, not a 400 `validation_error`,
    so it belongs to the view/service layer, exactly like NotFoundError
    is raised from BookingCreateSerializer.validate() but still isn't a
    'validation_error' response.
    """

    preview_token = serializers.CharField()
    acknowledged_conflicts = serializers.ListField(
        child=serializers.DateField(), required=False, default=list
    )
    acknowledged_adjustments = serializers.ListField(
        child=serializers.DateField(), required=False, default=list
    )
