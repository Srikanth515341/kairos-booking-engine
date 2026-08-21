"""`dispatch_cascade` (Implementation Plan Phase 17; RFC v1.0 §4.3; Test
Plan v1.0 WL-06) never lets a broker failure propagate out of a
`transaction.on_commit()` callback. This is a REGRESSION GUARD, not the
proof itself — `CELERY_TASK_ALWAYS_EAGER` (kairos.settings.test) means
`.delay()` never touches a real broker under pytest at all, so a genuine
Redis outage cannot be reproduced here. WL-06 was verified LIVE instead,
against the real `docker compose` stack with Redis actually stopped
(`kairos_celery_worker` logging `cascade_dispatch_failed_broker_
unavailable` with a real `kombu.exceptions.OperationalError`, and
`POST /bookings/{id}/cancel` still returning 200) — see CLAUDE.md for the
full transcript. This test only guards against a future change silently
removing the try/except that made that possible.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from kairos.waitlist.tasks import dispatch_cascade


def test_dispatch_cascade_swallows_a_broker_failure_and_logs_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise ConnectionError("simulated broker outage")

    monkeypatch.setattr("kairos.waitlist.tasks.create_offer_for_freed_range_task.delay", _boom)

    start = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    with caplog.at_level(logging.ERROR, logger="kairos.waitlist.tasks"):
        dispatch_cascade("resource-id", start, start + timedelta(hours=1), "req-id")

    assert any(r.message == "cascade_dispatch_failed_broker_unavailable" for r in caplog.records)
