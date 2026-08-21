"""RECLAIM-02 (reaper cascade with no booking traffic) and WL-05 Part B
(heartbeat staleness is detectable) — Test Plan v1.0 §3/§4; Implementation
Plan Phase 17.

"Wait past expiry" (RECLAIM-02's own Setup) is simulated, not slept
through — Test Plan v1.0 §13 itself flags "controllable time for expiry
tests" as an environment requirement ("sleeping through a real 15-minute
offer window is not viable in CI"). A hold seeded with `expires_at`
already in the past, then `reap_expired_holds()` called directly, is the
same "mechanism proven directly, before its real caller (Celery Beat)"
pattern this project has used since Phase 8.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from kairos.bookings.models import Booking, BookingStatus
from kairos.core.models import AuditLog, SystemCheckRun
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from kairos.waitlist.models import (
    WaitlistEntry,
    WaitlistEntryStatus,
    WaitlistOffer,
    WaitlistOfferStatus,
)
from kairos.waitlist.services import hold_reaper_heartbeat_is_stale, reap_expired_holds


def _seed_offer(
    resource: Resource,
    holder: AppUser,
    start: datetime,
    end: datetime,
    *,
    expires_at: datetime,
) -> tuple[Booking, WaitlistOffer, WaitlistEntry]:
    entry = WaitlistEntry.objects.create(resource=resource, user=holder, time_range=(start, end))
    hold = Booking.objects.create(
        resource=resource,
        user=holder,
        time_range=(start, end),
        status=BookingStatus.HELD,
        expires_at=expires_at,
    )
    offer = WaitlistOffer.objects.create(
        waitlist_entry=entry,
        hold_booking=hold,
        resource=resource,
        time_range=(start, end),
        status=WaitlistOfferStatus.ACTIVE,
        expires_at=expires_at,
    )
    WaitlistEntry.objects.filter(id=entry.id).update(status=WaitlistEntryStatus.OFFERED)
    entry.refresh_from_db()
    return hold, offer, entry


@pytest.mark.django_db
def test_reclaim_02_reaper_cascades_to_next_entry_with_no_booking_traffic(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    w = AppUser.objects.create(email="reclaim02-w@example.com", display_name="W")
    x = AppUser.objects.create(email="reclaim02-x@example.com", display_name="X")

    hold, offer, entry_w = _seed_offer(
        active_resource, w, start, end, expires_at=timezone.now() - timedelta(minutes=1)
    )
    entry_x = WaitlistEntry.objects.create(
        resource=active_resource, user=x, time_range=(start, end)
    )
    # X joined after W (already true by creation order) — FCFS still picks
    # X since W's entry is no longer 'waiting'.

    findings = reap_expired_holds()

    assert findings["holds_reclaimed"] == 1
    assert findings["cascades_triggered"] == 1

    hold.refresh_from_db()
    assert hold.status == BookingStatus.CANCELLED
    assert hold.expires_at is None

    offer.refresh_from_db()
    assert offer.status == WaitlistOfferStatus.EXPIRED
    entry_w.refresh_from_db()
    assert entry_w.status == WaitlistEntryStatus.EXPIRED

    new_offer = WaitlistOffer.objects.exclude(id=offer.id).get()
    assert new_offer.waitlist_entry_id == entry_x.id
    entry_x.refresh_from_db()
    assert entry_x.status == WaitlistEntryStatus.OFFERED
    new_hold = Booking.objects.get(id=new_offer.hold_booking_id)
    assert new_hold.status == BookingStatus.HELD
    assert new_hold.user_id == x.id


@pytest.mark.django_db
def test_reclaim_02_no_eligible_entry_releases_hold_without_cascading(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    w = AppUser.objects.create(email="reclaim02-solo@example.com", display_name="W")
    hold, offer, entry = _seed_offer(
        active_resource, w, start, end, expires_at=timezone.now() - timedelta(minutes=1)
    )

    findings = reap_expired_holds()

    assert findings["holds_reclaimed"] == 1
    assert findings["cascades_triggered"] == 0
    hold.refresh_from_db()
    assert hold.status == BookingStatus.CANCELLED
    assert not Booking.objects.filter(status=BookingStatus.HELD).exists()


@pytest.mark.django_db
def test_reclaim_02_writes_hold_reaper_heartbeat(
    app_user: AppUser, active_resource: Resource
) -> None:
    reap_expired_holds()
    run = SystemCheckRun.objects.get(check_name=SystemCheckRun.CheckName.HOLD_REAPER)
    assert run.status == SystemCheckRun.Status.PASS
    assert "holds_reclaimed" in run.findings


@pytest.mark.django_db
def test_reclaim_02_reclaimed_hold_writes_an_audit_row(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    w = AppUser.objects.create(email="reclaim02-audit@example.com", display_name="W")
    hold, _offer, _entry = _seed_offer(
        active_resource, w, start, end, expires_at=timezone.now() - timedelta(minutes=1)
    )

    reap_expired_holds()

    events = list(AuditLog.objects.filter(entity_type="booking", entity_id=hold.id))
    update_events = [e for e in events if e.action == "update"]
    assert len(update_events) == 1
    assert update_events[0].actor_type == "system"
    assert update_events[0].actor_id is None
    assert update_events[0].after_state["status"] == "cancelled"


# --------------------------------------------------------------------
# WL-05 Part B — detection actually works
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_wl_05b_no_heartbeat_at_all_is_stale() -> None:
    """If the reaper has never run, that is itself the signal — "no
    evidence of health" is not health."""
    assert hold_reaper_heartbeat_is_stale() is True


@pytest.mark.django_db
def test_wl_05b_seeded_stale_hold_with_no_recent_run_is_detected_stale() -> None:
    """Test Plan's own seed: a hold sitting `status='held' AND expires_at
    < now() - interval '5 minutes'` with nothing having reclaimed it —
    the heartbeat itself (no HOLD_REAPER row at all, or one old enough)
    is what makes that detectable, independent of the hold row itself."""
    SystemCheckRun.objects.create(
        check_name=SystemCheckRun.CheckName.HOLD_REAPER,
        status=SystemCheckRun.Status.PASS,
        findings={},
    )
    SystemCheckRun.objects.filter(check_name=SystemCheckRun.CheckName.HOLD_REAPER).update(
        run_at=timezone.now() - timedelta(hours=1)
    )
    assert hold_reaper_heartbeat_is_stale() is True


@pytest.mark.django_db
def test_wl_05b_recent_heartbeat_is_not_stale() -> None:
    reap_expired_holds()  # writes a fresh HOLD_REAPER row, run_at ~= now
    assert hold_reaper_heartbeat_is_stale() is False
