"""kairos.core.replica — Test Plan v1.0 FAIL-03 (Implementation Plan Phase
28; Rollout v1.0 §2.2). No real replica exists yet (Phase 30) — these
tests prove the DEGRADATION LOGIC directly, against simulated lag values,
per Phase 28's own explicit scope: "do NOT stand up a real replica just
for this one test." The real-infrastructure form of FAIL-03 (an actual
lagging replica, real degradation under real lag) is deferred to Phase 30
— see docs/test-plan-compliance.md.
"""

from __future__ import annotations

from kairos.core.constants import REPLICA_LAG_THRESHOLD_SECONDS
from kairos.core.replica import current_replica_lag_seconds, select_read_source


def test_unknown_lag_falls_back_to_primary() -> None:
    # The only honest answer today: no replica is configured at all.
    assert select_read_source(None) == "primary"


def test_lag_over_threshold_falls_back_to_primary() -> None:
    assert select_read_source(REPLICA_LAG_THRESHOLD_SECONDS + 1) == "primary"


def test_lag_well_under_threshold_uses_replica() -> None:
    # Proves the function CAN route to a replica once one reports fresh
    # enough lag — not that this codebase does so today (it doesn't; see
    # current_replica_lag_seconds's own docstring).
    assert select_read_source(0.5) == "replica"


def test_lag_exactly_at_threshold_uses_replica() -> None:
    # The boundary is strictly "over," not "at or over" — matching this
    # project's own established boundary convention elsewhere (e.g. the
    # advance-booking horizon's `start > now + N days`).
    assert select_read_source(float(REPLICA_LAG_THRESHOLD_SECONDS)) == "replica"


def test_current_replica_lag_seconds_reports_none_honestly() -> None:
    # No replica exists in this deployment (Phase 30 builds one) — a
    # fabricated non-None value here would make select_read_source route
    # reads to infrastructure that doesn't exist.
    assert current_replica_lag_seconds() is None
