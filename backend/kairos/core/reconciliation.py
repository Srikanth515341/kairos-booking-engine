"""Reconciliation (Implementation Plan Phase 20; PRD v1.0 M2; RFC v1.0
§14). A self-join for overlapping ACTIVE bookings on the same resource —
while `no_overlapping_bookings` is present and functioning, this query
is STRUCTURALLY INCAPABLE of returning a row (RFC v1.0 §14's own words,
reproduced in `RECONCILIATION_FAILURE_MESSAGE` below). It is not a race
detector; it detects the constraint's absence via its CONSEQUENCE, the
same way `kairos.core.schema_assertion` detects the constraint's absence
via its CAUSE — together, RFC v1.0 §14's "detect the cause, detect the
consequence" pair.

Uses the range `&&` operator — the SAME operator `no_overlapping_bookings`
itself is defined with (`time_range WITH &&`) — so `[)` bound semantics
are respected identically here: two bookings that are merely ADJACENT
(one's end exactly equals the other's start) are correctly NOT flagged.
Rollout v1.0 RUNBOOK-01 names this exact case as cause #4, a false-alarm
risk from a query whose overlap definition doesn't match `tstzrange`'s
own semantics — using the identical operator sidesteps that risk
entirely rather than reimplementing overlap comparison in SQL by hand.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import connection

from kairos.core.models import SystemCheckRun

logger = logging.getLogger(__name__)

# RECON-04 / RFC v1.0 §14: a TESTED property of the alert payload (see
# tests/test_reconciliation.py) — never merely a comment. An on-call
# engineer who reads this as "a race occurred" investigates the wrong
# thing under pressure; this text says so explicitly.
RECONCILIATION_FAILURE_MESSAGE = (
    "This is NOT a race detector. If the no_overlapping_bookings constraint is present "
    "and functioning, this query is structurally incapable of returning a row. A hit "
    "means the correctness guarantee has been REMOVED — a dropped constraint, a restore "
    "taken without it, or an out-of-band write that bypassed it — NOT that a race "
    "occurred. Treating this as a timing issue and waiting for it to resolve is the "
    "wrong response."
)


def find_overlapping_active_bookings() -> list[tuple[Any, Any, Any]]:
    """One row per overlapping PAIR: `(booking_id_1, booking_id_2,
    resource_id)`. `b1.id < b2.id` avoids both a row matching itself and
    the same pair being reported twice (once as (A,B), once as (B,A)).
    """
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT b1.id, b2.id, b1.resource_id
              FROM booking b1
              JOIN booking b2
                ON b1.resource_id = b2.resource_id
               AND b1.id < b2.id
               AND b1.time_range && b2.time_range
             WHERE b1.status IN ('confirmed', 'held')
               AND b2.status IN ('confirmed', 'held')
            """
        )
        rows: list[tuple[Any, Any, Any]] = cur.fetchall()
        return rows


def check_reconciliation() -> dict[str, Any]:
    """The scheduled job body. Writes a `system_check_run` heartbeat
    every run, pass or fail, mirroring `check_schema_assertion`.
    """
    pairs = find_overlapping_active_bookings()
    flagged_ids = sorted({str(row[0]) for row in pairs} | {str(row[1]) for row in pairs})

    findings: dict[str, Any] = {
        "overlaps_found": len(pairs),
        "flagged_booking_ids": flagged_ids,
        "flagged_pairs": [
            {"booking_id_1": str(b1), "booking_id_2": str(b2), "resource_id": str(rid)}
            for b1, b2, rid in pairs
        ],
    }
    status = SystemCheckRun.Status.PASS
    if pairs:
        findings["message"] = RECONCILIATION_FAILURE_MESSAGE
        status = SystemCheckRun.Status.FAIL
        # See kairos.core.schema_assertion's identical fix: `extra` can't
        # carry a key named "message" (logging's own LogRecord reserves
        # it). Renamed on this side only — the findings dict returned to
        # the caller keeps "message" as its key.
        logger.error(
            "reconciliation_overlap_detected",
            extra={k: v for k, v in findings.items() if k != "message"},
        )

    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.RECONCILIATION, status=status, findings=findings
    )
    return findings
