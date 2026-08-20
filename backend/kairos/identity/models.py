from __future__ import annotations

import uuid

from django.db import models


class AppUserPlatformRole(models.TextChoices):
    # PRD FR44 defines four roles. Three are global and live here; the
    # fourth (ResourceAdministrator) is inherently scoped and lives in
    # ResourceAdmin below.
    BOOKER = "booker", "Booker"
    SYSTEM_ADMIN = "system_admin", "System Administrator"
    OPERATIONS = "operations", "Operations"


class AppUserStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    DEACTIVATED = "deactivated", "Deactivated"


class AppUser(models.Model):
    """The local identity record every foreign key resolves against.

    Authentication is delegated to the SSO/OIDC provider (RFC v1.0 §4); this
    table holds no credentials.
    """

    PlatformRole = AppUserPlatformRole
    Status = AppUserStatus

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.TextField(unique=True)
    display_name = models.TextField()
    platform_role = models.TextField(
        choices=AppUserPlatformRole.choices, default=AppUserPlatformRole.BOOKER
    )
    status = models.TextField(choices=AppUserStatus.choices, default=AppUserStatus.ACTIVE)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "app_user"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(platform_role__in=list(AppUserPlatformRole.values)),
                name="app_user_platform_role_check",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=list(AppUserStatus.values)),
                name="app_user_status_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status=AppUserStatus.DEACTIVATED, deactivated_at__isnull=False)
                    | models.Q(status=AppUserStatus.ACTIVE, deactivated_at__isnull=True)
                ),
                name="deactivated_has_timestamp",
            ),
        ]

    def __str__(self) -> str:
        return self.email


class ResourceAdmin(models.Model):
    """Scoped resource administration grant (PRD FR45).

    A grant table, not a boolean on app_user: a facilities manager for
    Building A has no authority over Lab equipment.

    Spec v1.0 §3 gives this table a composite primary key (resource_id,
    user_id). Django's ORM does not model composite primary keys as cleanly
    as a surrogate key plus a uniqueness constraint, so this uses a surrogate
    `id` with `resource_admin_unique_grant` enforcing the same one-grant-per-
    pair guarantee — a Django-ergonomics deviation, not a correctness one.
    """

    resource = models.ForeignKey(
        "resources.Resource",
        on_delete=models.CASCADE,
        db_column="resource_id",
        related_name="admin_grants",
    )
    user = models.ForeignKey(
        AppUser,
        on_delete=models.CASCADE,
        db_column="user_id",
        related_name="admin_grants",
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    granted_by = models.ForeignKey(
        AppUser,
        on_delete=models.RESTRICT,
        db_column="granted_by",
        related_name="granted_admin_scopes",
    )

    class Meta:
        db_table = "resource_admin"
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "user"], name="resource_admin_unique_grant"
            ),
        ]

    def __str__(self) -> str:
        return f"user={self.user_id} admin_of_resource={self.resource_id}"
