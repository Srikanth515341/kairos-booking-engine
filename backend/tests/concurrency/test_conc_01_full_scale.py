"""CONC-01 full-scale exercise (Test Plan v1.0 §2, §13, §14; Implementation
Plan Phase 28) — the release-gating form: 100 CONSECUTIVE runs at N=200
(zero tolerance across all 100, not the CI tier's 10-run reduction), plus
the separate N=500 escalation. Staging/pre-release tier, deliberately
excluded from the `concurrency` CI job (`.github/workflows/ci.yml`) —
mirrors RECLAIM-04's (Phase 17) own precedent for the identical reason:
expensive enough that per-commit execution would make CI unusable. Run
manually before a release:

    pytest tests/concurrency/test_conc_01_full_scale.py -v -s

Shares every mechanism with tests/concurrency/test_conc_01.py (the CI-tier
10-run form) unchanged — same harness, same barrier-released raw-SQL
INSERT action, same "retry a round only on zero successes, never soften a
>1-success safety violation" discipline. Only RUNS/N and the module-level
docstring differ.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tests.concurrency.harness import (
    EXPECTED_NONSUCCESS_SQLSTATES,
    ClientOutcome,
    clear_bookings,
    count_active_overlapping,
    django_test_dsn,
    range_literal,
    run_concurrent,
)

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


def _run_one_round(
    dsn: str, resource_id: str, user_id: str, range_sql: str, n: int, label: str
) -> list[ClientOutcome]:
    """One barrier-released round of `n` concurrent INSERTs, retried only
    on zero successes (a documented liveness characteristic at this scale,
    not a safety violation — see test_conc_01.py's own module docstring).
    Returns the successes list (always length 1) or fails the test.
    """
    successes: list[ClientOutcome] = []
    for attempt in range(1, MAX_ROUND_ATTEMPTS + 1):
        actions = [
            _insert_action(str(uuid.uuid4()), resource_id, user_id, range_sql) for _ in range(n)
        ]
        outcomes = run_concurrent(dsn, actions)

        successes = [o for o in outcomes if o.success]
        failures = [o for o in outcomes if not o.success]
        unexplained = [o for o in failures if o.sqlstate not in EXPECTED_NONSUCCESS_SQLSTATES]
        sqlstate_counts = Counter(o.sqlstate for o in failures)

        print(
            f"{label} attempt {attempt}: successes={len(successes)} "
            f"failures={dict(sqlstate_counts)}"
        )

        assert len(outcomes) == n, f"{label}: expected {n} responses, got {len(outcomes)}"
        assert len(successes) <= 1, (
            f"{label} attempt {attempt}: SAFETY VIOLATION — {len(successes)} simultaneous successes"
        )
        assert not unexplained, (
            f"{label} attempt {attempt}: unexplained SQLSTATEs (not in "
            f"{sorted(EXPECTED_NONSUCCESS_SQLSTATES)}): {sorted({o.sqlstate for o in unexplained})}"
        )

        if successes:
            return successes
        clear_bookings(dsn, resource_id)

    pytest.fail(f"{label}: zero successes across {MAX_ROUND_ATTEMPTS} attempts")


@pytest.mark.django_db(transaction=True)
def test_conc_01_full_scale_100_consecutive_runs_at_n200(
    resource_and_user: dict[str, str],
) -> None:
    """Test Plan v1.0 §14 hard blocker: "CONC-01: 100% pass across 100
    consecutive runs at N=200, plus the N=500 escalation. Zero tolerance.
    Ground-truth verified." This is the 100-run half.
    """
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]

    start = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    range_sql = range_literal(start, end)

    n = 200
    runs = 100

    for run in range(1, runs + 1):
        label = f"CONC-01-full run {run}/{runs}"
        successes = _run_one_round(dsn, resource_id, user_id, range_sql, n, label)
        assert len(successes) == 1, f"run {run}: expected exactly 1 success, got {len(successes)}"

        ground_truth = count_active_overlapping(dsn, resource_id, range_sql)
        assert ground_truth == 1, f"run {run}: ground truth = {ground_truth}, expected 1"

        clear_bookings(dsn, resource_id)

    print("CONC-01 full-scale (100x N=200): completed 100/100 runs, all ground-truth verified")


@pytest.mark.django_db(transaction=True)
def test_conc_01_n500_escalation(resource_and_user: dict[str, str]) -> None:
    """Test Plan v1.0 §14 hard blocker: the N=500 escalation half. A single
    round at 2.5x CONC-01's own baseline concurrency, same safety
    invariant (never more than one success), same ground-truth
    verification.
    """
    dsn = django_test_dsn()
    resource_id = resource_and_user["resource_id"]
    user_id = resource_and_user["user_id"]

    start = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    range_sql = range_literal(start, end)

    n = 500
    successes = _run_one_round(dsn, resource_id, user_id, range_sql, n, "CONC-01 N=500 escalation")
    assert len(successes) == 1

    ground_truth = count_active_overlapping(dsn, resource_id, range_sql)
    assert ground_truth == 1, f"N=500 escalation: ground truth = {ground_truth}, expected 1"

    clear_bookings(dsn, resource_id)
    print("CONC-01 N=500 escalation: passed, ground-truth verified")
