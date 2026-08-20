"""Scope logic exists in exactly ONE place (RFC v1.0 §8.1) — every DRF
view consults `AuthorizationService`, never re-derives a permission
decision inline. A global admin flag is explicitly rejected as the model
(PRD FR45): `system_admin` is checked as a real role, `resource_admin` is
always resolved against the specific resource in question via the
`resource_admin` grant table, never assumed.
"""

from __future__ import annotations

import uuid

from django.db.models import Q, QuerySet

from kairos.bookings.models import Booking
from kairos.identity.models import AppUser, AppUserPlatformRole, ResourceAdmin, UserGroupMembership
from kairos.resources.models import Resource


class AuthorizationService:
    @staticmethod
    def is_system_admin(user: AppUser) -> bool:
        return user.platform_role == AppUserPlatformRole.SYSTEM_ADMIN

    @staticmethod
    def is_operations(user: AppUser) -> bool:
        return user.platform_role == AppUserPlatformRole.OPERATIONS

    @staticmethod
    def is_resource_admin(user: AppUser, resource_id: uuid.UUID) -> bool:
        return ResourceAdmin.objects.filter(resource_id=resource_id, user=user).exists()

    @staticmethod
    def can_administer_resource(user: AppUser, resource_id: uuid.UUID) -> bool:
        """system_admin (global, PRD FR44) or a resource_admin grant
        SCOPED to this exact resource — never resource_admin status for
        ANY resource. This is the one check every "admin for resource A
        can't touch resource B" test exercises."""
        return AuthorizationService.is_system_admin(user) or AuthorizationService.is_resource_admin(
            user, resource_id
        )

    @staticmethod
    def can_view_booking(user: AppUser, booking: Booking) -> bool:
        return (
            booking.user_id == user.id
            or AuthorizationService.can_administer_resource(user, booking.resource_id)
            or AuthorizationService.is_operations(user)
        )

    @staticmethod
    def can_edit_booking(user: AppUser, booking: Booking) -> bool:
        # Owner only (Spec v1.0 §5.5) — edit has no admin override, unlike
        # cancel; deliberately NOT can_administer_resource here.
        return booking.user_id == user.id

    @staticmethod
    def can_cancel_booking(user: AppUser, booking: Booking) -> tuple[bool, bool]:
        """Returns (allowed, is_override). Spec v1.0 §5.6: owner
        self-cancel needs no reason; a scoped-admin override does
        (enforced by the caller using is_override, not here)."""
        if booking.user_id == user.id:
            return True, False
        if AuthorizationService.can_administer_resource(user, booking.resource_id):
            return True, True
        return False, False

    @staticmethod
    def is_group_member(user: AppUser, group_id: uuid.UUID) -> bool:
        return UserGroupMembership.objects.filter(group_id=group_id, user=user).exists()

    @staticmethod
    def can_view_resource(user: AppUser, resource: Resource) -> bool:
        """PRD FR46 — open (no `restricted_group`) resources are visible
        to anyone authenticated. A restricted resource is visible to its
        group's members, whoever administers it, and system_admin/
        operations — never to an outside booker, who must see 404
        (existence itself is the secret, per SEC-06), not merely be denied
        the booking action.
        """
        if resource.restricted_group_id is None:
            return True
        return (
            AuthorizationService.can_administer_resource(user, resource.id)
            or AuthorizationService.is_operations(user)
            or AuthorizationService.is_group_member(user, resource.restricted_group_id)
        )

    @staticmethod
    def visible_resources_queryset(user: AppUser) -> QuerySet[Resource]:
        """The list-endpoint form of `can_view_resource` — one query
        expressing the identical rule as a filter, not a Python-side
        `can_view_resource(user, r)` call per row (which would be an N+1
        exactly like the one RFC v1.0 §7.2 already flags for availability).
        A restricted resource is absent from the list for anyone this
        filter excludes, satisfying SEC-06's "absent from list results" —
        not merely 404 on direct access.
        """
        if AuthorizationService.is_system_admin(user) or AuthorizationService.is_operations(user):
            return Resource.objects.all()
        member_group_ids = UserGroupMembership.objects.filter(user=user).values_list(
            "group_id", flat=True
        )
        administered_resource_ids = ResourceAdmin.objects.filter(user=user).values_list(
            "resource_id", flat=True
        )
        return Resource.objects.filter(
            Q(restricted_group__isnull=True)
            | Q(restricted_group_id__in=member_group_ids)
            | Q(id__in=administered_resource_ids)
        )
