"""WL-01 — Two overlapping simultaneous cancellations cannot produce two
overlapping holds (Test Plan v1.0 §3, WL-01; Implementation Plan Phase 16).

Unlike CONC-01–05/HOLD-02, this exercises the REAL SERVICE-LAYER cascade
mechanism under genuine concurrency — two threads independently calling
`cancel_booking` (Python, not raw psycopg), each triggering its own
`transaction.on_commit()`-dispatched `create_offer_for_freed_range` via the
Celery-eager test setting — rather than raw SQL, because the thing this
test needs to prove safe is the CASCADE CODE PATH itself (hold creation
via `create_booking(status=HELD)`, retry-on-conflict), not the bare
constraint (already proven by CONC-01 and HOLD-02).

Ground truth reuses `count_overlapping_pairs` — the same reconciliation-
style query RFC v1.0 §14 describes and CONC-05 already uses — rather than
counting held rows or notifications, matching Test Plan v1.0's own
instruction: "Verified at the database, not by counting notifications; a
notification-layer bug could mask a real double-hold at the data layer."

Note: B1/B2's ranges here are ADJACENT, not literally overlapping with
EACH OTHER — two genuinely overlapping ranges cannot both be `status=
'confirmed'` simultaneously on one resource in the first place (the exact
guarantee this whole project exists to enforce), so Test Plan's "B1
(09:00-10:00) and B2 (09:30-10:30)" example times cannot describe two
simultaneously-live confirmed rows literally. "Overlapping" in the test's
own title describes the CANCELLATIONS executing concurrently (barrier-
released in time), not the bookings' ranges.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection as django_connection

from kairos.bookings.models import Booking
from kairos.bookings.services import BookingCancelRequest, cancel_booking
from kairos.core.models import AuditActorType
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from kairos.waitlist.models import WaitlistEntry, WaitlistOffer
from tests.concurrency.harness import count_overlapping_pairs, django_test_dsn

RUNS = 50


def _cancel_in_thread(
    barrier: threading.Barrier,
    booking_id: str,
    actor: AppUser,
    request_id: str,
    errors: list[BaseException],
) -> None:
    try:
        barrier.wait()
        booking = Booking.objects.get(id=booking_id)
        cancel_booking(
            BookingCancelRequest(
                booking=booking,
                actor=actor,
                actor_type=AuditActorType.USER,
                reason=None,
                request_id=request_id,
            )
        )
    except BaseException as exc:  # noqa: BLE001 — captured for the assertion below, not swallowed
        errors.append(exc)
    finally:
        django_connection.close()


@pytest.mark.django_db(transaction=True)
def test_wl_01_two_simultaneous_cancellations_never_produce_overlapping_holds(
    resource_and_user: dict[str, str],
) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    resource = Resource.objects.get(id=resource_id)
    owner = AppUser.objects.get(id=resource_and_user["user_id"])

    for run in range(1, RUNS + 1):
        w1 = AppUser.objects.create(email=f"wl01-w1-{run}@example.com", display_name="W1")
        w2 = AppUser.objects.create(email=f"wl01-w2-{run}@example.com", display_name="W2")

        b1_start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
        b1_end = b1_start + timedelta(hours=1)
        b2_start = b1_end  # adjacent, not overlapping — see module docstring
        b2_end = b2_start + timedelta(hours=1)

        b1 = Booking.objects.create(resource=resource, user=owner, time_range=(b1_start, b1_end))
        b2 = Booking.objects.create(resource=resource, user=owner, time_range=(b2_start, b2_end))

        WaitlistEntry.objects.create(
            resource=resource,
            user=w1,
            time_range=(b1_start + timedelta(minutes=15), b1_start + timedelta(minutes=45)),
        )
        WaitlistEntry.objects.create(
            resource=resource,
            user=w2,
            time_range=(b2_start + timedelta(minutes=15), b2_start + timedelta(minutes=45)),
        )

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        threads = [
            threading.Thread(
                target=_cancel_in_thread,
                args=(barrier, str(b1.id), owner, f"wl01-{run}-b1", errors),
            ),
            threading.Thread(
                target=_cancel_in_thread,
                args=(barrier, str(b2.id), owner, f"wl01-{run}-b2", errors),
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"run {run}: cancel_booking/cascade raised: {errors}"

        # Ground truth (RFC v1.0 §14's reconciliation shape): zero distinct
        # pairs of active (confirmed/held) rows overlap each other, for
        # this resource, full stop — the actual invariant the exclusion
        # constraint exists to guarantee, checked at the database directly.
        overlapping = count_overlapping_pairs(dsn, resource_id)
        assert overlapping == 0, f"run {run}: {overlapping} overlapping active row pairs found"

        # Both cascades should have produced exactly one hold each — not
        # required for safety, but a real regression here (e.g. eligibility
        # silently matching nothing) would otherwise pass this test for the
        # wrong reason (trivially zero overlaps because nothing happened).
        held_count = Booking.objects.filter(resource=resource, status="held").count()
        assert held_count == 2, f"run {run}: expected 2 holds created, found {held_count}"

        # Reset for the next run — offers before bookings (hold_booking is
        # RESTRICT), entries have no FK to booking at all.
        WaitlistOffer.objects.filter(resource=resource).delete()
        WaitlistEntry.objects.filter(resource=resource).delete()
        Booking.objects.filter(resource=resource).delete()
