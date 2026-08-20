from __future__ import annotations

from datetime import time

import pytest

from kairos.identity.models import AppUser
from kairos.resources.models import Resource


@pytest.fixture
def resource_and_user(transactional_db: None) -> dict[str, str]:
    """A committed AppUser + Resource, visible to independent psycopg
    connections opened by worker threads.

    Requires `transactional_db` (not the default `db` fixture): pytest-
    django's default `db` fixture wraps each test in an outer transaction
    it rolls back at the end, so a separate psycopg connection in another
    thread would never see this row committed — every worker insert would
    fail on the resource_id/user_id foreign keys before the exclusion
    constraint is ever exercised. `transactional_db` uses Django's
    autocommit-by-default TransactionTestCase behavior instead, so `.create()`
    below commits immediately and is visible cross-connection.
    """
    owner = AppUser.objects.create(
        email="concurrency-test@example.com", display_name="Concurrency Test Owner"
    )
    resource = Resource.objects.create(
        name="Concurrency Test Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=owner,
    )
    return {"resource_id": str(resource.id), "user_id": str(owner.id)}
