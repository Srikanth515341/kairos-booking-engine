from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any, cast

from django.db.models import Q
from django.utils import timezone as django_timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.request import Request
from rest_framework.response import Response

from kairos.bookings.models import Booking, BookingStatus
from kairos.core.constants import MAX_AVAILABILITY_QUERY_DAYS
from kairos.core.exceptions import NotFoundError, PolicyValidationError
from kairos.core.pagination import decode_cursor, encode_cursor, parse_limit
from kairos.core.views import KairosAPIView
from kairos.identity.authorization import AuthorizationService
from kairos.identity.models import AppUser

from .models import Resource
from .serializers import ResourceSerializer


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_boundary_datetime(value: str) -> datetime:
    """Accepts either a bare date ('2026-09-01', interpreted as UTC
    midnight, matching Spec v1.0 §5.7's query example) or a full ISO
    datetime. Raises ValueError on anything else.
    """
    parsed_date = parse_date(value)
    if parsed_date is not None:
        return datetime.combine(parsed_date, time.min, tzinfo=UTC)

    parsed_dt = parse_datetime(value)
    if parsed_dt is None:
        raise ValueError(f"{value!r} is not a valid date or datetime")
    if django_timezone.is_naive(parsed_dt):
        parsed_dt = django_timezone.make_aware(parsed_dt, UTC)
    return parsed_dt


class ResourceCollectionView(KairosAPIView):
    """GET /api/v1/resources (Spec v1.0 §5.14) — read-only; resource CRUD
    is Phase 19."""

    def get(self, request: Request) -> Response:
        user = cast(AppUser, request.user)
        try:
            limit = parse_limit(request.query_params.get("limit"))
        except ValueError as exc:
            raise PolicyValidationError("limit", str(exc)) from exc

        # PRD FR46 / SEC-06: a restricted resource must be ABSENT from list
        # results for a non-member, not merely 404 on direct access.
        qs = AuthorizationService.visible_resources_queryset(user)

        cursor_param = request.query_params.get("cursor")
        if cursor_param:
            try:
                cursor_name, cursor_id = decode_cursor(cursor_param)
            except ValueError as exc:
                raise PolicyValidationError("cursor", "malformed cursor") from exc
            qs = qs.filter(Q(name__gt=cursor_name) | Q(name=cursor_name, id__gt=cursor_id))

        rows = list(qs.order_by("name", "id")[: limit + 1])

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last.name, str(last.id))

        return Response(
            {
                "data": [ResourceSerializer(r).data for r in rows],
                "next_cursor": next_cursor,
            }
        )


class ResourceDetailView(KairosAPIView):
    """GET /api/v1/resources/{id} (Spec v1.0 §5.14)."""

    def get(self, request: Request, pk: uuid.UUID) -> Response:
        user = cast(AppUser, request.user)
        try:
            resource = Resource.objects.get(id=pk)
        except Resource.DoesNotExist as exc:
            raise NotFoundError from exc
        if not AuthorizationService.can_view_resource(user, resource):
            # 404, not 403 — SEC-06: existence itself is the secret for a
            # restricted resource's non-members.
            raise NotFoundError
        return Response(ResourceSerializer(resource).data)


class ResourceAvailabilityView(KairosAPIView):
    """GET /api/v1/resources/{id}/availability (Spec v1.0 §5.7). Advisory,
    not authoritative (PRD FR29) — the authoritative conflict decision only
    ever happens at the `booking` INSERT (RFC v1.0 §3).
    """

    def get(self, request: Request, pk: uuid.UUID) -> Response:
        user = cast(AppUser, request.user)
        try:
            resource = Resource.objects.get(id=pk)
        except Resource.DoesNotExist as exc:
            raise NotFoundError from exc
        if not AuthorizationService.can_view_resource(user, resource):
            # 404 — RFC v1.0 §8.2: "Restricted resources must not leak
            # existence through availability views either."
            raise NotFoundError

        from_param = request.query_params.get("from")
        to_param = request.query_params.get("to")
        if not from_param:
            raise PolicyValidationError("from", "is required")
        if not to_param:
            raise PolicyValidationError("to", "is required")

        try:
            from_dt = _parse_boundary_datetime(from_param)
        except ValueError as exc:
            raise PolicyValidationError("from", str(exc)) from exc
        try:
            to_dt = _parse_boundary_datetime(to_param)
        except ValueError as exc:
            raise PolicyValidationError("to", str(exc)) from exc

        if to_dt <= from_dt:
            raise PolicyValidationError("to", "must be after from")
        if to_dt - from_dt > timedelta(days=MAX_AVAILABILITY_QUERY_DAYS):
            raise PolicyValidationError(
                "to", f"range must not exceed {MAX_AVAILABILITY_QUERY_DAYS} days"
            )

        # Computed once, not per row — the N+1 guard RFC v1.0 §7.2 asks
        # for. Whether the requester can see identifying fields depends
        # only on (requester, resource), never on which booking.
        privileged = AuthorizationService.can_administer_resource(
            user, resource.id
        ) or AuthorizationService.is_operations(user)

        bookings = (
            Booking.objects.filter(
                resource=resource,
                status__in=[BookingStatus.CONFIRMED, BookingStatus.HELD],
                time_range__overlap=(from_dt, to_dt),
            )
            .select_related("user")
            .order_by("starts_at")
        )

        busy_blocks: list[dict[str, Any]] = []
        for booking in bookings:
            block: dict[str, Any] = {
                "start": _iso(booking.time_range.lower),
                "end": _iso(booking.time_range.upper),
            }
            # Held slots never reveal identifying fields, to anyone,
            # regardless of ownership/admin status — exposing which
            # booking a hold corresponds to would leak waitlist queue
            # state (Spec v1.0 §5.7). A confirmed booking reveals them
            # only to its owner or someone who administers the resource.
            reveal = booking.status != BookingStatus.HELD and (
                booking.user_id == user.id or privileged
            )
            if reveal:
                block["booking_id"] = str(booking.id)
                block["owner"] = {
                    "id": str(booking.user_id),
                    "display_name": booking.user.display_name,
                }
            busy_blocks.append(block)

        return Response(
            {
                "resource_id": str(resource.id),
                "timezone": resource.timezone,
                "range": {"from": _iso(from_dt), "to": _iso(to_dt)},
                "as_of": _iso(django_timezone.now()),
                # No replica exists yet (Phase 30) — always "primary".
                "data_freshness": "primary",
                "busy_blocks": busy_blocks,
            }
        )
