"""Schema assertion (Implementation Plan Phase 20; PRD v1.0 M3; RFC v1.0
§14; Rollout v1.0 RUNBOOK-01 cause #1). Detects the CAUSE — the
`no_overlapping_bookings` constraint missing or narrowed — before
reconciliation (`kairos.core.reconciliation`) would ever detect the
CONSEQUENCE. Fires before anyone has been double-booked, which is what
makes it the more valuable of RFC v1.0 §14's two checks.

`get_no_overlapping_bookings_definition()` is the EXACT query
`tests/test_schema_assertion.py` has run since Phase 3 (written before
'held' had a real writer, specifically so the predicate was guarded from
the moment it was defined). That test now calls this function directly
instead of duplicating the SQL, so the CI-tier check and this scheduled
production job are PROVABLY the same check — if they ever checked the
predicate two different ways, a real predicate-narrowing bug could pass
one and fail the other, which is exactly the gap this shared function
closes.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.db import connection

from kairos.core.constants import SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS
from kairos.core.heartbeat import heartbeat_is_stale
from kairos.core.models import SystemCheckRun

logger = logging.getLogger(__name__)

CONSTRAINT_NAME = "no_overlapping_bookings"

# RECON-04 / RFC v1.0 §14: an on-call engineer who reads a hit as "a race
# occurred" investigates the wrong thing under pressure. This exact text
# is a TESTED property of the alert payload (RECON-04), not a comment —
# see tests/test_admin_checks.py.
SCHEMA_ASSERTION_FAILURE_MESSAGE = (
    "The no_overlapping_bookings EXCLUDE constraint is missing, or its predicate no "
    "longer covers both 'confirmed' and 'held'. This means the core correctness "
    "guarantee has been REMOVED — by a migration, a restore taken without it, or a "
    "predicate narrowed during an unrelated change (Rollout v1.0 RUNBOOK-01 cause #1). "
    "This check fires BEFORE any overlapping data can exist: it detects the cause, not "
    "the consequence."
)


def get_no_overlapping_bookings_definition() -> str | None:
    """`None` if the constraint doesn't exist at all; otherwise its FULL
    `pg_get_constraintdef()` text — never merely existence. Rollout v1.0
    RUNBOOK-01's own diagnostic step is explicit: "THE MOST IMPORTANT
    CHECK — the full definition, not just existence." A narrowed
    predicate (`WHERE status = 'confirmed'`) still exists as a row in
    `pg_constraint` and would pass a bare existence check while every
    waitlist guarantee silently stopped meaning anything.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = %s AND contype = 'x'",
            [CONSTRAINT_NAME],
        )
        row = cur.fetchone()
    return row[0] if row else None


def check_schema_assertion() -> dict[str, Any]:
    """The scheduled/on-deploy job body. Writes a `system_check_run`
    heartbeat every run, pass or fail — the heartbeat itself is the
    signal Rollout v1.0 §6.1's "no successful run in 2x interval" alert
    condition needs, independent of whether any individual run passed.
    """
    definition = get_no_overlapping_bookings_definition()
    covers_confirmed = definition is not None and "'confirmed'" in definition
    covers_held = definition is not None and "'held'" in definition
    ok = definition is not None and covers_confirmed and covers_held

    findings: dict[str, Any] = {
        "constraint_exists": definition is not None,
        "definition": definition,
        "covers_confirmed": covers_confirmed,
        "covers_held": covers_held,
    }
    status = SystemCheckRun.Status.PASS
    if not ok:
        findings["message"] = SCHEMA_ASSERTION_FAILURE_MESSAGE
        status = SystemCheckRun.Status.FAIL
        # `extra` can't carry a key named "message" — logging's own
        # LogRecord already reserves it (raises "Attempt to overwrite
        # 'message' in LogRecord" otherwise), caught empirically the
        # first time this branch actually ran under a test. Renamed on
        # this side only; the findings dict passed to the caller (and
        # persisted to system_check_run) keeps "message" as its key.
        logger.error(
            "schema_assertion_failed",
            extra={k: v for k, v in findings.items() if k != "message"},
        )

    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.SCHEMA_ASSERTION, status=status, findings=findings
    )
    return findings


def schema_assertion_heartbeat_is_stale(
    *,
    now: datetime | None = None,
    threshold_seconds: int = SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS,
) -> bool:
    """Rollout v1.0 §6.1: schema_assertion alerts on "fails, OR no run in
    2x interval" — the FAIL half is `check_schema_assertion`'s own
    `status` field (read directly by `kairos.core.alerting.
    evaluate_alerts`); this is the staleness half, previously missing
    despite `SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS` already existing as
    a named constant since Phase 20 with no caller (Implementation Plan
    Phase 21 closes that gap — the constant was defined ahead of its own
    consumer, the same "mechanism before its real caller" pattern this
    project has used repeatedly, e.g. `actor_type='system'`, Phase 8→13).
    Delegates to the one shared `heartbeat_is_stale`, exactly like
    `hold_reaper_heartbeat_is_stale`/`series_materialization_heartbeat_is_
    stale`/`tzdata_rematerialization_heartbeat_is_stale`.
    """
    return heartbeat_is_stale(SystemCheckRun.CheckName.SCHEMA_ASSERTION, threshold_seconds, now=now)
