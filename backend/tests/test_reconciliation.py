"""RECON-01, RECON-02, RECON-08 (Implementation Plan Phase 20; PRD v1.0
M2; RFC v1.0 §14). RECON-03/04 (end-to-end + alert text) and RECON-06
(background-job heartbeats) live in tests/test_admin_checks.py instead,
alongside the `GET /admin/checks/latest` endpoint they're expressed
through.

"The core problem" (Test Plan v1.0 §9's own words): under normal
operation the constraint makes a real overlap impossible, so injecting
one requires deliberately defeating the mechanism it backstops — done
ONLY against the isolated, disposable `kairos_test` database pytest
already owns, never staging or production (Test Plan v1.0's own explicit
instruction for RECON-01).
"""

from __future__ import annotations

import time as time_module
from datetime import time, timedelta

import pytest
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from kairos.bookings.models import Booking, BookingStatus, RecurringSeries
from kairos.core.reconciliation import find_overlapping_active_bookings
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

# Mirrors bookings/migrations/0002_exclusion_constraint.py exactly — see
# tests/test_schema_assertion.py's identical constants for why this is
# duplicated rather than imported from the migration module.
DROP_CONSTRAINT_SQL = "ALTER TABLE booking DROP CONSTRAINT no_overlapping_bookings;"
RESTORE_CONSTRAINT_SQL = """
ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        resource_id WITH =,
        time_range  WITH &&
    )
    WHERE (status IN ('confirmed', 'held'));
"""


@pytest.mark.django_db(transaction=True)
def test_recon_01_injected_violation_is_caught(
    app_user: AppUser, active_resource: Resource
) -> None:
    """`transaction=True` (real commits, not the default rollback-
    wrapped fixture): the audit trigger's pending AFTER DELETE event for
    `b2.delete()` (below) and the subsequent `ALTER TABLE ... ADD
    CONSTRAINT` conflict if both run inside ONE transaction — Postgres
    raises "cannot ALTER TABLE because it has pending trigger events."
    Real, separately-committed statements (this test's whole point is
    genuine DDL against a genuine schema state) sidestep that entirely,
    the same real-commit reasoning WL-01/WL-02 already needed for their
    own on_commit-triggered behavior (Phase 16).
    """
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    overlap_start = start + timedelta(minutes=30)
    overlap_end = overlap_start + timedelta(hours=1)

    with connection.cursor() as cur:
        cur.execute(DROP_CONSTRAINT_SQL)
    try:
        # Succeeds ONLY because the constraint is gone — this is the
        # deliberate defeat Test Plan v1.0 §9 describes as unavoidable
        # for testing a mechanism that otherwise makes its own input
        # impossible to produce.
        b1 = Booking.objects.create(
            resource=active_resource, user=app_user, time_range=(start, end)
        )
        b2 = Booking.objects.create(
            resource=active_resource, user=app_user, time_range=(overlap_start, overlap_end)
        )

        pairs = find_overlapping_active_bookings()
        assert len(pairs) == 1
        flagged_booking_ids = {pairs[0][0], pairs[0][1]}
        assert flagged_booking_ids == {b1.id, b2.id}
        assert pairs[0][2] == active_resource.id

        # Clean up before restoring: ADD CONSTRAINT validates every
        # EXISTING row, and these two would fail that validation,
        # producing a misleading "restore itself failed" error instead
        # of the real assertion this test is making.
        b2.delete()
    finally:
        with connection.cursor() as cur:
            cur.execute(RESTORE_CONSTRAINT_SQL)

    # Proves the test didn't accidentally alter the schema some other
    # way that coincidentally produced a passing reconciliation result —
    # the identical overlapping insert must now fail for real.
    with pytest.raises(DatabaseError) as exc_info:
        with transaction.atomic():
            Booking.objects.create(
                resource=active_resource, user=app_user, time_range=(overlap_start, overlap_end)
            )
    assert getattr(exc_info.value.__cause__, "sqlstate", None) == "23P01"


@pytest.mark.django_db
def test_recon_02_and_08_zero_false_positives_and_query_cost_on_realistic_data(
    app_user: AppUser,
) -> None:
    """RECON-02 (zero false positives against realistic, varied data)
    and RECON-08 (query cost within budget) share one dataset —
    building it is the expensive part of both tests, and RECON-08's
    entire point is timing the SAME query RECON-02 already needs to run.

    Scaled down from Test Plan v1.0's literal "hundreds of resources,
    thousands of bookings" for CI-tier runtime, the same reduction
    CONC-01's own CI-tier form already established (100 runs -> 10) —
    documented here, not silently substituted. Full-scale timing against
    PRD A1's real data volume is a staging-tier concern (Test Plan v1.0
    §13), not this test's job.
    """
    num_resources = 50
    bookings_per_resource = 20

    resources = Resource.objects.bulk_create(
        [
            Resource(
                name=f"Recon Room {i}",
                timezone="UTC",
                bookable_start_time=time(0, 0),
                bookable_end_time=time(23, 59),
                created_by=app_user,
            )
            for i in range(num_resources)
        ]
    )

    base = timezone.now() + timedelta(days=1)
    to_create = []
    for resource in resources:
        for j in range(bookings_per_resource):
            # Sequential, non-overlapping slots on their OWN resource —
            # different resources never compete (resource_id WITH =), so
            # this alone already guarantees no cross-resource overlap.
            slot_start = base + timedelta(days=j)
            to_create.append(
                Booking(
                    resource=resource,
                    user=app_user,
                    time_range=(slot_start, slot_start + timedelta(hours=1)),
                    status=BookingStatus.CONFIRMED,
                )
            )
    Booking.objects.bulk_create(to_create)

    # A recurring series' own materialized occurrences — genuinely
    # CONFIRMED rows linked via series_id, still non-overlapping.
    series_resource = resources[0]
    series = RecurringSeries.objects.create(
        resource=series_resource,
        created_by=app_user,
        timezone="UTC",
        local_start_time=time(6, 0),
        local_end_time=time(6, 30),
        weekday=0,
        series_start_date=(base + timedelta(days=100)).date(),
        occurrence_count=5,
        tzdata_version="2026.3",
        materialized_through=(base + timedelta(days=100)).date(),
    )
    for k in range(5):
        occ_start = base + timedelta(days=100 + 7 * k, hours=6)
        Booking.objects.create(
            resource=series_resource,
            user=app_user,
            series=series,
            time_range=(occ_start, occ_start + timedelta(minutes=30)),
        )

    # A HELD row, on its own untouched slot — the predicate this query
    # shares with the constraint covers 'held' too (PRD v1.0 FR21/RFC
    # v1.0 §10.1), so it must be represented (Test Plan v1.0's own
    # explicit instruction) without itself producing a false positive.
    held_start = base + timedelta(days=200)
    Booking.objects.create(
        resource=resources[1],
        user=app_user,
        time_range=(held_start, held_start + timedelta(hours=1)),
        status=BookingStatus.HELD,
        expires_at=timezone.now() + timedelta(minutes=15),
    )

    # A CANCELLED booking DELIBERATELY overlapping a CONFIRMED one on
    # the SAME resource — this is the actual false-positive risk RECON-02
    # exists to catch: a WHERE-clause bug that forgot to exclude
    # cancelled rows would flag this pair. Legal precisely because a
    # cancelled row sits outside `no_overlapping_bookings`'s own
    # predicate, so the database itself permits the overlap.
    conflict_target = to_create[0]
    # bulk_create() leaves time_range exactly as assigned in Python — a
    # plain tuple, not the Range object a fresh SELECT would return (the
    # same behavior BookingService.create_booking's own refresh_from_db
    # call works around elsewhere in this codebase).
    conflict_start, conflict_end = conflict_target.time_range
    Booking.objects.create(
        resource=conflict_target.resource,
        user=app_user,
        time_range=(conflict_start, conflict_end),
        status=BookingStatus.CANCELLED,
        cancelled_at=timezone.now(),
    )

    started = time_module.perf_counter()
    pairs = find_overlapping_active_bookings()
    elapsed_seconds = time_module.perf_counter() - started

    # RECON-02: a query returning zero against an EMPTY table proves
    # nearly nothing — this dataset is deliberately varied (multiple
    # statuses, a recurring series, a held row, a cancelled row that
    # WOULD overlap if the WHERE clause were wrong) so a real join/WHERE
    # bug has something to be exposed by.
    assert pairs == [], f"false positive(s) on realistic data: {pairs}"

    # RECON-08: generous CI-tier budget — this dataset (≈1000+ bookings)
    # is itself already a scale-down from PRD A1; a real production-scale
    # timing number is a staging-tier measurement (Test Plan v1.0 §13),
    # not asserted here as a hard production SLA.
    assert elapsed_seconds < 2.0, (
        f"reconciliation query took {elapsed_seconds:.3f}s against "
        f"{num_resources * bookings_per_resource} bookings — investigate before scaling up"
    )
