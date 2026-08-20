from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from django.db import IntegrityError

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser
from kairos.resources.models import Resource


@pytest.mark.django_db
def test_overlapping_confirmed_bookings_raise_exclusion_violation() -> None:
    """Implementation Plan Phase 2 DoD smoke test.

    Not CONC-01 (Test Plan v1.0 §2) — that is Phase 3's full 200-way
    concurrency proof under a real barrier release. This is a single-
    threaded sanity check that `no_overlapping_bookings` exists and fires:
    a second, overlapping insert against an already-committed row must be
    rejected with SQLSTATE 23P01.
    """
    owner = AppUser.objects.create(email="owner@example.com", display_name="Owner")
    resource = Resource.objects.create(
        name="Room 1",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=owner,
    )

    start = datetime(2026, 9, 1, 13, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    Booking.objects.create(resource=resource, user=owner, time_range=(start, end))

    overlap_start = start + timedelta(minutes=30)
    overlap_end = overlap_start + timedelta(hours=1)

    with pytest.raises(IntegrityError) as exc_info:
        Booking.objects.create(
            resource=resource, user=owner, time_range=(overlap_start, overlap_end)
        )

    assert exc_info.value.__cause__.sqlstate == "23P01"  # type: ignore[union-attr]
