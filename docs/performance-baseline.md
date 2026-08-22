# Performance Baseline — Phase 29

Committed by Implementation Plan Phase 29 ("Performance & Load Testing"), per that
phase's own Definition of Done: "`docs/performance-baseline.md` committed with real
numbers." Every number in this document was produced by actually running the load
against a real, PRD-A1-scale-seeded `kairos_dev`, not estimated or invented.

## Environment and methodology — read this before the numbers

**This is a single physical machine wearing every hat**: PostgreSQL 16, Redis, the
Celery worker/beat, the application server under test, AND the load-generating test
client all ran on the same workstation, sharing Docker Desktop's allocated 8 vCPUs /
3.7GiB. This is the same "local, connection-overhead-dominated baseline, not a
production ceiling" caveat this project already carries for Spike S1.6 (CLAUDE.md
Open Questions) — applied here for the identical reason: **no deployment platform has
been chosen yet (Rollout v1.0 §2.2, Phase 30's job)**, so there is no multi-machine,
production-shaped topology to test against. These numbers characterize the
APPLICATION's own behavior on the best available serving setup in THIS environment,
not a production SLA.

**A real, load-bearing finding, not just a caveat — read this part carefully.** The
first attempt at this measurement, `manage.py runserver` (this project's documented
"how to run it locally" mechanism), showed severe, unambiguous latency degradation
under concurrent load — availability-read P95 climbing from ~150ms (single serial
request) past 2 SECONDS at just 20 concurrent readers, growing roughly linearly with
request count. This was diagnosed, not assumed:

1. A single serial request (no concurrency at all) against the SAME endpoint/data
   completed in 130–200ms — ruling out "the query itself is slow."
2. Latency at N=2/5/10/20 concurrent requests grew roughly linearly with N, the
   signature of requests being SERIALIZED rather than genuinely running in parallel.
3. Swapping `manage.py runserver` for `waitress` (a real, independently-maintained,
   multi-threaded production WSGI server, installed ad hoc for this measurement only —
   see "What was and wasn't installed," below) showed the IDENTICAL degradation
   pattern, ruling out "the dev server specifically is just bad."
4. `gunicorn` — a real MULTI-PROCESS WSGI server, which would sidestep CPython's GIL
   entirely — is Unix-only and cannot run on this Windows workstation at all
   (`ModuleNotFoundError: No module named 'fcntl'`). A throwaway Linux container (the
   existing `backend/Dockerfile`, already built for the Celery worker/beat, reused
   here with `gunicorn` swapped in for its `CMD` — never committed to `infra/docker-
   compose.yml`, never a deployment-platform decision, torn down after this
   measurement) DID substantially improve matters: PERF-01 spike P95 dropped from
   ~7.4s (waitress, single process) to ~3.2s (gunicorn, 8 worker processes); CONC-06a's
   500-writer step dropped from 232 outright-refused connections (a genuine Windows
   platform ceiling — see below) to zero.

**Conclusion, stated plainly**: this application's request-handling cost, under real
concurrent load, is measurably CPU-bound (GIL-serialized under any single-process
Python server) at least as much as it is I/O-bound (waiting on the database) —
multi-PROCESS serving materially helps, multi-THREADING alone does not. This is a real
characteristic of the current codebase worth carrying into Phase 30's deployment
platform choice (favor multiple worker processes over a single multi-threaded one),
and — separately, for PERF-02(b) specifically — the sheer per-request cost of
serializing a ~2,000-entry `busy_blocks` array is itself a candidate optimization
target (see PERF-02 below).

**A second, purely local artifact, now understood and eliminated**: CONC-06's N=500
step, run against `waitress` on this Windows workstation, showed 232 of 500 requests
fail with `WinError 10061` ("connection actively refused") — not a 503, not a 500, a
TCP-level refusal before the request ever reached Django. Diagnosis: CPython's
`select()`-based socket multiplexing on Windows has a long-documented ~512-socket
ceiling (`FD_SETSIZE`), and `waitress`'s async accept loop uses exactly this
mechanism. This is a Windows-Python-specific artifact of the TEST CLIENT'S/local
SERVER's platform, unrelated to PostgreSQL, the GiST index, or this codebase — the
same throwaway Linux/gunicorn container above (a real POSIX `epoll`-based stack) shows
**zero** such refusals at N=500. All numbers reported below for every test use the
Linux/gunicorn setup, not the raw Windows `manage.py runserver`/`waitress` numbers.

**What was and wasn't installed.** `waitress` and `gunicorn` were both installed
directly into the local `.venv` / a throwaway Docker image respectively, purely as
LOAD-TESTING TOOLS to get a fair measurement free of the artifacts above — neither is
added to `backend/pyproject.toml`'s committed dependencies, and neither represents a
choice of Kairos's own production server (that choice belongs to Phase 30). `kairos/
settings/perf.py` (new, committed) is the one settings-module addition this phase
makes — see its own docstring for why `RATE_LIMIT_ENABLED=False` is the one
deliberate deviation from `dev.py`.

**Dataset**: `manage.py seed_perf_data` (new command, committed) seeded PRD A1 scale
into `kairos_dev` — 2,000 users, 301 resources (5 near-fully-booked "dense" resources
for PERF-02(b), 280 representative-density "moderate" resources for PERF-02(a), 15
resources kept empty for PERF-01's own guaranteed-free writes, 1 resource kept empty
for CONC-06), 17,859 confirmed bookings, 286 held, 681 cancelled — see that command's
own docstring for the exact density model.

**Tooling**: `scripts/perf/` (new, throwaway — the same "not application code, exists
to produce a report" status `scripts/spike/` already has). Real HTTP over the Python
standard library (`urllib`), real session tokens minted via the actual
`kairos.identity.oidc.issue_session_token`, real `threading.Barrier`-released true
simultaneity for every "concurrent" measurement (the identical discipline `tests/
concurrency/harness.py` already established for the raw-SQL proofs, applied here to
real HTTP instead).

## PERF-01 — Booking write P95 < 300ms, steady and spike

| | n | p50 | p95 | p99 | Target | Result |
|---|---|---|---|---|---|---|
| Steady (5 req/s, 60s, mixed resources) | 318/318 succeeded | 56.5ms | **78.4ms** | 122.6ms | < 300ms | **PASS** |
| Spike (200 concurrent, 2s window, on top of steady load) | 200/200 succeeded | 1783.4ms | **3209.8ms** | 3428.1ms | < 300ms | **FAIL** |

Every write in both profiles succeeded (201) — this is a LATENCY finding, not a
correctness one. The steady/nominal-load target (Test Plan's own explicit scope: "this
target applies to nominal load") is met with comfortable margin.

**Status, stated precisely — this target is NOT currently met, under either serving
setup tested, and is not being recorded as resolved:**

| Serving setup | Spike P95 | vs. 300ms target |
|---|---|---|
| `manage.py runserver` (single process) | ~7.4s | ~25x over |
| Throwaway Linux/`gunicorn` container (8 worker processes) | **3209.8ms** (the number in the table above) | **~10.7x over** |

Multi-process serving cut the spike P95 by more than half (7.4s → 3.2s) — real,
substantial evidence that CPython's GIL under single-process serving is a DOMINANT
contributing factor, not the only one. It is **not** evidence the target is met: 3.2s
is still an order of magnitude past 300ms, on hardware (8 shared vCPUs, no dedicated
database resources) well short of a real deployment. **This finding does not resolve
itself — it requires action:**

1. **PERF-01's spike case must be RE-RUN against Phase 30's actual chosen production
   deployment** (real multi-process WSGI/ASGI serving, dedicated database resources, no
   CPU-sharing with the app server) before this target can be honestly marked PASS or
   FAIL for release. Neither claim is defensible from this phase's data alone.
2. **Phase 30 must deploy with a real multi-process serving configuration
   (gunicorn/uvicorn with multiple workers, or equivalent) as the actual production
   setup — not left as an open choice.** This phase's own diagnostic (three eliminations:
   ruled out slow queries via a fast serial request, ruled out "the dev server
   specifically" via `waitress` showing the identical pattern, confirmed multi-process
   `gunicorn` cut spike P95 by >50%) is direct evidence that single-process serving is
   unacceptable for this codebase's write path under concurrent load, regardless of
   which specific server software Phase 30 ultimately picks.
3. If the real deployment's re-run still exceeds 300ms, that is the point to decide
   between raising the target, optimizing the write path further, or scaling
   horizontally — a decision for whoever owns that re-run, with real numbers in hand,
   not a decision this phase can make on synthetic, single-machine data.

## PERF-02 — Availability read P95 < 500ms

| | n | p50 | p95 | p99 | Target | Result |
|---|---|---|---|---|---|---|
| (a) Ordinary density, 30-day window | 300/300 succeeded | 223.4ms | **375.6ms** | 488.4ms | < 500ms | **PASS** |
| (b) Near-fully-booked, 92-day bound (~2,040 busy_blocks) | 300/300 succeeded | 1161.4ms | **1933.8ms** | 2441.0ms | < 500ms | **FAIL** |

(a) passes with real margin. (b) fails, and — unlike PERF-01's spike case — this one
has a genuine, identifiable, **non-environmental** contributing cause: a single SERIAL
request against the exact same near-fully-booked resource completes in 130–200ms
(confirmed directly, see the environment section), meaning the DATABASE query itself
is fast; the cost that dominates under concurrency is Python-side — building and
JSON-serializing a ~2,000-entry `busy_blocks` list, once per concurrent request. This
is a real cost that a bigger/better-deployed server reduces (proportionally, the same
way it helped PERF-01) but does not eliminate — unlike PERF-01, re-running this against
Phase 30's real deployment will not make this finding moot.

### Explicit decision: ACCEPT for go-live as a documented, scoped limitation —
### pagination on `busy_blocks` is the recommended fix, prioritized, not deferred
### indefinitely

Reasoning, at the same rigor as the RECLAIM-04 decision above, per this phase's own
instruction not to leave a real, non-environmental finding as a bare observation:

1. **The failure is narrowly scoped, not general.** It requires BOTH conditions at
   once: a resource booked densely enough to be "near-fully-booked" AND a query
   spanning the full 92-day bound. PERF-02(a) — ordinary density, a 30-day window, the
   shape of an everyday query — passes with real margin (P95 375.6ms vs. 500ms). This
   is a worst-case-query finding, not a general availability-read problem.
2. **No correctness or safety guarantee is at risk.** This is a read-only endpoint;
   the exclusion constraint, idempotency, and every write-path guarantee this project
   exists to provide are completely unaffected by how slowly a large `busy_blocks`
   array serializes. The cost of shipping with this known is degraded latency on an
   edge-case query, not a wrong answer or a lost booking.
3. **The concrete fix is identifiable but is a real API-contract change, not a
   same-day patch.** Pagination on `busy_blocks` — the recommended fix — changes
   `GET /resources/{id}/availability`'s response shape, which Spec v1.0 §5.7 does not
   currently describe as paginated; making that change correctly needs its own design
   pass (page size, cursor shape, and updating Spec itself), not something to improvise
   unilaterally inside a performance report. Implementing it was out of this phase's own
   Scope IN ("characterize performance... produce the report," not "redesign endpoints
   the report finds slow").
4. **This is still flagged as HIGH PRIORITY, not "someday."** RFC v1.0 §7.2 itself
   frames availability reads as this system's highest-traffic endpoint — a real,
   reproducible worst-case cost on the busiest read path deserves near-term engineering
   time, not the same low-urgency backlog treatment as a cosmetic issue.

**Concrete next step, named explicitly**: a near-term follow-up phase should implement
pagination on `busy_blocks` for `GET /resources/{id}/availability` (updating Spec v1.0
§5.7 alongside it) — recommended BEFORE Phase 30's go-live sign-off if any real
resource is expected to approach this booking density in early production use, and as
an immediate fast-follow immediately after Phase 30 otherwise. This is not being
carried forward as a vague "someone should look at this" — it has a named cause, a
named fix, and an explicit priority; what it does not yet have is an assigned phase
number, since inventing one wasn't this phase's call to make.

## PERF-03 — Waitlist dispatch P95 < 5s, including burst

| | n | p50 | p95 | p99 | Target | Result |
|---|---|---|---|---|---|---|
| (a) Isolated cancellation | 20/20 dispatched | 30.9ms | **33.9ms** | 45.3ms | < 5000ms | **PASS** |
| (b) Burst of 50 near-simultaneous cancellations | 50/50 dispatched | 129.3ms | **179.4ms** | 196.2ms | < 5000ms | **PASS** |

Both pass with enormous margin — three orders of magnitude under target. This is the
one measurement in this report NOT affected by the single-machine-CPU-sharing
findings above: dispatch happens in the Celery worker, a SEPARATE process from the web
server under test, so it never competed with PERF-01/02's own request-handling load.

**Methodology note** (documented here since it's a real interpretive choice, not
obvious from the target's own wording): "measured from cancellation commit to
notification enqueued" is measured as wall-clock time from the real `POST /bookings/
{id}/cancel` HTTP response to the corresponding `WaitlistOffer` row's `created_at`.
`NotificationLog` (the literal "notification" row) is written EXCLUSIVELY by the
Celery worker actually ATTEMPTING delivery, not at enqueue time (that table's own
docstring) — polling for it would measure delivery latency, not dispatch latency. The
`WaitlistOffer` row is created by `create_offer_for_freed_range`
(`kairos/waitlist/services.py`) in the SAME worker execution that then calls
`notify_offer_created()` — which enqueues the delivery task — making it the closest
externally-observable proxy for "enqueued" without instrumenting Celery's broker
directly.

## CONC-06 — Sustained hot-resource escalation

Not a pass/fail throughput gate (Test Plan's own framing — RFC v1.0 §18 deliberately
left the ceiling unresolved). One resource, writers targeting distinct, non-overlapping
30-minute slots — isolating index/write-path contention from overlap correctness,
which CONC-01–05 already cover.

| N writers | p50 | p95 | p99 | writes/sec | error rate | CONC-06a violations |
|---|---|---|---|---|---|---|
| 10 | 499.4ms | 539.1ms | 539.1ms | 18.5 | 0.000 | 0 |
| 25 | 240.3ms | 373.5ms | 378.5ms | 66.0 | 0.000 | 0 |
| 50 | 395.4ms | 666.3ms | 686.5ms | **72.8** | 0.000 | 0 |
| 100 | 893.1ms | 1759.9ms | 1846.6ms | 53.7 | 0.000 | 0 |
| 250 | 2342.3ms | 4150.9ms | 4354.2ms | 57.0 | 0.000 | 0 |
| 500 | 4263.0ms | 7639.2ms | 8073.0ms | 61.3 | 0.000 | 0 |

**CONC-06a (the actual hard gate — zero unexpected errors on non-conflicting writes):
PASS.** All 935 writes across all six steps returned 201. Zero 409s (correct — nothing
here overlaps), zero undocumented 5xx, zero connection-level failures, on the
Linux/gunicorn rig used for this final run (the Windows/waitress run's 232 refused
connections at N=500 were the platform artifact described above, not a CONC-06a
finding — re-run and superseded, not silently dropped).

**Named inflection point: N≈50 writers.** Successful-writes/sec climbs sharply from
N=10 to N=50 (18.5 → 66.0 → 72.8/sec — the system is still absorbing added
concurrency), then PLATEAUS from N=100 onward (53.7 → 57.0 → 61.3/sec, essentially flat
within measurement noise) while P95 latency keeps climbing steeply (666ms → 1.76s →
4.15s → 7.64s). Past N≈50 on this one hot resource, additional concurrent writers stop
buying additional throughput and only add queueing delay — the textbook shape of a
saturated resource. This ceiling reflects this SPECIFIC 8-vCPU, everything-on-one-
machine rig (see the environment section) — a real production deployment (dedicated
database resources, no CPU contention with the app server) should be expected to
sustain a meaningfully higher plateau, and this number should be revisited once real
resources exist to compare against (the identical "synthetic today, validated at real
scale later" framing Rollout v1.0 §9 already applies to this exact row).

## RECLAIM-04 — re-run, per this phase's explicit instruction

Phase 17's RECLAIM-04 finding (269 real SQLSTATE 40P01 deadlocks across 50 runs at
N=200, cleanup-on-write's DELETE in the hot path against a genuinely contested range)
was re-run in full at the identical scale (200 writers × 50 runs, 4 pre-seeded expired
holds per run, `pytest tests/concurrency/test_reclaim_04.py -v -s`) to get CURRENT
numbers, since several phases (idempotency, audit, holds, offers, reclamation itself)
have touched the booking write path since Phase 17 measured this.

**Result: 221 real deadlocks (SQLSTATE 40P01) across 22 of the 50 runs (44%)** — down
from Phase 17's 269 across roughly half the runs, a modest (~18%) decrease that reads
as ordinary run-to-run variance rather than a systematic improvement (nothing in the
four intervening phases specifically targeted deadlock reduction). **Safety held
perfectly, exactly as it did in Phase 17**: every one of the 10,000 raw attempts (200
writers × 50 runs) produced exactly one success per round — confirmed by the test's
own unconditional per-attempt assertion, never softened or retried around — zero
rounds needed the zero-success retry budget, and zero SQLSTATEs fell outside the
already-documented, already-retryable set (`23P01`/`55P03`/`40P01`/`57014`). The test
itself passed (`PASSED`, exit 0) — this row is reporting a real, current LIVENESS
characteristic, not a failing test.

### Explicit decision: ACCEPT the current deadlock rate as a known, documented,
### retryable liveness cost — no locking-strategy change before Phase 30

Reasoning, per this phase's own instruction to state it either way rather than defer a
third time:

1. **Zero safety violations, twice now, across 20,000 combined raw attempts** (Phase
   17's original run plus this re-run). This is a liveness characteristic of extreme
   contention, not a correctness gap — the exact distinction RFC v1.0 already draws for
   40P01 everywhere else in this codebase (Spike S1.2's own finding, cited in CLAUDE.md,
   is that a GiST exclusion constraint has no fixed lock ordering the way a plain btree
   unique index does, making some deadlock rate under N=200 identical-slot contention
   an expected property of the mechanism itself, not a bug introduced by cleanup-on-
   write specifically).
2. **Every deadlock already resolves correctly, not silently.** `_handle_write_
   database_error` (`kairos/bookings/services.py`) has mapped 40P01 to a proper 503
   `service_unavailable` + `Retry-After` since Phase 4 — a losing writer here gets the
   same well-defined, documented, retry-safe outcome any other lock-contention loser
   gets, never a bare 500 or an ambiguous result.
3. **The scenario is a genuine, intentional worst case, not ordinary traffic.** 200
   SIMULTANEOUS writers targeting the exact same identical time range is more extreme
   than PRD A1's own "spiky, concentrated load on a single hot resource" scale
   assumption describes — Test Plan v1.0 §13 already tiers RECLAIM-04 as staging/pre-
   release, deliberately excluded from the CI gate every PR runs, precisely because it
   characterizes an edge case, not a routine release-blocking correctness property.
4. **A narrower locking strategy is a real architecture change with its own risk, for
   a scenario already tiered as non-blocking.** The candidate fix (e.g., serializing
   writers with an app-level advisory lock keyed by `resource_id` before the write)
   would need its own correctness proof and could introduce a NEW bottleneck for
   legitimate concurrent traffic across DIFFERENT resources — a cost with a real
   design/testing budget of its own, for a benefit that only matters in a scenario this
   project's own Test Plan already excludes from the release gate.
5. **The rate did not get worse across four phases of added write-path complexity**
   (idempotency, audit, holds, offers, reclamation) — it held roughly steady (269 →
   221), evidence this is a stable, bounded characteristic of the exclusion-constraint
   design under extreme contention, not a growing regression accumulating unnoticed.

**This is not a permanent close-the-book decision** — it's the explicit, reasoned call
this phase's own instructions required, made with real data, twice-confirmed. If real
production traffic (post Phase 30) shows this exact scenario — hundreds of genuinely
simultaneous identical-slot writers — occurring with meaningful frequency and user
impact, that would be new evidence warranting revisiting this decision. Recorded here,
alongside CONC-06's throughput ceiling, as exactly the kind of named, permanent system
characteristic Rollout v1.0 §9 exists to track, not hide.

## GiST write-throughput alert — threshold set from this data

Rollout v1.0 §6's "GiST write throughput on booking" row was deliberately left with no
real alert in Phase 21 ("Set from CONC-06's real characterization data — deliberately
not invented here"). This phase gives it one: `kairos.core.alerting.evaluate_alerts`
now evaluates an eighth signal, `gist_write_throughput` (SEV-3, informational — not
page-worthy on its own, `schema_assertion`/`reconciliation` remain the SEV-1s), reading
the SAME live `p95_duration_ms(metric_type=BOOKING_WRITE)` the admin dashboard already
computes (Phase 21), against a new constant, `BOOKING_WRITE_P95_ALERT_THRESHOLD_MS`
(`kairos/core/constants.py`), set to **1500ms**.

**Why 1500ms, not a round guess**: the CONC-06 table above shows write P95 at 666ms at
N=50 (still scaling — a healthy, absorbing system) and 1760ms at N=100 (throughput
already plateaued — a saturated one). 1500ms sits inside that transition, closer to
the plateau side: a resource whose write P95 has crossed it is very likely already
past its own healthy-scaling point, which is exactly Rollout's own definition of
"approaching a known ceiling... not a false alarm by definition." A new `AlertKey`
value (migration `0016_gist_write_throughput_alert_key.py`, widening `AlertEvent`'s
own CHECK constraint — the same `RemoveConstraint`/`AlterField`/`AddConstraint` shape
already used for `NotificationType.SERIES_OWNER_DEACTIVATED`) and two new tests in
`tests/test_alerting.py` (fires over threshold, does not false-alarm under it) back
this.

**This threshold is explicitly PROVISIONAL**, derived from one synthetic, single-
machine escalation run, not real production traffic. Rollout v1.0 §9's own table
already anticipates this: "Sets the threshold left unset here, and validates or
revises CONC-06's synthetic assumptions" — listed as "Stage 2 onward" work, i.e. AFTER
real resources exist to compare against, not before. Recorded here as the deliberate,
reasoned, data-derived starting point Rollout §6/§9 asked for — not as a number that
should be treated as final without that later validation.
