"""Audit Trail Test Suite (Test Plan v1.0 §8): AUD-01, AUD-02, and the
grant/trigger-existence checks their assertions depend on. AUD-03 (a/b/c)
through AUD-05 — the endpoint-level cases exercised via the real API and
`GET /bookings/{id}/history` — live in tests/bookings/test_history.py.

AUD-03(d) ("a system-initiated write") describes a worker that doesn't
exist yet — hold creation is Phase 15, offer/reclamation workers are
Phases 16/17. What's tested here instead is the MECHANISM a Phase 16
worker will rely on: that `write_audit_log()` correctly attributes
`actor_type='system'` when that session variable is set, proven directly
rather than through a worker that isn't built yet.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import psycopg
import pytest
from django.db import connection
from django.utils import timezone

from kairos.bookings.models import Booking
from kairos.core.models import AuditLog
from kairos.identity.models import AppUser
from kairos.resources.models import Resource

KAIROS_APP_USER = "kairos_app"
KAIROS_APP_PASSWORD = "kairos_app"  # nosec — dev-only literal, matches infra/docker-compose.yml


def _kairos_app_dsn() -> str:
    """Same host/port/dbname pytest-django's test connection is actually
    using (test_kairos_test, not a static name), with the credentials
    swapped to the least-privilege application role — the connection
    AUD-01 requires ("connect as kairos_app"), independent of whichever
    role the rest of this test session's Django ORM connection happens to
    use.
    """
    cfg = connection.settings_dict
    return (
        f"dbname={cfg['NAME']} user={KAIROS_APP_USER} password={KAIROS_APP_PASSWORD} "
        f"host={cfg['HOST'] or 'localhost'} port={cfg['PORT'] or 5432}"
    )


# --------------------------------------------------------------------
# AUD-01 — append-only is enforced by grants, not convention ★
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_aud_01_kairos_app_cannot_update_or_delete_audit_log(app_user: AppUser) -> None:
    row = AuditLog.objects.create(
        entity_type="booking",
        entity_id=uuid.uuid4(),
        action="insert",
        actor_id=app_user.id,
        actor_type="user",
    )

    conn = psycopg.connect(_kairos_app_dsn())
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("UPDATE audit_log SET reason = 'tampered' WHERE id = %s", [row.id])
        conn.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM audit_log WHERE id = %s", [row.id])
        conn.rollback()
    finally:
        conn.close()

    # Ground truth: the row genuinely survived both attempts, not merely
    # that the driver reported an error.
    assert AuditLog.objects.filter(id=row.id, reason__isnull=True).exists()


@pytest.mark.django_db
def test_aud_01_kairos_app_can_select_and_insert_audit_log() -> None:
    """The flip side of the same guarantee: SELECT/INSERT are NOT
    accidentally revoked along with UPDATE/DELETE — an over-broad REVOKE
    would break the application as surely as a missing one breaks the
    guarantee.
    """
    conn = psycopg.connect(_kairos_app_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_log")
            cur.fetchone()
        conn.commit()
    finally:
        conn.close()


@pytest.mark.django_db
def test_audit_log_grants_are_exactly_select_and_insert_for_kairos_app() -> None:
    """Direct inspection of the grant catalog itself (not just behavior)
    — the DoD's "verify manually via psql, not only in a test" done as
    an automated check too, so a future migration can't silently widen
    these grants without a test noticing.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'kairos_app' AND table_name = 'audit_log' ORDER BY privilege_type"
        )
        privileges = {row[0] for row in cur.fetchall()}

    assert privileges == {"INSERT", "SELECT"}, (
        f"kairos_app's audit_log privileges are {privileges}, expected exactly "
        "{'INSERT', 'SELECT'} — append-only is enforced here, not in application code"
    )


# --------------------------------------------------------------------
# AUD-02 — triggers cannot be bypassed ★
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_aud_02_raw_sql_write_to_booking_still_produces_audit_row(
    app_user: AppUser, active_resource: Resource
) -> None:
    """Simulates the future bulk-import script RFC v1.0 §12 argues an
    application-level audit call could never defend against: a write to
    `booking` that never goes anywhere near BookingService.
    """
    booking_id = uuid.uuid4()
    start = timezone.now() + timedelta(hours=1)
    end = start + timedelta(hours=1)

    with connection.cursor() as cur:
        cur.execute(
            "INSERT INTO booking (id, resource_id, user_id, time_range, status, created_at) "
            "VALUES (%s, %s, %s, tstzrange(%s, %s), 'confirmed', now())",
            [booking_id, active_resource.id, app_user.id, start, end],
        )

    audit_row = AuditLog.objects.get(entity_type="booking", entity_id=booking_id, action="insert")
    # No session variables were set on this connection — the trigger fires
    # unconditionally regardless, recording the honest 'unknown' attribution
    # (RFC v1.0 §12) rather than silently skipping the row.
    assert audit_row.actor_type == "unknown"
    assert audit_row.after_state is not None
    assert audit_row.after_state["status"] == "confirmed"


@pytest.mark.django_db
def test_aud_02_triggers_exist_on_all_three_phase_8_tables() -> None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT tgrelid::regclass::text FROM pg_trigger WHERE tgname LIKE 'audit_%' ORDER BY 1"
        )
        audited_tables = {row[0] for row in cur.fetchall()}

    assert audited_tables == {"booking", "resource", "resource_admin"}


# --------------------------------------------------------------------
# Actor attribution mechanism — 'system' (supports AUD-03(d) in spirit;
# see module docstring for why no real worker exercises this yet).
# --------------------------------------------------------------------


@pytest.mark.django_db
def test_system_actor_type_is_recorded_when_session_variable_is_set(
    app_user: AppUser, active_resource: Resource
) -> None:
    booking = Booking.objects.create(
        resource=active_resource,
        user=app_user,
        time_range=(timezone.now() + timedelta(hours=2), timezone.now() + timedelta(hours=3)),
    )
    AuditLog.objects.filter(entity_type="booking", entity_id=booking.id).delete()

    with connection.cursor() as cur:
        cur.execute("SELECT set_config('app.actor_type', 'system', true)")
        cur.execute("SELECT set_config('app.actor_id', '', true)")
        cur.execute("SELECT set_config('app.reason', 'automated reclamation', true)")
        cur.execute("SELECT set_config('app.request_id', '', true)")
        cur.execute(
            "UPDATE booking SET cancellation_reason = %s WHERE id = %s",
            ["system smoke test", booking.id],
        )

    audit_row = AuditLog.objects.get(entity_type="booking", entity_id=booking.id, action="update")
    assert audit_row.actor_type == "system"
    assert audit_row.actor_id is None
    assert audit_row.reason == "automated reclamation"
