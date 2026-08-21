"""RECON-05 (CI form) — schema assertion (PRD M3; RFC v1.0 §14).

Rollout RUNBOOK-01 cause #1's primary CI-time defense: narrowing the
predicate to 'confirmed' only would disable every waitlist guarantee while
every other test still passes — this test existed BEFORE Phase 15 gave
'held' rows a real writer specifically so the predicate itself was already
guarded the moment it was defined (Phase 2), not left unverified until
something used it. This test fails the moment that predicate is narrowed,
not months later when a waitlist offer silently reserves nothing.

⚠️ Since Phase 20: calls `get_no_overlapping_bookings_definition()`
(`kairos/core/schema_assertion.py`) directly, rather than running its own
copy of the query — that function is also the body of the scheduled
production `schema_assertion` job. One query, two callers (this CI-tier
test and the production job), never two independently-written copies
that could silently drift apart and disagree about a real predicate
narrowing.
"""

from __future__ import annotations

import pytest
from django.db import connection

from kairos.core.models import SystemCheckRun
from kairos.core.schema_assertion import (
    SCHEMA_ASSERTION_FAILURE_MESSAGE,
    check_schema_assertion,
    get_no_overlapping_bookings_definition,
)

# Mirrors bookings/migrations/0002_exclusion_constraint.py's own
# ADD_CONSTRAINT_SQL/DROP_CONSTRAINT_SQL exactly — duplicated here rather
# than imported from the migration module (migrations are meant to run
# once, forward, via the migration graph, not be imported as ordinary
# library code) — but the DDL text itself must stay byte-identical to
# what production actually runs, or this test would validate a
# constraint shape production doesn't have.
DROP_CONSTRAINT_SQL = "ALTER TABLE booking DROP CONSTRAINT no_overlapping_bookings;"
RESTORE_CONSTRAINT_SQL = """
ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        resource_id WITH =,
        time_range  WITH &&
    )
    WHERE (status IN ('confirmed', 'held'));
"""
# RUNBOOK-01 cause #1, reproduced directly: a predicate narrowed to
# 'confirmed' alone. The constraint still exists — pg_constraint still
# returns it — but every waitlist guarantee (RFC v1.0 §10.1) is silently
# gone.
NARROWED_CONSTRAINT_SQL = """
ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
    EXCLUDE USING gist (
        resource_id WITH =,
        time_range  WITH &&
    )
    WHERE (status IN ('confirmed'));
"""


@pytest.mark.django_db
def test_no_overlapping_bookings_constraint_exists_and_covers_held() -> None:
    definition = get_no_overlapping_bookings_definition()

    assert definition is not None, (
        "no_overlapping_bookings EXCLUDE constraint is missing — the core "
        "correctness guarantee has been removed (RFC v1.0 §3; PRD M3)."
    )
    assert "'held'" in definition, (
        "no_overlapping_bookings no longer covers 'held' — waitlist offers "
        "no longer reserve anything (RFC v1.0 §10.1)."
    )
    assert "'confirmed'" in definition, (
        "no_overlapping_bookings no longer covers 'confirmed' — ordinary "
        "bookings are no longer protected (RFC v1.0 §3)."
    )


@pytest.mark.django_db
def test_recon_05_dropped_constraint_fails_the_check() -> None:
    """RECON-05, case 1: the constraint is gone entirely."""
    with connection.cursor() as cur:
        cur.execute(DROP_CONSTRAINT_SQL)
    try:
        findings = check_schema_assertion()
        assert findings["constraint_exists"] is False
        assert findings["message"] == SCHEMA_ASSERTION_FAILURE_MESSAGE
        run = SystemCheckRun.objects.filter(
            check_name=SystemCheckRun.CheckName.SCHEMA_ASSERTION
        ).latest("run_at")
        assert run.status == SystemCheckRun.Status.FAIL
    finally:
        with connection.cursor() as cur:
            cur.execute(RESTORE_CONSTRAINT_SQL)


@pytest.mark.django_db
def test_recon_05_narrowed_predicate_also_fails_the_check() -> None:
    """RECON-05, case 2 — Rollout v1.0 RUNBOOK-01 cause #1, the one a
    bare existence check would miss entirely: the constraint still
    exists, `pg_constraint` still returns a row, but the predicate no
    longer covers 'held'. This is the specific scenario that makes a
    schema assertion checking only existence (RFC v1.0 §14's own minimal
    SQL example) insufficient — `get_no_overlapping_bookings_definition`
    checks the FULL definition instead, precisely so this case fails
    loudly instead of passing silently.
    """
    with connection.cursor() as cur:
        cur.execute(DROP_CONSTRAINT_SQL)
        cur.execute(NARROWED_CONSTRAINT_SQL)
    try:
        definition = get_no_overlapping_bookings_definition()
        assert definition is not None  # the constraint DOES exist —
        assert "'confirmed'" in definition  # a bare existence check
        assert "'held'" not in definition  # would wrongly pass here

        findings = check_schema_assertion()
        assert findings["constraint_exists"] is True
        assert findings["covers_confirmed"] is True
        assert findings["covers_held"] is False
        assert findings["message"] == SCHEMA_ASSERTION_FAILURE_MESSAGE
        run = SystemCheckRun.objects.filter(
            check_name=SystemCheckRun.CheckName.SCHEMA_ASSERTION
        ).latest("run_at")
        assert run.status == SystemCheckRun.Status.FAIL
    finally:
        with connection.cursor() as cur:
            cur.execute(DROP_CONSTRAINT_SQL)
            cur.execute(RESTORE_CONSTRAINT_SQL)
