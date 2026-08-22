"""Idempotency Test Suite (Test Plan v1.0 §7): IDEM-01 through IDEM-04,
IDEM-06, IDEM-09, IDEM-10, IDEM-11 — the set Implementation Plan Phase 5
scopes in. IDEM-05 (recurring replay) is tests/bookings/test_recurring_
series.py (Phase 12). IDEM-07 (process-kill fault injection) and IDEM-08
(proxy-level response drop) are the last two rows in this file
(Implementation Plan Phase 28).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, time, timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.core.idempotency import run_idempotent_write
from kairos.core.models import IdempotencyKey, IdempotencyKeyStatus
from kairos.identity.models import AppUser
from kairos.identity.oidc import issue_session_token
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _headers(user: AppUser, idempotency_key: uuid.UUID) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key),
    }


def _bearer_headers(user: AppUser, idempotency_key: uuid.UUID) -> dict[str, str]:
    """A REAL minted session token (Phase 9) — see
    tests/bookings/test_views.py's identical helper for the full
    rationale. Used on IDEM-01/02 specifically: idempotency's key-claim
    INSERT is scoped to `(user_id, key)`, and IDEM-01/02 are the two most
    fundamental idempotency behaviors (first use, exact replay) — proving
    they work when `user_id` comes from a real authenticated principal,
    not just the stub, is the representative case for this mechanism.
    """
    token, _ = issue_session_token(user.id)
    return {
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_IDEMPOTENCY_KEY": str(idempotency_key),
    }


def _make_resource(owner: AppUser, name: str = "Idem Test Room") -> Resource:
    return Resource.objects.create(
        name=name,
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=owner,
    )


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------------
# Schema — the composite PK, verified directly (DoD: "verify with \d
# idempotency_key").
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idempotency_key_pk_is_composite_user_id_and_key() -> None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'idempotency_key'::regclass AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
        pk_columns = [row[0] for row in cur.fetchall()]
    assert pk_columns == ["user_id", "key"]


@pytest.mark.django_db
def test_in_progress_row_is_writable_with_null_response_columns(
    app_user: AppUser,
) -> None:
    row = IdempotencyKey.objects.create(
        user=app_user,
        key=uuid.uuid4(),
        endpoint="POST /api/v1/bookings",
        request_body_hash="deadbeef",
        status=IdempotencyKeyStatus.IN_PROGRESS,
    )
    assert row.response_status is None
    assert row.response_body is None


# --------------------------------------------------------------------
# Regression: session settings on the key-claim INSERT itself (RFC v1.0
# §11.2/§7.1, §12) — Phase 5's report claimed the timeout settings were
# "moved into a shared core/db.py helper applied once at the top of the
# outer transaction," but run_idempotent_write never actually called it;
# only each write function's own NESTED transaction did, which runs
# strictly after the key-claim INSERT has already executed. A concurrent
# replay's key-claim insert would then block under Postgres's untimed
# defaults instead of failing fast at the intended 3s lock_timeout. Fixed
# in Phase 7 alongside the two new write paths that go through this exact
# function. Phase 8 adds a SECOND category of session variable through
# the identical mechanism/call site — app.actor_type/app.reason, read by
# write_audit_log() (Spec v1.0 §3) exactly the way Postgres itself reads
# lock_timeout — and this same test is extended, not duplicated, to prove
# both categories together rather than trusting the second one by analogy.
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_session_settings_are_active_at_the_key_claim_insert_itself(
    monkeypatch: pytest.MonkeyPatch, app_user: AppUser
) -> None:
    """Spies on the exact call `run_idempotent_write` uses for the
    key-claim INSERT (`IdempotencyKey.objects.create`) and reads
    `SHOW lock_timeout`/`SHOW statement_timeout` PLUS
    `current_setting('app.actor_type')`/`current_setting('app.reason')` on
    the SAME connection immediately before letting the real INSERT
    proceed — proving BOTH categories of session variable (write-path
    timeouts and audit actor propagation) are active at that precise
    statement, not merely "somewhere" later in the transaction (which a
    naive assertion after the fact couldn't distinguish, since
    `set_config(..., true)` is scoped to the transaction and unreadable
    once it commits).
    """
    observed: dict[str, str] = {}
    original_create = IdempotencyKey.objects.create

    def _spy_create(*args: object, **kwargs: object) -> IdempotencyKey:
        with connection.cursor() as cur:
            cur.execute("SHOW lock_timeout")
            observed["lock_timeout"] = cur.fetchone()[0]  # type: ignore[index]
            cur.execute("SHOW statement_timeout")
            observed["statement_timeout"] = cur.fetchone()[0]  # type: ignore[index]
            cur.execute("SELECT current_setting('app.actor_type', true)")
            observed["actor_type"] = cur.fetchone()[0]  # type: ignore[index]
            cur.execute("SELECT current_setting('app.reason', true)")
            observed["reason"] = cur.fetchone()[0]  # type: ignore[index]
        return original_create(*args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(IdempotencyKey.objects, "create", _spy_create)

    result = run_idempotent_write(
        user=app_user,
        key=uuid.uuid4(),
        endpoint="TEST /session-settings-probe",
        body={},
        request_id="req-session-settings-probe",
        actor_type="admin",
        reason="testing session-variable propagation",
        perform_write=lambda: (200, {"ok": True}),
    )

    assert result.response_status == 200
    assert observed["lock_timeout"] == "3s"
    assert observed["statement_timeout"] == "10s"
    assert observed["actor_type"] == "admin"
    assert observed["reason"] == "testing session-variable propagation"


# --------------------------------------------------------------------
# IDEM-01 — first use
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_01_first_use(client: APIClient, app_user: AppUser, active_resource: Resource) -> None:
    """Real minted session token (Phase 9) — proves the key-claim INSERT's
    `(user_id, key)` scoping resolves `user_id` correctly from a real
    authenticated principal.
    """
    start = timezone.now() + timedelta(hours=1)
    key = uuid.uuid4()
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_bearer_headers(app_user, key),
    )
    assert response.status_code == 201
    assert "Idempotent-Replay" not in response.headers

    row = IdempotencyKey.objects.get(user=app_user, key=key)
    assert row.status == IdempotencyKeyStatus.COMPLETED
    assert row.response_status == 201
    assert row.response_body == response.json()
    assert row.request_body_hash  # populated, non-empty


# --------------------------------------------------------------------
# IDEM-02 — exact replay
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_02_exact_replay(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """Real minted session token (Phase 9) — same rationale as IDEM-01."""
    start = timezone.now() + timedelta(hours=1)
    key = uuid.uuid4()
    payload = {
        "resource_id": str(active_resource.id),
        "start": _iso(start),
        "end": _iso(start + timedelta(hours=1)),
    }

    first = client.post(BOOKINGS_URL, data=payload, format="json", **_bearer_headers(app_user, key))
    assert first.status_code == 201

    replay = client.post(
        BOOKINGS_URL, data=payload, format="json", **_bearer_headers(app_user, key)
    )
    assert replay.status_code == 201
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()
    assert Booking.objects.filter(resource=active_resource).count() == 1


# --------------------------------------------------------------------
# IDEM-03 — replay with a different body
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_03_different_body_returns_422(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    key = uuid.uuid4()

    first = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_headers(app_user, key),
    )
    assert first.status_code == 201

    second = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start + timedelta(hours=5)),
            "end": _iso(start + timedelta(hours=6)),
        },
        format="json",
        **_headers(app_user, key),
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "idempotency_key_conflict"
    # Ground truth: the second attempt created nothing.
    assert Booking.objects.filter(resource=active_resource).count() == 1


# --------------------------------------------------------------------
# IDEM-04 — after the retention window
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_04_after_retention_window_is_processed_fresh(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    key = uuid.uuid4()
    payload = {
        "resource_id": str(active_resource.id),
        "start": _iso(start),
        "end": _iso(start + timedelta(hours=1)),
    }

    first = client.post(BOOKINGS_URL, data=payload, format="json", **_headers(app_user, key))
    assert first.status_code == 201

    # Simulate the 24h retention window having passed and the cleanup job
    # having run — .update() bypasses auto_now_add, so this backdates the
    # row without touching anything else.
    IdempotencyKey.objects.filter(user=app_user, key=key).update(
        created_at=timezone.now() - timedelta(hours=25)
    )
    call_command("cleanup_idempotency_keys")
    assert not IdempotencyKey.objects.filter(user=app_user, key=key).exists()

    replay_attempt = client.post(
        BOOKINGS_URL, data=payload, format="json", **_headers(app_user, key)
    )
    # The original booking (untouched) still occupies this exact range, so
    # a genuinely fresh evaluation correctly hits a real 409 — the point of
    # this assertion is the ABSENCE of the replay header, proving this
    # wasn't served from the (now-deleted) stored response.
    assert "Idempotent-Replay" not in replay_attempt.headers
    assert replay_attempt.status_code == 409
    assert replay_attempt.json()["error"]["code"] == "slot_unavailable"


# --------------------------------------------------------------------
# IDEM-06 — concurrent replay (PRD FR36) ★, 100 repetitions
# --------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_idem_06_concurrent_replay_100_repetitions(transactional_db: None) -> None:
    owner = AppUser.objects.create(email="idem06@example.com", display_name="Idem06")
    resource = _make_resource(owner, "Idem06 Room")
    start = timezone.now() + timedelta(hours=1)
    payload = {
        "resource_id": str(resource.id),
        "start": _iso(start),
        "end": _iso(start + timedelta(hours=1)),
    }

    replay_as_201_count = 0

    for rep in range(1, 101):
        key = uuid.uuid4()
        results: dict[int, tuple[int, dict[str, object], dict[str, str]]] = {}
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(
            index: int,
            barrier: threading.Barrier = barrier,
            key: uuid.UUID = key,
            results: dict[int, tuple[int, dict[str, object], dict[str, str]]] = results,
            results_lock: threading.Lock = results_lock,
        ) -> None:
            worker_client = APIClient()
            try:
                barrier.wait()
                response = worker_client.post(
                    BOOKINGS_URL, data=payload, format="json", **_headers(owner, key)
                )
                with results_lock:
                    results[index] = (
                        response.status_code,
                        response.json(),
                        dict(response.headers),
                    )
            finally:
                # Django doesn't auto-close connections opened on a thread
                # it didn't create for a request cycle — without this,
                # 100 reps x 2 threads leaks connections and the test DB
                # teardown fails with "database is being accessed by other
                # users."
                connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        outcomes = [results[0], results[1]]
        statuses = [status for status, _, _ in outcomes]

        for status, body, _ in outcomes:
            assert status != 409 or body["error"]["code"] != "slot_unavailable", (
                f"rep {rep}: got slot_unavailable — the second request must never be "
                "told its own in-flight booking made the slot unavailable"
            )
            assert status in (201, 409), f"rep {rep}: unexpected status {status}: {body}"

        successes = [(s, b, h) for s, b, h in outcomes if s == 201]
        in_progress = [
            (s, b, h)
            for s, b, h in outcomes
            if s == 409 and b["error"]["code"] == "request_in_progress"
        ]
        assert len(successes) + len(in_progress) == 2, f"rep {rep}: outcomes were {statuses}"
        assert 1 <= len(successes) <= 2, f"rep {rep}: expected 1 or 2 successes, got {outcomes}"

        if len(successes) == 2:
            # Both 201: the second must be the replay of the first, not a
            # second independent booking.
            (_, body_a, headers_a), (_, body_b, headers_b) = successes
            assert body_a["id"] == body_b["id"]
            replay_headers = [h for h in (headers_a, headers_b) if h.get("Idempotent-Replay")]
            assert len(replay_headers) == 1
            replay_as_201_count += 1

        ground_truth = Booking.objects.filter(resource=resource).count()
        assert ground_truth == 1, f"rep {rep}: ground truth = {ground_truth}, expected 1"

        # Reset for the next repetition.
        Booking.objects.filter(resource=resource).delete()
        IdempotencyKey.objects.filter(user=owner, key=key).delete()

    print(
        f"IDEM-06: {replay_as_201_count}/100 reps resolved as a 201 replay "
        "rather than 409 request_in_progress (informational, not a gate)"
    )


# --------------------------------------------------------------------
# IDEM-09 — error-code confusion is impossible
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_09a_genuine_conflict_never_request_in_progress(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    payload = {
        "resource_id": str(active_resource.id),
        "start": _iso(start),
        "end": _iso(start + timedelta(hours=1)),
    }

    first = client.post(
        BOOKINGS_URL, data=payload, format="json", **_headers(app_user, uuid.uuid4())
    )
    assert first.status_code == 201

    # A DIFFERENT key — an ordinary, sequential, non-concurrent second
    # request for the same range. This must be a genuine conflict.
    second = client.post(
        BOOKINGS_URL, data=payload, format="json", **_headers(app_user, uuid.uuid4())
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "slot_unavailable"


@pytest.mark.django_db(transaction=True)
def test_idem_09b_genuine_inflight_replay_never_slot_unavailable(
    transactional_db: None,
) -> None:
    owner = AppUser.objects.create(email="idem09b@example.com", display_name="Idem09b")
    resource = _make_resource(owner, "Idem09b Room")
    start = timezone.now() + timedelta(hours=1)
    payload = {
        "resource_id": str(resource.id),
        "start": _iso(start),
        "end": _iso(start + timedelta(hours=1)),
    }
    key = uuid.uuid4()

    results: dict[int, tuple[int, dict[str, object]]] = {}
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(index: int) -> None:
        worker_client = APIClient()
        try:
            barrier.wait()
            response = worker_client.post(
                BOOKINGS_URL, data=payload, format="json", **_headers(owner, key)
            )
            with results_lock:
                results[index] = (response.status_code, response.json())
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for status, body in results.values():
        assert not (status == 409 and body["error"]["code"] == "slot_unavailable")


# --------------------------------------------------------------------
# IDEM-10 — key scoping across principals
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_10_key_scoped_to_principal(client: APIClient, app_user: AppUser) -> None:
    resource_a = _make_resource(app_user, "Idem10 Room A")
    other_user = AppUser.objects.create(email="idem10-other@example.com", display_name="Other")
    resource_b = _make_resource(other_user, "Idem10 Room B")

    shared_key = uuid.uuid4()
    start = timezone.now() + timedelta(hours=1)

    a_response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(resource_a.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_headers(app_user, shared_key),
    )
    assert a_response.status_code == 201

    b_response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(resource_b.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        **_headers(other_user, shared_key),
    )
    assert b_response.status_code == 201
    assert "Idempotent-Replay" not in b_response.headers
    assert b_response.json()["id"] != a_response.json()["id"]


# --------------------------------------------------------------------
# IDEM-11 — coverage on every required endpoint (only POST /bookings
# exists as of Phase 5; the rest arrive with their own phases)
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_11_missing_idempotency_key_returns_400(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    start = timezone.now() + timedelta(hours=1)
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(active_resource.id),
            "start": _iso(start),
            "end": _iso(start + timedelta(hours=1)),
        },
        format="json",
        HTTP_X_DEV_USER_ID=str(app_user.id),
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["field"] == "Idempotency-Key"


# --------------------------------------------------------------------
# IDEM-07 — process kill mid-transaction (Implementation Plan Phase 28)
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_07_process_kill_mid_transaction_never_leaves_a_booking_without_a_key(
    client: APIClient,
    app_user: AppUser,
    active_resource: Resource,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IDEM-07 (Test Plan v1.0 §7/§13; Implementation Plan Phase 28): "the
    two writes are in separate transactions" would be a real bug, not a
    test problem — `run_idempotent_write`'s own docstring (kairos/core/
    idempotency.py) states the key claim, the write, and the outcome
    record all share ONE outer `transaction.atomic()` for exactly this
    reason. Postgres cannot distinguish a killed process from an
    exception raised inside an open transaction — both trigger the
    identical ROLLBACK of everything the transaction did, including
    statements that already executed successfully. This forces a genuine
    exception at the exact point a process kill mid-write would land:
    after `perform_write()` has already run `Booking.objects.create(...)`
    inside the still-open transaction, but before the outcome-record
    UPDATE on `idempotency_key` completes. If the two writes were ever
    accidentally split into separate transactions, this test would catch
    it directly: the booking would survive the crash while the key
    vanished, producing exactly the "booking with no key record" case
    IDEM-07 exists to rule out.
    """
    original_update = QuerySet.update

    def _crash_immediately_before_the_outcome_record_commits(
        self: QuerySet[IdempotencyKey], *args: object, **kwargs: object
    ) -> int:
        if self.model is IdempotencyKey and kwargs.get("status") == IdempotencyKeyStatus.COMPLETED:
            raise ConnectionError("simulated process kill mid-transaction")
        return original_update(self, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(QuerySet, "update", _crash_immediately_before_the_outcome_record_commits)

    start = timezone.now() + timedelta(hours=1)
    key = uuid.uuid4()

    with pytest.raises(ConnectionError):
        client.post(
            BOOKINGS_URL,
            data={
                "resource_id": str(active_resource.id),
                "start": _iso(start),
                "end": _iso(start + timedelta(hours=1)),
            },
            format="json",
            **_headers(app_user, key),
        )

    # Ground truth, checked with the real (unpatched) database state:
    # EITHER both exist, or NEITHER does — never a booking with no key
    # record. The booking's own INSERT already executed inside the same
    # still-open transaction as the outcome UPDATE that then failed, so
    # Postgres rolled the whole thing back together.
    assert Booking.objects.filter(resource=active_resource).count() == 0
    assert not IdempotencyKey.objects.filter(user=app_user, key=key).exists()


# --------------------------------------------------------------------
# IDEM-08 — lost-response simulation (Implementation Plan Phase 28)
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_idem_08_lost_response_retry_shows_the_users_own_existing_booking(
    client: APIClient, app_user: AppUser, active_resource: Resource
) -> None:
    """IDEM-08 (Test Plan v1.0 §7/§13) — "the actual product requirement;
    everything else in the idempotency suite is machinery." Simulates a
    response dropped at the proxy level: the first request's response is
    deliberately never inspected at all (as if a reverse proxy or a flaky
    connection ate it before the client ever saw it) — only its SIDE
    EFFECT, a committed booking read back through a completely independent
    query, is used as ground truth. The retry, presented with the
    identical Idempotency-Key and body, must show the user their OWN
    existing confirmed booking — never a fresh 409 `slot_unavailable`,
    which would incorrectly tell the user someone else took the exact
    slot their own first (successful, merely unseen) request already
    claimed.
    """
    start = timezone.now() + timedelta(hours=1)
    key = uuid.uuid4()
    payload = {
        "resource_id": str(active_resource.id),
        "start": _iso(start),
        "end": _iso(start + timedelta(hours=1)),
    }

    client.post(BOOKINGS_URL, data=payload, format="json", **_headers(app_user, key))
    # The response above is intentionally never inspected — this is the
    # "lost at the proxy" simulation. Ground truth comes only from here:
    committed = Booking.objects.get(resource=active_resource)

    retry = client.post(BOOKINGS_URL, data=payload, format="json", **_headers(app_user, key))

    assert retry.status_code == 201
    assert retry.status_code != 409, "must never be told the slot is unavailable"
    assert retry.headers["Idempotent-Replay"] == "true"
    assert retry.json()["id"] == str(committed.id)
    assert Booking.objects.filter(resource=active_resource).count() == 1


# --------------------------------------------------------------------
# Cleanup command
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_cleanup_command_deletes_only_expired_keys_and_is_idempotent(
    app_user: AppUser,
) -> None:
    fresh = IdempotencyKey.objects.create(
        user=app_user,
        key=uuid.uuid4(),
        endpoint="POST /api/v1/bookings",
        request_body_hash="fresh",
        status=IdempotencyKeyStatus.COMPLETED,
        response_status=201,
        response_body={},
        completed_at=timezone.now(),
    )
    expired = IdempotencyKey.objects.create(
        user=app_user,
        key=uuid.uuid4(),
        endpoint="POST /api/v1/bookings",
        request_body_hash="expired",
        status=IdempotencyKeyStatus.COMPLETED,
        response_status=201,
        response_body={},
        completed_at=timezone.now(),
    )
    IdempotencyKey.objects.filter(user=app_user, key=expired.key).update(
        created_at=timezone.now() - timedelta(hours=25)
    )

    call_command("cleanup_idempotency_keys")

    assert IdempotencyKey.objects.filter(user=app_user, key=fresh.key).exists()
    assert not IdempotencyKey.objects.filter(user=app_user, key=expired.key).exists()

    # Idempotent: running it again with nothing left to expire is a no-op.
    call_command("cleanup_idempotency_keys")
    assert IdempotencyKey.objects.filter(user=app_user, key=fresh.key).exists()
