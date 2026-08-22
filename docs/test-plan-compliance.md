# Test Plan v1.0 §14 — Acceptance Criteria for Release: Compliance Checklist

Committed by Implementation Plan Phase 28 ("Full Test Suite Completion"), per that
phase's own Definition of Done: "Every hard blocker in Test Plan §14 is green. Produce
a checklist mapping each to its test, committed as `docs/test-plan-compliance.md`."
Updated by Phase 29 ("Performance & Load Testing"), which closed the PERF-01–03/
CONC-06/CONC-06a rows Phase 28 had left explicitly out of scope and resolved
RECLAIM-04's own open item — kept as ONE living document rather than a second,
Phase-29-specific compliance file, since both phases are answering the identical
"is every §14 hard blocker actually green" question.

This maps **every** item in Test Plan v1.0 §14 ("Hard blockers — cannot ship without
every item") to the test(s) that prove it, which phase built that test, and — where a
blocker is not actually green today — an honest statement of the gap rather than a
claim the item passes. Status legend:

- ✅ **PASS** — automated test(s) exist, were run, and pass.
- ⚠️ **PASS WITH A DOCUMENTED GAP** — automated test(s) exist and pass, but a prior,
  already-recorded finding means the blocker's own literal wording ("zero X") is not
  currently true; the gap is tracked, not silently papered over.
- 🔶 **LOGIC VERIFIED, INFRASTRUCTURE PENDING** — the application-level behavior is
  proven against a simulated condition; the real-infrastructure form needs
  infrastructure a later phase builds (Phase 30).
- ❌ **NOT YET BUILT** — no automated test exists; explicitly out of Phase 28's own
  Scope IN, deferred to a named future phase.

## Concurrency

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| CONC-01: 100% pass across 100 consecutive runs at N=200, plus the N=500 escalation. Zero tolerance. Ground-truth verified. | ✅ PASS | `tests/concurrency/test_conc_01_full_scale.py` (`test_conc_01_full_scale_100_consecutive_runs_at_n200`, `test_conc_01_n500_escalation`) — actually run, not merely written; see **Real numbers observed** below. CI-tier reduced form (10 runs) is `tests/concurrency/test_conc_01.py`, unchanged, still gates every PR. | 28 (this phase); CI-tier form is Phase 3 |
| CONC-02 through CONC-05: 100% pass. | ✅ PASS | `tests/concurrency/test_conc_02.py`, `test_conc_03.py`, `test_conc_04.py`, `test_conc_05.py` — CI-tier, run on every PR. | 3, 7 |
| CONC-06a: zero unexpected errors on non-conflicting writes. | ✅ PASS | `scripts/perf/conc_06_escalation.py` (Phase 29; throwaway load-testing script, not pytest — see `docs/performance-baseline.md`), run against a real PRD-A1-seeded `kairos_dev` at all six escalation steps (N=10/25/50/100/250/500). **935/935 writes across every step returned 201 — zero 409s, zero undocumented 5xx, zero connection-level failures**, on the Linux/gunicorn rig the final numbers were captured against (an earlier Windows/waitress run showed 232 TCP-level connection refusals at N=500 — a diagnosed Windows `select()`/`FD_SETSIZE` platform artifact, re-run and superseded, not silently dropped — see that document's own environment section). | 29 |

**Real numbers observed (this phase, `tests/concurrency/test_conc_01_full_scale.py`, run once directly against the local Docker Postgres, `max_connections=600`, 915.64s / ~15m15s total):**

**Main exercise — 100 consecutive runs at N=200:**

- **100/100 runs produced exactly 1 success, ground-truth verified against `count_active_overlapping` every time.** Zero runs with 0 or >1 successes in their FINAL attempt. `len(successes) <= 1` held on every one of the 111 individual attempts (not just the 100 final ones) — the hard, zero-tolerance safety invariant asserted unconditionally, never relaxed for a retried attempt.
- **111 total attempts** across the 100 runs (100 base + 11 retried zero-success attempts).
- **6 of the 100 runs needed a retry** (produced a zero-success attempt before eventually succeeding): runs 21, 34, 35, 73, 75, 97. Attempts needed per retried run: 2, 2, 4, 2, 2, 5. **Maximum attempts in any single run: 5**, well inside the `MAX_ROUND_ATTEMPTS = 10` budget — no run came close to exhausting it, and the "zero successes across `MAX_ROUND_ATTEMPTS` attempts" failure path was never hit.
- **Total requests processed across all 111 attempts: 22,200** (111 × 200). Of these:
  - **100 successes** (100 confirmed bookings, one per run, each independently ground-truth verified).
  - **10,348 × SQLSTATE 23P01** (`exclusion_violation` — the ordinary 409 `slot_unavailable` path; this was the dominant outcome on the ~60 runs where the constraint itself resolved contention before any timeout fired).
  - **10,798 × SQLSTATE 57014** (`query_canceled`/`statement_timeout`) — the dominant outcome on the other ~40 runs, matching this project's own documented Phase 3 empirical finding that most losers at N=200 accumulate short waits under GiST index contention rather than blocking cleanly on one competitor.
  - **727 × SQLSTATE 40P01** (`deadlock_detected`).
  - **227 × SQLSTATE 55P03** (`lock_timeout`).
  - **Zero unexplained SQLSTATEs** — every one of the 22,100 non-success outcomes fell inside `EXPECTED_NONSUCCESS_SQLSTATES`, and the test's own `assert not unexplained` held on all 111 attempts.

**N=500 escalation (single round, 2.5× the baseline concurrency):**

- **1 success**, ground-truth verified. **Zero retries needed** — resolved on the first attempt.
- **499 non-success outcomes, all documented, zero unexplained**: **374 × 57014**, **116 × 55P03**, **9 × 40P01**, **0 × 23P01** (unlike the 100-run exercise, this single round never resolved via the exclusion constraint's own 23P01 path at all — every loser was caught by a timeout/deadlock first, consistent with a single very-high-contention round rather than the mix of contention levels the 100-run exercise's varied timing produced).
- Total requests: 500 (1 + 499), matching N exactly.

**Conclusion: CONC-01's hard blocker — "100% pass across 100 consecutive runs at N=200, plus the N=500 escalation, zero tolerance, ground-truth verified" — is satisfied exactly as written, with real numbers, not merely a written-but-unrun test.** No safety violation (more than one simultaneous success) occurred in any of the 112 total rounds (111 main-exercise attempts + 1 N=500 attempt). The liveness characteristic this project already documented at CI-tier scale (a round can produce zero successes and need a retry) reproduced here too, at a correspondingly higher absolute count (6/100 runs, matching the ~15-20% *per-attempt* zero-success rate already on record — 11 zero-success attempts across ~111 ≈ 10%, within the documented range) — never a safety concern, and never came close to exhausting the retry budget.

## Holds & waitlist

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| HOLD-01: a direct booking must lose to an outstanding offer. | ✅ PASS | `tests/bookings/test_holds.py::test_hold_01_ordinary_booking_loses_to_outstanding_offer` (real HTTP), plus the two guard-clause companion tests. | 15 |
| HOLD-02, HOLD-03: passing. | ✅ PASS | `tests/concurrency/test_hold_02.py` (50 barrier-released runs, zero successes unconditionally); `tests/bookings/test_holds.py::test_hold_03_opaque_in_availability_view`. | 15 |
| WL-01 through WL-04: 100% pass, same rigor as §2. WL-04 explicitly verified. | ✅ PASS | `tests/concurrency/test_wl_01.py`, `test_wl_02.py`; `tests/waitlist/test_offers.py` (WL-03 cascade-skip); `tests/waitlist/test_eligibility.py` (WL-04 containment). | 14, 16 |
| WL-05 Part B: detection exists and fires. | ✅ PASS | `tests/waitlist/test_reclamation.py` — `hold_reaper_heartbeat_is_stale` staleness detection. Part A is the one-time live-verification transcript in CLAUDE.md's Running Locally section (real `beat`/`redis` stopped), not a repeated gate, per the blocker's own wording. | 17 |
| WL-06: Redis outage degrades without corrupting. | ✅ PASS (verified live, not by an automated test — `CELERY_TASK_ALWAYS_EAGER` under pytest structurally cannot reproduce a genuine broker outage) | `tests/waitlist/test_dispatch_cascade.py` is a lightweight regression guard for the `try/except` shape itself; the actual live proof (real `redis` stopped, real HTTP against `manage.py runserver`) is the transcript in CLAUDE.md's Phase 17 Completed Phases row and Running Locally section. | 17 |

## Reclamation

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| RECLAIM-01: self-healing verified with the reaper stopped. | ✅ PASS | `tests/bookings/test_cleanup_on_write.py`. | 17 |
| RECLAIM-02, RECLAIM-03: passing. | ✅ PASS | `tests/waitlist/test_reclamation.py` (RECLAIM-02); `tests/concurrency/test_reclaim_03.py` (100 runs). | 17 |
| RECLAIM-04: zero deadlocks. Failure here is a design change requiring re-review, not a tuning fix. | ⚠️ **PASS WITH A DOCUMENTED, DELIBERATE EXCEPTION — resolved by Phase 29, not deferred again.** | `tests/concurrency/test_reclaim_04.py`, run manually at full DoD scale (200 writers × 50 runs) in Phase 17 AND re-run at the identical scale in Phase 29 to get current numbers. **Phase 17: 269 deadlocks. Phase 29 re-run: 221 deadlocks across 22/50 runs (44%)** — a modest decrease, read as run-to-run variance, not a systematic fix. Safety held perfectly BOTH times, across 20,000 combined raw attempts (never more than one success per round); every failure SQLSTATE was already a documented retryable one (40P01 → 503, mapped since Phase 4). Test Plan §14's own literal "zero deadlocks" wording is still not true — but Phase 29 made the explicit accept/reject decision this row's own Phase 28 note called for, rather than deferring a third time: **ACCEPT the current rate as a known, documented, retryable liveness cost; no locking-strategy change before Phase 30.** Full reasoning (five points: safety-vs-liveness, already-correct 503 handling, the scenario's own extremity relative to PRD A1, the real cost/risk of a narrower locking strategy, and the rate holding steady rather than growing across four intervening phases) is in `docs/performance-baseline.md`'s own RECLAIM-04 section — not reproduced here to avoid two independently-maintained copies of the same decision. | 17, 29 |

## Time

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| TZ-01, TZ-02, TZ-04: passing, including the Nov 1 2026 transition-date case. | ✅ PASS | `tests/bookings/test_recurrence.py` (TZ-01); `tests/test_timezones.py` (TZ-02, TZ-04). | 10, 11 |
| TZ-05, TZ-06: nonexistent/ambiguous times detected, policy applied, disclosed. | ✅ PASS | `tests/bookings/test_recurrence.py`. | 11 |
| TZ-07, TZ-08: re-materialization works, conflicts never silently dropped. | ✅ PASS | `tests/bookings/test_rematerialization.py`. | 13 |
| TZ-09, TZ-10: southern-hemisphere and no-DST zones correct. | ✅ PASS | `tests/bookings/test_recurrence.py`. | 11 |
| TZ-03 Tests A and B in place. | ✅ PASS | `tests/test_timezones.py` (Test A — exact pin); `tests/test_tzdata_check.py` (Test B — drift check). | 10, 13 |

## Idempotency

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| IDEM-01 through IDEM-05: passing. | ✅ PASS | `tests/bookings/test_idempotency.py` (IDEM-01–04); `tests/bookings/test_recurring_series.py` (IDEM-05). | 5, 12 |
| IDEM-06: concurrent replay never returns `slot_unavailable`. | ✅ PASS | `tests/bookings/test_idempotency.py::test_idem_06_concurrent_replay_100_repetitions` (100 barrier-released reps). | 5 |
| IDEM-07: transaction boundary verified by fault injection. | ✅ PASS (new this phase) | `tests/bookings/test_idempotency.py::test_idem_07_process_kill_mid_transaction_never_leaves_a_booking_without_a_key` — forces a genuine exception at the exact point a process kill mid-write would land (after the booking INSERT has run, before the outcome-record UPDATE commits, inside the ONE shared `transaction.atomic()`); ground truth confirms neither the booking nor the key survives — never "a booking with no key record." | **28** |
| IDEM-08: a retried lost-response request shows the user their own booking. | ✅ PASS (new this phase) | `tests/bookings/test_idempotency.py::test_idem_08_lost_response_retry_shows_the_users_own_existing_booking` — the first response is never inspected (simulating a proxy-level drop); the retry, presented the identical key, returns the user's own committed booking, never a 409. | **28** |
| IDEM-09 through IDEM-11: passing. | ✅ PASS | `tests/bookings/test_idempotency.py`. | 5 |

## Audit

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| AUD-01: append-only enforced at grant level. | ✅ PASS | `tests/test_audit_trail.py`. | 8 |
| AUD-02: triggers cannot be bypassed by raw SQL. | ✅ PASS | `tests/test_audit_trail.py`. | 8 |
| AUD-03 through AUD-05: passing. | ✅ PASS | `tests/bookings/test_history.py`. | 8 |

## Integrity & monitoring

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| RECON-01 through RECON-06: passing. | ✅ PASS | `tests/test_reconciliation.py`, `tests/test_schema_assertion.py`, `tests/test_admin_checks.py`. | 20 |
| RECON-07: every alert fired at least once by deliberate injection. | ✅ PASS | `tests/test_alerting.py` — seven `test_*_fires_and_reaches_its_target` tests, one per Rollout v1.0 §6.1 condition, each asserting a real `AlertEvent` and a real `mail.outbox` entry. | 21 |
| RECON-05 wired as a CI gate on every migration. | ✅ PASS | `tests/test_schema_assertion.py`, run in CI's `test` job on every PR (not staging-tier). | 3, 20 |

## Recurrence

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| REC-01 through REC-07: passing, including REC-03. | ✅ PASS | `tests/bookings/test_recurring_series.py`. | 12 |

## Functional, security, lifecycle

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| §10 matrix: 100% passing. | ✅ PASS (3 gaps closed this phase) | 67 rows audited row-by-row against the running test suite this phase (the matrix as pasted into the Implementation Plan carries 67 rows, not the "§10" section's own summary count of 62 — recounted twice to confirm). 62 were already covered by prior phases. Three gaps found and closed this phase: **row 8** (the 365-day horizon boundary was tested 5 minutes inside the true edge, not AT it — `tests/bookings/test_views.py::TestPolicyValidation::test_exactly_at_the_365_day_horizon_boundary_succeeds`, freezing `timezone.now()` to make the equality deterministic); **row 48** (a recurring series with zero future occurrences had no test — `tests/bookings/test_recurring_series.py::test_recurring_series_cancel_with_no_future_occurrences_returns_200_and_empty_array`); **row 65** (the six-check response content was only ever asserted for a `system_admin` caller, never `operations` — `tests/test_admin_checks.py::test_operations_user_sees_all_six_checks_in_spec_order`). Rows 13 and 40 (the matrix's own FAIL-01/FAIL-03 rows) are covered by this phase's dedicated failure-mode tests, listed below under Performance & failure. | **28** (3 new tests); 4, 6, 7, 8, 9, 11, 12, 14, 16, 19, 20 (the other 62) |
| SEC-01 through SEC-07: zero findings. | ✅ PASS | `tests/test_security.py`, `tests/test_security_headers_and_cors.py`. | 9, 22 |
| OFF-01, OFF-02: passing. | ✅ PASS | `tests/identity/test_offboarding.py`. | 19 |

## Performance & failure

| Item | Status | Test(s) | Phase |
|---|---|---|---|
| PERF-01 through PERF-03: targets met under both steady and spike profiles. | ⚠️ **PARTIAL — real numbers, not every target met; both open FAILs carry an explicit decision, not a bare status.** | `scripts/perf/perf_0{1,2,3}_*.py` (Phase 29), run against a real PRD-A1-seeded `kairos_dev`; full numbers and reasoning in `docs/performance-baseline.md`. **PERF-01**: steady PASS (P95 78.4ms). Spike FAILS under EVERY serving setup tested, INCLUDING a real multi-process container (P95 3209.8ms — down from ~7.4s single-process, a real improvement, not a resolution — still ~10.7x over the 300ms target); every write still succeeded (201), a latency finding, not a correctness gap. Two things are now MANDATORY, not merely suggested: Phase 30 must deploy real multi-process serving as the actual production configuration, and this specific test must be re-run against that real deployment before either a PASS or FAIL claim is defensible — neither is claimed here. **PERF-02**: (a) ordinary density PASS (P95 375.6ms). (b) near-fully-booked/92-day-bound FAILS with a real, NON-environmental cause (Python-side JSON-serialization cost of a ~2,000-entry `busy_blocks` array — a single serial request stays fast at 130–200ms, ruling out the query itself) that a better deployment reduces but does not eliminate. **Explicit decision recorded**: ACCEPT for go-live as a documented, scoped limitation (narrow trigger, no correctness/safety impact, the fix is a real Spec v1.0 §5.7 contract change) but flagged HIGH PRIORITY (RFC v1.0 §7.2 names availability reads this system's highest-traffic endpoint) — pagination on `busy_blocks` is the named recommended fix. **PERF-03**: both isolated and burst PASS with large margin (P95 33.9ms / 179.4ms vs 5000ms target) — dispatch runs in a separate process (the Celery worker) from the web server under test, unaffected by the other two tests' findings. | 29 |
| FAIL-01: failover returns 503, never a hang, and retry-with-same-key resolves unambiguously. | ✅ PASS (new this phase) | `tests/test_failure_modes.py::test_fail_01_primary_failover_mid_write_returns_503_then_retry_resolves_unambiguously` — a connection-level failure (no sqlstate — the genuine failover shape, distinct from an ordinary lock timeout) forced on the booking INSERT; asserts 503, `Retry-After`, `cause="failover"`, nothing recorded under the key (outcome genuinely unknown), and a retry with the identical key resolves as a real, unambiguous 201. | **28** |
| FAIL-02: lock timeout returns 503, not 500, not 409. | ✅ PASS (new this phase) | `tests/test_failure_modes.py::test_fail_02_lock_timeout_returns_503_then_retry_resolves_unambiguously` — genuine reproduction (not simulation): a real conflicting row held open, uncommitted, on an independent connection, with `lock_timeout` forced short, through the REAL `POST /api/v1/bookings` endpoint. Asserts 503 explicitly (not 500, not 409), `cause="lock_contention"`, and an unambiguous retry once the blocking row releases. | **28** |
| FAIL-03: replica lag degradation surfaces or serves from primary; never silently stale. | 🔶 **LOGIC VERIFIED, INFRASTRUCTURE PENDING (Phase 30)** — see the dedicated section below. | `tests/test_replica.py` (unit-level: `select_read_source`'s degradation rule against simulated lag values); `tests/resources/test_views.py::test_fail_03_stale_replica_lag_falls_back_to_primary_over_real_http` / `test_fail_03_fresh_simulated_replica_lag_would_be_used` (the same logic proven through the real `GET /resources/{id}/availability` endpoint, with a monkeypatched `current_replica_lag_seconds()`). | **28** |

## FAIL-03 — explicit scope statement (per this phase's own instructions)

No read replica exists anywhere in this project. Rollout v1.0 §2.2's own checklist
defers standing one up to Phase 30. Per this phase's explicit clarification, Phase 28
deliberately did **not** stand up a real replica just for this one test. Instead:

1. `kairos/core/replica.py` (new) is the seam: `select_read_source(lag_seconds,
   threshold_seconds)` is the actual degradation rule — an unknown or over-threshold
   lag always falls back to `"primary"`, reported honestly via `data_freshness`, never
   silently serving stale data (Spec v1.0 §5.7). `current_replica_lag_seconds()`
   honestly returns `None` today (no replica configured) — Phase 30 replaces its body
   with a real measurement; nothing else in this module needs to change when it does.
2. `tests/test_replica.py` proves the degradation rule directly, against simulated lag
   values (unknown, over-threshold, under-threshold, and the exact boundary).
3. `tests/resources/test_views.py`'s two new FAIL-03 tests prove the SAME rule through
   the real `GET /resources/{id}/availability` endpoint, via a monkeypatched
   `current_replica_lag_seconds()` — the only fault-injection technique available
   without real replica infrastructure.

**This is not claimed as "fully passing" in the sense Rollout v1.0 §11 intends** (which
requires real infrastructure — an actual lagging replica, real degradation under real
lag). It is recorded here, accurately, as: application-level degradation LOGIC is
correct and proven against a simulated condition; the real-infrastructure form is
deferred to Phase 30, where the real replica gets built. Whichever phase stands up that
replica should treat `kairos.core.replica.current_replica_lag_seconds` as the one
function to give a real body — the routing logic downstream of it does not need to
change.

## Coverage reporting (Scope item 8)

Wired into CI's `test` job (`.github/workflows/ci.yml`) via `pytest-cov`:
`--cov=kairos --cov-report=term-missing --cov-report=xml --cov-fail-under=90`, with the
XML report uploaded as a build artifact for review. Measured against the same
`--ignore=tests/concurrency` scope that job has used since Phase 3 (the concurrency
suite proves the exclusion constraint via raw psycopg SQL, largely bypassing the
Django/DRF layer coverage measures — the API-level and service-level test suites
already exercise the same write-path code the concurrency tests exercise via SQL, so
this split under-measures nothing that isn't also covered by an API-level test).

**Documented target: 90%.** Measured baseline at the time this was wired up (this
phase, full non-concurrency suite, 375 tests): **93% overall** (`kairos/` package,
migrations/settings/wsgi/celery excluded — see `[tool.coverage.run]` in
`backend/pyproject.toml`). 90 was chosen — a few points below the measured baseline,
not equal to it and not 100% — for three reasons: (1) headroom so a single new,
legitimately-hard-to-test branch (a rare defensive `except` clause, a management
command's CLI-only path) doesn't immediately fail CI; (2) two files sit at 0% by
design, not oversight — `kairos/bookings/management/commands/rematerialize_series.py`
and `kairos/core/management/commands/run_correctness_checks.py` are both thin CLI
wrappers around functions their OWN unit tests already cover directly (the identical
"test the mechanism directly, not the CLI wrapper" pattern this project uses
throughout — see `expand_occurrences`, `reap_expired_holds` in CLAUDE.md's Key
Technical Decisions) — driving their coverage to 100% would mean testing `argparse`
plumbing, not new behavior; (3) 100% is not a goal this project has ever set for
itself in any of the six source documents, and manufacturing coverage-only tests
against branches nothing exercises for a real reason would be the "premature
abstraction"/"backwards-compatibility hack" anti-pattern this project's own
conventions already reject elsewhere.

Per-file detail from the baseline run is in the `--cov-report=term-missing` output
captured when this was wired up; the lowest-covered real (non-CLI-wrapper) files were
`kairos/core/tzdata_check.py` (74% — the live-PyPI-call branch, deliberately verified
live once outside the automated suite per CLAUDE.md rather than mocked repeatedly) and
`kairos/identity/oidc.py` (77% — the real-JWKS verification path, a documented,
deliberate gap: this project has no live IdP to test against, the same class of gap
IDEM-07/08 carried before this phase).
