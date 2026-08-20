"""
S1.6 — Single-resource write throughput ceiling (RFC v1.0 §16, §6).

Escalating concurrency writing DISTINCT, NON-OVERLAPPING ranges on the SAME
resource — isolating GiST index contention from overlap correctness (which
S1.2 already covers). Feeds RFC §6's scalability mitigations and PRD M9.
"""

from __future__ import annotations

import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from common import connect, reset_schema

STEPS = (10, 25, 50, 100, 250, 500)
RESOURCE_ID = str(uuid.uuid4())
BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def worker(barrier: threading.Barrier, slot_index: int, latencies: list[float], lock: threading.Lock) -> None:
    conn = connect()
    start_dt = BASE + timedelta(minutes=slot_index * 15)
    end_dt = start_dt + timedelta(minutes=10)  # 10-min booking inside a 15-min slot: no overlap
    range_sql = f"['{start_dt.isoformat()}', '{end_dt.isoformat()}')"
    barrier.wait()
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO spike_booking (resource_id, time_range) VALUES (%s, %s::tstzrange)",
            (RESOURCE_ID, range_sql),
        )
    conn.commit()
    elapsed = time.perf_counter() - t0
    conn.close()
    with lock:
        latencies.append(elapsed)


def run_step(n: int) -> None:
    reset_schema()
    latencies: list[float] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)
    threads = [threading.Thread(target=worker, args=(barrier, i, latencies, lock)) for i in range(n)]

    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_elapsed = time.perf_counter() - wall_start

    latencies.sort()

    def pctl(p: float) -> float:
        idx = min(len(latencies) - 1, int(len(latencies) * p))
        return latencies[idx]

    writes_per_sec = n / wall_elapsed if wall_elapsed > 0 else float("inf")
    print(
        f"N={n:4d}  wall={wall_elapsed:7.3f}s  writes/sec={writes_per_sec:8.1f}  "
        f"p50={statistics.median(latencies)*1000:7.2f}ms  "
        f"p95={pctl(0.95)*1000:7.2f}ms  p99={pctl(0.99)*1000:7.2f}ms  "
        f"max={max(latencies)*1000:7.2f}ms"
    )


if __name__ == "__main__":
    print("=== S1.6 — single-resource throughput ceiling (non-overlapping writes) ===")
    for n in STEPS:
        run_step(n)
