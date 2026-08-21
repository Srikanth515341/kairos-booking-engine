"""WL-02 — Reaper-vs-acceptance race on the same hold row (Test Plan v1.0
§3, WL-02; Implementation Plan Phase 16).

The hold reaper does not exist until Phase 17 (explicitly deferred in
Phase 16's own Scope). Per this phase's explicit instruction, "reaper-
expiry vs. acceptance" is simulated: one connection executes the exact
conditional expiry update a real reaper will use, racing against a second
connection performing the exact RFC v1.0 §10.3 / Spec v1.0 §4.3 acceptance
update — the same "mechanism before its real caller" pattern already used
in Phases 13/14/15.

The reaper's simulated form, derived (not guessed) from the schema:
`booking.status`'s CHECK constraint only allows ('confirmed', 'held',
'cancelled') — there is no 'expired' value at the booking level (that
belongs to `waitlist_offer.status` only, Spec v1.0 §3). So reclaiming an
expired hold can only be an UPDATE to 'cancelled' (mirroring
`cancel_booking`'s own shape and `decline_offer`'s hold-release, Phase
16), never a DELETE (RFC v1.0 §10.4's cleanup-on-write DELETE is a
different, narrower mechanism scoped to a resource+range ahead of an
INSERT) and never an invented status value the DB would reject outright.

Since exactly one of `expires_at <= now()` / `expires_at > now()` can be
true for a FIXED `expires_at`, the 100 repetitions are split 50/50
between a hold whose `expires_at` is safely in the future (acceptance
must structurally win) and one safely in the past (the reaper must
structurally win) — deterministically exercising BOTH orderings under
genuine barrier-released concurrency, rather than gambling on microsecond
timing jitter to produce a natural mix. "Exactly one affects 1 row; the
other affects 0" (Test Plan's own assertion) is checked on every single
repetition regardless of which side is expected to win — the SAFETY
property under test is "never both, never neither, never ambiguous," not
"the winner varies across runs."
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.concurrency.harness import (
    EXPECTED_NONSUCCESS_SQLSTATES,
    clear_bookings,
    django_test_dsn,
    range_literal,
    run_concurrent,
)

RUNS_PER_ORDERING = 50


def _insert_hold(dsn: str, resource_id: str, user_id: str, range_sql: str, expires_at: str) -> str:
    hold_id = str(uuid.uuid4())
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO booking (id, resource_id, user_id, time_range, status, "
                "expires_at, created_at) "
                "VALUES (%s, %s, %s, %s::tstzrange, 'held', %s, now())",
                (hold_id, resource_id, user_id, range_sql, expires_at),
            )
    finally:
        conn.close()
    return hold_id


def _reaper_expiry_action(hold_id: str) -> Callable[[psycopg.Cursor], None]:
    def action(cur: psycopg.Cursor) -> None:
        # expires_at must be cleared, not just status changed — the
        # hold_has_expiry DB CHECK constraint (Phase 2) requires
        # expires_at IS NULL for any non-'held' row; the identical fix
        # decline_offer (kairos.waitlist.services) needed, caught by the
        # same SQLSTATE 23514 empirically while building this test.
        cur.execute(
            "UPDATE booking SET status = 'cancelled', cancelled_at = now(), expires_at = NULL "
            "WHERE id = %s AND status = 'held' AND expires_at <= now()",
            (hold_id,),
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


def _final_status(dsn: str, hold_id: str) -> str:
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM booking WHERE id = %s", (hold_id,))
            row = cur.fetchone()
            assert row is not None
            return str(row[0])
    finally:
        conn.close()


@pytest.mark.django_db(transaction=True)
def test_wl_02_reaper_vs_acceptance_race(resource_and_user: dict[str, str]) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]

    start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    range_sql = range_literal(start, end)

    orderings = (
        # (label, expires_at_offset, expected_reaper_wins)
        ("acceptance-should-win", timedelta(hours=1), False),
        ("reaper-should-win", -timedelta(hours=1), True),
    )

    for label, offset, reaper_should_win in orderings:
        for run in range(1, RUNS_PER_ORDERING + 1):
            expires_at = (datetime.now(UTC) + offset).isoformat()
            hold_id = _insert_hold(dsn, resource_id, user_id, range_sql, expires_at)

            outcomes = run_concurrent(
                dsn,
                [
                    _reaper_expiry_action(hold_id),
                    _acceptance_action(hold_id, user_id),
                ],
            )
            reaper_outcome, acceptance_outcome = outcomes

            unexplained = [
                o
                for o in outcomes
                if not o.success and o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES
            ]
            assert not unexplained, f"{label} run {run}: unexplained SQLSTATEs: {unexplained}"
            assert reaper_outcome.success and acceptance_outcome.success, (
                f"{label} run {run}: a conditional UPDATE with zero matching rows is not an "
                f"error — both statements must succeed regardless of rowcount: {outcomes}"
            )

            reaper_rows = reaper_outcome.rowcount
            acceptance_rows = acceptance_outcome.rowcount
            assert {reaper_rows, acceptance_rows} == {0, 1}, (
                f"{label} run {run}: expected exactly one UPDATE to affect 1 row and the "
                f"other 0 — got reaper={reaper_rows}, acceptance={acceptance_rows}"
            )

            # The winner must match which side's WHERE clause was
            # structurally satisfiable given this rep's fixed expires_at
            # — proving BOTH orderings deterministically (not hoping
            # timing jitter happens to produce a mix).
            reaper_won = reaper_rows == 1
            assert reaper_won == reaper_should_win, (
                f"{label} run {run}: expected "
                f"{'the reaper' if reaper_should_win else 'acceptance'} to win, but "
                f"{'the reaper' if reaper_won else 'acceptance'} did"
            )

            final_status = _final_status(dsn, hold_id)
            expected_final = "cancelled" if reaper_won else "confirmed"
            assert final_status == expected_final, (
                f"{label} run {run}: final status is {final_status!r}, expected {expected_final!r}"
            )

            clear_bookings(dsn, resource_id)
