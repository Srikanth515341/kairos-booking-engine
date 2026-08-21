"""RECLAIM-03 — Cleanup-on-write vs. acceptance race (Test Plan v1.0 §4;
Implementation Plan Phase 17).

Party 1 mirrors what a REAL `POST /bookings` attempt does under the hood
(`kairos.bookings.services.create_booking`): the cleanup-on-write DELETE
followed by the INSERT, both in one transaction. Party 2 is the literal
RFC v1.0 §10.3 / Spec v1.0 §4.3 acceptance conditional UPDATE.

Ground truth is inferred from correlating the two outcomes rather than
reading Party 1's DELETE rowcount directly (`ClientOutcome.rowcount`
captures only the LAST statement psycopg's cursor ran, which for Party 1
is the INSERT) — the two legal orderings are structurally exhaustive and
mutually exclusive:

- Acceptance wins (1 row): the hold is now 'confirmed' and occupies the
  exclusion domain — Party 1's cleanup DELETE necessarily matches 0 rows
  (nothing 'held' left to delete) and its INSERT necessarily hits 23P01.
- Acceptance loses (0 rows): either cleanup committed first (deleting the
  hold, so acceptance's own WHERE clause finds nothing 'held' on
  re-evaluation) or the hold had already gone in some other way — either
  way Party 1's INSERT has a clear range to land in and must succeed.

There is no third outcome given a clean 2-party race on one row with
nothing else contending — asserting this correlation on every single run
is a stronger check than asserting each side's outcome in isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.concurrency.harness import (
    EXCLUSION_VIOLATION,
    EXPECTED_NONSUCCESS_SQLSTATES,
    clear_bookings,
    count_active_overlapping,
    django_test_dsn,
    range_literal,
    run_concurrent,
)

RUNS = 100


def _insert_expired_hold(dsn: str, resource_id: str, user_id: str, range_sql: str) -> str:
    hold_id = str(uuid.uuid4())
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO booking (id, resource_id, user_id, time_range, status, "
                "expires_at, created_at) "
                "VALUES (%s, %s, %s, %s::tstzrange, 'held', now() - interval '1 second', now())",
                (hold_id, resource_id, user_id, range_sql),
            )
    finally:
        conn.close()
    return hold_id


def _booking_attempt_with_cleanup_action(
    new_booking_id: str, resource_id: str, competitor_user_id: str, range_sql: str
) -> Callable[[psycopg.Cursor], None]:
    """Mirrors create_booking's own statement order: cleanup-on-write's
    DELETE, then the INSERT, in one transaction."""

    def action(cur: psycopg.Cursor) -> None:
        cur.execute(
            "DELETE FROM booking WHERE resource_id = %s AND status = 'held' "
            "AND expires_at <= now() AND time_range && %s::tstzrange",
            (resource_id, range_sql),
        )
        cur.execute(
            "INSERT INTO booking (id, resource_id, user_id, time_range, status, created_at) "
            "VALUES (%s, %s, %s, %s::tstzrange, 'confirmed', now())",
            (new_booking_id, resource_id, competitor_user_id, range_sql),
        )

    return action


def _acceptance_action(hold_id: str, user_id: str) -> Callable[[psycopg.Cursor], None]:
    def action(cur: psycopg.Cursor) -> None:
        cur.execute(
            "UPDATE booking SET status = 'confirmed', expires_at = NULL "
            "WHERE id = %s AND status = 'held' AND user_id = %s AND expires_at > now()",
            (hold_id, user_id),
        )

    return action


@pytest.mark.django_db(transaction=True)
def test_reclaim_03_cleanup_vs_acceptance_race(resource_and_user: dict[str, str]) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    waitlisted_user_id = resource_and_user["user_id"]

    start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    range_sql = range_literal(start, end)

    for run in range(1, RUNS + 1):
        hold_id = _insert_expired_hold(dsn, resource_id, waitlisted_user_id, range_sql)
        new_booking_id = str(uuid.uuid4())

        outcomes = run_concurrent(
            dsn,
            [
                _booking_attempt_with_cleanup_action(
                    new_booking_id, resource_id, waitlisted_user_id, range_sql
                ),
                _acceptance_action(hold_id, waitlisted_user_id),
            ],
        )
        booking_attempt, acceptance = outcomes

        unexplained = [
            o for o in outcomes if not o.success and o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES
        ]
        assert not unexplained, f"run {run}: unexplained SQLSTATEs: {unexplained}"

        acceptance_won = acceptance.success and acceptance.rowcount == 1
        if acceptance_won:
            assert not booking_attempt.success, (
                f"run {run}: acceptance won but the booking attempt ALSO succeeded — "
                "SAFETY VIOLATION, two active rows for the same range"
            )
            assert booking_attempt.sqlstate == EXCLUSION_VIOLATION, (
                f"run {run}: acceptance won, expected the booking attempt to fail with "
                f"23P01, got {booking_attempt.sqlstate}"
            )
        else:
            assert acceptance.success and acceptance.rowcount == 0, (
                f"run {run}: acceptance neither won (1 row) nor cleanly lost (0 rows): {acceptance}"
            )
            assert booking_attempt.success, (
                f"run {run}: acceptance lost but the booking attempt ALSO failed — "
                f"nobody ended up with the range: {booking_attempt}"
            )

        ground_truth = count_active_overlapping(dsn, resource_id, range_sql)
        assert ground_truth == 1, f"run {run}: ground truth = {ground_truth}, expected 1"

        clear_bookings(dsn, resource_id)
