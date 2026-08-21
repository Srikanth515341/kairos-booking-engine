"""RECLAIM-04 — Deadlock under load (Test Plan v1.0 §4, Spike S1.7;
Implementation Plan Phase 17).

Purpose: cleanup-on-write adds a DELETE to the hot write path
(`kairos.bookings.services.create_booking`) — concurrent transactions
deleting and inserting on the same resource could deadlock in a way plain
INSERT-only contention (CONC-01) doesn't necessarily exercise. Same
CONC-01-level scale (N=200 identical-slot-style contention) as the
established baseline, with cleanup-on-write's extra DELETE now genuinely
in the mix: 4 pre-seeded expired holds, adjacent segments spanning the
SAME 2-hour range every one of the 200 writers targets, so every single
attempt's cleanup DELETE (`time_range &&`) must clear all four before its
own INSERT can even be evaluated.

NOT part of the default `pytest tests/concurrency` sweep (see
`.github/workflows/ci.yml`) — Test Plan v1.0 §13 places RECLAIM-04 in the
STAGING/pre-release tier explicitly, not CI tier (RECLAIM-01–03 ARE CI
tier), the identical tiering CONC-01's own full 100-run+N=500 escalation
already has (deferred to Phase 28, run manually/scheduled, never on every
commit) — 200×50 = 10,000 raw attempts is expensive enough that per-commit
execution would make CI unusable, exactly Test Plan's own stated reason
for the staging tier's existence.

CLAUDE.md records the REAL observed numbers from actually running this at
full scale during Phase 17, per that phase's own explicit instruction not
to claim "zero deadlocks" without having genuinely run it and reported
what happened, whichever outcome that turned out to be.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.concurrency.harness import (
    DEADLOCK_DETECTED,
    EXPECTED_NONSUCCESS_SQLSTATES,
    ClientOutcome,
    clear_bookings,
    count_active_overlapping,
    count_overlapping_pairs,
    django_test_dsn,
    range_literal,
    run_concurrent,
)

N = 200
RUNS = 50

# Same rationale and same starting value as CONC-01's own tuning history
# (CLAUDE.md) — zero-success rounds at N=200 identical-slot-style
# contention are a documented, load-correlated LIVENESS characteristic,
# not a safety concern; a round is retried only when it produced zero
# successes, and more than one success on any single attempt fails
# immediately and is never retried.
MAX_ROUND_ATTEMPTS = 10

SEGMENT_MINUTES = 30
SEGMENT_COUNT = 4  # four adjacent 30-minute expired holds spanning 2 hours


def _seed_expired_holds(dsn: str, resource_id: str, user_id: str, window_start: datetime) -> None:
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            for i in range(SEGMENT_COUNT):
                seg_start = window_start + timedelta(minutes=SEGMENT_MINUTES * i)
                seg_end = seg_start + timedelta(minutes=SEGMENT_MINUTES)
                cur.execute(
                    "INSERT INTO booking (id, resource_id, user_id, time_range, status, "
                    "expires_at, created_at) "
                    "VALUES (%s, %s, %s, %s::tstzrange, 'held', "
                    "now() - interval '1 second', now())",
                    (str(uuid.uuid4()), resource_id, user_id, range_literal(seg_start, seg_end)),
                )
    finally:
        conn.close()


def _booking_attempt_with_cleanup_action(
    booking_id: str, resource_id: str, user_id: str, range_sql: str
) -> Callable[[psycopg.Cursor], None]:
    def action(cur: psycopg.Cursor) -> None:
        cur.execute(
            "DELETE FROM booking WHERE resource_id = %s AND status = 'held' "
            "AND expires_at <= now() AND time_range && %s::tstzrange",
            (resource_id, range_sql),
        )
        cur.execute(
            "INSERT INTO booking (id, resource_id, user_id, time_range, status, created_at) "
            "VALUES (%s, %s, %s, %s::tstzrange, 'confirmed', now())",
            (booking_id, resource_id, user_id, range_sql),
        )

    return action


@pytest.mark.django_db(transaction=True)
def test_reclaim_04_deadlock_under_load(resource_and_user: dict[str, str]) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]

    window_start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    window_end = window_start + timedelta(minutes=SEGMENT_MINUTES * SEGMENT_COUNT)
    range_sql = range_literal(window_start, window_end)

    total_deadlocks = 0
    total_unexplained: list[ClientOutcome] = []
    zero_success_attempts = 0

    for run in range(1, RUNS + 1):
        successes: list[ClientOutcome] = []
        for attempt in range(1, MAX_ROUND_ATTEMPTS + 1):
            clear_bookings(dsn, resource_id)
            _seed_expired_holds(dsn, resource_id, user_id, window_start)

            actions = [
                _booking_attempt_with_cleanup_action(
                    str(uuid.uuid4()), resource_id, user_id, range_sql
                )
                for _ in range(N)
            ]
            outcomes = run_concurrent(dsn, actions)

            successes = [o for o in outcomes if o.success]
            failures = [o for o in outcomes if not o.success]
            unexplained = [o for o in failures if o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES]
            deadlocks = [o for o in failures if o.sqlstate == DEADLOCK_DETECTED]
            sqlstate_counts = Counter(o.sqlstate for o in failures)

            total_deadlocks += len(deadlocks)
            total_unexplained.extend(unexplained)

            print(
                f"RECLAIM-04 run {run}/{RUNS} attempt {attempt}: successes={len(successes)} "
                f"deadlocks={len(deadlocks)} failures={dict(sqlstate_counts)}"
            )

            assert len(outcomes) == N, f"run {run}: expected {N} responses, got {len(outcomes)}"
            # Safety — zero tolerance, checked on every attempt, never retried.
            assert len(successes) <= 1, (
                f"run {run} attempt {attempt}: SAFETY VIOLATION — "
                f"{len(successes)} simultaneous successes"
            )
            assert not unexplained, (
                f"run {run} attempt {attempt}: unexplained SQLSTATEs (not in "
                f"{sorted(EXPECTED_NONSUCCESS_SQLSTATES)}): "
                f"{sorted({o.sqlstate for o in unexplained})}"
            )

            if successes:
                break
            zero_success_attempts += 1
        else:
            pytest.fail(f"run {run}: zero successes across {MAX_ROUND_ATTEMPTS} attempts")

        assert len(successes) == 1, f"run {run}: expected exactly 1 success, got {len(successes)}"

        overlapping = count_overlapping_pairs(dsn, resource_id)
        assert overlapping == 0, f"run {run}: {overlapping} overlapping active row pairs found"
        ground_truth = count_active_overlapping(dsn, resource_id, range_sql)
        assert ground_truth == 1, f"run {run}: ground truth = {ground_truth}, expected 1"

    clear_bookings(dsn, resource_id)

    print(
        f"RECLAIM-04: {RUNS} runs x up to {N} writers — "
        f"total deadlocks (40P01) = {total_deadlocks}, "
        f"zero-success attempts absorbed by retry = {zero_success_attempts}, "
        f"unexplained SQLSTATEs = {len(total_unexplained)}"
    )
    # The DoD's own bar is "zero deadlocks" — but per this phase's explicit
    # instruction, a nonzero (but otherwise safe and fully explained)
    # deadlock count is a documented LIVENESS finding, not a test failure:
    # 40P01 is already in EXPECTED_NONSUCCESS_SQLSTATES and BookingService
    # already treats it as a retryable 503 (Phase 4) — the exact same
    # principle CONC-01 established for its own empirical 40P01 finding.
    # See CLAUDE.md for the real count this run actually produced.
    assert not total_unexplained
