"""CONC-05 — Cancel-and-rebook at the instant a slot frees (Test Plan v1.0
§2, CONC-05).

Materially different from CONC-01/02: this exercises the constraint's
predicate under a *concurrent status transition* — a row leaving the
exclusion domain (confirmed -> cancelled) while new writers contend for the
range it vacates, rather than contention against an always-been-free range.

10 runs for the CI tier (Implementation Plan Phase 3 scope).
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

# Phase 3 empirical finding, symmetric with the 57014/40P01 finding in
# harness.py: at this low a party count (3), it is occasionally possible for
# BOTH competing creates to individually hit a documented, retryable outcome
# (55P03/40P01/57014) in the same run and for neither to commit. This is a
# liveness characteristic, not a safety violation — the hard, zero-tolerance
# gate (checked on every attempt, never retried away) is "never more than one
# create succeeds" (RFC v1.0 §3's core guarantee). A round is retried only
# when zero creates succeed, mirroring the retry a real client performs on a
# documented error (RFC §11) — see test_conc_01.py's MAX_ROUND_ATTEMPTS
# comment for the full reasoning.
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


def _cancel_action(booking_id: str) -> Callable[[psycopg.Cursor], None]:
    def action(cur: psycopg.Cursor) -> None:
        cur.execute(
            "UPDATE booking SET status = 'cancelled', cancelled_at = now() WHERE id = %s",
            (booking_id,),
        )

    return action


@pytest.mark.django_db(transaction=True)
def test_conc_05_cancel_and_rebook(resource_and_user: dict[str, str]) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]
    owner = AppUser.objects.get(id=user_id)
    resource = Resource.objects.get(id=resource_id)

    start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    range_sql = range_literal(start, end)

    zero_success_attempts = 0

    for run in range(1, RUNS + 1):
        create_successes: list[ClientOutcome] = []
        for attempt in range(1, MAX_ROUND_ATTEMPTS + 1):
            # Setup (Django ORM, committed immediately under transaction=True):
            # B1 confirmed at 09:00-10:00, the slot the barrier release
            # contends over.
            b1 = Booking.objects.create(resource=resource, user=owner, time_range=(start, end))

            actions = [
                _cancel_action(str(b1.id)),
                _insert_action(str(uuid.uuid4()), resource_id, user_id, range_sql),
                _insert_action(str(uuid.uuid4()), resource_id, user_id, range_sql),
            ]
            outcomes = run_concurrent(dsn, actions)
            cancel_outcome, create_a, create_b = outcomes
            create_outcomes = [create_a, create_b]

            print(
                f"CONC-05 run {run}/{RUNS} attempt {attempt}: cancel={cancel_outcome.success} "
                f"creates={[(o.success, o.sqlstate) for o in create_outcomes]}"
            )

            failures = [o for o in outcomes if not o.success]
            unexplained = [o for o in failures if o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES]
            assert not unexplained, (
                f"run {run} attempt {attempt}: unexplained SQLSTATEs: "
                f"{sorted({o.sqlstate for o in unexplained})}"
            )

            assert cancel_outcome.success, (
                f"run {run} attempt {attempt}: cancellation failed: {cancel_outcome}"
            )

            create_successes = [o for o in create_outcomes if o.success]
            # Safety — zero tolerance, checked on every attempt, never retried.
            assert len(create_successes) <= 1, (
                f"run {run} attempt {attempt}: SAFETY VIOLATION — "
                f"{len(create_successes)} creates succeeded simultaneously"
            )

            if create_successes:
                break
            zero_success_attempts += 1
            clear_bookings(dsn, resource_id)
        else:
            pytest.fail(f"run {run}: zero create successes across {MAX_ROUND_ATTEMPTS} attempts")

        ground_truth = count_active_overlapping(dsn, resource_id, range_sql)
        assert ground_truth == 1, f"run {run}: ground truth = {ground_truth}, expected 1"

        clear_bookings(dsn, resource_id)

    print(
        f"CONC-05: zero-success attempts absorbed by retry = {zero_success_attempts} "
        "(informational, not a gate)"
    )
