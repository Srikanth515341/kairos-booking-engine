from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, cast

from django.db.models import Q
from django.utils import timezone as django_timezone
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response

from kairos.core.exceptions import NotFoundError, PolicyValidationError
from kairos.core.idempotency import run_idempotent_write
from kairos.core.pagination import decode_cursor, encode_cursor, parse_limit
from kairos.core.views import KairosAPIView
from kairos.identity.authorization import is_operations, is_resource_admin
from kairos.identity.models import AppUser

from .models import Booking, BookingStatus
from .serializers import BookingCreateSerializer, BookingResponseSerializer
from .services import BookingCreateRequest, create_booking

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
VALID_TIME_FILTERS = frozenset({"upcoming", "past", "all"})
VALID_STATUS_FILTERS = frozenset({BookingStatus.CONFIRMED, BookingStatus.CANCELLED})


class BookingCollectionView(KairosAPIView):
    """POST /api/v1/bookings (Spec v1.0 §5.1) and GET /api/v1/bookings
    (Spec v1.0 §5.4) — the collection endpoint for both actions."""

    def post(self, request: Request) -> Response:
        # Required (Spec v1.0 §5.1, §7; PRD FR34). Checked before body
        # validation — a malformed/missing key means there is nothing safe
        # to retry, regardless of what the body contains.
        header_value = request.headers.get(IDEMPOTENCY_KEY_HEADER)
        if not header_value:
            raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "header is required")
        try:
            idempotency_key = uuid.UUID(header_value)
        except ValueError as exc:
            raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "must be a valid UUID") from exc

        serializer = BookingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data

        django_request = getattr(request, "_request", request)
        request_id = getattr(django_request, "request_id", "") or ""

        # IsAuthenticated already guarantees request.user is a real AppUser,
        # never AnonymousUser/None — DRF's stubs type request.user against
        # Django's standard auth model, which AppUser deliberately isn't
        # (Phase 2).
        user = cast(AppUser, request.user)

        def perform_write() -> tuple[int, dict[str, Any]]:
            booking = create_booking(
                BookingCreateRequest(
                    resource=attrs["resource"],
                    user=user,
                    start=attrs["start"],
                    end=attrs["end"],
                    request_id=request_id,
                )
            )
            return status.HTTP_201_CREATED, dict(BookingResponseSerializer(booking).data)

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint="POST /api/v1/bookings",
            body=dict(request.data),
            request_id=request_id,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response

    def get(self, request: Request) -> Response:
        user = cast(AppUser, request.user)
        params = request.query_params

        # Held rows are reservations, not bookings (Spec v1.0 §5.4) — never
        # returned by this endpoint regardless of any other filter.
        qs = Booking.objects.exclude(status=BookingStatus.HELD)

        resource_id_param = params.get("resource_id")
        if resource_id_param:
            try:
                resource_id = uuid.UUID(resource_id_param)
            except ValueError as exc:
                raise PolicyValidationError("resource_id", "must be a valid UUID") from exc
            # The resource is browsable, so its existence isn't secret —
            # 403, not 404 (Spec v1.0 §1 convention).
            if not (is_resource_admin(user, resource_id) or is_operations(user)):
                raise drf_exceptions.PermissionDenied()
            qs = qs.filter(resource_id=resource_id)
        else:
            qs = qs.filter(user=user)

        status_param = params.get("status")
        if status_param is not None:
            if status_param not in VALID_STATUS_FILTERS:
                raise PolicyValidationError(
                    "status", f"must be one of {sorted(VALID_STATUS_FILTERS)}"
                )
            qs = qs.filter(status=status_param)

        time_param = params.get("time", "all")
        if time_param not in VALID_TIME_FILTERS:
            raise PolicyValidationError("time", f"must be one of {sorted(VALID_TIME_FILTERS)}")

        descending = time_param == "past"
        now = django_timezone.now()
        if time_param == "upcoming":
            qs = qs.filter(starts_at__gte=now)
        elif time_param == "past":
            qs = qs.filter(starts_at__lt=now)

        try:
            limit = parse_limit(params.get("limit"))
        except ValueError as exc:
            raise PolicyValidationError("limit", str(exc)) from exc

        cursor_param = params.get("cursor")
        if cursor_param:
            try:
                cursor_sort_key, cursor_id = decode_cursor(cursor_param)
                cursor_starts_at = datetime.fromisoformat(cursor_sort_key)
            except ValueError as exc:
                raise PolicyValidationError("cursor", "malformed cursor") from exc
            if descending:
                qs = qs.filter(
                    Q(starts_at__lt=cursor_starts_at)
                    | Q(starts_at=cursor_starts_at, id__gt=cursor_id)
                )
            else:
                qs = qs.filter(
                    Q(starts_at__gt=cursor_starts_at)
                    | Q(starts_at=cursor_starts_at, id__gt=cursor_id)
                )

        order_field = "-starts_at" if descending else "starts_at"
        rows = list(qs.order_by(order_field, "id")[: limit + 1])

        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last.starts_at.isoformat(), str(last.id))

        return Response(
            {
                "data": [BookingResponseSerializer(b).data for b in rows],
                "next_cursor": next_cursor,
            }
        )


class BookingDetailView(KairosAPIView):
    """GET /api/v1/bookings/{id} (Spec v1.0 §5.2)."""

    def get(self, request: Request, pk: uuid.UUID) -> Response:
        user = cast(AppUser, request.user)
        try:
            booking = Booking.objects.select_related("resource").get(id=pk)
        except Booking.DoesNotExist as exc:
            raise NotFoundError from exc

        is_owner = booking.user_id == user.id
        if not (is_owner or is_resource_admin(user, booking.resource_id) or is_operations(user)):
            # 404, not 403 (Spec v1.0 §1) — protects existence, not only
            # the action, since an arbitrary booking id isn't otherwise
            # discoverable/browsable the way a resource id is.
            raise NotFoundError

        return Response(BookingResponseSerializer(booking).data)
