"""RFC v1.0 §14: schema assertion "runs on every deploy and hourly in
production." The scheduled half is the `run-reconciliation`/`run-
schema-assertion` Celery Beat entries (kairos/settings/base.py); this
command is the "on deploy" half — invoked once by a deploy pipeline
(Phase 30 builds one; none exists yet) right after a new release ships,
rather than waiting for the next scheduled run, so a predicate narrowed
by the very migration just applied is caught immediately. Calls the SAME
functions Beat calls — no separate "deploy-time" implementation to keep
in sync, the identical pattern `rematerialize_series` (Phase 13)
established for the other "on deploy + on schedule" pair.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from django.core.management.base import BaseCommand

from kairos.core.reconciliation import check_reconciliation
from kairos.core.schema_assertion import check_schema_assertion


class Command(BaseCommand):
    help = (
        "Run schema assertion and reconciliation synchronously (RFC v1.0 §14's "
        "'on deploy' trigger) — the same functions Celery Beat calls on schedule, "
        "run here directly with no worker required. Exits non-zero if either check "
        "fails, so a deploy pipeline can gate on it."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        schema_findings = check_schema_assertion()
        self.stdout.write(f"schema_assertion: {json.dumps(schema_findings, default=str)}")

        reconciliation_findings = check_reconciliation()
        self.stdout.write(f"reconciliation: {json.dumps(reconciliation_findings, default=str)}")

        schema_ok = (
            schema_findings.get("constraint_exists")
            and schema_findings.get("covers_confirmed")
            and schema_findings.get("covers_held")
        )
        reconciliation_ok = reconciliation_findings.get("overlaps_found") == 0

        if not (schema_ok and reconciliation_ok):
            self.stderr.write(self.style.ERROR("correctness check FAILED — see findings above"))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS("both checks passed"))
