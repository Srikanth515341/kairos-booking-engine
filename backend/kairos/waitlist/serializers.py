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

from .models import WaitlistEntry, WaitlistOffer
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

    def get_active_offer(self, obj: WaitlistEntry) -> dict[str, str] | None:
        # Present only when an ACTIVE offer actually exists for this entry
        # (Spec v1.0 §5.12: "present only when status: 'offered'") — always
        # present AS A KEY regardless (null otherwise), matching this
        # codebase's "always-present, sometimes null" field convention
        # (see BookingResponseSerializer's cancelled_at/cancelled_by).
        # Batched by the view (one query for the whole page, RFC v1.0
        # §7.2's N+1 guard — the same principle queue_position already
        # applies), never queried per row here.
        offers: dict[uuid.UUID, WaitlistOffer] = self.context.get("active_offers", {})
        offer = offers.get(obj.id)
        if offer is None:
            return None
        return {
            "id": str(offer.id),
            "expires_at": offer.expires_at.isoformat().replace("+00:00", "Z"),
        }


class WaitlistOfferResponseSerializer(serializers.Serializer[WaitlistOffer]):
    """POST /waitlist-offers/{id}/decline's 200 response body (Spec v1.0
    §5.13: "offer with status: 'declined'"). `/confirm`'s response is the
    BOOKING, not the offer (Spec v1.0 §5.13's own wording) — reuses
    `BookingResponseSerializer` directly instead, with `waitlist_offer_id`
    added at the view layer (see `WaitlistOfferConfirmView`).
    """

    id = serializers.UUIDField()
    waitlist_entry_id = serializers.SerializerMethodField()
    hold_booking_id = serializers.SerializerMethodField()
    resource_id = serializers.SerializerMethodField()
    start = serializers.DateTimeField(source="time_range.lower")
    end = serializers.DateTimeField(source="time_range.upper")
    status = serializers.CharField()
    expires_at = serializers.DateTimeField()
    created_at = serializers.DateTimeField()

    def get_waitlist_entry_id(self, obj: WaitlistOffer) -> str:
        return str(obj.waitlist_entry_id)

    def get_hold_booking_id(self, obj: WaitlistOffer) -> str:
        return str(obj.hold_booking_id)

    def get_resource_id(self, obj: WaitlistOffer) -> str:
        return str(obj.resource_id)
