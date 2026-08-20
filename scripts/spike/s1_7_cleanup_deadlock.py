"""
S1.7 — Deadlock behavior when cleanup-on-write and an insert run concurrently
on the same resource. DESIGN-SHAPING (RFC v1.0 §16, §10.4).

Phase 17's cleanup-on-write mechanism puts a DELETE (of expired holds)
immediately before every booking INSERT, in the same transaction, scoped to
the resource/range being booked (RFC §10.4). This tests whether 200
barrier-released writers, each doing that DELETE-then-INSERT pattern against
overlapping/adjacent expired holds on the same resource, ever deadlock.

If this deadlocks materially, RFC §4.3's self-healing property (a stalled
reaper can never permanently block a slot, because the next booker clears it
themselves) is compromised and cleanup-on-write must move to reaper-only.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg

from common import connect, reset_schema

N = 200
REPS = 50
RESOURCE_ID = str(uuid.uuid4())
BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def seed_expired_holds(conn: psycopg.Connection) -> None:
    """Adjacent, non-overlapping expired holds — one per 10-minute slot
    across the range workers will target, so every worker's cleanup DELETE
    and INSERT touch overlapping/adjacent rows, which is what could deadlock."""
    with conn.cursor() as cur:
        for i in range(N):
            start_dt = BASE + timedelta(minutes=i * 10)
            end_dt = start_dt + timedelta(minutes=10)
            range_sql = f"['{start_dt.isoformat()}', '{end_dt.isoformat()}')"
            cur.execute(
                "INSERT INTO spike_booking (resource_id, time_range, status, expires_at) "
                "VALUES (%s, %s::tstzrange, 'held', now() - interval '1 minute')",
                (RESOURCE_ID, range_sql),
            )
    conn.commit()


def worker(barrier: threading.Barrier, slot_index: int, results: list, lock: threading.Lock) -> None:
    conn = connect()
    start_dt = BASE + timedelta(minutes=slot_index * 10)
    end_dt = start_dt + timedelta(minutes=10)
    range_sql = f"['{start_dt.isoformat()}', '{end_dt.isoformat()}')"
    barrier.wait()
    try:
        with conn.cursor() as cur:
            # Exact pattern from Spec v1.0 §4.1 step 2 (cleanup-on-write),
            # scoped to resource + range, in the SAME transaction as the insert.
            cur.execute(
                "DELETE FROM spike_booking WHERE resource_id = %s AND status = 'held' "
                "AND expires_at <= now() AND time_range && %s::tstzrange",
                (RESOURCE_ID, range_sql),
            )
            cur.execute(
                "INSERT INTO spike_booking (resource_id, time_range, status) "
                "VALUES (%s, %s::tstzrange, 'confirmed')",
                (RESOURCE_ID, range_sql),
            )
        conn.commit()
        outcome = (True, None)
    except psycopg.Error as e:
        conn.rollback()
        outcome = (False, getattr(e, "sqlstate", None))
    finally:
        conn.close()
    with lock:
        results.append(outcome)


def run_once(rep: int) -> dict:
    reset_schema()
    seed_conn = connect()
    seed_expired_holds(seed_conn)
    seed_conn.close()

    results: list[tuple[bool, str | None]] = []
    lock = threading.Lock()
    barrier = threading.Barrier(N)
    threads = [threading.Thread(target=worker, args=(barrier, i, results, lock)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    deadlocks = sum(1 for ok, code in results if not ok and code == "40P01")
    successes = sum(1 for ok, _ in results if ok)
    other_failures = sum(1 for ok, code in results if not ok and code != "40P01")
    print(f"rep {rep:2d}: successes={successes}/{N}  deadlocks(40P01)={deadlocks}  other_failures={other_failures}")
    return {"successes": successes, "deadlocks": deadlocks, "other_failures": other_failures}


if __name__ == "__main__":
    print(f"=== S1.7 — cleanup-on-write vs. insert, N={N}, {REPS} repetitions ===")
    runs = [run_once(i + 1) for i in range(REPS)]
    total_deadlocks = sum(r["deadlocks"] for r in runs)
    reps_with_deadlock = sum(1 for r in runs if r["deadlocks"] > 0)
    print()
    print("=== Summary ===")
    print(f"Total deadlocks (40P01) across all {REPS} reps: {total_deadlocks}")
    print(f"Reps with at least one deadlock: {reps_with_deadlock}/{REPS}")
