from __future__ import annotations

from datetime import time

import pytest

from kairos.identity.models import AppUser
from kairos.resources.models import Resource


@pytest.fixture
def app_user(db: None) -> AppUser:
    return AppUser.objects.create(email="test-user@example.com", display_name="Test User")


@pytest.fixture
def active_resource(db: None, app_user: AppUser) -> Resource:
    return Resource.objects.create(
        name="Test Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=app_user,
    )
