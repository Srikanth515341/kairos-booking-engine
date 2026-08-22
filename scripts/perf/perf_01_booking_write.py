"""PERF-01 — Booking write P95 < 300ms, steady AND spike (Test Plan v1.0
§11; Implementation Plan Phase 29).

Requires a real `manage.py runserver` already running under
`kairos.settings.perf`, connected to a `kairos_dev` seeded via
`manage.py seed_perf_data` (see that command's own docstring).

Every write here targets the dedicated, otherwise-EMPTY "write pool"
resources from the seed manifest — never the general PRD-A1-density
resources — via a deterministic, pre-computed slot allocation (one
booking per resource-day-hour triple) so concurrent writers can NEVER
collide with each other or with anything the seed command wrote. This is
deliberate: PERF-01's own scope note says contested-slot latency is
CONC-06's job, not this test's — a write that hits the exclusion
constraint here would be measuring something PERF-01 explicitly excludes.

- Steady: a sustained baseline rate for a fixed window; P95 over that
  whole window.
- Spike: 200 concurrent requests within a 2-second window, fired WHILE
  the steady background load is still running — P95 computed over the
  spike's own 200 requests only, per Test Plan's explicit warning that a
  long-window aggregate would dilute a bad spike number into an
  acceptable-looking one.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    HttpResult,
    clear_resource_bookings,
    django_setup,
    http_request,
    load_manifest,
    mint_tokens,
    print_percentile_table,
    run_barrier_released,
)

django_setup()
from django.utils import timezone as django_timezone  # noqa: E402

STEADY_DURATION_SECONDS = 60
STEADY_REQUESTS_PER_SECOND = 5
SPIKE_N = 200


def _allocate_slot(resource_ids: list[str], slot_index: int) -> tuple[str, str, str]:
    """One booking per (resource, day-offset, hour) triple — a pure
    function of `slot_index`, so pre-allocating N slots for N requests
    guarantees zero collisions between them, by construction, without
    needing a shared counter/lock across worker threads.
    """
    resource = resource_ids[slot_index % len(resource_ids)]
    within_resource = slot_index // len(resource_ids)
    day_offset = 1 + within_resource // 24
    hour = within_resource % 24
    base = django_timezone.now() + timedelta(days=day_offset)
    start = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    return resource, start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def main() -> None:
    manifest = load_manifest()
    write_pool = manifest["write_pool_resource_ids"]
    users = manifest["users"]

    cleared = clear_resource_bookings(write_pool)
    if cleared:
        print(f"Cleared {cleared} booking(s) left over from a previous run of this script.")

    total_slots = STEADY_DURATION_SECONDS * STEADY_REQUESTS_PER_SECOND + SPIKE_N + 50
    n_users_needed = min(len(users), total_slots)
    print(f"Minting {n_users_needed} session tokens...")
    user_pool = users[:n_users_needed]
    tokens = mint_tokens(user_pool)

    slot_counter = {"i": 0}
    slot_lock = threading.Lock()

    def _next_slot() -> int:
        with slot_lock:
            i = slot_counter["i"]
            slot_counter["i"] += 1
            return i

    def _do_write(worker_index: int) -> HttpResult:
        slot_i = _next_slot()
        resource_id, start_iso, end_iso = _allocate_slot(write_pool, slot_i)
        token = tokens[user_pool[slot_i % len(user_pool)]]
        return http_request(
            "POST",
            "/api/v1/bookings",
            token=token,
            json_body={"resource_id": resource_id, "start": start_iso, "end": end_iso},
        )

    # --- Steady: fixed rate for a window, background thread -------------
    print(
        f"Steady: {STEADY_REQUESTS_PER_SECOND} req/s for {STEADY_DURATION_SECONDS}s "
        f"({STEADY_REQUESTS_PER_SECOND * STEADY_DURATION_SECONDS} requests)..."
    )
    steady_results: list[HttpResult] = []
    steady_results_lock = threading.Lock()
    steady_stop = threading.Event()

    def _steady_loop() -> None:
        interval = 1.0 / STEADY_REQUESTS_PER_SECOND
        next_fire = time.monotonic()
        while not steady_stop.is_set():
            now = time.monotonic()
            if now < next_fire:
                time.sleep(min(next_fire - now, 0.05))
                continue
            r = _do_write(0)
            with steady_results_lock:
                steady_results.append(r)
            next_fire += interval

    steady_thread = threading.Thread(target=_steady_loop, daemon=True)
    steady_thread.start()

    # Let steady load establish itself before firing the spike "on top of"
    # it (Test Plan's own wording), then fire while it's still running.
    time.sleep(STEADY_DURATION_SECONDS * 0.4)

    print(f"Spike: {SPIKE_N} concurrent requests within a 2s window, on top of steady load...")
    spike_results = run_barrier_released(_do_write, SPIKE_N, max_workers=SPIKE_N)

    # Let steady load finish out its window.
    remaining = STEADY_DURATION_SECONDS * 0.6
    time.sleep(remaining)
    steady_stop.set()
    steady_thread.join(timeout=5)

    steady_ok = [r for r in steady_results if r.status_code == 201]
    steady_failed = [r for r in steady_results if r.status_code != 201]
    spike_ok = [r for r in spike_results if r.status_code == 201]
    spike_failed = [r for r in spike_results if r.status_code != 201]

    print()
    print(f"Steady: {len(steady_ok)}/{len(steady_results)} succeeded (201)")
    if steady_failed:
        print(f"  non-201: {[(r.status_code, r.error) for r in steady_failed]}")
    steady_stats = print_percentile_table("PERF-01 steady", [r.latency_ms for r in steady_ok])

    print()
    print(f"Spike: {len(spike_ok)}/{len(spike_results)} succeeded (201)")
    if spike_failed:
        print(f"  non-201: {[(r.status_code, r.error) for r in spike_failed]}")
    spike_stats = print_percentile_table("PERF-01 spike", [r.latency_ms for r in spike_ok])

    target_ms = 300.0
    print()
    print(f"Target: P95 < {target_ms}ms")
    print(f"  steady P95 = {steady_stats['p95']:.1f}ms -> {'PASS' if steady_stats['p95'] < target_ms else 'FAIL'}")
    print(f"  spike P95  = {spike_stats['p95']:.1f}ms -> {'PASS' if spike_stats['p95'] < target_ms else 'FAIL'}")

    out = {
        "steady": {
            "n": len(steady_results),
            "n_success": len(steady_ok),
            "stats_ms": steady_stats,
        },
        "spike": {
            "n": len(spike_results),
            "n_success": len(spike_ok),
            "stats_ms": spike_stats,
        },
        "target_ms": target_ms,
    }
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("perf_01_results.json")
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
