from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, cast

from django.db.models import Q
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from kairos.bookings.serializers import BookingResponseSerializer
from kairos.core.exceptions import NotFoundError, PolicyValidationError
from kairos.core.idempotency import run_idempotent_write
from kairos.core.models import AuditActorType
from kairos.core.pagination import decode_cursor, encode_cursor, parse_limit
from kairos.core.views import KairosAPIView
from kairos.core.views import request_id as _request_id
from kairos.identity.models import AppUser

from .models import WaitlistEntry, WaitlistEntryStatus, WaitlistOffer, WaitlistOfferStatus
from .serializers import (
    WaitlistEntryResponseSerializer,
    WaitlistJoinSerializer,
    WaitlistOfferResponseSerializer,
)
from .services import (
    AcceptOfferRequest,
    DeclineOfferRequest,
    WaitlistCancelRequest,
    WaitlistJoinRequest,
    accept_offer,
    cancel_waitlist_entry,
    decline_offer,
    join_waitlist,
)

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
VALID_STATUS_FILTERS = frozenset(WaitlistEntryStatus.values)


def _require_idempotency_key(request: Request) -> uuid.UUID:
    # Required — Spec v1.0 §5.11; Spec v1.0 §7's coverage list names this
    # endpoint explicitly, and this project's own broader-than-Spec
    # precedent (RecurringSeriesCancelView, Phase 12) already applies the
    # requirement to every mutating endpoint, not only the ones §7's
    # example list enumerates.
    header_value = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if not header_value:
        raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "header is required")
    try:
        return uuid.UUID(header_value)
    except ValueError as exc:
        raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "must be a valid UUID") from exc


def _queue_positions(entries: list[WaitlistEntry]) -> dict[uuid.UUID, int]:
    """One position query per DISTINCT resource among this page's WAITING
    entries, not one per entry (RFC v1.0 §7.2's N+1 guard, the same
    principle Phase 6's availability view already applies)."""
    positions: dict[uuid.UUID, int] = {}
    resource_ids = {e.resource_id for e in entries if e.status == WaitlistEntryStatus.WAITING}
    for resource_id in resource_ids:
        ordered_ids = list(
            WaitlistEntry.objects.filter(
                resource_id=resource_id, status=WaitlistEntryStatus.WAITING
            )
            .order_by("joined_at", "id")
            .values_list("id", flat=True)
        )
        for idx, entry_id in enumerate(ordered_ids, start=1):
            positions[entry_id] = idx
    return positions


def _active_offers(entries: list[WaitlistEntry]) -> dict[uuid.UUID, WaitlistOffer]:
    """One query for the whole page (RFC v1.0 §7.2's N+1 guard), not one
    per entry — mirrors `_queue_positions` above."""
    return {
        offer.waitlist_entry_id: offer
        for offer in WaitlistOffer.objects.filter(
            waitlist_entry_id__in=[e.id for e in entries], status=WaitlistOfferStatus.ACTIVE
        )
    }


def _entry_response(entry: WaitlistEntry) -> dict[str, Any]:
    return dict(
        WaitlistEntryResponseSerializer(
            entry,
            context={
                "queue_positions": _queue_positions([entry]),
                "active_offers": _active_offers([entry]),
            },
        ).data
    )


class WaitlistEntryCollectionView(KairosAPIView):
    """POST /api/v1/waitlist-entries (Spec v1.0 §5.11) and
    GET /api/v1/waitlist-entries (Spec v1.0 §5.12)."""

    def post(self, request: Request) -> Response:
        idempotency_key = _require_idempotency_key(request)
        user = cast(AppUser, request.user)

        serializer = WaitlistJoinSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data

        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            entry = join_waitlist(
                WaitlistJoinRequest(
                    resource=attrs["resource"],
                    user=user,
                    start=attrs["start"],
                    end=attrs["end"],
                    request_id=request_id,
                )
            )
            return status.HTTP_201_CREATED, _entry_response(entry)

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint="POST /api/v1/waitlist-entries",
            body=dict(request.data),
            request_id=request_id,
            actor_type=AuditActorType.USER,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response

    def get(self, request: Request) -> Response:
        user = cast(AppUser, request.user)
        params = request.query_params

        # Always self-scoped — no user_id parameter exists (Spec v1.0
        # §5.12), so there is no permission check to get wrong.
        qs = WaitlistEntry.objects.filter(user=user)

        status_param = params.get("status")
        if status_param is not None:
            if status_param not in VALID_STATUS_FILTERS:
                raise PolicyValidationError(
                    "status", f"must be one of {sorted(VALID_STATUS_FILTERS)}"
                )
            qs = qs.filter(status=status_param)

        try:
            limit = parse_limit(params.get("limit"))
        except ValueError as exc:
            raise PolicyValidationError("limit", str(exc)) from exc

        cursor_param = params.get("cursor")
        if cursor_param:
            try:
                cursor_sort_key, cursor_id = decode_cursor(cursor_param)
                cursor_joined_at = datetime.fromisoformat(cursor_sort_key)
            except ValueError as exc:
                raise PolicyValidationError("cursor", "malformed cursor") from exc
            qs = qs.filter(
                Q(joined_at__gt=cursor_joined_at) | Q(joined_at=cursor_joined_at, id__gt=cursor_id)
            )

        # joined_at ASC — matches the actual FCFS queue position (Spec
        # v1.0 §8's pagination table), backed by idx_waitlist_entry_order.
        rows = list(qs.order_by("joined_at", "id")[: limit + 1])

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last.joined_at.isoformat(), str(last.id))

        positions = _queue_positions(rows)
        offers = _active_offers(rows)
        return Response(
            {
                "data": [
                    dict(
                        WaitlistEntryResponseSerializer(
                            e, context={"queue_positions": positions, "active_offers": offers}
                        ).data
                    )
                    for e in rows
                ],
                "next_cursor": next_cursor,
            }
        )


class WaitlistEntryCancelView(KairosAPIView):
    """POST /api/v1/waitlist-entries/{id}/cancel — owner-only self-service
    withdrawal. Not documented in Spec v1.0 §5.11/§5.12 (which cover only
    join and list), but required by Implementation Plan Phase 14's own
    Definition of Done ("join, list, and cancel a waitlist entry all
    work") — the same kind of phase-DoD-beyond-Spec's-literal-text
    addition Phase 12 made for the reason-on-series-cancel requirement,
    built on the identical owner-only, idempotent-double-cancel shape
    `BookingCancelView` already established (minus the admin-override
    branch, which Spec never describes for a waitlist entry).
    """

    def post(self, request: Request, pk: uuid.UUID) -> Response:
        idempotency_key = _require_idempotency_key(request)
        user = cast(AppUser, request.user)

        try:
            entry = WaitlistEntry.objects.get(id=pk)
        except WaitlistEntry.DoesNotExist as exc:
            raise NotFoundError from exc

        # Owner only — 404, not 403, matching every other object-level
        # protection convention in this codebase (Spec v1.0 §1).
        if entry.user_id != user.id:
            raise NotFoundError

        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            result = cancel_waitlist_entry(
                WaitlistCancelRequest(entry=entry, actor=user, request_id=request_id)
            )
            return status.HTTP_200_OK, _entry_response(result.entry)

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint=f"POST /api/v1/waitlist-entries/{pk}/cancel",
            # entry_id folded into the fingerprinted body — same reasoning
            # as booking cancel/edit (kairos.bookings.views): the body
            # alone (empty here) never mentions which entry this is about.
            body={"entry_id": str(pk), **dict(request.data)},
            request_id=request_id,
            actor_type=AuditActorType.USER,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response


def _get_offer_for_owner(pk: uuid.UUID, user: AppUser) -> WaitlistOffer:
    try:
        offer = WaitlistOffer.objects.select_related("waitlist_entry").get(id=pk)
    except WaitlistOffer.DoesNotExist as exc:
        raise NotFoundError from exc
    # Permission: the entry's owner. Otherwise 404 (Spec v1.0 §5.13) — same
    # object-level protection convention as every other endpoint here.
    if offer.waitlist_entry.user_id != user.id:
        raise NotFoundError
    return offer


class WaitlistOfferConfirmView(KairosAPIView):
    """POST /api/v1/waitlist-offers/{id}/confirm (Spec v1.0 §5.13)."""

    def post(self, request: Request, pk: uuid.UUID) -> Response:
        idempotency_key = _require_idempotency_key(request)
        user = cast(AppUser, request.user)
        offer = _get_offer_for_owner(pk, user)
        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            booking = accept_offer(
                AcceptOfferRequest(offer=offer, user=user, request_id=request_id)
            )
            # Spec v1.0 §5.13: "the booking (the hold row, now confirmed),
            # with waitlist_offer_id populated" — booking has no such
            # column (Spec v1.0 §3's DDL doesn't define one), so it's
            # added here at the response layer rather than broadening
            # BookingResponseSerializer's shared shape for every OTHER
            # booking response that doesn't promise this field.
            body = dict(BookingResponseSerializer(booking).data)
            body["waitlist_offer_id"] = str(offer.id)
            return status.HTTP_201_CREATED, body

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint=f"POST /api/v1/waitlist-offers/{pk}/confirm",
            body={"offer_id": str(pk), **dict(request.data)},
            request_id=request_id,
            actor_type=AuditActorType.USER,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response


class WaitlistOfferDeclineView(KairosAPIView):
    """POST /api/v1/waitlist-offers/{id}/decline (Spec v1.0 §5.13). Not in
    Spec v1.0 §7's Idempotency-Key coverage list any more than
    `POST /recurring-series/{id}/cancel` or `POST /waitlist-entries/{id}/
    cancel` were (Phase 12/14) — this project's own established
    broader-than-Spec's-literal-list precedent applies here a third time,
    for the identical reason: consistency, and Spec v1.0 §7 point 7's
    general "conflict outcomes are recorded too" rule (`offer_already_
    resolved` is exactly such an outcome).
    """

    def post(self, request: Request, pk: uuid.UUID) -> Response:
        idempotency_key = _require_idempotency_key(request)
        user = cast(AppUser, request.user)
        offer = _get_offer_for_owner(pk, user)
        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            declined = decline_offer(
                DeclineOfferRequest(offer=offer, user=user, request_id=request_id)
            )
            body = dict(WaitlistOfferResponseSerializer(declined).data)
            return status.HTTP_200_OK, body

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint=f"POST /api/v1/waitlist-offers/{pk}/decline",
            body={"offer_id": str(pk), **dict(request.data)},
            request_id=request_id,
            actor_type=AuditActorType.USER,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response
