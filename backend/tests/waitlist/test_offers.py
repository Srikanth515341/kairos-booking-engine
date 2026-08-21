"""Offer creation (cancellation → cascade), confirm, decline — Spec v1.0
§4.2/§4.3/§5.13, Implementation Plan Phase 16's Definition of Done, and
Test Plan v1.0 WL-03. WL-01/WL-02 (the barrier-released races) live in
tests/concurrency/.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking, BookingStatus
from kairos.bookings.services import BookingCancelRequest, cancel_booking
from kairos.core.models import AuditActorType, AuditLog
from kairos.identity.models import AppUser
from kairos.resources.models import Resource
from kairos.waitlist.models import (
    WaitlistEntry,
    WaitlistEntryStatus,
    WaitlistOffer,
    WaitlistOfferStatus,
)

WAITLIST_OFFERS_URL = "/api/v1/waitlist-offers"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser, idempotency_key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key or uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def _cancel_and_capture_cascade(booking: Booking, actor: AppUser, request_id: str) -> None:
    """Cancels `booking` for real and manually fires the `on_commit`
    callback it registers — Django's officially blessed way to test
    on_commit-triggered code under the default (rollback-based)
    `@pytest.mark.django_db` fixture, without needing `transaction=True`
    real commits the way tests/concurrency/ does. Combined with
    `CELERY_TASK_ALWAYS_EAGER` (kairos.settings.test), the captured
    callback's `.delay()` call runs `create_offer_for_freed_range`
    synchronously, in-process, right here.
    """
    with TestCase.captureOnCommitCallbacks(execute=True):
        cancel_booking(
            BookingCancelRequest(
                booking=booking,
                actor=actor,
                actor_type=AuditActorType.USER,
                reason=None,
                request_id=request_id,
            )
        )


@pytest.mark.django_db
def test_cancellation_creates_hold_backed_offer_for_correct_entry(
    app_user: AppUser, active_resource: Resource
) -> None:
    """PRD FR23: on cancellation, the highest-ranked ELIGIBLE entry gets a
    hold, created BEFORE the offer — proven here by asserting both exist
    and are correctly linked, not merely that "something" was created."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )

    waiter = AppUser.objects.create(email="offer-waiter@example.com", display_name="Waiter")
    entry = WaitlistEntry.objects.create(
        resource=active_resource,
        user=waiter,
        time_range=(start + timedelta(minutes=10), start + timedelta(minutes=40)),
    )
    # .create() leaves time_range as the plain tuple assigned in Python,
    # not the Range object a fresh SELECT returns (same reasoning as
    # create_booking's identical refresh_from_db call).
    entry.refresh_from_db()

    _cancel_and_capture_cascade(booking, app_user, "test-cancel-offer")

    offer = WaitlistOffer.objects.get(waitlist_entry=entry)
    assert offer.status == WaitlistOfferStatus.ACTIVE
    hold = Booking.objects.get(id=offer.hold_booking_id)
    assert hold.status == BookingStatus.HELD
    assert hold.user_id == waiter.id
    assert hold.time_range.lower == entry.time_range.lower
    assert hold.time_range.upper == entry.time_range.upper
    assert hold.expires_at == offer.expires_at
    entry.refresh_from_db()
    assert entry.status == WaitlistEntryStatus.OFFERED


@pytest.mark.django_db
def test_cancellation_with_no_eligible_entry_creates_no_offer(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )

    _cancel_and_capture_cascade(booking, app_user, "test-cancel-no-offer")

    assert not WaitlistOffer.objects.exists()
    assert not Booking.objects.filter(status=BookingStatus.HELD).exists()


@pytest.mark.django_db
def test_cancellation_writes_an_audit_row_for_the_hold(
    app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )
    waiter = AppUser.objects.create(email="offer-audit-waiter@example.com", display_name="Waiter")
    WaitlistEntry.objects.create(
        resource=active_resource,
        user=waiter,
        time_range=(start + timedelta(minutes=10), start + timedelta(minutes=40)),
    )

    _cancel_and_capture_cascade(booking, app_user, "test-cancel-audit")

    offer = WaitlistOffer.objects.get()
    hold_events = list(
        AuditLog.objects.filter(entity_type="booking", entity_id=offer.hold_booking_id)
    )
    assert len(hold_events) == 1
    assert hold_events[0].action == "insert"
    # The hold is a SYSTEM write (the cascade worker), not the cancelling
    # user's own action — actor_type='system' (Phase 13's convention).
    assert hold_events[0].actor_type == "system"
    assert hold_events[0].actor_id is None

    offer_events = list(AuditLog.objects.filter(entity_type="waitlist_offer", entity_id=offer.id))
    assert len(offer_events) == 1
    assert offer_events[0].action == "insert"


# --------------------------------------------------------------------
# POST /waitlist-offers/{id}/confirm
# --------------------------------------------------------------------


def _make_offer(resource: Resource, waiter: AppUser, other_owner: AppUser) -> WaitlistOffer:
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(resource=resource, user=other_owner, time_range=(start, end))
    WaitlistEntry.objects.create(
        resource=resource,
        user=waiter,
        time_range=(start + timedelta(minutes=10), start + timedelta(minutes=40)),
    )
    _cancel_and_capture_cascade(booking, other_owner, f"test-setup-{uuid.uuid4()}")
    return WaitlistOffer.objects.get()


@pytest.mark.django_db
def test_confirm_converts_hold_in_place_no_second_row(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="confirm-waiter@example.com", display_name="Waiter")
    offer = _make_offer(active_resource, waiter, app_user)
    hold_id = offer.hold_booking_id

    response = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/confirm", **_headers(waiter))
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == str(hold_id)
    assert body["status"] == "confirmed"
    assert body["waitlist_offer_id"] == str(offer.id)

    assert Booking.objects.filter(resource=active_resource, user=waiter).count() == 1
    hold = Booking.objects.get(id=hold_id)
    assert hold.status == BookingStatus.CONFIRMED
    assert hold.expires_at is None

    offer.refresh_from_db()
    assert offer.status == WaitlistOfferStatus.CONFIRMED
    entry = WaitlistEntry.objects.get(id=offer.waitlist_entry_id)
    assert entry.status == WaitlistEntryStatus.FULFILLED


@pytest.mark.django_db
def test_confirm_expired_offer_returns_409(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="expired-waiter@example.com", display_name="Waiter")
    offer = _make_offer(active_resource, waiter, app_user)
    Booking.objects.filter(id=offer.hold_booking_id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )

    response = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/confirm", **_headers(waiter))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "offer_expired"

    hold = Booking.objects.get(id=offer.hold_booking_id)
    assert hold.status == BookingStatus.HELD


@pytest.mark.django_db
def test_confirm_by_non_owner_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="real-waiter@example.com", display_name="Waiter")
    stranger = AppUser.objects.create(email="confirm-stranger@example.com", display_name="Stranger")
    offer = _make_offer(active_resource, waiter, app_user)

    response = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/confirm", **_headers(stranger))
    assert response.status_code == 404


@pytest.mark.django_db
def test_confirm_nonexistent_offer_returns_404(client: APIClient, app_user: AppUser) -> None:
    response = client.post(f"{WAITLIST_OFFERS_URL}/{uuid.uuid4()}/confirm", **_headers(app_user))
    assert response.status_code == 404


@pytest.mark.django_db
def test_confirm_without_idempotency_key_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="no-key-waiter@example.com", display_name="Waiter")
    offer = _make_offer(active_resource, waiter, app_user)

    response = client.post(
        f"{WAITLIST_OFFERS_URL}/{offer.id}/confirm", HTTP_X_DEV_USER_ID=str(waiter.id)
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_confirm_replay_returns_identical_response(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="replay-waiter@example.com", display_name="Waiter")
    offer = _make_offer(active_resource, waiter, app_user)
    key = uuid.uuid4()

    first = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/confirm", **_headers(waiter, key))
    assert first.status_code == 201

    second = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/confirm", **_headers(waiter, key))
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second["Idempotent-Replay"] == "true"
    assert Booking.objects.filter(resource=active_resource, user=waiter).count() == 1


# --------------------------------------------------------------------
# POST /waitlist-offers/{id}/decline
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_decline_releases_hold_and_cascades(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="declining-waiter@example.com", display_name="Decliner")
    next_waiter = AppUser.objects.create(email="next-in-line@example.com", display_name="Next")

    offer = _make_offer(active_resource, waiter, app_user)
    entry = WaitlistEntry.objects.get(id=offer.waitlist_entry_id)
    # A second entry, eligible for the SAME range, joined AFTER the first
    # — should be next in line once the first declines.
    WaitlistEntry.objects.create(
        resource=active_resource, user=next_waiter, time_range=entry.time_range
    )

    with TestCase.captureOnCommitCallbacks(execute=True):
        response = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/decline", **_headers(waiter))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "declined"

    offer.refresh_from_db()
    assert offer.status == WaitlistOfferStatus.DECLINED
    original_hold = Booking.objects.get(id=offer.hold_booking_id)
    assert original_hold.status == BookingStatus.CANCELLED
    assert original_hold.expires_at is None

    entry.refresh_from_db()
    assert entry.status == WaitlistEntryStatus.EXPIRED

    # Cascade reached the next entry, not a re-offer to the declining one.
    new_offer = WaitlistOffer.objects.exclude(id=offer.id).get()
    assert new_offer.waitlist_entry_id != entry.id
    new_entry = WaitlistEntry.objects.get(id=new_offer.waitlist_entry_id)
    assert new_entry.user_id == next_waiter.id
    assert new_entry.status == WaitlistEntryStatus.OFFERED


@pytest.mark.django_db
def test_decline_already_resolved_returns_409(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="double-decline@example.com", display_name="Waiter")
    offer = _make_offer(active_resource, waiter, app_user)

    with TestCase.captureOnCommitCallbacks(execute=True):
        first = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/decline", **_headers(waiter))
    assert first.status_code == 200

    second = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/decline", **_headers(waiter))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "offer_already_resolved"


@pytest.mark.django_db
def test_decline_by_non_owner_returns_404(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    waiter = AppUser.objects.create(email="decline-real@example.com", display_name="Waiter")
    stranger = AppUser.objects.create(email="decline-stranger@example.com", display_name="Stranger")
    offer = _make_offer(active_resource, waiter, app_user)

    response = client.post(f"{WAITLIST_OFFERS_URL}/{offer.id}/decline", **_headers(stranger))
    assert response.status_code == 404


# --------------------------------------------------------------------
# WL-03 — Cascade reaches the correct next candidate
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_wl_03_cascade_reaches_entry_two_not_three(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """WL-03: offer to entry 1 via cancellation; force expiry (decline is
    this phase's real, in-scope trigger for "sooner than expiry would" —
    Spec v1.0 §5.13 — the same effect Phase 17's reaper will eventually
    also produce); assert entry 2, not 3, receives the next offer."""
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )

    entry_range = (start + timedelta(minutes=10), start + timedelta(minutes=40))
    w1 = AppUser.objects.create(email="wl03-w1@example.com", display_name="W1")
    w2 = AppUser.objects.create(email="wl03-w2@example.com", display_name="W2")
    w3 = AppUser.objects.create(email="wl03-w3@example.com", display_name="W3")
    e1 = WaitlistEntry.objects.create(resource=active_resource, user=w1, time_range=entry_range)
    WaitlistEntry.objects.filter(id=e1.id).update(joined_at=timezone.now())
    e2 = WaitlistEntry.objects.create(resource=active_resource, user=w2, time_range=entry_range)
    WaitlistEntry.objects.filter(id=e2.id).update(joined_at=timezone.now() + timedelta(seconds=1))
    e3 = WaitlistEntry.objects.create(resource=active_resource, user=w3, time_range=entry_range)
    WaitlistEntry.objects.filter(id=e3.id).update(joined_at=timezone.now() + timedelta(seconds=2))

    _cancel_and_capture_cascade(booking, app_user, "wl03-cancel")
    first_offer = WaitlistOffer.objects.get()
    assert first_offer.waitlist_entry_id == e1.id

    with TestCase.captureOnCommitCallbacks(execute=True):
        result = client.post(f"{WAITLIST_OFFERS_URL}/{first_offer.id}/decline", **_headers(w1))
    assert result.status_code == 200

    second_offer = WaitlistOffer.objects.exclude(id=first_offer.id).get()
    assert second_offer.waitlist_entry_id == e2.id, "cascade must reach entry 2, not entry 3"


@pytest.mark.django_db
def test_wl_03_cascade_skips_withdrawn_entry(app_user: AppUser, active_resource: Resource) -> None:
    """Extension: entry 2 withdrew (status='cancelled') before cascade
    reaches it — assert entry 3 is offered, confirming the eligibility
    query filters on status='waiting' and does not offer to a withdrawn
    entry by position alone.

    Entry 1 is ALREADY resolved (fulfilled, from some earlier round) here
    — "before cascade reaches it" (Test Plan's own wording) means this is
    the state once cascade has already moved past entry 1, not entry 1
    freshly competing for THIS offer too (which it would trivially win by
    FCFS, telling us nothing about entry 2 being skipped).
    """
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(
        resource=active_resource, user=app_user, time_range=(start, end)
    )

    entry_range = (start + timedelta(minutes=10), start + timedelta(minutes=40))
    w1 = AppUser.objects.create(email="wl03b-w1@example.com", display_name="W1")
    w2 = AppUser.objects.create(email="wl03b-w2@example.com", display_name="W2")
    w3 = AppUser.objects.create(email="wl03b-w3@example.com", display_name="W3")
    e1 = WaitlistEntry.objects.create(
        resource=active_resource,
        user=w1,
        time_range=entry_range,
        status=WaitlistEntryStatus.FULFILLED,
    )
    WaitlistEntry.objects.filter(id=e1.id).update(joined_at=timezone.now())
    e2 = WaitlistEntry.objects.create(
        resource=active_resource,
        user=w2,
        time_range=entry_range,
        status=WaitlistEntryStatus.CANCELLED,
    )
    WaitlistEntry.objects.filter(id=e2.id).update(joined_at=timezone.now() + timedelta(seconds=1))
    e3 = WaitlistEntry.objects.create(resource=active_resource, user=w3, time_range=entry_range)
    WaitlistEntry.objects.filter(id=e3.id).update(joined_at=timezone.now() + timedelta(seconds=2))

    _cancel_and_capture_cascade(booking, app_user, "wl03b-cancel")

    offer = WaitlistOffer.objects.get()
    assert offer.waitlist_entry_id == e3.id, "cascade must skip the withdrawn entry 2"
    e2.refresh_from_db()
    assert e2.status == WaitlistEntryStatus.CANCELLED  # untouched
