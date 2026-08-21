"""Named configuration constants for the write path (RFC v1.0 §7.1) and
domain bounds (PRD v1.0). Every timeout, interval, and bound is named here,
sourced from environment variables (.env.example), rather than a magic
number buried in a call site — Rollout v1.0 §9 requires several of these to
be tuned post-launch, and a tunable buried as a literal is not tunable.
"""

from __future__ import annotations

import os

# Write-path session settings (RFC v1.0 §7.1). Applied per transaction by
# BookingService via `set_config(..., true)` — the functional equivalent of
# `SET LOCAL`.
WRITE_PATH_LOCK_TIMEOUT = os.environ.get("DB_LOCK_TIMEOUT", "3s")
WRITE_PATH_STATEMENT_TIMEOUT = os.environ.get("DB_STATEMENT_TIMEOUT", "10s")
WRITE_PATH_IDLE_IN_TRANSACTION_TIMEOUT = os.environ.get("DB_IDLE_IN_TRANSACTION_TIMEOUT", "30s")

# Booking policy bounds (PRD FR14b).
MAX_ADVANCE_HORIZON_DAYS = int(os.environ.get("MAX_ADVANCE_HORIZON_DAYS", "365"))

# Idempotency key retention (PRD FR37; Spec v1.0 §7). A browser retry after
# a dropped connection happens within seconds to minutes, not days — 24h is
# the concrete default the contract needs.
IDEMPOTENCY_RETENTION_HOURS = int(os.environ.get("IDEMPOTENCY_RETENTION_HOURS", "24"))

# Availability query bound (PRD FR30). Unbounded ranges combined with
# recurrence expansion are the most likely source of the first production
# latency incident (RFC v1.0 §6.3).
MAX_AVAILABILITY_QUERY_DAYS = int(os.environ.get("MAX_AVAILABILITY_QUERY_DAYS", "92"))

# Recurring series bound (PRD FR14a). Unbounded expansion writes thousands
# of rows in one transaction while holding locks across an entire resource
# (RFC v1.0 §15b's cost of the materialized-rows design) — this is the
# structural ceiling on that, checked before expansion ever runs.
MAX_SERIES_OCCURRENCES = int(os.environ.get("MAX_SERIES_OCCURRENCES", "100"))

# Recurring-series preview token lifetime (Spec v1.0 §5.8/§5.9; Test Plan
# REC-04). A preview is advisory — availability can change while the user
# is deciding — so the token that lets confirm reuse the preview's exact
# computation without re-evaluating it is deliberately short-lived.
PREVIEW_TOKEN_TTL_SECONDS = int(os.environ.get("PREVIEW_TOKEN_TTL_SECONDS", str(15 * 60)))

# Celery Beat intervals (Implementation Plan Phase 13; RFC v1.0 §9.4,
# §14). Both jobs fail silently if they simply stop running — RFC §14
# flags this explicitly ("no errors, just absence") — so how OFTEN they
# run is itself a tunable worth naming, not a magic number in the beat
# schedule dict.
ROLLING_MATERIALIZATION_INTERVAL_SECONDS = int(
    os.environ.get("ROLLING_MATERIALIZATION_INTERVAL_SECONDS", str(60 * 60))  # hourly
)
TZDATA_REMATERIALIZATION_INTERVAL_SECONDS = int(
    os.environ.get("TZDATA_REMATERIALIZATION_INTERVAL_SECONDS", str(60 * 60))  # hourly
)
TZDATA_DRIFT_CHECK_INTERVAL_SECONDS = int(
    os.environ.get("TZDATA_DRIFT_CHECK_INTERVAL_SECONDS", str(24 * 60 * 60))  # daily
)

# Waitlist offer window (Implementation Plan Phase 15; RFC v1.0 §10.5) — also
# the hold DURATION, since a hold's expires_at is `now() + this`. Not merely
# a UX parameter: it directly determines how long a resource is unbookable
# by everyone else while an offer is outstanding. 15 minutes is the PRD's
# working proposal pending product sign-off (PRD v1.0 §11 open question 1) —
# tunable here, not a magic number at the hold-creation call site.
OFFER_WINDOW_MINUTES = int(os.environ.get("OFFER_WINDOW_MINUTES", "15"))

# Hold reaper sweep interval (Implementation Plan Phase 17; RFC v1.0 §10.4
# mechanism 2). Trades cascade latency (how long an expired-but-unreclaimed
# hold sits before the next eligible entry gets a shot at it) against sweep
# cost at real waitlist volume — RFC's own "tune from observed volume" note,
# 30s is the initial proposal.
HOLD_REAPER_INTERVAL_SECONDS = int(os.environ.get("HOLD_REAPER_INTERVAL_SECONDS", "30"))

# Correctness monitoring (Implementation Plan Phase 20; PRD M2/M3; RFC v1.0
# §14). Both run hourly by default — the same interval already used for
# rolling/tzdata materialization, not a coincidence: RFC v1.0 §14 names
# "hourly in production" explicitly for schema assertion, and reconciliation
# is scheduled the same way.
RECONCILIATION_INTERVAL_SECONDS = int(
    os.environ.get("RECONCILIATION_INTERVAL_SECONDS", str(60 * 60))
)
SCHEMA_ASSERTION_INTERVAL_SECONDS = int(
    os.environ.get("SCHEMA_ASSERTION_INTERVAL_SECONDS", str(60 * 60))
)

# Heartbeat staleness thresholds (Rollout v1.0 §6.1's table) — "no
# successful run in 2x interval" for the interval-driven checks. Named
# separately from the intervals above (not computed as `interval * 2`
# inline at every call site) so each is independently tunable, matching
# CLAUDE.md's own "every threshold is a named constant" rule.
RECONCILIATION_STALE_THRESHOLD_SECONDS = int(
    os.environ.get(
        "RECONCILIATION_STALE_THRESHOLD_SECONDS", str(RECONCILIATION_INTERVAL_SECONDS * 2)
    )
)
SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS = int(
    os.environ.get(
        "SCHEMA_ASSERTION_STALE_THRESHOLD_SECONDS", str(SCHEMA_ASSERTION_INTERVAL_SECONDS * 2)
    )
)
SERIES_MATERIALIZATION_STALE_THRESHOLD_SECONDS = int(
    os.environ.get(
        "SERIES_MATERIALIZATION_STALE_THRESHOLD_SECONDS",
        str(ROLLING_MATERIALIZATION_INTERVAL_SECONDS * 2),
    )
)
TZDATA_REMATERIALIZATION_STALE_THRESHOLD_SECONDS = int(
    os.environ.get(
        "TZDATA_REMATERIALIZATION_STALE_THRESHOLD_SECONDS",
        str(TZDATA_REMATERIALIZATION_INTERVAL_SECONDS * 2),
    )
)
# offer_cascade is event-triggered (a cancellation/decline/reclaim fires
# it), not interval-driven like the checks above — there is no "its own
# interval" to multiply. Rollout v1.0 §6.1's table gives it the same
# fixed 90s hold_reaper already uses (3x hold_reaper's 30s sweep) rather
# than a multiple of an interval that doesn't exist for this check.
OFFER_CASCADE_STALE_THRESHOLD_SECONDS = int(
    os.environ.get("OFFER_CASCADE_STALE_THRESHOLD_SECONDS", "90")
)

# Notification delivery retry (Implementation Plan Phase 18; PRD FR55 —
# "delivery failure must not roll back or block the underlying state
# transition, but must be recorded and retried"). Exponential backoff via
# Celery's own retry_backoff (kairos.core.tasks.send_notification_task) —
# a transient SMTP outage should be retried with growing spacing, not
# hammered immediately or abandoned after one attempt.
NOTIFICATION_MAX_RETRIES = int(os.environ.get("NOTIFICATION_MAX_RETRIES", "5"))
NOTIFICATION_RETRY_BACKOFF_MAX_SECONDS = int(
    os.environ.get("NOTIFICATION_RETRY_BACKOFF_MAX_SECONDS", "600")
)
