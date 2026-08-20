"""All booking write logic lives here, never in views (RFC v1.0 §17). Every
future write path — an admin override, a future bulk-import script — gets
consistent SQLSTATE translation, session settings, and audit attribution for
free, rather than reimplementing them per code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import DatabaseError, connection, transaction
from django.db.backends.utils import CursorWrapper

from kairos.bookings.models import Booking, BookingStatus
from kairos.core.constants import (
    WRITE_PATH_IDLE_IN_TRANSACTION_TIMEOUT,
    WRITE_PATH_LOCK_TIMEOUT,
    WRITE_PATH_STATEMENT_TIMEOUT,
)
from kairos.core.exceptions import ServiceUnavailableError, SlotUnavailableError
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

logger = logging.getLogger(__name__)

# SQLSTATE 23P01 (exclusion_violation) — the constraint did its job; someone
# else's row already occupies the range (Spec v1.0 §6.1). Checked by this
# SPECIFIC code, never a generic exception-type catch, which would conflate
# a real conflict with an unrelated integrity error such as a foreign-key
# violation.
EXCLUSION_VIOLATION = "23P01"

# SQLSTATEs where the outcome is unknown, not decided (Spec v1.0 §5.1) — all
# three retryable and mapped identically to 503 + Retry-After, never a bare
# 500 or 409. 55P03/40P01 are RFC v1.0 §7.1's documented pair; 57014 is
# Phase 3's empirical addition (CLAUDE.md): under heavy contention, most
# losers exceed statement_timeout via cumulative pileup rather than any
# single lock_timeout wait.
LOCK_TIMEOUT = "55P03"
DEADLOCK_DETECTED = "40P01"
QUERY_CANCELED = "57014"
RETRYABLE_SQLSTATES = frozenset({LOCK_TIMEOUT, DEADLOCK_DETECTED, QUERY_CANCELED})


@dataclass(frozen=True)
class BookingCreateRequest:
    resource: Resource
    user: AppUser
    start: datetime
    end: datetime
    request_id: str


def apply_write_path_session_settings(
    cursor: CursorWrapper, *, actor_id: str, request_id: str
) -> None:
    """RFC v1.0 §7.1 (write-path timeouts) and §12 (audit actor
    propagation), applied per transaction via `set_config(..., true)` — the
    functional equivalent of `SET LOCAL`, except it's a plain function call
    that safely accepts bind parameters. The `SET` statement itself doesn't
    reliably accept them, which matters here because actor_id and
    request_id are request-influenced values; interpolating those directly
    into SQL text would be an injection risk that `set_config` avoids.
    """
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", [WRITE_PATH_LOCK_TIMEOUT])
    cursor.execute(
        "SELECT set_config('statement_timeout', %s, true)", [WRITE_PATH_STATEMENT_TIMEOUT]
    )
    cursor.execute(
        "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
        [WRITE_PATH_IDLE_IN_TRANSACTION_TIMEOUT],
    )
    cursor.execute("SELECT set_config('app.actor_id', %s, true)", [actor_id])
    cursor.execute("SELECT set_config('app.actor_type', 'user', true)")
    cursor.execute("SELECT set_config('app.request_id', %s, true)", [request_id])


def create_booking(req: BookingCreateRequest) -> Booking:
    """The write. No availability check precedes it — that is the entire
    point of RFC v1.0 §3: the EXCLUDE constraint IS the check, and no window
    exists between "looks free" and "is free" for a second writer to land
    in. Idempotency (Phase 5) and hold reclamation (Phase 17) are not yet
    part of this transaction — both are documented, temporary gaps.
    """
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                apply_write_path_session_settings(
                    cursor, actor_id=str(req.user.id), request_id=req.request_id
                )
            booking = Booking.objects.create(
                resource=req.resource,
                user=req.user,
                time_range=(req.start, req.end),
                status=BookingStatus.CONFIRMED,
            )
            # .create() leaves fields exactly as assigned in Python — here,
            # `time_range` as the plain tuple passed in, not the Range
            # object a fresh SELECT returns. Refresh so the returned
            # instance reflects true DB-computed state (also picks up the
            # generated `starts_at` and the DB-stored `created_at`
            # precision) rather than only what Python assigned.
            booking.refresh_from_db()
    except DatabaseError as exc:
        sqlstate = getattr(exc.__cause__, "sqlstate", None)
        log_context = {
            "request_id": req.request_id,
            "user_id": str(req.user.id),
            "resource_id": str(req.resource.id),
        }

        if sqlstate == EXCLUSION_VIOLATION:
            logger.info("booking_conflict", extra={**log_context, "outcome": "slot_unavailable"})
            raise SlotUnavailableError from exc

        if sqlstate in RETRYABLE_SQLSTATES:
            logger.warning(
                "booking_retryable_failure",
                extra={**log_context, "outcome": "service_unavailable", "sqlstate": sqlstate},
            )
            raise ServiceUnavailableError from exc

        raise

    logger.info(
        "booking_created",
        extra={
            "request_id": req.request_id,
            "user_id": str(req.user.id),
            "resource_id": str(req.resource.id),
            "booking_id": str(booking.id),
            "outcome": "success",
        },
    )
    return booking
