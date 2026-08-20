"""Deletes idempotency_key rows older than IDEMPOTENCY_RETENTION_HOURS
(Spec v1.0 §7; PRD FR37). Scheduling this on a recurring interval is
Phase 21's job — this command exists now so that job has something to call.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from kairos.core.constants import IDEMPOTENCY_RETENTION_HOURS
from kairos.core.models import IdempotencyKey


class Command(BaseCommand):
    help = "Delete idempotency_key rows older than IDEMPOTENCY_RETENTION_HOURS."

    def handle(self, *args: Any, **options: Any) -> None:
        cutoff = timezone.now() - timedelta(hours=IDEMPOTENCY_RETENTION_HOURS)
        deleted_count, _ = IdempotencyKey.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(f"Deleted {deleted_count} expired idempotency key(s).")
