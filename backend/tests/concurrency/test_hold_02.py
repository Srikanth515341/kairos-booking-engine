"""HOLD-02 — Concurrent booking attempt during a hold, barrier-released
(Test Plan v1.0 §3, HOLD-02; Implementation Plan Phase 15).

Raw psycopg INSERTs through the same barrier-released harness as
CONC-01/02/03/04/05, not real `POST /bookings` HTTP calls — the same
"proving the constraint itself, independent of application code" choice
CLAUDE.md already documents for CONC-03/04 (which also conceptually map to
an HTTP verb — edit — yet exercise raw SQL directly). Session settings are
applied identically either way (harness.SESSION_SETTINGS_SQL mirrors
kairos.core.db.apply_write_path_session_settings); what's being proven is
that the EXCLUDE constraint's `status IN ('confirmed','held')` predicate
stops every one of 50 concurrent writers, not that BookingService's HTTP
layer correctly translates 23P01 — that translation is already proven by
test_hold_01_ordinary_booking_loses_to_outstanding_offer's real HTTP step.

Unlike CONC-01, zero successes is the ONLY correct outcome here (the range
is never actually free — a hold already occupies it) — there is no
"retry the round on zero successes" logic, because zero successes every
single round is the invariant being asserted, not a liveness characteristic
to route around.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from kairos.identity.models import AppUser
from tests.concurrency.harness import (
    EXPECTED_NONSUCCESS_SQLSTATES,
    clear_bookings,
    count_active_overlapping,
    django_test_dsn,
    range_literal,
    run_concurrent,
)

PARTIES = 50
RUNS = 50


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


def _insert_hold(resource_id: str, user_id: str, range_sql: str) -> str:
    """Committed synchronously, before the barrier release — this is the
    active hold every one of the 50 competitors below is racing against
    (RFC v1.0 §10.1)."""
    hold_id = str(uuid.uuid4())
    conn = psycopg.connect(django_test_dsn(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO booking (id, resource_id, user_id, time_range, status, "
                "expires_at, created_at) "
                "VALUES (%s, %s, %s, %s::tstzrange, 'held', now() + interval '15 minutes', now())",
                (hold_id, resource_id, user_id, range_sql),
            )
    finally:
        conn.close()
    return hold_id


@pytest.mark.django_db(transaction=True)
def test_hold_02_concurrent_bookings_against_a_held_range(
    resource_and_user: dict[str, str],
) -> None:
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    waitlisted_user_id = resource_and_user["user_id"]

    # 50 distinct competitors (Test Plan v1.0 HOLD-02: "50 different
    # users"), created once and reused across every run.
    competitors = [
        AppUser.objects.create(
            email=f"hold02-competitor-{i}@example.com", display_name=f"Competitor {i}"
        )
        for i in range(PARTIES)
    ]
    competitor_ids = [str(u.id) for u in competitors]

    start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    range_sql = range_literal(start, end)

    for run in range(1, RUNS + 1):
        hold_id = _insert_hold(resource_id, waitlisted_user_id, range_sql)

        actions = [
            _insert_action(str(uuid.uuid4()), resource_id, competitor_ids[i], range_sql)
            for i in range(PARTIES)
        ]
        outcomes = run_concurrent(dsn, actions)

        successes = [o for o in outcomes if o.success]
        failures = [o for o in outcomes if not o.success]
        unexplained = [o for o in failures if o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES]
        sqlstate_counts = Counter(o.sqlstate for o in failures)

        print(
            f"HOLD-02 run {run}/{RUNS}: successes={len(successes)} failures={dict(sqlstate_counts)}"
        )

        assert len(outcomes) == PARTIES, (
            f"run {run}: expected {PARTIES} responses, got {len(outcomes)}"
        )
        # The invariant: the hold already occupies the exclusion domain, so
        # NOTHING should ever succeed — not "at most one" (CONC-01's rule),
        # zero, unconditionally.
        assert not successes, (
            f"run {run}: SAFETY VIOLATION — {len(successes)} bookings succeeded "
            "against an actively held range"
        )
        assert not unexplained, (
            f"run {run}: unexplained SQLSTATEs (not in "
            f"{sorted(EXPECTED_NONSUCCESS_SQLSTATES)}): "
            f"{sorted({o.sqlstate for o in unexplained})}"
        )

        # Ground truth: exactly one active row for the range, and it is
        # still the hold — never overwritten, never duplicated.
        ground_truth = count_active_overlapping(dsn, resource_id, range_sql)
        assert ground_truth == 1, f"run {run}: ground truth = {ground_truth}, expected 1"

        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM booking WHERE id = %s", (hold_id,))
            row = cur.fetchone()
            assert row is not None and row[0] == "held", (
                f"run {run}: the hold row itself was mutated — status = {row}"
            )

        clear_bookings(dsn, resource_id)
