"""
S1.2 shows N=200 true-simultaneous identical-range contention is NOT
deterministic: some runs produce a clean single 23P01-losing-everyone-else
result, others produce deadlocks (40P01) and, under retry, can cascade to
zero successes within the attempt budget. This script runs N=200 repeatedly
(single attempt, no retry) to measure how often deadlocks actually occur —
directly informing whether BookingService (Phase 4) needs to catch and
retry 40P01 in addition to translating 23P01, and validating why Test Plan
v1.0 CONC-01 mandates 100 consecutive runs rather than trusting one.
"""

from __future__ import annotations

import threading
import uuid

import psycopg

from common import connect, reset_schema

RANGE_SQL = "['2026-09-01 13:00:00+00', '2026-09-01 14:00:00+00')"
N = 200
REPS = 10


def run_once(rep: int) -> dict:
    reset_schema()
    resource_id = str(uuid.uuid4())
    barrier = threading.Barrier(N)
    results: list[tuple[bool, str | None]] = []
    lock = threading.Lock()

    def worker() -> None:
        conn = connect()
        barrier.wait()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO spike_booking (resource_id, time_range) "
                    "VALUES (%s, %s::tstzrange)",
                    (resource_id, RANGE_SQL),
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

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for ok, _ in results if ok)
    sqlstates: dict[str, int] = {}
    for ok, code in results:
        if not ok:
            sqlstates[code] = sqlstates.get(code, 0) + 1

    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM spike_booking WHERE resource_id = %s "
            "AND status IN ('confirmed','held')",
            (resource_id,),
        )
        ground_truth = cur.fetchone()[0]
    conn.close()

    print(f"rep {rep:2d}: successes={successes}  sqlstate_counts={sqlstates}  "
          f"ground_truth={ground_truth}")
    return {"successes": successes, "sqlstates": sqlstates, "ground_truth": ground_truth}


if __name__ == "__main__":
    print(f"=== N={N}, single attempt, {REPS} repetitions ===")
    runs = [run_once(i + 1) for i in range(REPS)]

    clean = sum(1 for r in runs if r["successes"] == 1 and "40P01" not in r["sqlstates"])
    had_deadlock = sum(1 for r in runs if "40P01" in r["sqlstates"])
    zero_success = sum(1 for r in runs if r["successes"] == 0)
    multi_success = sum(1 for r in runs if r["successes"] > 1)

    print()
    print("=== Summary across all reps ===")
    print(f"Clean (1 success, no deadlocks): {clean}/{REPS}")
    print(f"Runs with at least one deadlock (40P01): {had_deadlock}/{REPS}")
    print(f"Runs with ZERO successes (correctness-neutral, availability concern): {zero_success}/{REPS}")
    print(f"Runs with MORE THAN ONE success (would be a correctness violation): {multi_success}/{REPS}")
