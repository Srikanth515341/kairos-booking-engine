"""RFC v1.0 §9.4: "a tzdata version check runs on deploy AND on schedule."
The scheduled half is the `check-tzdata-drift`/`rematerialize-stale-
series` Celery Beat entries (kairos/settings/base.py); this command is the
"on deploy" half — invoked once by a deploy pipeline (Phase 30 builds one;
none exists yet) right after a new tzdata version ships, rather than
waiting for the next scheduled run. Calls the SAME functions Beat calls —
no separate "deploy-time" implementation to keep in sync.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand

from kairos.bookings.tasks import rematerialize_stale_series, rolling_materialize_series


class Command(BaseCommand):
    help = (
        "Run rolling materialization and tzdata re-materialization synchronously "
        "(RFC v1.0 §9.4's 'on deploy' trigger) — the same functions Celery Beat "
        "calls on schedule, run here directly with no worker required."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        materialization_findings = rolling_materialize_series()
        self.stdout.write(
            f"series_materialization: {json.dumps(materialization_findings, default=str)}"
        )

        rematerialization_findings = rematerialize_stale_series()
        self.stdout.write(
            f"tzdata_rematerialization: {json.dumps(rematerialization_findings, default=str)}"
        )
