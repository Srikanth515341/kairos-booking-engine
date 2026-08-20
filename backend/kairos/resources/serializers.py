from __future__ import annotations

from rest_framework import serializers

from .models import Resource


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
