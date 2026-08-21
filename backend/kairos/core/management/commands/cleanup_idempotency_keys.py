"""Deletes idempotency_key rows older than IDEMPOTENCY_RETENTION_HOURS
(Spec v1.0 §7; PRD FR37). Scheduled on a recurring interval as of
Implementation Plan Phase 21 (`kairos.core.tasks.
cleanup_idempotency_keys_task`, `IDEMPOTENCY_CLEANUP_INTERVAL_SECONDS`) —
this command calls the SAME `cleanup_expired_idempotency_keys` function
Beat now calls, the "on deploy" trigger alongside the schedule, matching
`rematerialize_series`/`run_correctness_checks`'s own precedent (Phases
13/20) rather than duplicating the delete logic here.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand

from kairos.core.idempotency import cleanup_expired_idempotency_keys


class Command(BaseCommand):
    help = "Delete idempotency_key rows older than IDEMPOTENCY_RETENTION_HOURS."

    def handle(self, *args: Any, **options: Any) -> None:
        findings = cleanup_expired_idempotency_keys()
        self.stdout.write(f"Deleted {findings['deleted_count']} expired idempotency key(s).")
