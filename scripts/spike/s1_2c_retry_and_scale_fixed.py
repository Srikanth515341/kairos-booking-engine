"""
Corrected version of s1_2b: the follow-up script accidentally opened each
connection AFTER the barrier release, which staggers arrival via TCP/auth
setup time and is NOT the harness Test Plan v1.0 §2.0 specifies ("each
thread holds its own connection, all blocked on Barrier(N), released
together"). That staggering is exactly why s1_2b saw clean single-winner
results at every N while the original s1_2 (which connects BEFORE the
barrier, per spec) saw deadlocks at N=200.

This version restores connect-before-barrier (true harness semantics) and
adds retry-on-deadlock, to find (a) the N at which true simultaneity starts
producing deadlocks instead of clean 23P01, and (b) whether client-side
retry on 40P01/57014 still converges to exactly one winner.
"""

from __future__ import annotations

import threading
import time
import uuid

import psycopg

from common import connect, reset_schema

RANGE_SQL = "['2026-09-01 13:00:00+00', '2026-09-01 14:00:00+00')"
RETRYABLE_SQLSTATES = {"40P01", "57014"}
MAX_ATTEMPTS = 8


def attempt_insert(conn: psycopg.Connection, resource_id: str) -> tuple[bool, str | None]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO spike_booking (resource_id, time_range) "
                "VALUES (%s, %s::tstzrange)",
                (resource_id, RANGE_SQL),
            )
        conn.commit()
        return True, None
    except psycopg.Error as e:
        conn.rollback()
        return False, getattr(e, "sqlstate", None)


def run_at(n: int, with_retry: bool) -> dict:
    reset_schema()
    resource_id = str(uuid.uuid4())
    barrier = threading.Barrier(n)
    results: list[tuple[bool, str | None, int]] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = connect()  # BEFORE the barrier — true simultaneity, per Test Plan §2.0
        barrier.wait()
        attempts = 0
        ok, sqlstate = False, None
        while attempts < MAX_ATTEMPTS:
            attempts += 1
            ok, sqlstate = attempt_insert(conn, resource_id)
            if ok or not with_retry or sqlstate not in RETRYABLE_SQLSTATES:
                break
            time.sleep(0.05 * attempts)
        conn.close()
        with lock:
            results.append((ok, sqlstate, attempts))

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

    summary = {
        "n": n,
        "with_retry": with_retry,
        "successes": len(successes),
        "sqlstates": sqlstates,
        "max_attempts_used": max_attempts_used,
        "ground_truth": ground_truth,
    }
    mode = "WITH retry" if with_retry else "NO retry  "
    print(
        f"N={n:4d} [{mode}]  successes={len(successes)}  "
        f"failure_sqlstates={sqlstates}  max_attempts_used={max_attempts_used}  "
        f"ground_truth_active_rows={ground_truth}"
    )
    return summary


if __name__ == "__main__":
    print("=== True-simultaneity (connect-before-barrier), NO retry ===")
    no_retry_results = [run_at(n, with_retry=False) for n in (10, 25, 50, 100, 150, 200)]

    print()
    print("=== True-simultaneity (connect-before-barrier), WITH retry-on-deadlock ===")
    retry_results = [run_at(n, with_retry=True) for n in (10, 25, 50, 100, 150, 200)]

    print()
    print("=== Summary ===")
    for r in no_retry_results:
        flag = "OK" if r["successes"] == 1 else "!! NOT EXACTLY ONE WINNER !!"
        print(f"no-retry  N={r['n']:4d}: successes={r['successes']}  {flag}")
    for r in retry_results:
        flag = "OK" if r["successes"] == 1 else "!! NOT EXACTLY ONE WINNER !!"
        print(f"w/-retry  N={r['n']:4d}: successes={r['successes']}  {flag}")
