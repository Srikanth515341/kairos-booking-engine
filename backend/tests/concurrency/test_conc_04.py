"""CONC-04 — Edit-vs-edit race (Test Plan v1.0 §2, CONC-04).

Two DIFFERENT bookings, both simultaneously edited toward the SAME
contested target range. Same raw-SQL, barrier-released harness as
CONC-01/02/03/05, proving the constraint's behavior on two concurrent
UPDATEs racing each other, independent of the service/view layer.

10 runs for the CI tier (Test Plan v1.0 §13 CI tier).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from tests.concurrency.harness import (
    EXPECTED_NONSUCCESS_SQLSTATES,
    ClientOutcome,
    clear_bookings,
    count_active_overlapping,
    django_test_dsn,
    range_literal,
    run_concurrent,
)

RUNS = 10

# See test_conc_01.py's MAX_ROUND_ATTEMPTS comment for the full reasoning.
# This test's party count (2) is much smaller than CONC-01's 200, so the
# real zero-success risk here is lower — matching the value keeps the
# reasoning in one place rather than re-deriving a smaller number.
MAX_ROUND_ATTEMPTS = 10


def _update_action(booking_id: str, range_sql: str) -> Callable[[psycopg.Cursor], None]:
    def action(cur: psycopg.Cursor) -> None:
        cur.execute(
            "UPDATE booking SET time_range = %s::tstzrange WHERE id = %s",
            (range_sql, booking_id),
        )

    return action


@pytest.mark.django_db(transaction=True)
def test_conc_04_edit_vs_edit(resource_and_user: dict[str, str]) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]
    owner = AppUser.objects.get(id=user_id)
    resource = Resource.objects.get(id=resource_id)

    # Test Plan setup: "B1 at 09:00-10:00, B2 at 14:00-15:00. Both
    # simultaneously PATCHed toward 11:00-12:00."
    b1_start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    b1_end = b1_start + timedelta(hours=1)
    b2_start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    b2_end = b2_start + timedelta(hours=1)

    target_start = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)
    target_end = target_start + timedelta(hours=1)
    target_range_sql = range_literal(target_start, target_end)

    for run in range(1, RUNS + 1):
        successes: list[ClientOutcome] = []
        for attempt in range(1, MAX_ROUND_ATTEMPTS + 1):
            b1 = Booking.objects.create(
                resource=resource, user=owner, time_range=(b1_start, b1_end)
            )
            b2 = Booking.objects.create(
                resource=resource, user=owner, time_range=(b2_start, b2_end)
            )

            actions = [
                _update_action(str(b1.id), target_range_sql),
                _update_action(str(b2.id), target_range_sql),
            ]
            outcomes = run_concurrent(dsn, actions)
            edit_b1, edit_b2 = outcomes
            print(
                f"CONC-04 run {run}/{RUNS} attempt {attempt}: "
                f"b1={(edit_b1.success, edit_b1.sqlstate)} b2={(edit_b2.success, edit_b2.sqlstate)}"
            )

            failures = [o for o in outcomes if not o.success]
            unexplained = [o for o in failures if o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES]
            assert not unexplained, (
                f"run {run} attempt {attempt}: unexplained SQLSTATEs: "
                f"{sorted({o.sqlstate for o in unexplained})}"
            )

            successes = [o for o in outcomes if o.success]
            # Safety — zero tolerance, checked on every attempt, never
            # retried: exactly one of the two edits may win the target.
            assert len(successes) <= 1, (
                f"run {run} attempt {attempt}: SAFETY VIOLATION — "
                f"{len(successes)} simultaneous edit successes"
            )

            if successes:
                break
            clear_bookings(dsn, resource_id)
        else:
            pytest.fail(f"run {run}: zero successes across {MAX_ROUND_ATTEMPTS} attempts")

        # The loser is verified unchanged at its ORIGINAL range — never
        # left ambiguous or partially updated (Test Plan CONC-04
        # assertion) — read straight from the database.
        b1.refresh_from_db()
        b2.refresh_from_db()
        if not edit_b1.success:
            assert b1.time_range.lower == b1_start and b1.time_range.upper == b1_end, (
                f"run {run}: B1 lost but its range changed anyway: {b1.time_range}"
            )
        if not edit_b2.success:
            assert b2.time_range.lower == b2_start and b2.time_range.upper == b2_end, (
                f"run {run}: B2 lost but its range changed anyway: {b2.time_range}"
            )

        ground_truth = count_active_overlapping(dsn, resource_id, target_range_sql)
        assert ground_truth == 1, f"run {run}: ground truth = {ground_truth}, expected 1"

        clear_bookings(dsn, resource_id)
