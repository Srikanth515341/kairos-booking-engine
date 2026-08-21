"""Domain exceptions for the write path, translated to the Spec v1.0 §6
error envelope by `kairos.core.drf.kairos_exception_handler`. Kept separate
from Django/DRF's own exception types so the service layer stays free of
any HTTP-framework dependency — BookingService raises these; the view layer
is the only place that knows they mean specific HTTP statuses.
"""

from __future__ import annotations


class RecordableConflictError(Exception):
    """Base for domain exceptions whose outcome is a legitimate FINAL
    result under idempotent replay (Spec v1.0 §7 point 7: "conflict
    outcomes are recorded too" — a retry with the same key must receive
    the SAME response, not a fresh attempt). `kairos.core.idempotency.
    run_idempotent_write` catches this ONE base class and records/re-
    raises generically; `kairos.core.drf.kairos_exception_handler` maps it
    to its own `code`/`message`/`http_status` generically too — added
    (Phase 16) once a FOURTH near-identical subclass made the "isinstance
    branch per exception, in two places" pattern (Phase 14's
    `SlotUnavailableError`/`AlreadyOnWaitlistError`) worth consolidating,
    per this project's own "worth extracting once real duplication
    exists, not before" convention (see `KairosAPIView`, Phase 6).
    """

    code: str
    message: str
    http_status: int


class SlotUnavailableError(RecordableConflictError):
    """SQLSTATE 23P01 (exclusion_violation) — the `no_overlapping_bookings`
    constraint rejected the write. Maps to 409 `slot_unavailable`
    (Spec v1.0 §6.1). Someone else's row already occupies the range; this is
    the constraint working as designed, not a bug.
    """

    code = "slot_unavailable"
    message = "This time slot is no longer available."
    http_status = 409


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


class PreviewExpiredError(Exception):
    """A recurring-series `preview_token` failed verification — expired
    (older than 15 minutes), tampered, malformed, or presented by a
    different user than the one who generated it (Spec v1.0 §5.9). All
    four collapse to the same outcome deliberately: Spec defines only
    `preview_expired`, and distinguishing "wrong user" from "expired"
    would leak whether a given token exists at all.
    """


class UnacknowledgedConflictsError(Exception):
    """The preview reported a conflict or a time adjustment the confirm
    body did not acknowledge (PRD FR33; Spec v1.0 §5.9). Maps to 409
    `unacknowledged_conflicts`. Raised before any occurrence is attempted —
    a series with unacknowledged items commits nothing at all.
    """


class AlreadyOnWaitlistError(RecordableConflictError):
    """SQLSTATE 23505 on `uniq_live_waitlist_per_user_slot` — this exact
    (user, resource, time_range) already has a live entry, 'waiting' or
    'offered' (RFC v1.0 §8.2: a user must not re-join while already
    holding an outstanding offer). Maps to 409 `already_on_waitlist`
    (Spec v1.0 §5.11).
    """

    code = "already_on_waitlist"
    message = "You already have a live waitlist entry for this resource and time range."
    http_status = 409


class SlotAlreadyAvailableError(Exception):
    """The requested range has no conflicting confirmed/held booking right
    now — joining a waitlist for an already-bookable range is nonsensical;
    book it directly (Spec v1.0 §5.11). Advisory, not authoritative: this
    is a check-then-act read and the range may be taken by the time the
    client acts on it, the same caveat every availability read in this
    system carries. Maps to 422 `slot_already_available`. NOT a
    `RecordableConflictError` — it's raised from `WaitlistJoinSerializer.
    validate()` BEFORE any idempotency key is ever claimed (mirroring
    `PolicyValidationError`/`NotFoundError`'s "a request that can never
    succeed shouldn't consume a key slot" precedent), so there is never an
    outcome for `run_idempotent_write` to record in the first place.
    """


class OfferExpiredError(RecordableConflictError):
    """The conditional UPDATE (RFC v1.0 §10.3 / Spec v1.0 §4.3) affected
    zero rows — the hold's `expires_at` has passed, someone else already
    accepted it, or the id/user doesn't match a live offer. Maps to 409
    `offer_expired` (Spec v1.0 §5.13). Never `slot_unavailable`: there is
    no INSERT on this path, only an UPDATE against a row the hold already
    reserved — `slot_unavailable` here would mean the hold mechanism
    itself is broken, an incident, not a routine user-facing outcome.
    """

    code = "offer_expired"
    message = "This offer has expired or is no longer valid."
    http_status = 409


class AlreadyResourceAdminError(Exception):
    """SQLSTATE 23505 on `resource_admin_unique_grant` — this exact
    (resource, user) pair already holds a grant. Maps to 409 `already_
    resource_admin` (Spec v1.0 §5.14: "409 if already granted"). NOT a
    `RecordableConflictError` — `POST /resources/{id}/admins` carries no
    `Idempotency-Key` (Spec v1.0 §7's coverage list doesn't name it), so
    there is no idempotent-replay outcome for `run_idempotent_write` to
    ever record here.
    """


class OfferAlreadyResolvedError(RecordableConflictError):
    """A decline's conditional UPDATE (`WHERE status='active'`) affected
    zero rows — the offer was already confirmed or declined. Maps to 409
    `offer_already_resolved` (Spec v1.0 §5.13). Unlike waitlist-entry
    cancel or booking cancel, decline is NOT idempotent-as-a-200-no-op on
    an already-resolved offer — Spec explicitly names this as a distinct
    failure code, not "make sure it's declined" semantics.
    """

    code = "offer_already_resolved"
    message = "This offer has already been confirmed or declined."
    http_status = 409
