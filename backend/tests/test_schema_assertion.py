"""RECON-05 (CI form) — schema assertion (PRD M3; RFC v1.0 §14).

Rollout RUNBOOK-01 cause #1's primary CI-time defense: narrowing the
predicate to 'confirmed' only would disable every waitlist guarantee while
every other test still passes, since 'held' rows don't exist until Phase 15.
This test fails the moment that predicate is narrowed, not months later when
a waitlist offer silently reserves nothing.
"""

from __future__ import annotations

import pytest
from django.db import connection


@pytest.mark.django_db
def test_no_overlapping_bookings_constraint_exists_and_covers_held() -> None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'no_overlapping_bookings' AND contype = 'x'"
        )
        row = cur.fetchone()

    assert row is not None, (
        "no_overlapping_bookings EXCLUDE constraint is missing — the core "
        "correctness guarantee has been removed (RFC v1.0 §3; PRD M3)."
    )
    definition = row[0]
    assert "'held'" in definition, (
        "no_overlapping_bookings no longer covers 'held' — waitlist offers "
        "no longer reserve anything (RFC v1.0 §10.1)."
    )
    assert "'confirmed'" in definition, (
        "no_overlapping_bookings no longer covers 'confirmed' — ordinary "
        "bookings are no longer protected (RFC v1.0 §3)."
    )
