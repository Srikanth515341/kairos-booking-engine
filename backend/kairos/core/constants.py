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
