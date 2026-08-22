"""PERF-03 — Waitlist dispatch P95 < 5s, including a 50-cancellation burst
(Test Plan v1.0 §11; Implementation Plan Phase 29).

"Measured from cancellation commit to notification enqueued" (Test Plan's
own wording). `NotificationLog` is written EXCLUSIVELY by the Celery
worker actually attempting delivery (kairos/core/models.py's own
docstring on that table) — polling for THAT row would measure delivery,
not enqueue. The closest externally-observable proxy for "enqueued"
without instrumenting Celery's broker directly is the `WaitlistOffer` row
itself: `create_offer_for_freed_range` (kairos/waitlist/services.py)
creates that row and calls `notify_offer_created` — which enqueues the
notification's own delivery task — in the SAME worker execution,
synchronously, right before returning. This script measures wall-clock
time from the real `POST /bookings/{id}/cancel` HTTP response to that
`WaitlistOffer` row's `created_at`, documented here so
docs/performance-baseline.md's methodology is explicit about the proxy.

Requires a real `manage.py runserver` AND a real Celery worker already
running (both point at the same `kairos_dev`/Redis — see docker-compose;
`manage.py runserver` here runs OUTSIDE Docker but on the same mapped
ports) — this is a genuine cross-process, real-broker measurement,
deliberately not `CELERY_TASK_ALWAYS_EAGER` (that's test-settings-only).
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import time as dtime
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    django_setup,
    http_request,
    load_manifest,
    mint_tokens,
    print_percentile_table,
)

django_setup()

from django.utils import timezone as django_timezone  # noqa: E402

from kairos.bookings.models import Booking, BookingStatus  # noqa: E402
from kairos.identity.models import AppUser  # noqa: E402
from kairos.resources.models import Resource  # noqa: E402
from kairos.waitlist.models import WaitlistEntry, WaitlistOffer  # noqa: E402

POLL_INTERVAL_SECONDS = 0.01
POLL_TIMEOUT_SECONDS = 10.0
BURST_N = 50


def _setup_pair(owner: AppUser, waiter: AppUser, canceller: AppUser, name: str) -> tuple[Resource, Booking, WaitlistEntry]:
    """One dedicated resource, one confirmed booking occupying a range,
    one waiting entry for the IDENTICAL range (trivially eligible via
    containment, PRD FR21) — so cancelling the booking deterministically
    triggers exactly one cascade with exactly one candidate.
    """
    resource = Resource.objects.create(
        name=f"PERF {name}",
        timezone="UTC",
        bookable_start_time=dtime(0, 0),
        bookable_end_time=dtime(23, 59, 59),
        created_by=owner,
    )
    start = django_timezone.now() + timedelta(days=1)
    end = start + timedelta(hours=1)
    booking = Booking.objects.create(
        resource=resource, user=canceller, time_range=(start, end), status=BookingStatus.CONFIRMED
    )
    entry = WaitlistEntry.objects.create(resource=resource, user=waiter, time_range=(start, end))
    return resource, booking, entry


def _cancel_and_measure(token: str, booking_id: str, entry_id: uuid.UUID) -> float | None:
    result = http_request(
        "POST", f"/api/v1/bookings/{booking_id}/cancel", token=token, json_body={}
    )
    if result.status_code != 200:
        print(f"  cancel failed: status={result.status_code} body={result.body} error={result.error}")
        return None
    t0 = time.monotonic()

    deadline = t0 + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        offer = WaitlistOffer.objects.filter(waitlist_entry_id=entry_id).first()
        if offer is not None:
            return (time.monotonic() - t0) * 1000
        time.sleep(POLL_INTERVAL_SECONDS)
    return None  # timed out — treated as a failure, not silently dropped


def main() -> None:
    manifest = load_manifest()
    owner_id = manifest["owner_user_id"]
    users = manifest["users"]
    owner = AppUser.objects.get(id=owner_id)
    canceller_id, waiter_id = users[0], users[1]
    canceller = AppUser.objects.get(id=canceller_id)
    waiter = AppUser.objects.get(id=waiter_id)
    token = mint_tokens([canceller_id])[canceller_id]

    # --- (a) Isolated cancellation, repeated for a percentile sample ----
    N_ISOLATED = 20
    print(f"PERF-03(a): {N_ISOLATED} isolated cancellations...")
    isolated_latencies: list[float] = []
    isolated_failures = 0
    for i in range(N_ISOLATED):
        _, booking, entry = _setup_pair(owner, waiter, canceller, f"WL03-iso-{uuid.uuid4().hex[:8]}")
        latency = _cancel_and_measure(token, str(booking.id), entry.id)
        if latency is None:
            isolated_failures += 1
        else:
            isolated_latencies.append(latency)
    print(f"  {len(isolated_latencies)}/{N_ISOLATED} dispatched, {isolated_failures} timed out")
    isolated_stats = print_percentile_table("PERF-03(a) isolated", isolated_latencies)

    # --- (b) Burst of 50 near-simultaneous cancellations -----------------
    print(f"\nPERF-03(b): burst of {BURST_N} near-simultaneous cancellations...")
    pairs = [
        _setup_pair(owner, waiter, canceller, f"WL03-burst-{uuid.uuid4().hex[:8]}")
        for _ in range(BURST_N)
    ]
    burst_tokens = mint_tokens([canceller_id] * 1)  # same canceller, distinct bookings — fine
    burst_token = burst_tokens[canceller_id]

    def _burst_worker(i: int) -> float | None:
        _, booking, entry = pairs[i]
        return _cancel_and_measure(burst_token, str(booking.id), entry.id)

    # common.run_barrier_released expects an HttpResult-returning fn; here
    # each worker returns float | None (a dispatch latency or a timeout),
    # so the barrier is driven directly instead.
    barrier = threading.Barrier(BURST_N)
    burst_results: list[float | None] = [None] * BURST_N

    def _run(i: int) -> None:
        barrier.wait()
        burst_results[i] = _burst_worker(i)

    with ThreadPoolExecutor(max_workers=BURST_N) as pool:
        list(pool.map(_run, range(BURST_N)))

    burst_latencies = [r for r in burst_results if r is not None]
    burst_failures = sum(1 for r in burst_results if r is None)
    print(f"  {len(burst_latencies)}/{BURST_N} dispatched, {burst_failures} timed out")
    burst_stats = print_percentile_table("PERF-03(b) burst", burst_latencies)

    target_ms = 5000.0
    print()
    print(f"Target: P95 < {target_ms}ms")
    print(f"  (a) isolated P95 = {isolated_stats['p95']:.1f}ms -> {'PASS' if isolated_stats['p95'] < target_ms else 'FAIL'}")
    print(f"  (b) burst P95    = {burst_stats['p95']:.1f}ms -> {'PASS' if burst_stats['p95'] < target_ms else 'FAIL'}")

    out = {
        "isolated": {"n": N_ISOLATED, "n_dispatched": len(isolated_latencies), "n_timed_out": isolated_failures, "stats_ms": isolated_stats},
        "burst": {"n": BURST_N, "n_dispatched": len(burst_latencies), "n_timed_out": burst_failures, "stats_ms": burst_stats},
        "target_ms": target_ms,
        "methodology": "measured from cancel HTTP response to the corresponding WaitlistOffer row's created_at (see module docstring)",
    }
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("perf_03_results.json")
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
