"""CONC-02 — Partial-overlap contention (Test Plan v1.0 §2, CONC-02).

10 runs each for the CI tier (Implementation Plan Phase 3 scope; Test Plan
§13 CI tier), covering both the plain two-range case and the five-way
chained-overlap extension that verifies the constraint catches transitive,
not merely pairwise, overlap.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.concurrency.harness import (
    EXPECTED_NONSUCCESS_SQLSTATES,
    ClientOutcome,
    clear_bookings,
    count_overlapping_pairs,
    django_test_dsn,
    range_literal,
    run_concurrent,
)

RUNS = 10

# See test_conc_01.py's MAX_ROUND_ATTEMPTS comment: a round can rarely
# produce zero successes under heavy contention (every competitor entangled
# in the same documented timeout/deadlock pileup), which is a liveness
# characteristic, not a safety violation. Retried only when zero succeed;
# more than the expected count fails immediately, never retried.
MAX_ROUND_ATTEMPTS = 3


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


def _assert_no_unexplained_failures(outcomes: Sequence[ClientOutcome], run: int) -> None:
    failures = [o for o in outcomes if not o.success]
    unexplained = [o for o in failures if o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES]
    assert not unexplained, (
        f"run {run}: unexplained SQLSTATEs: {sorted({o.sqlstate for o in unexplained})}"
    )


@pytest.mark.django_db(transaction=True)
def test_conc_02_partial_overlap_contention(resource_and_user: dict[str, str]) -> None:
    """Two overlapping ranges (09:00-10:00 vs 09:30-10:30). Exactly one
    succeeds every run — the two candidates fully conflict with each other,
    so this reduces to the same two-party race CONC-01 already proves."""
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]

    base = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    range_a = range_literal(base, base + timedelta(hours=1))
    range_b = range_literal(base + timedelta(minutes=30), base + timedelta(hours=1, minutes=30))

    for run in range(1, RUNS + 1):
        successes: list[ClientOutcome] = []
        for attempt in range(1, MAX_ROUND_ATTEMPTS + 1):
            actions = [
                _insert_action(str(uuid.uuid4()), resource_id, user_id, range_a),
                _insert_action(str(uuid.uuid4()), resource_id, user_id, range_b),
            ]
            outcomes = run_concurrent(dsn, actions)
            print(
                f"CONC-02 run {run}/{RUNS} attempt {attempt}: "
                f"{[(o.success, o.sqlstate) for o in outcomes]}"
            )

            _assert_no_unexplained_failures(outcomes, run)
            successes = [o for o in outcomes if o.success]
            assert len(successes) <= 1, (
                f"run {run} attempt {attempt}: SAFETY VIOLATION — "
                f"{len(successes)} simultaneous successes"
            )

            if successes:
                break
            clear_bookings(dsn, resource_id)
        else:
            pytest.fail(f"run {run}: zero successes across {MAX_ROUND_ATTEMPTS} attempts")

        assert len(successes) == 1, f"run {run}: expected exactly 1 success, got {len(successes)}"

        overlapping_pairs = count_overlapping_pairs(dsn, resource_id)
        assert overlapping_pairs == 0, (
            f"run {run}: {overlapping_pairs} overlapping active pair(s) found"
        )

        clear_bookings(dsn, resource_id)


@pytest.mark.django_db(transaction=True)
def test_conc_02_chained_overlap_contention(resource_and_user: dict[str, str]) -> None:
    """Five ranges, each offset 10 minutes from the last, 30-minute
    duration: adjacent ranges overlap, but ranges far enough apart in the
    chain don't overlap each other directly (e.g. range 0 [00:00-00:30) and
    range 3 [00:30-01:00) are adjacent, not overlapping). This is what makes
    it a genuine transitive-overlap case rather than "5-way CONC-01": since
    not every pair conflicts, more than one insert can legitimately commit
    in the same run. The correctness property under test isn't "exactly one
    succeeds" — it's that whatever set of rows *does* end up active never
    contains an overlapping pair (RFC v1.0 §14's reconciliation query),
    which the exclusion constraint enforces regardless of how many
    non-conflicting winners there are.
    """
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]

    base = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    ranges = [
        range_literal(base + timedelta(minutes=10 * i), base + timedelta(minutes=10 * i + 30))
        for i in range(5)
    ]

    for run in range(1, RUNS + 1):
        successes: list[ClientOutcome] = []
        for attempt in range(1, MAX_ROUND_ATTEMPTS + 1):
            actions = [_insert_action(str(uuid.uuid4()), resource_id, user_id, r) for r in ranges]
            outcomes = run_concurrent(dsn, actions)
            successes = [o for o in outcomes if o.success]
            print(
                f"CONC-02 chained run {run}/{RUNS} attempt {attempt}: "
                f"successes={len(successes)}/5 {[(o.success, o.sqlstate) for o in outcomes]}"
            )

            _assert_no_unexplained_failures(outcomes, run)
            assert len(successes) <= 5, (
                f"run {run} attempt {attempt}: {len(successes)} successes exceeds the "
                "5 candidates offered"
            )

            if successes:
                overlapping_pairs = count_overlapping_pairs(dsn, resource_id)
                assert overlapping_pairs == 0, (
                    f"run {run} attempt {attempt}: {overlapping_pairs} overlapping active "
                    "pair(s) found — the constraint failed to catch a transitive "
                    "(non-adjacent-in-time-order) overlap"
                )
                break
            clear_bookings(dsn, resource_id)
        else:
            pytest.fail(f"run {run}: zero successes across {MAX_ROUND_ATTEMPTS} attempts")

        clear_bookings(dsn, resource_id)
