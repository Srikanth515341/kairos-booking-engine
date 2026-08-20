"""Domain exceptions for the write path, translated to the Spec v1.0 §6
error envelope by `kairos.core.drf.kairos_exception_handler`. Kept separate
from Django/DRF's own exception types so the service layer stays free of
any HTTP-framework dependency — BookingService raises these; the view layer
is the only place that knows they mean specific HTTP statuses.
"""

from __future__ import annotations


class SlotUnavailableError(Exception):
    """SQLSTATE 23P01 (exclusion_violation) — the `no_overlapping_bookings`
    constraint rejected the write. Maps to 409 `slot_unavailable`
    (Spec v1.0 §6.1). Someone else's row already occupies the range; this is
    the constraint working as designed, not a bug.
    """


class ServiceUnavailableError(Exception):
    """The outcome of a write is unknown, not decided (Spec v1.0 §5.1) —
    SQLSTATE 55P03 (lock timeout), 40P01 (deadlock), or 57014 (statement
    timeout fired while waiting on contention). All three are retryable and
    map identically to 503 `service_unavailable` + `Retry-After`.
    """

    def __init__(self, retry_after_seconds: int = 1) -> None:
        super().__init__("service outcome unknown — retry the same request")
        self.retry_after_seconds = retry_after_seconds


class NotFoundError(Exception):
    """The referenced entity doesn't exist — or does, but the requester has
    no right to know that (Spec v1.0 §1 convention: object-level protection
    is 404, not 403, so existence isn't confirmed to someone without
    access). Maps to 404 `not_found`. Used for a missing/inactive resource,
    a missing booking, and a booking the requester may not view — the same
    status and code regardless of which, by design.
    """


class PolicyValidationError(Exception):
    """A single policy-validation failure. Spec v1.0 §6's `validation_error`
    details example is a single {"field", "issue"} pair, not DRF's default
    per-field list aggregation — policy checks stop at the first violation
    rather than collecting all of them.
    """

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(f"{field}: {issue}")
        self.field = field
        self.issue = issue


class RequestInProgressError(Exception):
    """An earlier request with this exact idempotency key is still
    executing (RFC v1.0 §11.3). Maps to 409 `request_in_progress` — never
    confused with `slot_unavailable`, even though both are 409: this means
    *your own* request is still in flight, not that someone else took the
    slot. Returning `slot_unavailable` here would misinform the user that
    their own in-flight booking made the slot unavailable (PRD FR38).
    """


class IdempotencyKeyConflictError(Exception):
    """The same (user, key) was presented with a different request body
    (Spec v1.0 §7). Maps to 422 `idempotency_key_conflict` — a client bug
    signal, never a silent replay of a different request under an old key.
    """
