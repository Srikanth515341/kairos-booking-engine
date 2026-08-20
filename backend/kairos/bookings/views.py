from __future__ import annotations

import logging
import uuid
from typing import Any, cast

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from kairos.core.exceptions import PolicyValidationError
from kairos.core.idempotency import run_idempotent_write
from kairos.identity.authentication import StubUserIdAuthentication
from kairos.identity.models import AppUser

from .serializers import BookingCreateSerializer, BookingResponseSerializer
from .services import BookingCreateRequest, create_booking

logger = logging.getLogger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


class BookingCreateView(APIView):
    authentication_classes = [StubUserIdAuthentication]
    permission_classes = [IsAuthenticated]

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
