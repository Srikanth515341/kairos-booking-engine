"""Resource CRUD and admin-scope grants (Implementation Plan Phase 19;
Spec v1.0 §5.14). No `Idempotency-Key`/`run_idempotent_write` involved —
Spec v1.0 §7's idempotency coverage list does not name any of these
endpoints, unlike booking/waitlist/deactivate writes. Session settings
(`apply_write_path_session_settings`) are still applied on every write,
though: `resource`/`resource_admin` are two of the five tables Phase 8's
`write_audit_log()` trigger fires on, and every prior write path in this
codebase applies those settings before touching an audited table — an
omission here would silently produce `actor_type='unknown'` audit rows,
the exact failure mode RFC v1.0 §12 warns about.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import time

from django.db import DatabaseError, connection, transaction

from kairos.core.db import apply_write_path_session_settings
from kairos.core.exceptions import AlreadyResourceAdminError, NotFoundError
from kairos.core.models import AuditActorType
from kairos.identity.models import AppUser, ResourceAdmin

from .models import Resource

# SQLSTATE 23505 (unique_violation) on `resource_admin_unique_grant` — the
# same "checked by this SPECIFIC code, never a generic exception-type
# catch" discipline every other write path in this codebase follows
# (kairos.bookings.services.EXCLUSION_VIOLATION, kairos.waitlist.services.
# UNIQUE_VIOLATION).
UNIQUE_VIOLATION = "23505"


@dataclass(frozen=True)
class ResourceCreateRequest:
    actor: AppUser
    request_id: str
    name: str
    category: str
    timezone: str
    bookable_start_time: time
    bookable_end_time: time
    max_booking_duration_minutes: int | None
    offboarding_policy: str
    restricted_group_id: uuid.UUID | None


def create_resource(req: ResourceCreateRequest) -> Resource:
    """Spec v1.0 §5.14: `system_admin` only (enforced by the view, via
    `AuthorizationService.is_system_admin` — this function trusts the
    caller already checked). `Resource.save()` (Phase 10) validates the
    IANA timezone unconditionally, so that guarantee holds here for free.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(req.actor.id),
                actor_type=AuditActorType.ADMIN,
                request_id=req.request_id,
            )
        resource = Resource.objects.create(
            name=req.name,
            category=req.category,
            timezone=req.timezone,
            bookable_start_time=req.bookable_start_time,
            bookable_end_time=req.bookable_end_time,
            max_booking_duration_minutes=req.max_booking_duration_minutes,
            offboarding_policy=req.offboarding_policy,
            restricted_group_id=req.restricted_group_id,
            created_by=req.actor,
        )
    return resource


@dataclass(frozen=True)
class ResourceUpdateRequest:
    resource: Resource
    actor: AppUser
    request_id: str
    # Spec v1.0 §5.14's literal updatable-field list: "name, bookable
    # window, max duration, offboarding_policy, status." `None` means
    # "leave unchanged" for every field here (a real PATCH partial
    # update) — `restricted_group` is deliberately NOT in this list, only
    # settable at creation (see ResourceCreateRequest); Spec names it
    # nowhere in PATCH's updatable set.
    name: str | None = None
    bookable_start_time: time | None = None
    bookable_end_time: time | None = None
    max_booking_duration_minutes: int | None = None
    max_booking_duration_minutes_set: bool = False
    offboarding_policy: str | None = None
    status: str | None = None


def update_resource(req: ResourceUpdateRequest) -> Resource:
    """`max_booking_duration_minutes` needs its own `_set` flag because
    `None` is itself a MEANINGFUL value for that field ("no cap") —
    unlike every other field here, `None` can't double as "leave
    unchanged" for it.
    """
    fields: dict[str, object] = {}
    if req.name is not None:
        fields["name"] = req.name
    if req.bookable_start_time is not None:
        fields["bookable_start_time"] = req.bookable_start_time
    if req.bookable_end_time is not None:
        fields["bookable_end_time"] = req.bookable_end_time
    if req.max_booking_duration_minutes_set:
        fields["max_booking_duration_minutes"] = req.max_booking_duration_minutes
    if req.offboarding_policy is not None:
        fields["offboarding_policy"] = req.offboarding_policy
    if req.status is not None:
        fields["status"] = req.status

    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(req.actor.id),
                actor_type=AuditActorType.ADMIN,
                request_id=req.request_id,
            )
        if fields:
            Resource.objects.filter(id=req.resource.id).update(**fields)
        resource = Resource.objects.get(id=req.resource.id)
    return resource


@dataclass(frozen=True)
class GrantResourceAdminRequest:
    resource: Resource
    grantee: AppUser
    actor: AppUser
    request_id: str


def grant_resource_admin(req: GrantResourceAdminRequest) -> ResourceAdmin:
    """Spec v1.0 §5.14: `POST /resources/{id}/admins`, 409 if already
    granted — enforced by `resource_admin_unique_grant` (kairos.identity.
    models.ResourceAdmin.Meta), not a check-then-insert race.
    """
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                apply_write_path_session_settings(
                    cursor,
                    actor_id=str(req.actor.id),
                    actor_type=AuditActorType.ADMIN,
                    request_id=req.request_id,
                )
            grant = ResourceAdmin.objects.create(
                resource=req.resource, user=req.grantee, granted_by=req.actor
            )
    except DatabaseError as exc:
        sqlstate = getattr(exc.__cause__, "sqlstate", None)
        if sqlstate == UNIQUE_VIOLATION:
            raise AlreadyResourceAdminError from exc
        raise
    return grant


@dataclass(frozen=True)
class RevokeResourceAdminRequest:
    resource: Resource
    user_id: uuid.UUID
    actor: AppUser
    request_id: str


def revoke_resource_admin(req: RevokeResourceAdminRequest) -> None:
    """Spec v1.0 §5.14: `DELETE /resources/{id}/admins/{user_id}`."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(req.actor.id),
                actor_type=AuditActorType.ADMIN,
                request_id=req.request_id,
            )
        deleted, _ = ResourceAdmin.objects.filter(
            resource=req.resource, user_id=req.user_id
        ).delete()
    if not deleted:
        raise NotFoundError
