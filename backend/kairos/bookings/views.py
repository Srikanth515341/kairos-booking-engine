from __future__ import annotations

import logging
from typing import cast

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from kairos.identity.authentication import StubUserIdAuthentication
from kairos.identity.models import AppUser

from .serializers import BookingCreateSerializer, BookingResponseSerializer
from .services import BookingCreateRequest, create_booking

logger = logging.getLogger(__name__)


class BookingCreateView(APIView):
    authentication_classes = [StubUserIdAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
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

        booking = create_booking(
            BookingCreateRequest(
                resource=attrs["resource"],
                user=user,
                start=attrs["start"],
                end=attrs["end"],
                request_id=request_id,
            )
        )
        return Response(BookingResponseSerializer(booking).data, status=status.HTTP_201_CREATED)
