"""BookingService — session settings, and SQLSTATE-to-domain-exception
translation for all four codes the write path can see (Implementation Plan
Phase 4 DoD).

23P01 is exercised end-to-end already by test_booking_exclusion_smoke.py
(Phase 2) and the CONC suite (Phase 3). 55P03 is forced here genuinely, via
a real held row on an independent connection — the same technique the
concurrency harness uses. 40P01 and 57014 are forced by simulation at the
Booking.objects.create() boundary: naturally reproducing a deadlock or a
statement-timeout pileup on demand isn't controllable (Phase 3's CONC-01
needed 200 barrier-released clients and still only saw them some of the
time) — what Phase 4 actually needs to prove is that the SERVICE LAYER
translates whichever SQLSTATE occurs into the right outcome, which a direct
simulation proves precisely and deterministically. Genuine reproduction
under real concurrency remains the CONC suite's job.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import psycopg
import pytest
from django.db import OperationalError, connection, transaction
from django.utils import timezone

from kairos.bookings import services
from kairos.bookings.models import Booking
from kairos.bookings.services import BookingCreateRequest, create_booking
from kairos.core.exceptions import ServiceUnavailableError, SlotUnavailableError
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from tests.concurrency.harness import django_test_dsn, range_literal


@pytest.mark.django_db
def test_write_path_session_settings_applied() -> None:
    """DoD: 'a test asserts SHOW lock_timeout returns 3s inside the write
    transaction.'"""
    with transaction.atomic():
        with connection.cursor() as cursor:
            services.apply_write_path_session_settings(
                cursor, actor_id=str(uuid.uuid4()), request_id="req-session-settings"
            )
            cursor.execute("SHOW lock_timeout")
            assert cursor.fetchone()[0] == "3s"
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()[0] == "10s"
            cursor.execute("SHOW idle_in_transaction_session_timeout")
            assert cursor.fetchone()[0] == "30s"


class _FakeDeadlockCause(Exception):
    sqlstate = "40P01"


class _FakeStatementTimeoutCause(Exception):
    sqlstate = "57014"


@pytest.mark.django_db
def test_deadlock_maps_to_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, app_user: AppUser, active_resource: Resource
) -> None:
    def _raise_deadlock(*args: object, **kwargs: object) -> Booking:
        raise OperationalError("simulated deadlock") from _FakeDeadlockCause()

    monkeypatch.setattr(Booking.objects, "create", _raise_deadlock)

    start = timezone.now() + timedelta(hours=1)
    with pytest.raises(ServiceUnavailableError):
        create_booking(
            BookingCreateRequest(
                resource=active_resource,
                user=app_user,
                start=start,
                end=start + timedelta(hours=1),
                request_id="req-deadlock",
            )
        )


@pytest.mark.django_db
def test_statement_timeout_maps_to_service_unavailable(
    monkeypatch: pytest.MonkeyPatch, app_user: AppUser, active_resource: Resource
) -> None:
    def _raise_statement_timeout(*args: object, **kwargs: object) -> Booking:
        raise OperationalError("simulated statement timeout") from _FakeStatementTimeoutCause()

    monkeypatch.setattr(Booking.objects, "create", _raise_statement_timeout)

    start = timezone.now() + timedelta(hours=1)
    with pytest.raises(ServiceUnavailableError):
        create_booking(
            BookingCreateRequest(
                resource=active_resource,
                user=app_user,
                start=start,
                end=start + timedelta(hours=1),
                request_id="req-statement-timeout",
            )
        )


@pytest.mark.django_db(transaction=True)
def test_lock_timeout_maps_to_service_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Genuine reproduction: a real conflicting row held open, uncommitted,
    on an independent connection — create_booking()'s own insert has to
    wait, and with lock_timeout forced very short, it times out for real.
    """
    monkeypatch.setattr(services, "WRITE_PATH_LOCK_TIMEOUT", "200ms")

    owner = AppUser.objects.create(email="lock-timeout@example.com", display_name="Owner")
    resource = Resource.objects.create(
        name="Lock Timeout Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59, 59),
        created_by=owner,
    )

    start: datetime = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)

    blocking_conn = psycopg.connect(django_test_dsn())
    blocking_conn.autocommit = False
    try:
        with blocking_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO booking (id, resource_id, user_id, time_range, status, "
                "created_at) VALUES (%s, %s, %s, %s::tstzrange, 'confirmed', now())",
                (str(uuid.uuid4()), str(resource.id), str(owner.id), range_literal(start, end)),
            )
            # Deliberately not committed — the row stays uncommitted for the
            # duration of this test.

            with pytest.raises(ServiceUnavailableError):
                create_booking(
                    BookingCreateRequest(
                        resource=resource,
                        user=owner,
                        start=start,
                        end=end,
                        request_id="req-lock-timeout",
                    )
                )
    finally:
        blocking_conn.rollback()
        blocking_conn.close()


@pytest.mark.django_db
def test_exclusion_violation_maps_to_slot_unavailable(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    create_booking(
        BookingCreateRequest(
            resource=active_resource,
            user=app_user,
            start=start,
            end=end,
            request_id="req-first",
        )
    )

    with pytest.raises(SlotUnavailableError):
        create_booking(
            BookingCreateRequest(
                resource=active_resource,
                user=app_user,
                start=start,
                end=end,
                request_id="req-conflict",
            )
        )
