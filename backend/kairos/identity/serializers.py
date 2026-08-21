from __future__ import annotations

from rest_framework import serializers


class TokenExchangeSerializer(serializers.Serializer[None]):
    """POST /api/v1/auth/token body — the OIDC ID token obtained from a
    real (or, in dev/test, mock) provider, exchanged for this backend's
    own short-lived session token."""

    id_token = serializers.CharField()


class DevMockLoginSerializer(serializers.Serializer[None]):
    """POST /api/v1/auth/dev-mock-login body — Implementation Plan Phase
    9's local mock provider stand-in. Only reachable when
    KAIROS_OIDC_MOCK_ENABLED is True (dev, test)."""

    email = serializers.EmailField()
    display_name = serializers.CharField(required=False, allow_blank=False, default=None)


class UserDeactivateSerializer(serializers.Serializer[None]):
    """POST /api/v1/admin/users/{id}/deactivate (Spec v1.0 §5.15) — the
    reason is REQUIRED, not optional, unlike a self-cancel's `reason`
    (PRD FR40/FR47's "administrative override requires a recorded
    reason" applies here at least as strongly as it does to a single
    booking cancel: deactivation cascades into cancelling/transferring
    OTHER people's bookings)."""

    reason = serializers.CharField(allow_blank=False)
