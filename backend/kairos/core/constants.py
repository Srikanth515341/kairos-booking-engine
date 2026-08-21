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
