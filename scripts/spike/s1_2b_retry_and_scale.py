"""
Follow-up to S1.2, run because the first pass returned an unexpected result:
0 successes out of 200, with SQLSTATE 40P01 (deadlock_detected) and 57014
(statement_timeout) instead of the RFC's assumed "one winner, N-1 clean
23P01". This script answers two follow-up questions the plain S1.2 result
raises:

  (a) Is this specific to N=200-on-the-identical-row, or does it appear at
      lower, more realistic concurrency too?
  (b) If a client RETRIES on 40P01 (the same way it must already retry on
      55P03/lock_timeout), does the system converge to exactly one winner?

This determines whether BookingService (Phase 4) needs deadlock-retry logic
in addition to 23P01/55P03 handling, and whether Test Plan CONC-01's
"exactly one success, N-1 clean conflict responses" needs to be read as
"...after client-side retry on deadlock", not as a first-attempt guarantee.
"""

from __future__ import annotations

import threading
import time
import uuid

import psycopg

from common import connect, reset_schema

RANGE_SQL = "['2026-09-01 13:00:00+00', '2026-09-01 14:00:00+00')"
RETRYABLE_SQLSTATES = {"40P01", "57014"}  # deadlock_detected, query_canceled
MAX_ATTEMPTS = 8


def insert_with_retry(resource_id: str) -> tuple[bool, str | None, int]:
    """Mirrors the retry policy a real client must apply on 503
    service_unavailable (RFC §7.1 / Spec §5.1): backoff and retry with the
    same idempotency key. Here we just retry the raw insert to isolate the
    database-level question from the idempotency-key machinery, which is a
    Phase 5 concern."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        conn = connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO spike_booking (resource_id, time_range) "
                    "VALUES (%s, %s::tstzrange)",
                    (resource_id, RANGE_SQL),
                )
            conn.commit()
            return True, None, attempt
        except psycopg.Error as e:
            conn.rollback()
            sqlstate = getattr(e, "sqlstate", None)
            if sqlstate not in RETRYABLE_SQLSTATES or attempt == MAX_ATTEMPTS:
                return False, sqlstate, attempt
            time.sleep(0.05 * attempt)  # simple linear backoff
        finally:
            conn.close()
    return False, "exhausted", MAX_ATTEMPTS


def run_at(n: int, with_retry: bool) -> None:
    reset_schema()
    resource_id = str(uuid.uuid4())
    barrier = threading.Barrier(n)
    results: list[tuple[bool, str | None, int]] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        if with_retry:
            outcome = insert_with_retry(resource_id)
        else:
            conn = connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO spike_booking (resource_id, time_range) "
                        "VALUES (%s, %s::tstzrange)",
                        (resource_id, RANGE_SQL),
                    )
                conn.commit()
                outcome = (True, None, 1)
            except psycopg.Error as e:
                conn.rollback()
                outcome = (False, getattr(e, "sqlstate", None), 1)
            finally:
                conn.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r[0]]
    sqlstates = sorted({r[1] for r in results if not r[0]})
    max_attempts_used = max(r[2] for r in results)

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM spike_booking WHERE resource_id = %s "
            "AND status IN ('confirmed','held')",
            (resource_id,),
        )
        ground_truth = cur.fetchone()[0]
    conn.close()

    mode = "WITH retry-on-deadlock" if with_retry else "NO retry (single attempt)"
    print(
        f"N={n:4d} [{mode:28s}]  successes={len(successes)}  "
        f"failure_sqlstates={sqlstates}  max_attempts_used={max_attempts_used}  "
        f"ground_truth_active_rows={ground_truth}"
    )


if __name__ == "__main__":
    print("=== Scaling down N, single attempt (no retry) ===")
    for n in (10, 25, 50, 100, 150, 200):
        run_at(n, with_retry=False)

    print()
    print("=== Same range of N, WITH retry-on-deadlock/timeout ===")
    for n in (10, 25, 50, 100, 150, 200):
        run_at(n, with_retry=True)
