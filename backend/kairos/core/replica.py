"""Read-replica freshness routing (Implementation Plan Phase 28; Rollout
v1.0 §2.2; Test Plan v1.0 FAIL-03; Spec v1.0 §5.7's `data_freshness`
field).

No read replica exists anywhere in this project yet — Rollout v1.0 §2.2's
own checklist defers standing one up to Phase 30. Phase 28's job is
narrower and explicit (per its own Scope IN clarification on FAIL-03):
prove the DEGRADATION LOGIC is correct against a simulated lag condition,
not build real replica infrastructure just to have something to test
against. This module is that seam — small and honest, not a preview of
Phase 30's real routing:

- `current_replica_lag_seconds()` reports the only thing that's actually
  true today: no replica is configured, so lag is unknown (`None`). Phase
  30 replaces this function's body with a real measurement (e.g. `SELECT
  now() - pg_last_xact_replay_timestamp()` against the replica) — nothing
  else in this module needs to change when it does, since `None` and "a
  known lag over threshold" already produce the identical, correct
  outcome below.
- `select_read_source()` is the actual FAIL-03 claim, unit-testable in
  isolation: an unknown lag OR a lag past `REPLICA_LAG_THRESHOLD_SECONDS`
  must never be silently served as fresh (Spec v1.0 §5.7) — both fall
  back to `"primary"`, reported honestly via `data_freshness`, never a
  fabricated `"replica"` value.

`kairos.resources.views.ResourceAvailabilityView` is this module's only
caller today. Until Phase 30, `select_read_source(current_replica_lag_
seconds())` always evaluates to `"primary"` — identical to the hardcoded
literal this replaces — but the LOGIC that will matter once a real
replica exists is now written, proven, and waiting, not invented for the
first time under Phase 30's own time pressure.
"""

from __future__ import annotations

from kairos.core.constants import REPLICA_LAG_THRESHOLD_SECONDS


def current_replica_lag_seconds() -> float | None:
    """Real replication lag, in seconds. `None` means "no replica is
    configured" — the honest, only-possible answer in this codebase today.
    Never fabricated: a stand-in non-`None` value here would make
    `select_read_source` route reads to a replica that doesn't exist.
    """
    return None


def select_read_source(
    lag_seconds: float | None, threshold_seconds: int = REPLICA_LAG_THRESHOLD_SECONDS
) -> str:
    """FAIL-03's actual degradation rule: a replica whose lag is unknown OR
    over `threshold_seconds` is never used — the caller falls back to the
    primary and reports that honestly, rather than silently serving
    (possibly stale) replica data (Spec v1.0 §5.7). Returns the literal
    `data_freshness` value the availability response carries.
    """
    if lag_seconds is None or lag_seconds > threshold_seconds:
        return "primary"
    return "replica"
