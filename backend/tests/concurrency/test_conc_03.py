"""CONC-03 — Edit-vs-create race (Test Plan v1.0 §2, CONC-03; PRD FR5).

Same raw-SQL, barrier-released harness as CONC-01/02/05 — this proves the
`no_overlapping_bookings` EXCLUDE constraint itself handles a concurrent
UPDATE (an edit moving a booking's range) racing an INSERT (a create)
correctly, independent of the service/view layer above it.

10 runs for the CI tier (Test Plan v1.0 §13 CI tier).
"""

from __future__ import annotations

import uuid
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


def _insert_action(
    booking_id: str, resource_id: str, user_id: str, range_sql: str
) -> Callable[[psycopg.Cursor], None]:
    def action(cur: psycopg.Cursor) -> None:
        cur.execute(
            "INSERT INTO booking (id, resource_id, user_id, time_range, status, created_at) "
            "VALUES (%s, %s, %s, %s::tstzrange, 'confirmed', now())",
            (booking_id, resource_id, user_id, range_sql),
        )

    return action


def _update_action(booking_id: str, range_sql: str) -> Callable[[psycopg.Cursor], None]:
    def action(cur: psycopg.Cursor) -> None:
        cur.execute(
            "UPDATE booking SET time_range = %s::tstzrange WHERE id = %s",
            (range_sql, booking_id),
        )

    return action


@pytest.mark.django_db(transaction=True)
def test_conc_03_edit_vs_create(resource_and_user: dict[str, str]) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]
    owner = AppUser.objects.get(id=user_id)
    resource = Resource.objects.get(id=resource_id)

    # B1's own starting range (Test Plan setup: "B1 confirmed at
    # 14:00-15:00") — never contested directly, just where the edit
    # starts from, so the loser's "unchanged" assertion has a fixed point
    # of comparison.
    b1_start = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    b1_end = b1_start + timedelta(hours=1)

    # The contested target both the edit and the create race for.
    target_start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    target_end = target_start + timedelta(hours=1)
    target_range_sql = range_literal(target_start, target_end)

    for run in range(1, RUNS + 1):
        successes: list[ClientOutcome] = []
        for attempt in range(1, MAX_ROUND_ATTEMPTS + 1):
            b1 = Booking.objects.create(
                resource=resource, user=owner, time_range=(b1_start, b1_end)
            )

            actions = [
                _update_action(str(b1.id), target_range_sql),
                _insert_action(str(uuid.uuid4()), resource_id, user_id, target_range_sql),
            ]
            outcomes = run_concurrent(dsn, actions)
            edit_outcome, create_outcome = outcomes
            print(
                f"CONC-03 run {run}/{RUNS} attempt {attempt}: "
                f"edit={(edit_outcome.success, edit_outcome.sqlstate)} "
                f"create={(create_outcome.success, create_outcome.sqlstate)}"
            )

            failures = [o for o in outcomes if not o.success]
            unexplained = [o for o in failures if o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES]
            assert not unexplained, (
                f"run {run} attempt {attempt}: unexplained SQLSTATEs: "
                f"{sorted({o.sqlstate for o in unexplained})}"
            )

            successes = [o for o in outcomes if o.success]
            # Safety — zero tolerance, checked on every attempt, never
            # retried: exactly one of {edit, create} may win the range.
            assert len(successes) <= 1, (
                f"run {run} attempt {attempt}: SAFETY VIOLATION — "
                f"{len(successes)} of {{edit, create}} succeeded simultaneously"
            )

            if successes:
                break
            clear_bookings(dsn, resource_id)
        else:
            pytest.fail(f"run {run}: zero successes across {MAX_ROUND_ATTEMPTS} attempts")

        if not edit_outcome.success:
            # The loser must not have partially applied (RFC v1.0 §3; PRD
            # FR5) — B1's row is verified unchanged at its ORIGINAL range,
            # read straight from the database, not inferred from the
            # response.
            b1.refresh_from_db()
            assert b1.time_range.lower == b1_start, (
                f"run {run}: edit lost but B1's range changed anyway: {b1.time_range}"
            )
            assert b1.time_range.upper == b1_end

        ground_truth = count_active_overlapping(dsn, resource_id, target_range_sql)
        assert ground_truth == 1, f"run {run}: ground truth = {ground_truth}, expected 1"

        clear_bookings(dsn, resource_id)
