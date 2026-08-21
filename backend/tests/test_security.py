"""Security & Lifecycle Test Suite (Test Plan v1.0 §12): SEC-01 and SEC-06
— the two Implementation Plan Phase 9 explicitly scopes in. SEC-02 (rate
limiting) is tests/test_rate_limit.py (Phase 22 — a dedicated file, not
here, since it needs the `settings`/`monkeypatch` fixtures every other
test in THIS file deliberately doesn't touch); SEC-04 (injection) and
SEC-07 (audit tamper, re-verifying AUD-01) are below (Phase 22); SEC-03
(waitlist manipulation) needs endpoints/paths this project hasn't built
(unchanged from Phase 9); SEC-05 (field-level authorization) is already
covered by tests/resources/test_views.py (Phase 6).
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from datetime import time, timedelta

import psycopg
import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.test import APIClient

from kairos.bookings.models import Booking
from kairos.core.models import AuditLog
from kairos.identity.models import AppUser, UserGroup, UserGroupMembership
from kairos.resources.models import Resource

BOOKINGS_URL = "/api/v1/bookings"
RESOURCES_URL = "/api/v1/resources"


def _headers(user: AppUser) -> dict[str, str]:
    return {
        "HTTP_X_DEV_USER_ID": str(user.id),
        "HTTP_IDEMPOTENCY_KEY": str(uuid.uuid4()),
    }


@pytest.fixture
def client() -> APIClient:
    return APIClient()


# --------------------------------------------------------------------
# SEC-01 — IDOR, with response-body leakage check
# --------------------------------------------------------------------


@pytest.fixture
def user_b_booking(app_user: AppUser, active_resource: Resource) -> tuple[AppUser, Booking]:
    user_b = AppUser.objects.create(email="sec01-user-b@example.com", display_name="User B")
    booking = Booking.objects.create(
        resource=active_resource,
        user=user_b,
        time_range=(timezone.now() + timedelta(hours=1), timezone.now() + timedelta(hours=2)),
    )
    return user_b, booking


def _assert_empty_error_envelope(body: dict[str, object]) -> None:
    """Status-code-only checks would miss a bug returning the right code
    with an informative body (Test Plan §12 SEC-01's own stated premise)."""
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error.keys()) == {"code", "message", "details", "request_id"}
    assert error["code"] == "not_found"
    # No leaked resource_id, time range, or any field from B's booking —
    # the details object for a 404 is empty, not a partial echo.
    assert error["details"] in ({}, None)


@pytest.mark.django_db
def test_sec_01_get_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    response = client.get(f"{BOOKINGS_URL}/{booking.id}", **_headers(app_user))
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)
    assert str(booking.resource_id) not in str(body)


@pytest.mark.django_db
def test_sec_01_patch_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    new_start = timezone.now() + timedelta(hours=10)
    response = client.patch(
        f"{BOOKINGS_URL}/{booking.id}",
        data={
            "start": new_start.isoformat().replace("+00:00", "Z"),
            "end": (new_start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)

    booking.refresh_from_db()
    assert booking.time_range.lower != new_start


@pytest.mark.django_db
def test_sec_01_cancel_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    response = client.post(
        f"{BOOKINGS_URL}/{booking.id}/cancel", data={}, format="json", **_headers(app_user)
    )
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)

    booking.refresh_from_db()
    assert booking.status == "confirmed"


@pytest.mark.django_db
def test_sec_01_history_returns_404_with_no_leaked_fields(
    client: APIClient, app_user: AppUser, user_b_booking: tuple[AppUser, Booking]
) -> None:
    _user_b, booking = user_b_booking
    response = client.get(f"{BOOKINGS_URL}/{booking.id}/history", **_headers(app_user))
    assert response.status_code == 404
    body = response.json()
    _assert_empty_error_envelope(body)
    assert str(booking.id) not in str(body)


# --------------------------------------------------------------------
# SEC-06 — restricted resources don't leak existence (PRD FR46)
# --------------------------------------------------------------------


@pytest.fixture
def restricted_resource(app_user: AppUser) -> Resource:
    group = UserGroup.objects.create(name="Facilities Team")
    return Resource.objects.create(
        name="Restricted Room",
        timezone="UTC",
        bookable_start_time=time(0, 0),
        bookable_end_time=time(23, 59),
        created_by=app_user,
        restricted_group=group,
    )


@pytest.mark.django_db
def test_sec_06_non_member_gets_404_on_direct_access(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider@example.com", display_name="Outsider")
    response = client.get(f"{RESOURCES_URL}/{restricted_resource.id}", **_headers(outsider))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_sec_06_non_member_absent_from_list(
    client: APIClient, app_user: AppUser, active_resource: Resource, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider2@example.com", display_name="Outsider2")
    response = client.get(RESOURCES_URL, **_headers(outsider))
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["data"]}
    assert str(restricted_resource.id) not in ids
    assert str(active_resource.id) in ids  # the open resource is still listed


@pytest.mark.django_db
def test_sec_06_group_member_can_access(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    member = AppUser.objects.create(email="sec06-member@example.com", display_name="Member")
    assert restricted_resource.restricted_group is not None
    UserGroupMembership.objects.create(group=restricted_resource.restricted_group, user=member)

    detail_response = client.get(f"{RESOURCES_URL}/{restricted_resource.id}", **_headers(member))
    assert detail_response.status_code == 200

    list_response = client.get(RESOURCES_URL, **_headers(member))
    ids = {row["id"] for row in list_response.json()["data"]}
    assert str(restricted_resource.id) in ids


@pytest.mark.django_db
def test_sec_06_non_member_cannot_book_restricted_resource(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider3@example.com", display_name="Outsider3")
    start = timezone.now() + timedelta(hours=1)
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": str(restricted_resource.id),
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
        format="json",
        **_headers(outsider),
    )
    # PRD FR46: "Non-members may neither book nor join its waitlist" — 404,
    # the same as a nonexistent resource, not a distinct "restricted" code.
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_sec_06_non_member_gets_404_on_availability(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    outsider = AppUser.objects.create(email="sec06-outsider4@example.com", display_name="Outsider4")
    response = client.get(
        f"{RESOURCES_URL}/{restricted_resource.id}/availability?from=2026-09-01&to=2026-09-08",
        **_headers(outsider),
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_sec_06_resource_admin_can_access_restricted_resource_they_administer(
    client: APIClient, app_user: AppUser, restricted_resource: Resource
) -> None:
    from kairos.identity.models import ResourceAdmin

    admin = AppUser.objects.create(email="sec06-admin@example.com", display_name="Admin")
    ResourceAdmin.objects.create(resource=restricted_resource, user=admin, granted_by=app_user)

    response = client.get(f"{RESOURCES_URL}/{restricted_resource.id}", **_headers(admin))
    assert response.status_code == 200


# --------------------------------------------------------------------
# SEC-04 — injection resistance: "all range queries use parameterized
# ORM constructs" made into a static check plus a runtime test
# --------------------------------------------------------------------

_INJECTION_PAYLOADS = (
    "'; DROP TABLE booking; --",
    "' OR '1'='1",
    "1' UNION SELECT * FROM audit_log --",
    "2026-01-01'; DELETE FROM resource WHERE '1'='1",
)


def test_sec_04_static_no_raw_sql_built_via_string_interpolation() -> None:
    """The actual mechanism that makes SQL injection structurally
    impossible here, not merely untested: every `cursor.execute(...)`
    call in this codebase must pass its SQL as a plain string literal
    with `%s` placeholders and a separate params list/tuple — never an
    f-string, `%`-operator, or `.format()`-built string, all of which
    would put caller-controlled data directly into the SQL text itself.
    Walks the actual AST rather than grepping for a pattern, so it can't
    be fooled by which quote style or whitespace a given call site uses.
    """
    package_root = pathlib.Path(__file__).resolve().parent.parent / "kairos"
    violations: list[str] = []

    for path in sorted(package_root.rglob("*.py")):
        if "migrations" in path.parts or "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "execute" or not node.args:
                continue
            sql_arg = node.args[0]
            if isinstance(sql_arg, ast.JoinedStr):
                violations.append(f"{path}:{node.lineno} — f-string passed to .execute()")
            elif isinstance(sql_arg, ast.BinOp) and isinstance(sql_arg.op, ast.Mod):
                violations.append(f"{path}:{node.lineno} — %-formatted string passed to .execute()")
            elif (
                isinstance(sql_arg, ast.Call)
                and isinstance(sql_arg.func, ast.Attribute)
                and sql_arg.func.attr == "format"
            ):
                violations.append(
                    f"{path}:{node.lineno} — .format()-built string passed to .execute()"
                )

    assert violations == [], "Raw SQL built via string interpolation found:\n" + "\n".join(
        violations
    )


@pytest.mark.django_db
@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_sec_04_injection_payload_on_availability_from_param_yields_400(
    client: APIClient, app_user: AppUser, active_resource: Resource, payload: str
) -> None:
    response = client.get(
        f"{RESOURCES_URL}/{active_resource.id}/availability",
        {"from": payload, "to": "2026-06-01T00:00:00Z"},
        **_headers(app_user),
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"


@pytest.mark.django_db
@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_sec_04_injection_payload_on_availability_to_param_yields_400(
    client: APIClient, app_user: AppUser, active_resource: Resource, payload: str
) -> None:
    response = client.get(
        f"{RESOURCES_URL}/{active_resource.id}/availability",
        {"from": "2026-06-01T00:00:00Z", "to": payload},
        **_headers(app_user),
    )
    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"


@pytest.mark.django_db
@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_sec_04_injection_payload_as_resource_id_in_booking_body_yields_400(
    client: APIClient, app_user: AppUser, payload: str
) -> None:
    """`BookingCreateSerializer.resource_id` is a DRF `UUIDField` — format
    is validated (and rejected) before any lookup, let alone raw SQL,
    ever runs against it.
    """
    response = client.post(
        BOOKINGS_URL,
        data={
            "resource_id": payload,
            "start": "2026-06-01T10:00:00Z",
            "end": "2026-06-01T11:00:00Z",
        },
        format="json",
        **_headers(app_user),
    )
    assert response.status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize("payload", _INJECTION_PAYLOADS)
def test_sec_04_injection_shaped_path_segment_never_reaches_a_view_or_sql(
    client: APIClient, app_user: AppUser, payload: str
) -> None:
    """Every `{id}`-shaped path segment in this project uses Django's
    `<uuid:...>` URL converter (confirmed by inspection of every urls.py
    in this codebase) — an injection-shaped, non-UUID value simply
    doesn't MATCH the route at all, so it never reaches this application's
    code, the ORM, or raw SQL. That's a 404 (Django's own routing
    rejecting an unmatched URL), not a 400 from application-level
    validation — a STRONGER guarantee than "safely rejected inside a
    view," not a weaker one. As of this phase (kairos.core.error_handlers)
    it's also a properly enveloped 404, not a bare Django error page —
    the one prior gap in kairos_exception_handler's "no bare, unenveloped
    error body" claim, since URL resolution happens before any DRF
    exception handler runs.
    """
    from urllib.parse import quote

    response = client.get(f"{BOOKINGS_URL}/{quote(payload, safe='')}", **_headers(app_user))
    assert response.status_code == 404
    # handler404 returns a plain JsonResponse (there is no DRF Request at
    # all at this point — URL resolution failed before DRF's dispatch
    # ever ran), so this reads the body via .json(), not the DRF-only
    # `.data` property the rest of this file's real-view tests use.
    body = response.json()
    assert body["error"]["code"] == "not_found"
    # The response must not, under any circumstances, echo the raw SQL
    # error text a real injection attempt reaching the database would
    # produce (e.g. a Postgres syntax-error message naming the payload).
    assert "syntax error" not in str(body).lower()
    assert "psycopg" not in str(body).lower()


# --------------------------------------------------------------------
# SEC-07 — audit tamper resistance (re-verifies AUD-01)
# --------------------------------------------------------------------

KAIROS_APP_USER = "kairos_app"
KAIROS_APP_PASSWORD = "kairos_app"  # nosec — dev-only literal, matches infra/docker-compose.yml


def _kairos_app_dsn() -> str:
    cfg = connection.settings_dict
    return (
        f"dbname={cfg['NAME']} user={KAIROS_APP_USER} password={KAIROS_APP_PASSWORD} "
        f"host={cfg['HOST'] or 'localhost'} port={cfg['PORT'] or 5432}"
    )


@pytest.mark.django_db
def test_sec_07_audit_log_cannot_be_tampered_with_by_the_application_role(
    app_user: AppUser,
) -> None:
    """Test Plan v1.0 §12's own framing: SEC-07 re-verifies AUD-01 under
    the Security suite's numbering, not a second, independent mechanism —
    the guarantee IS AUD-01's (`kairos_app` structurally cannot UPDATE or
    DELETE `audit_log`, enforced by Postgres grants), re-asserted here so
    the security suite's own DoD checklist has direct, standalone
    evidence rather than a cross-reference as its only proof.
    """
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
                cur.execute(
                    "UPDATE audit_log SET reason = 'SEC-07 tamper attempt' WHERE id = %s",
                    [row.id],
                )
        conn.rollback()

        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM audit_log WHERE id = %s", [row.id])
        conn.rollback()
    finally:
        conn.close()

    # Ground truth: the row genuinely survived, not merely that the
    # driver reported an error.
    assert AuditLog.objects.filter(id=row.id, reason__isnull=True).exists()
