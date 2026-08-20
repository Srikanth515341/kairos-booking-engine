"""Concurrency proof harness (Test Plan v1.0 §2.0; Implementation Plan Phase 3).

Every test under tests/concurrency/ builds on `run_concurrent`: N independent
OS threads, each holding its own psycopg connection — never a shared pool,
never `asyncio.gather()` (Test Plan §2.0(a); the whole point is that a
shared event loop or connection pool can serialize what looks like
concurrent code and produce a false-confidence pass). Threads block on a
shared `threading.Barrier(N)` and are released together, which is what
"barrier-released" means as distinct from "fired in a fast loop."
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from django.db import connections

# Production write-path session settings (RFC v1.0 §7.1; .env.example
# DB_LOCK_TIMEOUT / DB_STATEMENT_TIMEOUT / DB_IDLE_IN_TRANSACTION_TIMEOUT).
# Applied directly per connection here because no service layer exists yet
# to apply them (Phase 4/5's BookingService will own that for the real
# write path) — a run against default, untuned session settings would test
# a different system (Test Plan §13).
SESSION_SETTINGS_SQL = """
SET lock_timeout = '3s';
SET statement_timeout = '10s';
SET idle_in_transaction_session_timeout = '30s';
"""

# SQLSTATEs a barrier-released write against a contested range may
# legitimately receive. All four are documented, expected, retryable
# outcomes at this scale — never counted as "unexplained errors":
#   23P01  exclusion_violation  — the constraint did its job; someone else won
#   55P03  lock_timeout         — waited on an uncommitted competitor past the
#                                  3s budget (RFC §7.1)
#   40P01  deadlock_detected    — Spike S1.2 recorded this in 2/10 runs at
#                                  N=200 identical-slot contention. Safety held
#                                  10/10 regardless (never more than one
#                                  success); only liveness was at risk, and
#                                  it is retryable exactly like 55P03.
#   57014  query_canceled       — Phase 3 CONC-01 finding, empirically
#                                  reproducible at N=200: under the heaviest
#                                  pileups, most losers don't cleanly block on
#                                  one uncommitted competitor for a single
#                                  >3s stretch (which lock_timeout would
#                                  catch) — they accumulate many shorter waits
#                                  under GiST index contention that together
#                                  exceed statement_timeout (10s) before any
#                                  one wait exceeds lock_timeout. Safety was
#                                  unaffected in every observed run (exactly
#                                  one success, ground truth 1); this is the
#                                  same liveness-only category as 40P01, not
#                                  a new failure mode. BookingService (Phase
#                                  4) must retry this exactly like 55P03/
#                                  40P01 — see CLAUDE.md.
EXCLUSION_VIOLATION = "23P01"
LOCK_TIMEOUT = "55P03"
DEADLOCK_DETECTED = "40P01"
QUERY_CANCELED = "57014"
EXPECTED_NONSUCCESS_SQLSTATES = frozenset(
    {EXCLUSION_VIOLATION, LOCK_TIMEOUT, DEADLOCK_DETECTED, QUERY_CANCELED}
)


@dataclass(frozen=True)
class ClientOutcome:
    success: bool
    sqlstate: str | None
    latency_s: float


def django_test_dsn() -> str:
    """DSN for the database Django's test runner actually created.

    Not a static .env value: pytest-django names the test database
    dynamically (e.g. 'test_kairos_test'), and every independent worker
    connection must target exactly that database, not whatever DATABASE_URL
    happens to say.
    """
    cfg = connections["default"].settings_dict
    return (
        f"dbname={cfg['NAME']} user={cfg['USER']} password={cfg['PASSWORD']} "
        f"host={cfg['HOST'] or 'localhost'} port={cfg['PORT'] or 5432}"
    )


def range_literal(start: datetime, end: datetime) -> str:
    """A tstzrange literal in Postgres's default [start, end) bounds."""
    return f"['{start.isoformat()}', '{end.isoformat()}')"


def _connect(dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=False)
    with conn.cursor() as cur:
        cur.execute(SESSION_SETTINGS_SQL)
    conn.commit()
    return conn


def run_concurrent(
    dsn: str, actions: Sequence[Callable[[psycopg.Cursor], None]]
) -> list[ClientOutcome]:
    """Run len(actions) threads, each on its own connection, all blocked on
    a shared Barrier(len(actions)) and released together.

    Each action runs inside its own transaction: on success it is
    committed; on a psycopg error it is rolled back and the SQLSTATE
    recorded. Returns one ClientOutcome per action, in the same order the
    actions were given.
    """
    n = len(actions)
    barrier = threading.Barrier(n)
    results: dict[int, ClientOutcome] = {}
    results_lock = threading.Lock()

    def worker(index: int, action: Callable[[psycopg.Cursor], None]) -> None:
        conn = _connect(dsn)
        try:
            barrier.wait()
            start = time.perf_counter()
            try:
                with conn.cursor() as cur:
                    action(cur)
                conn.commit()
                outcome = ClientOutcome(
                    success=True, sqlstate=None, latency_s=time.perf_counter() - start
                )
            except psycopg.Error as exc:
                conn.rollback()
                sqlstate = getattr(exc, "sqlstate", None)
                outcome = ClientOutcome(
                    success=False, sqlstate=sqlstate, latency_s=time.perf_counter() - start
                )
        finally:
            conn.close()
        with results_lock:
            results[index] = outcome

    threads = [
        threading.Thread(target=worker, args=(i, action)) for i, action in enumerate(actions)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return [results[i] for i in range(n)]


def clear_bookings(dsn: str, resource_id: str) -> None:
    """Delete every booking row for a resource — resets state between runs
    within a single test, so each barrier-released run starts from the Test
    Plan's stated setup ('no existing bookings')."""
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM booking WHERE resource_id = %s", (resource_id,))
    finally:
        conn.close()


def count_active_overlapping(dsn: str, resource_id: str, range_sql: str) -> int:
    """Ground truth per Test Plan §2.0: a direct SELECT, independent of what
    any caller's response/outcome claimed. A rollback-but-report-failure
    bug would still show up here even though every outcome said `success`.
    """
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM booking WHERE resource_id = %s "
                "AND status IN ('confirmed', 'held') AND time_range && %s::tstzrange",
                (resource_id, range_sql),
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])
    finally:
        conn.close()


def count_overlapping_pairs(dsn: str, resource_id: str) -> int:
    """Reconciliation-style ground truth (RFC v1.0 §14): how many distinct
    pairs of active rows for this resource overlap each other. Zero is the
    only value the exclusion constraint permits, structurally — a nonzero
    count means the guarantee has been violated, not that a race merely
    occurred.
    """
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM booking b1 JOIN booking b2 "
                "  ON b1.resource_id = b2.resource_id AND b1.id < b2.id "
                "  AND b1.time_range && b2.time_range "
                "WHERE b1.resource_id = %s "
                "  AND b1.status IN ('confirmed', 'held') "
                "  AND b2.status IN ('confirmed', 'held')",
                (resource_id,),
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])
    finally:
        conn.close()
