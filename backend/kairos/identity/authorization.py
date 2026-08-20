"""Scoped-admin and role checks (RFC v1.0 §8.1) — resolved through one
place so authorization logic isn't duplicated per view. Real permission
classes / a full authorization service arrive in Phase 9; these two checks
are what Phase 6's read endpoints need today.
"""

from __future__ import annotations

import uuid

from kairos.identity.models import AppUser, AppUserPlatformRole, ResourceAdmin


def is_resource_admin(user: AppUser, resource_id: uuid.UUID) -> bool:
    return ResourceAdmin.objects.filter(resource_id=resource_id, user=user).exists()


def is_operations(user: AppUser) -> bool:
    return user.platform_role == AppUserPlatformRole.OPERATIONS
