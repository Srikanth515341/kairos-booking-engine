"""Write-path database helpers shared across every write path (RFC v1.0
§17) — not booking-specific, because idempotency (Phase 5) needs the same
session settings applied before ITS first statement (the key-claim insert)
that `BookingService` needs before the booking insert.
"""

from __future__ import annotations

from django.db.backends.utils import CursorWrapper

from kairos.core.constants import (
    WRITE_PATH_IDLE_IN_TRANSACTION_TIMEOUT,
    WRITE_PATH_LOCK_TIMEOUT,
    WRITE_PATH_STATEMENT_TIMEOUT,
)


def apply_write_path_session_settings(
    cursor: CursorWrapper,
    *,
    actor_id: str,
    actor_type: str,
    request_id: str,
    reason: str | None = None,
) -> None:
    """RFC v1.0 §7.1 (write-path timeouts) and §12 (audit actor
    propagation), applied per transaction via `set_config(..., true)` — the
    functional equivalent of `SET LOCAL`, except it's a plain function call
    that safely accepts bind parameters. The `SET` statement itself doesn't
    reliably accept them, which matters here because actor_id/actor_type/
    reason/request_id are all request-influenced values; interpolating
    those directly into SQL text would be an injection risk that
    `set_config` avoids.

    Phase 8 note: `app.actor_id`/`app.actor_type`/`app.request_id` are read
    by `write_audit_log()` (Spec v1.0 §3) exactly like the timeout settings
    above are read by Postgres itself — ONE shared mechanism and ONE call
    site per transaction, not a second, separate context manager for
    "audit settings" layered on top. `reason or ""` matches the trigger's
    own `NULLIF(current_setting('app.reason', true), '')`: an absent
    reason is passed as empty string here and converted back to NULL
    there, the same convention already used for every other unset session
    variable in this function.

    Callers that open their own outer transaction around a call to
    BookingService (or any future write service) should call this ONCE at
    the top of that outer transaction — see kairos.core.idempotency, which
    must apply these settings before its key-claim INSERT, not only before
    the protected write that follows it. A write function's own NESTED
    atomic re-calling this is not redundant: it overwrites the OUTER
    call's values (typically generic/not-yet-known) with the specific
    actor_type/reason for the actual audited write, using the exact same
    `set_config(..., true)` mechanism throughout the transaction.
    """
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", [WRITE_PATH_LOCK_TIMEOUT])
    cursor.execute(
        "SELECT set_config('statement_timeout', %s, true)", [WRITE_PATH_STATEMENT_TIMEOUT]
    )
    cursor.execute(
        "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
        [WRITE_PATH_IDLE_IN_TRANSACTION_TIMEOUT],
    )
    cursor.execute("SELECT set_config('app.actor_id', %s, true)", [actor_id])
    cursor.execute("SELECT set_config('app.actor_type', %s, true)", [actor_type])
    cursor.execute("SELECT set_config('app.request_id', %s, true)", [request_id])
    cursor.execute("SELECT set_config('app.reason', %s, true)", [reason or ""])
