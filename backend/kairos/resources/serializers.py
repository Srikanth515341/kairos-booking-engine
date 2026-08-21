from __future__ import annotations

from typing import Any

from rest_framework import serializers

from kairos.core.timezones import validate_iana_zone
from kairos.identity.models import UserGroup

from .models import Resource, ResourceOffboardingPolicy, ResourceStatus


def _validate_bookable_window(start: Any, end: Any) -> None:
    if end <= start:
        raise serializers.ValidationError(
            {"bookable_end_time": "must be after bookable_start_time"}
        )


class ResourceCreateSerializer(serializers.Serializer[None]):
    """POST /api/v1/resources (Spec v1.0 §5.14)."""

    name = serializers.CharField()
    category = serializers.CharField(required=False, default="meeting_room")
    timezone = serializers.CharField()
    bookable_start_time = serializers.TimeField()
    bookable_end_time = serializers.TimeField()
    max_booking_duration_minutes = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1
    )
    offboarding_policy = serializers.ChoiceField(
        choices=ResourceOffboardingPolicy.choices,
        required=False,
        default=ResourceOffboardingPolicy.TRANSFER,
    )
    restricted_group_id = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate_timezone(self, value: str) -> str:
        # PRD FR8, at the API boundary — the model's own save() also
        # enforces this (Phase 10), but validating here produces the
        # standard 400 validation_error shape rather than an uncaught
        # exception from the model layer.
        try:
            validate_iana_zone(value)
        except Exception as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate_restricted_group_id(self, value: Any) -> Any:
        if value is not None and not UserGroup.objects.filter(id=value).exists():
            raise serializers.ValidationError("no such group")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        _validate_bookable_window(attrs["bookable_start_time"], attrs["bookable_end_time"])
        return attrs


class ResourceUpdateSerializer(serializers.Serializer[None]):
    """PATCH /api/v1/resources/{id} (Spec v1.0 §5.14) — every field is
    optional; only fields actually present in the request body are
    changed (a real partial update, not merely optional-with-defaults).
    """

    name = serializers.CharField(required=False)
    bookable_start_time = serializers.TimeField(required=False)
    bookable_end_time = serializers.TimeField(required=False)
    max_booking_duration_minutes = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    offboarding_policy = serializers.ChoiceField(
        choices=ResourceOffboardingPolicy.choices, required=False
    )
    status = serializers.ChoiceField(choices=ResourceStatus.choices, required=False)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        resource: Resource = self.context["resource"]
        start = attrs.get("bookable_start_time", resource.bookable_start_time)
        end = attrs.get("bookable_end_time", resource.bookable_end_time)
        _validate_bookable_window(start, end)
        return attrs


class ResourceAdminGrantSerializer(serializers.Serializer[None]):
    """POST /api/v1/resources/{id}/admins (Spec v1.0 §5.14)."""

    user_id = serializers.UUIDField()


class ResourceSerializer(serializers.Serializer[Resource]):
    id = serializers.UUIDField()
    name = serializers.CharField()
    category = serializers.CharField()
    timezone = serializers.CharField()
    bookable_start_time = serializers.TimeField()
    bookable_end_time = serializers.TimeField()
    max_booking_duration_minutes = serializers.IntegerField(allow_null=True)
    offboarding_policy = serializers.CharField()
    status = serializers.CharField()
    created_by = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()

    def get_created_by(self, obj: Resource) -> str:
        return str(obj.created_by_id)
