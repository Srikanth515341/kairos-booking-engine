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


class ResourceNotFoundError(Exception):
    """The referenced resource doesn't exist, or `status='inactive'`. Maps
    to 404 `not_found` (Spec v1.0 §5.1) — an inactive resource is treated
    identically to a nonexistent one, not surfaced as a distinct error.
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
