"""Idempotency — the transaction boundary (RFC v1.0 §11.2; Spec v1.0
§4.1, §7). The single load-bearing decision in this mechanism: the
idempotency key is claimed in the SAME transaction as the write it
protects.

If the key were claimed in a separate transaction, a crash between the two
would leave the write committed and the key absent — the retry then finds
no key, attempts a fresh write, and tells the user their own successful
booking is unavailable. That is a worse trust failure than the double-
booking bug this project exists to prevent, because the message actively
misinforms (RFC v1.0 §11.1). `run_idempotent_write` below is generic
infrastructure — every future write path (edit, cancel, waitlist join,
offer confirm, admin deactivate: Phases 7, 14, 16, 19) reuses it rather
than reimplementing the transaction boundary per endpoint (RFC v1.0 §17).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from kairos.core.db import apply_write_path_session_settings
from kairos.core.drf import build_error_envelope
from kairos.core.exceptions import (
    IdempotencyKeyConflictError,
    RecordableConflictError,
    RequestInProgressError,
)
from kairos.core.models import IdempotencyKey, IdempotencyKeyStatus
from kairos.identity.models import AppUser

logger = logging.getLogger(__name__)

# Raised on the KEY-CLAIM insert specifically (never the protected write —
# that step's own SQLSTATEs are already translated by the callee into
# SlotUnavailableError/ServiceUnavailableError before they'd ever reach
# here). Distinguishing by which statement failed is what makes 23505 here
# mean "someone already finished with this key" rather than an unrelated
# uniqueness bug.
KEY_CLAIM_UNIQUE_VIOLATION = "23505"
# 55P03 (lock timeout), 40P01 (deadlock), 57014 (statement timeout) on the
# key-claim insert specifically all mean the same thing here: another
# transaction currently holds this exact (user_id, key) pair, uncommitted.
# Under READ COMMITTED that row is invisible to us regardless of which of
# these three SQLSTATEs we hit, so there is nothing to re-read — the
# blocking itself is the signal (RFC v1.0 §11.3).
KEY_CLAIM_CONTENDED_SQLSTATES = frozenset({"55P03", "40P01", "57014"})


def compute_request_fingerprint(body: dict[str, Any]) -> str:
    """SHA-256 of the normalized request body (Spec v1.0 §7; RFC v1.0
    §11.4). Normalization: JSON-encode with sorted keys and no incidental
    whitespace, so two requests carrying the same logical body (different
    key order, formatting) hash identically, while any semantic difference
    changes the hash.
    """
    normalized = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotentWriteResult:
    response_status: int
    response_body: dict[str, Any]
    is_replay: bool


def run_idempotent_write(
    *,
    user: AppUser,
    key: UUID,
    endpoint: str,
    body: dict[str, Any],
    request_id: str | None,
    actor_type: str,
    reason: str | None = None,
    perform_write: Callable[[], tuple[int, dict[str, Any]]],
) -> IdempotentWriteResult:
    """Claims `key`, runs `perform_write` in the same transaction, and
    records the outcome — also in that same transaction (Spec v1.0 §4.1
    steps 1/3/4). `perform_write` returns (status, body) on success, or
    raises a domain exception (e.g. SlotUnavailableError,
    ServiceUnavailableError) exactly as it would for a non-idempotent call.

    A 409 slot_unavailable outcome is a legitimate final result and is
    recorded too (Spec v1.0 §7 point 7) — but necessarily in its OWN
    transaction, after the rollback (RFC v1.0 §11.2): the failed write
    rolls back the entire outer transaction, including the key claim, so
    there is nothing left to update. A 503 is different: the outcome is
    unknown, not decided (Spec v1.0 §5.1), so nothing is recorded — the
    retry, finding no key at all, starts completely fresh.

    `actor_type`/`reason` (Phase 8, RFC v1.0 §12) are the caller's to
    supply, exactly like `user`/`request_id` already are: this function is
    generic infrastructure with no idea whether the write behind it is a
    self-service create/edit or an administrative override, so it cannot
    derive them itself. The `idempotency_key` table has no audit trigger,
    so what's applied here has no observable effect on ITS row — but
    `perform_write`'s own nested call to the same helper overwrites these
    values with the specific ones for the table it actually audits, before
    that write's trigger ever fires.
    """
    fingerprint = compute_request_fingerprint(body)

    try:
        with transaction.atomic():
            # Applied HERE, before the key-claim INSERT below — that
            # INSERT is the actual first statement of the outer
            # transaction (Spec v1.0 §4.1's literal ordering). Each write
            # function (create_booking/edit_booking/cancel_booking) also
            # calls this inside its own NESTED atomic, which only runs
            # after this key-claim insert has already committed to its
            # savepoint — too late to matter for the claim itself. Without
            # this call, a concurrent replay's key-claim insert blocks
            # under Postgres's untimed defaults instead of failing fast at
            # the intended 3s lock_timeout budget.
            with connection.cursor() as cursor:
                apply_write_path_session_settings(
                    cursor,
                    actor_id=str(user.id),
                    actor_type=actor_type,
                    request_id=request_id or "",
                    reason=reason,
                )
            IdempotencyKey.objects.create(
                user=user,
                key=key,
                endpoint=endpoint,
                request_body_hash=fingerprint,
                status=IdempotencyKeyStatus.IN_PROGRESS,
            )
            response_status, response_body = perform_write()
            IdempotencyKey.objects.filter(user=user, key=key).update(
                status=IdempotencyKeyStatus.COMPLETED,
                response_status=response_status,
                response_body=response_body,
                completed_at=timezone.now(),
            )
    except DatabaseError as exc:
        # perform_write() implementations translate their own DatabaseErrors
        # into domain exceptions before they'd ever surface here (see
        # kairos.bookings.services.create_booking) — so a DatabaseError
        # reaching this handler can only be from the key-claim INSERT above.
        sqlstate = getattr(exc.__cause__, "sqlstate", None)
        if sqlstate == KEY_CLAIM_UNIQUE_VIOLATION:
            return _replay_or_conflict(user, key, fingerprint)
        if sqlstate in KEY_CLAIM_CONTENDED_SQLSTATES:
            logger.info(
                "idempotency_key_contended",
                extra={"request_id": request_id, "user_id": str(user.id), "sqlstate": sqlstate},
            )
            raise RequestInProgressError from exc
        raise
    except RecordableConflictError as exc:
        # Generic across every domain exception whose outcome is a
        # legitimate final 409/422 (Spec v1.0 §7 point 7) — SlotUnavailableError
        # (booking create/edit), AlreadyOnWaitlistError (Phase 14),
        # OfferExpiredError/OfferAlreadyResolvedError (Phase 16). One
        # branch reading the exception's own code/message/http_status,
        # not one isinstance clause per exception type (see
        # RecordableConflictError's own docstring for why this was
        # consolidated in Phase 16, not before).
        _record_conflict_outcome(
            user,
            key,
            endpoint,
            fingerprint,
            request_id,
            code=exc.code,
            message=exc.message,
            http_status=exc.http_status,
        )
        raise

    return IdempotentWriteResult(response_status, response_body, is_replay=False)


def _replay_or_conflict(user: AppUser, key: UUID, fingerprint: str) -> IdempotentWriteResult:
    # Reaching here means the key-claim INSERT hit a unique violation, which
    # can only happen once a competing transaction has already COMMITTED
    # (under READ COMMITTED, an uncommitted row can't cause a visible
    # uniqueness conflict here — it would instead block, which is the
    # KEY_CLAIM_CONTENDED_SQLSTATES path). A committed row in this design is
    # always already 'completed' — see run_idempotent_write's docstring —
    # so there is no 'in_progress' branch to handle.
    existing = IdempotencyKey.objects.get(user=user, key=key)
    if existing.request_body_hash == fingerprint:
        assert existing.response_status is not None
        assert existing.response_body is not None
        return IdempotentWriteResult(existing.response_status, existing.response_body, True)
    raise IdempotencyKeyConflictError


def run_idempotent_recurring_confirm(
    *,
    user: AppUser,
    key: UUID,
    endpoint: str,
    body: dict[str, Any],
    request_id: str | None,
    actor_type: str,
    perform_confirm: Callable[[], tuple[int, dict[str, Any]]],
) -> IdempotentWriteResult:
    """Recurring-series confirm (Implementation Plan Phase 12; Spec v1.0
    §5.9; Test Plan IDEM-05) needs a DIFFERENT transaction shape than
    `run_idempotent_write` above, not a variation of it — RFC v1.0 §5d
    requires each occurrence to commit in its OWN independent transaction
    (never one all-or-nothing transaction across the series), so the key
    claim CANNOT live in the same transaction as "the write" the way
    `run_idempotent_write`'s docstring requires, because there is no
    single write here — there are N of them.

    The key is claimed and COMMITTED in its own transaction first;
    `perform_confirm` then runs each occurrence's own transaction via the
    ordinary `create_booking` path (fresh `apply_write_path_session_
    settings` every call — Phase 12's explicit requirement); the outcome
    is recorded in a THIRD, final transaction afterward. This means a
    replay arriving while confirm is still running sees a COMMITTED
    'in_progress' row (unlike run_idempotent_write, where a concurrent
    replay only ever blocks on lock contention — see _replay_or_conflict's
    docstring) and must be told 409 request_in_progress explicitly, not
    inferred from a database error.

    Documented, deliberate cost of this design (not covered by any test in
    Phase 12's Test Plan scope): a crash between the key-claim commit and
    the final outcome-record leaves the key permanently 'in_progress' even
    though some occurrences may have already committed successfully. A
    naive retry after such a crash would see 'in_progress' and get 409
    request_in_progress indefinitely, never a silent double-create — but
    also never automatically resolves without the existing 24h
    idempotency-key cleanup (kairos.core.management.commands.
    cleanup_idempotency_keys) eventually deleting the stale row, after
    which a fresh confirm could re-create already-created occurrences.
    Recovering from a crash mid-series is unsolved here, exactly as
    IDEM-07/08's fault-injection gaps are unsolved elsewhere in this
    project — flagged, not silently assumed away.
    """
    fingerprint = compute_request_fingerprint(body)

    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                apply_write_path_session_settings(
                    cursor,
                    actor_id=str(user.id),
                    actor_type=actor_type,
                    request_id=request_id or "",
                )
            IdempotencyKey.objects.create(
                user=user,
                key=key,
                endpoint=endpoint,
                request_body_hash=fingerprint,
                status=IdempotencyKeyStatus.IN_PROGRESS,
            )
    except DatabaseError as exc:
        sqlstate = getattr(exc.__cause__, "sqlstate", None)
        if sqlstate == KEY_CLAIM_UNIQUE_VIOLATION:
            return _replay_or_conflict_allowing_in_progress(user, key, fingerprint)
        if sqlstate in KEY_CLAIM_CONTENDED_SQLSTATES:
            logger.info(
                "idempotency_key_contended",
                extra={"request_id": request_id, "user_id": str(user.id), "sqlstate": sqlstate},
            )
            raise RequestInProgressError from exc
        raise

    response_status, response_body = perform_confirm()

    with transaction.atomic():
        with connection.cursor() as cursor:
            apply_write_path_session_settings(
                cursor,
                actor_id=str(user.id),
                actor_type=actor_type,
                request_id=request_id or "",
            )
        IdempotencyKey.objects.filter(user=user, key=key).update(
            status=IdempotencyKeyStatus.COMPLETED,
            response_status=response_status,
            response_body=response_body,
            completed_at=timezone.now(),
        )

    return IdempotentWriteResult(response_status, response_body, is_replay=False)


def _replay_or_conflict_allowing_in_progress(
    user: AppUser, key: UUID, fingerprint: str
) -> IdempotentWriteResult:
    # Unlike _replay_or_conflict (run_idempotent_write's version), a
    # committed row here CAN legitimately still be 'in_progress' — the key
    # claim and the outcome record are two separate committed
    # transactions, with perform_confirm's real work happening between
    # them, so a concurrent replay can genuinely observe the row mid-flight.
    existing = IdempotencyKey.objects.get(user=user, key=key)
    if existing.status == IdempotencyKeyStatus.IN_PROGRESS:
        raise RequestInProgressError
    if existing.request_body_hash == fingerprint:
        assert existing.response_status is not None
        assert existing.response_body is not None
        return IdempotentWriteResult(existing.response_status, existing.response_body, True)
    raise IdempotencyKeyConflictError


def _record_conflict_outcome(
    user: AppUser,
    key: UUID,
    endpoint: str,
    fingerprint: str,
    request_id: str | None,
    *,
    code: str,
    message: str,
    http_status: int,
) -> None:
    """Generic across every write path that can fail with a legitimate,
    replayable 409/422 (Spec v1.0 §7 point 7: "a 409 is a legitimate final
    outcome; a retry must receive the same 409, not a fresh attempt") —
    `code`/`message`/`http_status` are the caller's to supply exactly like
    `SlotUnavailableError` (booking create/edit) and `AlreadyOnWaitlistError`
    (waitlist join, Phase 14) already do, rather than this function
    hardcoding one specific outcome.
    """
    envelope = build_error_envelope(code, message, None, request_id)
    with transaction.atomic():
        IdempotencyKey.objects.create(
            user=user,
            key=key,
            endpoint=endpoint,
            request_body_hash=fingerprint,
            status=IdempotencyKeyStatus.COMPLETED,
            response_status=http_status,
            response_body=envelope,
            completed_at=timezone.now(),
        )


def cleanup_expired_idempotency_keys() -> dict[str, int]:
    """Deletes `idempotency_key` rows older than `IDEMPOTENCY_RETENTION_
    HOURS` (Spec v1.0 §7; PRD FR37) and records an `OperationalHeartbeat`
    row — the shared mechanism `kairos.core.management.commands.
    cleanup_idempotency_keys` (Phase 5) and `kairos.core.tasks.
    cleanup_idempotency_keys_task` (Phase 21, the scheduling that
    command's own docstring deferred to "Phase 21's job") both call,
    rather than the management command duplicating this logic — the same
    "one mechanism, callable both from `manage.py` and from Beat" choice
    already made for `rematerialize_series`/`run_correctness_checks`
    (Phases 13/20).
    """
    from kairos.core.constants import IDEMPOTENCY_RETENTION_HOURS
    from kairos.core.models import OperationalHeartbeat

    cutoff = timezone.now() - timedelta(hours=IDEMPOTENCY_RETENTION_HOURS)
    deleted_count, _ = IdempotencyKey.objects.filter(created_at__lt=cutoff).delete()
    findings = {"deleted_count": deleted_count}
    OperationalHeartbeat.objects.update_or_create(
        name="idempotency_cleanup",
        defaults={"last_run_at": timezone.now(), "findings": findings},
    )
    return findings
