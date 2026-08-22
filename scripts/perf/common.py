"""
Shared helpers for Phase 29's performance/load-testing scripts (Test Plan
v1.0 §11, §16; Implementation Plan Phase 29).

THROWAWAY, LIKE scripts/spike/: these scripts drive a REAL, already-running
`manage.py runserver` (kairos.settings.perf — see that module's own
docstring for why it exists) over REAL HTTP, using only the Python standard
library (`urllib`, `threading`, `concurrent.futures`) — no new dependency,
matching this project's own established "write the small thing, don't add
a library for it" convention (CorsMiddleware, hand-rolled dateGrid.ts).
None of this becomes application code; it exists to produce
docs/performance-baseline.md's real numbers.

Django is set up (`django_setup()`) ONLY so these scripts can call the real
`kairos.identity.oidc.issue_session_token` directly — minting a batch of
session tokens once at start, not re-authenticating per request, since
that's setup overhead this project doesn't want polluting a write-latency
measurement (the same "unmeasured setup, measured operation" split every
pytest fixture in this codebase already draws).
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("KAIROS_PERF_BASE_URL", "http://127.0.0.1:8000")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
DEFAULT_MANIFEST_PATH = Path(
    os.environ.get(
        "KAIROS_PERF_MANIFEST",
        str(Path(os.environ.get("TEMP", "/tmp")) / "claude" / "perf_manifest.json"),
    )
)


def django_setup() -> None:
    """Idempotent — safe to call from every script, even ones importing
    each other. Only needed for `mint_tokens`; everything else in this
    module is plain HTTP + stdlib.
    """
    sys.path.insert(0, str(BACKEND_DIR))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kairos.settings.perf")
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()


def load_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def mint_tokens(user_ids: Iterable[str]) -> dict[str, str]:
    """One real `issue_session_token` call per user — the SAME function
    `POST /auth/token` calls internally (kairos/identity/views.py). The
    token itself is real and is verified by the real `OIDCSessionAuthentication`
    class on every single measured request below; only the MINTING call
    happens in-process here rather than over its own HTTP round trip.
    """
    django_setup()
    from kairos.identity.oidc import issue_session_token

    return {uid: issue_session_token(uuid.UUID(uid))[0] for uid in user_ids}


def clear_resource_bookings(resource_ids: Iterable[str]) -> int:
    """PERF-01 and CONC-06 both allocate slots deterministically from
    index 0 on every run (`_allocate_slot`/`_slot`) — re-running either
    script against a resource a PREVIOUS run already wrote into would
    collide with that old data and turn every write into a genuine 409,
    which is a stale-fixture problem, not a real finding. Called once at
    the start of each such script, unmeasured, the same "clean slate
    before the timed portion" discipline `tests/concurrency/harness.py`'s
    own `clear_bookings` already established for the raw-SQL proofs.
    """
    django_setup()
    from kairos.bookings.models import Booking

    deleted, _ = Booking.objects.filter(resource_id__in=list(resource_ids)).delete()
    return deleted


@dataclass
class HttpResult:
    status_code: int | None
    latency_ms: float
    body: dict[str, Any] | None
    error: str | None = None


def http_request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> HttpResult:
    url = f"{BASE_URL}{path}"
    if params:
        from urllib.parse import urlencode

        url = f"{url}?{urlencode(params)}"

    data = None
    headers = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers["Idempotency-Key"] = str(uuid.uuid4())

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            latency_ms = (time.perf_counter() - start) * 1000
            body = json.loads(raw) if raw else None
            return HttpResult(status_code=resp.status, latency_ms=latency_ms, body=body)
    except urllib.error.HTTPError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        raw = exc.read()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        return HttpResult(status_code=exc.code, latency_ms=latency_ms, body=body)
    except Exception as exc:  # noqa: BLE001 — a load-test client must record, not crash, on any transport failure
        latency_ms = (time.perf_counter() - start) * 1000
        return HttpResult(status_code=None, latency_ms=latency_ms, body=None, error=str(exc))


def percentiles(values: list[float], points: tuple[int, ...] = (50, 95, 99)) -> dict[str, float]:
    """Nearest-rank percentile over a sorted copy — no numpy dependency.
    `p == 100` isn't offered deliberately: this project consistently
    reports p50/p95/p99, matching every Rollout v1.0 §6.2 target and Test
    Plan §11's own PERF-01/02/03 wording, never a max.
    """
    if not values:
        empty = {f"p{p}": float("nan") for p in points}
        empty["mean"] = float("nan")
        empty["n"] = 0.0
        return empty
    ordered = sorted(values)
    result: dict[str, float] = {}
    for p in points:
        idx = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered))) - 1))
        result[f"p{p}"] = ordered[idx]
    result["mean"] = statistics.mean(ordered)
    result["n"] = float(len(ordered))
    return result


def run_barrier_released(
    worker_fn: Callable[[int], HttpResult], n: int, max_workers: int | None = None
) -> list[HttpResult]:
    """True simultaneity, the same `threading.Barrier` discipline
    `tests/concurrency/harness.py` already established for the raw-SQL
    concurrency proofs — here applied to real HTTP requests instead of
    raw psycopg connections, since PERF-01's spike and CONC-06's escalation
    both measure the APPLICATION layer, not the bare constraint.
    """
    barrier = threading.Barrier(n)
    results: list[HttpResult | None] = [None] * n

    def _run(i: int) -> None:
        barrier.wait()
        results[i] = worker_fn(i)

    with ThreadPoolExecutor(max_workers=max_workers or n) as pool:
        list(pool.map(_run, range(n)))

    return [r for r in results if r is not None]


@dataclass
class SustainedRunResult:
    results: list[HttpResult] = field(default_factory=list)


def run_sustained(
    worker_fn: Callable[[int], HttpResult],
    *,
    duration_seconds: float,
    requests_per_second: float,
    max_workers: int = 16,
) -> list[HttpResult]:
    """A steady baseline rate over a window (PERF-01's own "sustained
    baseline rate; P95 over the window") — requests are dispatched on a
    fixed-interval schedule from a bounded worker pool, not barrier-
    released (that's the SPIKE shape, not the steady one).
    """
    interval = 1.0 / requests_per_second
    results: list[HttpResult] = []
    results_lock = threading.Lock()
    stop_at = time.monotonic() + duration_seconds
    counter = 0

    def _dispatch(i: int) -> None:
        r = worker_fn(i)
        with results_lock:
            results.append(r)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        next_fire = time.monotonic()
        while time.monotonic() < stop_at:
            now = time.monotonic()
            if now < next_fire:
                time.sleep(next_fire - now)
            pool.submit(_dispatch, counter)
            counter += 1
            next_fire += interval
        pool.shutdown(wait=True)

    return results


def print_percentile_table(label: str, latencies_ms: list[float]) -> dict[str, float]:
    stats = percentiles(latencies_ms)
    print(
        f"{label}: n={int(stats['n'])} "
        f"p50={stats['p50']:.1f}ms p95={stats['p95']:.1f}ms p99={stats['p99']:.1f}ms "
        f"mean={stats['mean']:.1f}ms"
    )
    return stats
