"""Generic staleness detection (Implementation Plan Phase 20; RFC v1.0
§14; Rollout v1.0 §6.1). All six correctness/background-job checks share
one property: they fail SILENTLY — no exception, no error rate, just
absence — so "no heartbeat within threshold" is the only signal Rollout
v1.0 §6.1's table gives for five of the six (`reconciliation` is judged
on its OWN finding — `overlaps_found > 0` — never on staleness alone;
see `kairos.core.reconciliation`).

`kairos.waitlist.services.hold_reaper_heartbeat_is_stale` (Phase 17) was
the first, bespoke version of exactly this check. This is that same
mechanism, generalized once four more checks needed the identical
logic — this project's own "worth extracting once real duplication
exists, not before" threshold (`RecordableConflictError`, Phase 16;
`request_id()`, Phase 19).
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone as django_timezone

from kairos.core.models import SystemCheckRun


def heartbeat_is_stale(
    check_name: str, threshold_seconds: int, *, now: datetime | None = None
) -> bool:
    """A missing heartbeat (the check has never run at all) counts as
    stale too — "no evidence of health" is not health, the same
    principle `hold_reaper_heartbeat_is_stale` already established.
    """
    now = now or django_timezone.now()
    latest = SystemCheckRun.objects.filter(check_name=check_name).order_by("-run_at").first()
    if latest is None:
        return True
    return (now - latest.run_at).total_seconds() > threshold_seconds
