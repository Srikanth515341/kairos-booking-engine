"""
S1.2 — 200 barrier-released concurrent inserts for one identical slot.
S1.3 — Does a conflicting insert against an UNCOMMITTED competitor block,
       or fail immediately?

RFC v1.0 §16, §7.1. This is the single most important spike result: it
determines the entire latency model in RFC §7.1 and what CONC-01
(Test Plan v1.0) actually measures.
"""

from __future__ import annotations

import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg

from common import connect, reset_schema

N = 200
RESOURCE_ID = str(uuid.uuid4())
RANGE_SQL = "['2026-09-01 13:00:00+00', '2026-09-01 14:00:00+00')"


@dataclass
class Outcome:
    ok: bool
    sqlstate: str | None
    latency_s: float


results: list[Outcome] = []
results_lock = threading.Lock()


def worker(barrier: threading.Barrier) -> None:
    conn = connect()
    barrier.wait()  # true simultaneity — release together, not "fired in a loop"
    start = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO spike_booking (resource_id, time_range) "
                "VALUES (%s, %s::tstzrange)",
                (RESOURCE_ID, RANGE_SQL),
            )
        conn.commit()
        outcome = Outcome(ok=True, sqlstate=None, latency_s=time.perf_counter() - start)
    except psycopg.Error as e:
        conn.rollback()
        sqlstate = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
        outcome = Outcome(ok=False, sqlstate=sqlstate, latency_s=time.perf_counter() - start)
    finally:
        conn.close()
    with results_lock:
        results.append(outcome)


def run_s1_2() -> None:
    print("=== S1.2 — 200 barrier-released concurrent inserts, identical range ===")
    reset_schema()
    results.clear()
    barrier = threading.Barrier(N)
    threads = [threading.Thread(target=worker, args=(barrier,)) for _ in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r.ok]
    failures = [r for r in results if not r.ok]
    sqlstates = sorted({r.sqlstate for r in failures})
    loser_latencies = [r.latency_s for r in failures]

    print(f"Total responses: {len(results)} (expected {N})")
    print(f"Successes: {len(successes)} (must be exactly 1)")
    print(f"Failures: {len(failures)}")
    print(f"Distinct SQLSTATEs among failures: {sqlstates}")
    if loser_latencies:
        print(
            "Loser latency (s) — "
            f"min={min(loser_latencies):.4f} "
            f"p50={statistics.median(loser_latencies):.4f} "
            f"max={max(loser_latencies):.4f}"
        )

    # Ground truth, not just response codes.
    conn = connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM spike_booking "
            "WHERE resource_id = %s AND status IN ('confirmed','held')",
            (RESOURCE_ID,),
        )
        ground_truth = cur.fetchone()[0]
    conn.close()
    print(f"Ground truth active rows for this resource/range: {ground_truth} (must be 1)")


def run_s1_3() -> None:
    print()
    print("=== S1.3 — blocking vs. fail-fast against an UNCOMMITTED competitor ===")
    reset_schema()
    resource_id = str(uuid.uuid4())

    conn_a = connect()
    with conn_a.cursor() as cur:
        cur.execute(
            "INSERT INTO spike_booking (resource_id, time_range) VALUES (%s, %s::tstzrange)",
            (resource_id, RANGE_SQL),
        )
    # Deliberately NOT committed yet.

    result: dict = {}

    def try_conflicting_insert() -> None:
        conn_b = connect()
        start = time.perf_counter()
        try:
            with conn_b.cursor() as cur:
                cur.execute(
                    "INSERT INTO spike_booking (resource_id, time_range) "
                    "VALUES (%s, %s::tstzrange)",
                    (resource_id, RANGE_SQL),
                )
            conn_b.commit()
            result["outcome"] = "succeeded (unexpected — A had not committed)"
        except psycopg.Error as e:
            conn_b.rollback()
            result["outcome"] = f"failed, sqlstate={getattr(e, 'sqlstate', None)}"
        result["elapsed_before_resolution_s"] = time.perf_counter() - start
        conn_b.close()

    b_thread = threading.Thread(target=try_conflicting_insert)
    b_thread.start()

    # Give B a moment to reach the point of blocking (or failing) against A's
    # still-uncommitted row, then measure how long B has been waiting.
    time.sleep(1.5)
    b_still_running = b_thread.is_alive()
    print(f"After 1.5s wait, is B's insert still in flight (blocked)? {b_still_running}")

    commit_time = time.perf_counter()
    conn_a.commit()  # release A; B should now resolve
    conn_a.close()

    b_thread.join(timeout=10)
    resolved_after_commit_s = time.perf_counter() - commit_time
    print(f"B resolved {resolved_after_commit_s:.4f}s after A committed")
    print(f"B's outcome: {result.get('outcome')}")
    print(f"B's total elapsed time (insert attempt -> resolution): "
          f"{result.get('elapsed_before_resolution_s'):.4f}s")


if __name__ == "__main__":
    run_s1_2()
    run_s1_3()
