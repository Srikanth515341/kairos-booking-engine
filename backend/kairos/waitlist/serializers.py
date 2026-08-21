"""Request/response shapes for Spec v1.0 §5.11/§5.12. Policy validation
(range well-formedness, resource visibility, the advisory availability
check) lives in `validate()`, before the idempotency key is ever claimed —
the same "a request that can never succeed shouldn't consume a key slot"
precedent `BookingCreateSerializer` established (Phase 4).
"""

from __future__ import annotations

import uuid
from typing import Any

from rest_framework import serializers

from kairos.core.exceptions import NotFoundError, PolicyValidationError, SlotAlreadyAvailableError
from kairos.identity.authorization import AuthorizationService
from kairos.resources.models import Resource, ResourceStatus

from .models import WaitlistEntry
from .services import slot_is_free


class WaitlistJoinSerializer(serializers.Serializer[None]):
    resource_id = serializers.UUIDField()
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        try:
            resource = Resource.objects.get(id=attrs["resource_id"])
        except Resource.DoesNotExist as exc:
            raise NotFoundError from exc
        if resource.status != ResourceStatus.ACTIVE:
            raise NotFoundError
        # PRD FR46: "non-members may neither book nor join its waitlist" —
        # the identical 404 BookingCreateSerializer already applies for
        # booking a restricted resource, applied here for joining its
        # waitlist too.
        if not AuthorizationService.can_view_resource(self.context["user"], resource):
            raise NotFoundError

        start, end = attrs["start"], attrs["end"]
        if end <= start:
            raise PolicyValidationError("end", "must be after start")

        if slot_is_free(resource, start, end):
            raise SlotAlreadyAvailableError

        attrs["resource"] = resource
        return attrs


class WaitlistEntryResponseSerializer(serializers.Serializer[WaitlistEntry]):
    """Spec v1.0 §5.11's 201 response body, extended with the §5.12 list
    fields (`queue_position`, `active_offer`) — one shared serializer for
    both, the same "GET's shape matches create's exactly" convention
    BookingResponseSerializer already established, rather than a
    list-only variant.
    """

    id = serializers.UUIDField()
    resource_id = serializers.SerializerMethodField()
    start = serializers.DateTimeField(source="time_range.lower")
    end = serializers.DateTimeField(source="time_range.upper")
    status = serializers.CharField()
    joined_at = serializers.DateTimeField()
    queue_position = serializers.SerializerMethodField()
    active_offer = serializers.SerializerMethodField()

    def get_resource_id(self, obj: WaitlistEntry) -> str:
        return str(obj.resource_id)

    def get_queue_position(self, obj: WaitlistEntry) -> int | None:
        positions: dict[uuid.UUID, int] = self.context.get("queue_positions", {})
        return positions.get(obj.id)

    def get_active_offer(self, obj: WaitlistEntry) -> None:
        # No offer mechanism exists yet (Phase 16) — every entry's active
        # offer is None regardless of status, always present as a key
        # (never omitted) to match Spec v1.0 §5.12's shape and this
        # codebase's "always-present, sometimes null" field convention
        # (see BookingResponseSerializer's cancelled_at/cancelled_by).
        return None
