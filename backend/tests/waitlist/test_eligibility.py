"""`find_eligible_entries` (kairos.waitlist.services) — Test Plan v1.0
WL-04, PRD FR21/FR22. Direct unit tests against the query itself, not
through a real cancellation-triggered cascade: offer creation is Phase 16
(Scope — DEFERRED in Implementation Plan Phase 14), so this is the same
"mechanism before its real caller" situation Phase 11's `expand_occurrences`
was in before Phase 12 gave it an endpoint — proven directly here, on its
own terms.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from kairos.waitlist.models import WaitlistEntry, WaitlistEntryStatus
from kairos.waitlist.services import find_eligible_entries


@pytest.mark.django_db
def test_eligibility_is_containment_not_overlap(
    app_user: AppUser, active_resource: Resource
) -> None:
    """WL-04 ★. User W waitlisted for 10:00-11:00 (using an arbitrary
    future anchor, not literal wall-clock 10:00, since bookable hours only
    matter for booking policy, not waitlist join — this test exercises
    the query directly, bypassing the API layer's own bookable-hours
    check entirely).

    (1) A freed 10:00-10:30 range does NOT fully contain W's 10:00-11:00
    request -> W is NOT eligible. This is exactly the case that would
    incorrectly pass under && (overlap) semantics, which is why this test
    exists (PRD FR21's own stated purpose).
    (2) A freed 10:00-11:00 range DOES fully contain it -> W IS eligible.
    """
    anchor = timezone.now() + timedelta(days=1)
    w_start = anchor
    w_end = anchor + timedelta(hours=1)
    entry = WaitlistEntry.objects.create(
        resource=active_resource, user=app_user, time_range=(w_start, w_end)
    )

    partial_freed_start = w_start
    partial_freed_end = w_start + timedelta(minutes=30)
    eligible_after_partial = find_eligible_entries(
        active_resource.id, partial_freed_start, partial_freed_end
    )
    assert entry not in eligible_after_partial

    full_freed_start = w_start
    full_freed_end = w_end
    eligible_after_full = find_eligible_entries(
        active_resource.id, full_freed_start, full_freed_end
    )
    assert eligible_after_full == [entry]


@pytest.mark.django_db
def test_eligibility_excludes_non_waiting_entries(
    app_user: AppUser, active_resource: Resource
) -> None:
    anchor = timezone.now() + timedelta(days=1)
    start, end = anchor, anchor + timedelta(hours=1)
    WaitlistEntry.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(start, end),
        status=WaitlistEntryStatus.CANCELLED,
    )

    assert find_eligible_entries(active_resource.id, start, end) == []


@pytest.mark.django_db
def test_eligibility_orders_fcfs_by_joined_at(app_user: AppUser, active_resource: Resource) -> None:
    """PRD FR22 — ordered by join timestamp, earliest first, with `id` as
    the deterministic tiebreak (matching idx_waitlist_entry_order's own
    column order)."""
    other = AppUser.objects.create(email="second-in-line@example.com", display_name="Second")
    anchor = timezone.now() + timedelta(days=1)
    start, end = anchor, anchor + timedelta(hours=1)

    first = WaitlistEntry.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )
    second = WaitlistEntry.objects.create(
        resource=active_resource, user=other, time_range=(start, end)
    )
    # joined_at is server-set (SEC-03(a)); pin explicit values here rather
    # than relying on real creation-order timing, so the assertion below
    # tests ORDERING logic, not wall-clock granularity.
    now = timezone.now()
    WaitlistEntry.objects.filter(id=first.id).update(joined_at=now)
    WaitlistEntry.objects.filter(id=second.id).update(joined_at=now + timedelta(seconds=1))

    eligible = find_eligible_entries(active_resource.id, start, end)
    assert [e.id for e in eligible] == [first.id, second.id]


@pytest.mark.django_db
def test_eligibility_scoped_to_resource(app_user: AppUser, active_resource: Resource) -> None:
    other_resource = Resource.objects.create(
        name="Other Room",
        timezone="UTC",
        bookable_start_time=active_resource.bookable_start_time,
        bookable_end_time=active_resource.bookable_end_time,
        created_by=app_user,
    )
    anchor = timezone.now() + timedelta(days=1)
    start, end = anchor, anchor + timedelta(hours=1)
    WaitlistEntry.objects.create(resource=other_resource, user=app_user, time_range=(start, end))

    assert find_eligible_entries(active_resource.id, start, end) == []
