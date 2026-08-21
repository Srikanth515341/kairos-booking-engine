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

from kairos.core.exceptions import (
    NotFoundError,
    PolicyValidationError,
    UnacknowledgedConflictsError,
)
from kairos.core.idempotency import run_idempotent_recurring_confirm, run_idempotent_write
from kairos.core.models import AuditActorType, AuditLog
from kairos.core.pagination import decode_cursor, encode_cursor, parse_limit
from kairos.core.rate_limit import BookingCreatePrincipalThrottle, PerIPThrottle
from kairos.core.views import KairosAPIView
from kairos.core.views import request_id as _request_id
from kairos.identity.authorization import AuthorizationService
from kairos.identity.models import AppUser

from .models import Booking, BookingStatus, RecurringSeries
from .recurring_series import (
    confirm_recurring_series,
    decode_preview_token,
    preview_recurring_series,
)
from .serializers import (
    BookingCancelSerializer,
    BookingCreateSerializer,
    BookingEditSerializer,
    BookingResponseSerializer,
    RecurringSeriesCancelSerializer,
    RecurringSeriesConfirmSerializer,
    RecurringSeriesPreviewSerializer,
)
from .services import (
    BookingCancelRequest,
    BookingCreateRequest,
    BookingEditRequest,
    RecurringSeriesCancelRequest,
    cancel_booking,
    cancel_recurring_series,
    create_booking,
    edit_booking,
)

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
VALID_TIME_FILTERS = frozenset({"upcoming", "past", "all"})
VALID_STATUS_FILTERS = frozenset({BookingStatus.CONFIRMED, BookingStatus.CANCELLED})


def _require_idempotency_key(request: Request) -> uuid.UUID:
    # Required (Spec v1.0 §5.1/§5.5/§5.6, §7; PRD FR34). Checked before any
    # body validation on every mutating endpoint — a malformed/missing key
    # means there is nothing safe to retry, regardless of what the body
    # contains.
    header_value = request.headers.get(IDEMPOTENCY_KEY_HEADER)
    if not header_value:
        raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "header is required")
    try:
        return uuid.UUID(header_value)
    except ValueError as exc:
        raise PolicyValidationError(IDEMPOTENCY_KEY_HEADER, "must be a valid UUID") from exc


def _compute_changes(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, list[Any]]:
    """Field-level diff between an audit row's `before_state`/`after_state`
    snapshots (Spec v1.0 §5.3's `changes` shape). A full diff, not just a
    status transition: Phase 7's edit changes `time_range` while leaving
    `status` untouched entirely, so a history that only ever surfaced
    status changes would show NOTHING for an edit event — the one thing
    AUD-04's "full lifecycle reconstruction" actually needs to see. `None`
    stands in for a genuinely absent side (insert has no before, delete has
    no after), never a field's own null value, which the trigger stores as
    JSON `null` inside the snapshot dict itself.
    """
    before = before or {}
    after = after or {}
    changed = {}
    for field in before.keys() | after.keys():
        old_value = before.get(field)
        new_value = after.get(field)
        if old_value != new_value:
            changed[field] = [old_value, new_value]
    return changed


class BookingCollectionView(KairosAPIView):
    """POST /api/v1/bookings (Spec v1.0 §5.1) and GET /api/v1/bookings
    (Spec v1.0 §5.4) — the collection endpoint for both actions."""

    # Implementation Plan Phase 22; RFC v1.0 §8.2 — rate limiting is
    # scoped to booking CREATION specifically, not this whole collection
    # endpoint; both throttle classes internally no-op for anything but
    # POST (see kairos.core.rate_limit) rather than splitting GET/POST
    # into two view classes just to attach throttling to one verb.
    throttle_classes = [BookingCreatePrincipalThrottle, PerIPThrottle]

    def post(self, request: Request) -> Response:
        idempotency_key = _require_idempotency_key(request)

        # IsAuthenticated already guarantees request.user is a real AppUser,
        # never AnonymousUser/None — DRF's stubs type request.user against
        # Django's standard auth model, which AppUser deliberately isn't
        # (Phase 2). Resolved before the serializer runs (Phase 9) because
        # BookingCreateSerializer needs it in context to apply PRD FR46's
        # restricted-resource check during resource lookup.
        user = cast(AppUser, request.user)

        serializer = BookingCreateSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data

        request_id = _request_id(request)

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
            if not AuthorizationService.can_administer_resource(
                user, resource_id
            ) and not AuthorizationService.is_operations(user):
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
    """GET/PATCH /api/v1/bookings/{id} (Spec v1.0 §5.2, §5.5)."""

    def get(self, request: Request, pk: uuid.UUID) -> Response:
        user = cast(AppUser, request.user)
        try:
            booking = Booking.objects.select_related("resource").get(id=pk)
        except Booking.DoesNotExist as exc:
            raise NotFoundError from exc

        if not AuthorizationService.can_view_booking(user, booking):
            # 404, not 403 (Spec v1.0 §1) — protects existence, not only
            # the action, since an arbitrary booking id isn't otherwise
            # discoverable/browsable the way a resource id is.
            raise NotFoundError

        return Response(BookingResponseSerializer(booking).data)

    def patch(self, request: Request, pk: uuid.UUID) -> Response:
        idempotency_key = _require_idempotency_key(request)

        user = cast(AppUser, request.user)
        try:
            booking = Booking.objects.select_related("resource").get(id=pk)
        except Booking.DoesNotExist as exc:
            raise NotFoundError from exc

        # Owner only (Spec v1.0 §5.5) — unlike cancel, edit has no
        # resource-admin override. 404, not 403, matching GET's convention.
        if not AuthorizationService.can_edit_booking(user, booking):
            raise NotFoundError

        serializer = BookingEditSerializer(data=request.data, context={"booking": booking})
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data

        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            updated = edit_booking(
                BookingEditRequest(
                    booking=booking,
                    start=attrs["start"],
                    end=attrs["end"],
                    request_id=request_id,
                )
            )
            return status.HTTP_200_OK, dict(BookingResponseSerializer(updated).data)

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint=f"PATCH /api/v1/bookings/{pk}",
            # `booking_id` is folded into the fingerprinted body, not just
            # the `endpoint` label: compute_request_fingerprint() hashes
            # only the body, so without this, replaying the identical key
            # + identical {"start","end"} body against a DIFFERENT booking
            # would be misread as a replay of the first edit rather than a
            # genuinely different request.
            body={"booking_id": str(pk), **dict(request.data)},
            request_id=request_id,
            actor_type=AuditActorType.USER,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response


class BookingCancelView(KairosAPIView):
    """POST /api/v1/bookings/{id}/cancel (Spec v1.0 §5.6)."""

    def post(self, request: Request, pk: uuid.UUID) -> Response:
        idempotency_key = _require_idempotency_key(request)

        user = cast(AppUser, request.user)
        try:
            booking = Booking.objects.select_related("resource").get(id=pk)
        except Booking.DoesNotExist as exc:
            raise NotFoundError from exc

        allowed, is_override = AuthorizationService.can_cancel_booking(user, booking)
        if not allowed:
            # 404, not 403 (Spec v1.0 §1) — same object-level convention as
            # GET. Cancel's override is scoped-admin only (PRD FR47);
            # `operations` has no cancel authority per Spec v1.0 §5.6.
            raise NotFoundError

        serializer = BookingCancelSerializer(
            data=request.data, context={"is_owner": not is_override}
        )
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        # Self-cancel is a 'user' action; a scoped-admin override is an
        # 'admin' one (RFC v1.0 §12) — computed once, here, from the SAME
        # is_override the permission check above already resolved, not
        # re-derived downstream.
        actor_type = AuditActorType.ADMIN if is_override else AuditActorType.USER

        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            result = cancel_booking(
                BookingCancelRequest(
                    booking=booking,
                    actor=user,
                    actor_type=actor_type,
                    reason=reason,
                    request_id=request_id,
                )
            )
            return status.HTTP_200_OK, dict(BookingResponseSerializer(result.booking).data)

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint=f"POST /api/v1/bookings/{pk}/cancel",
            # Same reasoning as the edit endpoint above: `booking_id` must
            # be part of what gets fingerprinted, not only logged, or a
            # reused key with a coincidentally identical (or empty) reason
            # body would misread as a replay across two different bookings.
            body={"booking_id": str(pk), **dict(request.data)},
            request_id=request_id,
            actor_type=actor_type,
            reason=reason,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response


class BookingHistoryView(KairosAPIView):
    """GET /api/v1/bookings/{id}/history (Spec v1.0 §5.3; PRD FR42)."""

    def get(self, request: Request, pk: uuid.UUID) -> Response:
        user = cast(AppUser, request.user)
        try:
            booking = Booking.objects.select_related("resource").get(id=pk)
        except Booking.DoesNotExist as exc:
            raise NotFoundError from exc

        # Same permission model as GET (Spec v1.0 §5.3: "Permission: same
        # as 5.2") — owner, resource admin, or operations; 404 otherwise,
        # not 403, so a booking's existence isn't confirmed to someone
        # without access to it.
        if not AuthorizationService.can_view_booking(user, booking):
            raise NotFoundError

        events = list(
            AuditLog.objects.filter(entity_type="booking", entity_id=pk).order_by(
                "occurred_at", "id"
            )
        )

        # Resolved in ONE batched query, not per event (RFC v1.0 §7.2's
        # N+1 guard, same principle Phase 6's availability view already
        # applies) — display_name isn't stored on audit_log itself, only
        # actor_id, so it has to be looked up separately regardless.
        actor_ids = {e.actor_id for e in events if e.actor_id is not None}
        display_names = dict(
            AppUser.objects.filter(id__in=actor_ids).values_list("id", "display_name")
        )

        return Response(
            {
                "booking_id": str(pk),
                "events": [
                    {
                        "occurred_at": e.occurred_at.isoformat().replace("+00:00", "Z"),
                        "action": e.action,
                        "actor": {
                            "id": str(e.actor_id) if e.actor_id is not None else None,
                            "display_name": (
                                display_names.get(e.actor_id) if e.actor_id is not None else None
                            ),
                            "type": e.actor_type,
                        },
                        "reason": e.reason,
                        "request_id": e.request_id,
                        "changes": _compute_changes(e.before_state, e.after_state),
                    }
                    for e in events
                ],
            }
        )


class RecurringSeriesPreviewView(KairosAPIView):
    """POST /api/v1/bookings/recurring/preview (Spec v1.0 §5.8). No
    Idempotency-Key — this endpoint commits nothing, so there is no write
    outcome to protect against a retry duplicating."""

    def post(self, request: Request) -> Response:
        user = cast(AppUser, request.user)
        serializer = RecurringSeriesPreviewSerializer(data=request.data, context={"user": user})
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data

        result = preview_recurring_series(
            user=user,
            resource=attrs["resource"],
            timezone=attrs["timezone"],
            local_start_time=attrs["local_start_time"],
            local_end_time=attrs["local_end_time"],
            weekday=attrs["weekday"],
            series_start_date=attrs["series_start_date"],
            occurrence_count=attrs["occurrence_count"],
        )

        return Response(
            {
                "preview_token": result.token,
                "resource_id": str(result.resource_id),
                "tzdata_version": result.tzdata_version,
                "would_create": result.would_create,
                "conflicts": result.conflicts,
                "time_adjustments": result.time_adjustments,
            }
        )


class RecurringSeriesConfirmView(KairosAPIView):
    """POST /api/v1/bookings/recurring (Spec v1.0 §5.9)."""

    def post(self, request: Request) -> Response:
        idempotency_key = _require_idempotency_key(request)
        user = cast(AppUser, request.user)

        serializer = RecurringSeriesConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data

        # PreviewExpiredError (409) is deliberately not a validation_error
        # (400) — see RecurringSeriesConfirmSerializer's docstring.
        decoded = decode_preview_token(attrs["preview_token"], user)

        acknowledged_conflicts = set(attrs["acknowledged_conflicts"])
        acknowledged_adjustments = set(attrs["acknowledged_adjustments"])
        # PRD FR33: EVERY conflict and adjustment the preview reported must
        # be explicitly acknowledged, checked BEFORE the idempotency key is
        # ever claimed and before any occurrence is attempted — REC-02's
        # "zero bookings created" (the same "validate before claiming a
        # key" ordering Phase 4 established for single-booking policy
        # checks). A superset is fine; only a MISSING acknowledgment blocks.
        if (decoded.conflict_dates - acknowledged_conflicts) or (
            decoded.adjustment_dates - acknowledged_adjustments
        ):
            raise UnacknowledgedConflictsError

        request_id = _request_id(request)

        def perform_confirm() -> tuple[int, dict[str, Any]]:
            outcome = confirm_recurring_series(user=user, decoded=decoded, request_id=request_id)
            body = {
                "series_id": str(outcome.series_id),
                "created": outcome.created,
                "conflicts": outcome.conflicts,
            }
            return status.HTTP_207_MULTI_STATUS, body

        result = run_idempotent_recurring_confirm(
            user=user,
            key=idempotency_key,
            endpoint="POST /api/v1/bookings/recurring",
            body=dict(request.data),
            request_id=request_id,
            actor_type=AuditActorType.USER,
            perform_confirm=perform_confirm,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response


class RecurringSeriesCancelView(KairosAPIView):
    """POST /api/v1/recurring-series/{id}/cancel (Spec v1.0 §5.10)."""

    def post(self, request: Request, pk: uuid.UUID) -> Response:
        idempotency_key = _require_idempotency_key(request)

        user = cast(AppUser, request.user)
        try:
            series = RecurringSeries.objects.select_related("resource").get(id=pk)
        except RecurringSeries.DoesNotExist as exc:
            raise NotFoundError from exc

        allowed, is_override = AuthorizationService.can_cancel_series(user, series)
        if not allowed:
            # 404, not 403 — same object-level convention as booking cancel.
            raise NotFoundError

        serializer = RecurringSeriesCancelSerializer(
            data=request.data, context={"is_owner": not is_override}
        )
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"]

        actor_type = AuditActorType.ADMIN if is_override else AuditActorType.USER
        request_id = _request_id(request)

        def perform_write() -> tuple[int, dict[str, Any]]:
            result = cancel_recurring_series(
                RecurringSeriesCancelRequest(
                    series=series,
                    actor=user,
                    actor_type=actor_type,
                    reason=reason,
                    request_id=request_id,
                )
            )
            return status.HTTP_200_OK, {
                "series_id": str(series.id),
                "cancelled_booking_ids": [str(i) for i in result.cancelled_booking_ids],
                "occurrences_already_past": result.occurrences_already_past,
            }

        result = run_idempotent_write(
            user=user,
            key=idempotency_key,
            endpoint=f"POST /api/v1/recurring-series/{pk}/cancel",
            body={"series_id": str(pk), **dict(request.data)},
            request_id=request_id,
            actor_type=actor_type,
            perform_write=perform_write,
        )

        response = Response(result.response_body, status=result.response_status)
        if result.is_replay:
            response["Idempotent-Replay"] = "true"
        return response
