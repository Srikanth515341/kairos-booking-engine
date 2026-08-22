"""CONC-06 — Sustained hot-resource escalation (Test Plan v1.0 §2, §11;
Implementation Plan Phase 29). Not a pass/fail throughput gate — RFC v1.0
§18 deliberately left the ceiling unresolved, and this script's job is to
produce the missing latency-vs-concurrency data, not grade against a
number nobody committed to.

CONC-06a IS a hard gate, checked at every step: writers target DISTINCT,
NON-OVERLAPPING slots on the one dedicated CONC-06 resource (Test Plan's
own setup — isolating index contention from overlap correctness, which
CONC-01-05 already cover elsewhere), so every single response MUST be 201,
or a documented 503 under lock contention — never a 409 (nothing here
should ever conflict), never a 429 (kairos.settings.perf disables rate
limiting for exactly this reason), never a 500. Any other outcome is
flagged loudly as a genuine bug, not folded quietly into the throughput
numbers.

Requires a real `manage.py runserver` already running under
`kairos.settings.perf`, connected to a `kairos_dev` seeded via
`manage.py seed_perf_data`.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
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
    percentiles,
    run_barrier_released,
)

django_setup()
from django.utils import timezone as django_timezone  # noqa: E402

ESCALATION_STEPS = (10, 25, 50, 100, 250, 500)
DOCUMENTED_503_CAUSES = {"lock_contention", "failover"}


def _slot(resource_id: str, slot_index: int) -> tuple[str, str]:
    """One booking per hour offset — a pure function of `slot_index`, so
    every writer across every escalation step gets a globally-unique
    slot, guaranteeing zero overlap by construction (never relying on
    randomness or retry logic the way the general seed data does).
    """
    day_offset = 1 + slot_index // 24
    hour = slot_index % 24
    base = django_timezone.now() + timedelta(days=day_offset)
    start = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=30)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def main() -> None:
    manifest = load_manifest()
    resource_id = manifest["conc06_resource_id"]
    users = manifest["users"]

    cleared = clear_resource_bookings([resource_id])
    if cleared:
        print(f"Cleared {cleared} booking(s) left over from a previous run of this script.")

    max_n = max(ESCALATION_STEPS)
    if len(users) < max_n:
        print(f"WARNING: only {len(users)} seeded users, need {max_n} for the largest step; reusing some.")
    print(f"Minting up to {min(len(users), max_n)} session tokens...")
    user_pool = users[: min(len(users), max_n)]
    tokens = mint_tokens(user_pool)

    slot_offset = {"i": 0}
    report_rows: list[dict[str, object]] = []
    any_violation = False

    for n in ESCALATION_STEPS:
        base_slot = slot_offset["i"]

        def _worker(i: int, base_slot: int = base_slot) -> HttpResult:
            start_iso, end_iso = _slot(resource_id, base_slot + i)
            token = tokens[user_pool[i % len(user_pool)]]
            return http_request(
                "POST",
                "/api/v1/bookings",
                token=token,
                json_body={"resource_id": resource_id, "start": start_iso, "end": end_iso},
            )

        print(f"\n=== N={n} writers, distinct non-overlapping slots ===")
        results = run_barrier_released(_worker, n, max_workers=n)
        slot_offset["i"] += n

        def _is_documented_503(r: HttpResult) -> bool:
            return (
                r.status_code == 503
                and r.body is not None
                and r.body.get("error", {}).get("code") == "service_unavailable"
            )

        successes = [r for r in results if r.status_code == 201]
        documented_503 = [r for r in results if _is_documented_503(r)]
        violations = [
            r for r in results if r.status_code != 201 and not _is_documented_503(r)
        ]

        status_counts = Counter(r.status_code for r in results)
        latencies = [r.latency_ms for r in results]
        stats = percentiles(latencies)

        wall_time_s = max(latencies) / 1000 if latencies else 0.0
        writes_per_sec = len(successes) / wall_time_s if wall_time_s > 0 else float("nan")
        error_rate = 1 - (len(successes) / len(results)) if results else float("nan")

        print(
            f"  n={n} success={len(successes)} 503={len(documented_503)} "
            f"violations={len(violations)} status_counts={dict(status_counts)}"
        )
        print(
            f"  latency: p50={stats['p50']:.1f}ms p95={stats['p95']:.1f}ms p99={stats['p99']:.1f}ms  "
            f"writes/sec={writes_per_sec:.1f}  error_rate={error_rate:.3f}"
        )
        if violations:
            any_violation = True
            print(f"  *** CONC-06a VIOLATION at N={n}: {len(violations)} unexpected outcome(s) ***")
            for v in violations[:10]:
                print(f"      status={v.status_code} body={v.body} error={v.error}")

        report_rows.append(
            {
                "n": n,
                "n_success": len(successes),
                "n_503_documented": len(documented_503),
                "n_violations": len(violations),
                "status_counts": {str(k): v for k, v in status_counts.items()},
                "p50_ms": stats["p50"],
                "p95_ms": stats["p95"],
                "p99_ms": stats["p99"],
                "writes_per_sec": writes_per_sec,
                "error_rate": error_rate,
            }
        )

    print("\n=== CONC-06 latency-vs-concurrency table ===")
    print(f"{'N':>5} {'p50ms':>8} {'p95ms':>8} {'p99ms':>8} {'writes/s':>10} {'err_rate':>9} {'violations':>11}")
    for row in report_rows:
        print(
            f"{row['n']:>5} {row['p50_ms']:>8.1f} {row['p95_ms']:>8.1f} {row['p99_ms']:>8.1f} "
            f"{row['writes_per_sec']:>10.1f} {row['error_rate']:>9.3f} {row['n_violations']:>11}"
        )

    print(f"\nCONC-06a hard gate: {'FAIL — see violations above' if any_violation else 'PASS — zero unexpected errors at every step'}")

    out = {"steps": report_rows, "conc06a_pass": not any_violation}
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("conc_06_results.json")
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
