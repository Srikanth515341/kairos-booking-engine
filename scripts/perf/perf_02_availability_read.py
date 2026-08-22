"""PERF-02 — Availability read P95 < 500ms (Test Plan v1.0 §11;
Implementation Plan Phase 29).

(a) Ordinary booking density — a random mix of the seeded "moderate"
    resources, a representative 30-day query window.
(b) A near-fully-booked hot resource at the 92-day query bound — the
    seeded "dense" resources, queried at exactly `MAX_AVAILABILITY_QUERY_
    DAYS` (92), confirming no material degradation at the upper bound of
    a single query's result size (Test Plan's own wording).

Requires a real `manage.py runserver` already running under
`kairos.settings.perf`, connected to a `kairos_dev` seeded via
`manage.py seed_perf_data`.
"""

from __future__ import annotations

import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    HttpResult,
    django_setup,
    http_request,
    load_manifest,
    mint_tokens,
    print_percentile_table,
)

django_setup()
from django.utils import timezone as django_timezone  # noqa: E402

N_REQUESTS = 300
CONCURRENCY = 20
AVAILABILITY_BOUND_DAYS = 92


def _iso(dt: object) -> str:
    return dt.isoformat().replace("+00:00", "Z")  # type: ignore[attr-defined]


def main() -> None:
    manifest = load_manifest()
    moderate = manifest["moderate_resource_ids"]
    dense = manifest["dense_resource_ids"]
    users = manifest["users"][:5]
    token = mint_tokens(users)[users[0]]

    now = django_timezone.now()

    # (a) Ordinary density, 30-day window, random resource per request.
    def _ordinary(_: int) -> HttpResult:
        resource_id = random.choice(moderate)
        frm = now + timedelta(days=1)
        to = frm + timedelta(days=30)
        return http_request(
            "GET",
            f"/api/v1/resources/{resource_id}/availability",
            token=token,
            params={"from": _iso(frm), "to": _iso(to)},
        )

    print(f"PERF-02(a): {N_REQUESTS} requests, ordinary density, 30-day window...")
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        ordinary_results = list(pool.map(_ordinary, range(N_REQUESTS)))

    # (b) Near-fully-booked hot resource, exactly the 92-day bound.
    def _dense(_: int) -> HttpResult:
        resource_id = random.choice(dense)
        frm = now + timedelta(days=1)
        to = frm + timedelta(days=AVAILABILITY_BOUND_DAYS)
        return http_request(
            "GET",
            f"/api/v1/resources/{resource_id}/availability",
            token=token,
            params={"from": _iso(frm), "to": _iso(to)},
        )

    print(f"PERF-02(b): {N_REQUESTS} requests, near-fully-booked resource, {AVAILABILITY_BOUND_DAYS}-day bound...")
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        dense_results = list(pool.map(_dense, range(N_REQUESTS)))

    ordinary_ok = [r for r in ordinary_results if r.status_code == 200]
    dense_ok = [r for r in dense_results if r.status_code == 200]
    ordinary_failed = [r for r in ordinary_results if r.status_code != 200]
    dense_failed = [r for r in dense_results if r.status_code != 200]

    print()
    print(f"(a) ordinary: {len(ordinary_ok)}/{len(ordinary_results)} succeeded (200)")
    if ordinary_failed:
        print(f"  non-200: {[(r.status_code, r.error) for r in ordinary_failed][:5]}")
    ordinary_stats = print_percentile_table("PERF-02(a) ordinary", [r.latency_ms for r in ordinary_ok])

    print()
    print(f"(b) dense/92d: {len(dense_ok)}/{len(dense_results)} succeeded (200)")
    if dense_failed:
        print(f"  non-200: {[(r.status_code, r.error) for r in dense_failed][:5]}")
    dense_stats = print_percentile_table("PERF-02(b) dense/92d", [r.latency_ms for r in dense_ok])

    # Confirm one dense-resource response's busy_blocks count as a sanity
    # check that the "near-fully-booked" seed data is actually being hit,
    # not silently querying an empty range.
    sample = next((r for r in dense_ok if r.body), None)
    n_busy_blocks = len(sample.body.get("busy_blocks", [])) if sample and sample.body else None

    target_ms = 500.0
    print()
    print(f"Target: P95 < {target_ms}ms")
    print(f"  (a) ordinary P95  = {ordinary_stats['p95']:.1f}ms -> {'PASS' if ordinary_stats['p95'] < target_ms else 'FAIL'}")
    print(f"  (b) dense/92d P95 = {dense_stats['p95']:.1f}ms -> {'PASS' if dense_stats['p95'] < target_ms else 'FAIL'}")
    print(f"  sanity: a sampled dense-resource response carried {n_busy_blocks} busy_blocks")

    out = {
        "ordinary": {"n": len(ordinary_results), "n_success": len(ordinary_ok), "stats_ms": ordinary_stats},
        "dense_92d": {
            "n": len(dense_results),
            "n_success": len(dense_ok),
            "stats_ms": dense_stats,
            "sample_busy_blocks": n_busy_blocks,
        },
        "target_ms": target_ms,
    }
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("perf_02_results.json")
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
