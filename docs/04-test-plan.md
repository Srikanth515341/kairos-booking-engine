# Test Plan / Acceptance Criteria
## Concurrency-Safe Resource Booking Engine

| | |
|---|---|
| **Document version** | 1.0 |
| **Status** | Draft for Review |
| **Supersedes** | v0.1 (written against RFC v0.1 / Spec v0.1) |
| **Verifies** | PRD v1.0, RFC v1.0, API & Data Design Spec v1.0 |

---

## 0. Revision History

**v1.0 — changes from v0.1.** v0.1 verified the waitlist mechanism RFC v1.0 replaced. Every test that asserted against `waitlist_offer`'s exclusion constraint has been rewritten against holds.

| # | Change | Reason |
|---|---|---|
| 1 | Waitlist suite rewritten around holds (§3); HOLD-01 added as the headline test. | v0.1's WL-01 asserted against `one_active_offer_per_slot`, a constraint Spec v1.0 deleted. No test existed for the failure that motivated the redesign: an ordinary booking taking a slot with an outstanding offer. |
| 2 | Hold reclamation suite added (§4): RECLAIM-01 through 04. | RFC §10.4 specifies two reclamation mechanisms and argues neither alone suffices. v0.1 tested neither. RECLAIM-04 is Spike S1.7. |
| 3 | IDEM-06 through IDEM-09 added (§7). | v0.1 covered the easy half. Concurrent replay (PRD FR36), the transaction boundary (RFC §11.2), and the actual user-facing requirement (PRD FR38) were all untested. |
| 4 | TZ-05 through TZ-09 added (§5); TZ-03's blast-radius claim corrected. | PRD FR11 (nonexistent times), FR12 (ambiguous times), FR13 (tzdata re-materialization) had no coverage. Southern-hemisphere transition added — a sign error passes every northern test. |
| 5 | Audit suite added (§8). | PRD FR39–43 and RFC §12 had zero coverage. |
| 6 | Recurring tests rewritten for preview → acknowledge → confirm (§6). | Spec v1.0 splits the flow per PRD FR33. v0.1 tested the single-step design. |
| 7 | Failover pulled back into scope (§11). | v0.1 deferred it to Rollout. PRD M14 is a stated behavioral requirement with a defined error code in Spec §5.1 — testable now via fault injection. |
| 8 | Numbers aligned to v1.0: 200 concurrent (was 50), 100 occurrences (was 53), 92-day window (was 31), full 16-code error surface. Offboarding, eligibility-rule, held-slot opacity, and schema-assertion-alerting tests added. | v0.1 verified stale figures. |

## 1. Testing Philosophy & Scope

This document verifies three qualitatively different kinds of confidence. Conflating them is itself a testing failure mode.

- **Unit-level correctness** — a validator rejects a malformed range; a DST conversion returns the right offset. Necessary, but structurally incapable of proving this system's central claim, because that claim is entirely about behavior under concurrent access, which a test exercising one execution path at a time cannot exercise by construction.
- **System-level correctness under real concurrency** (§2–§4) — the only category that can verify the PRD's primary goal. Qualitatively different from ordinary feature testing: for most features, one well-chosen passing example generalizes. For a race condition it does not — a bug can be present and a test still pass, because the failure manifests only when the race window is actually hit, and whether a run hits it depends on timing this document must deliberately engineer rather than hope for.
- **Production-observable correctness** (§9) — the ongoing backstop for whatever the first two categories didn't anticipate. Not "proven before shipping" but "detectable if it happens anyway."

**Why concurrency carries disproportionate weight.** PRD P1 is to *eliminate* double-booking, not reduce it. For a typical feature, "works in the tests we wrote, with known edge cases documented" is an acceptable release posture. Here it is not: a single reproducible failure in the concurrency suite is not a flaky test to retry or a limitation to document — it is direct evidence that the system's central promise is false. §2–§4 are held to a zero-tolerance standard no other section is, and that asymmetry is intentional.

A second class of requirement this document must verify, easily overlooked. PRD §2.4 identifies three failure modes that are *correctness-neutral and trust-destroying*: a retry misinforming a user about their own booking, an offer that can be beaten, and a series that drifts across DST. A test plan that verifies only the exclusion constraint would pass a system that fails all three. §3, §5, and §7 exist for this class specifically.

## 2. Concurrency Correctness Test Suite

### 2.0 Harness requirements — stated once, binding on every test in §2–§4

**Achieving true simultaneity.** A test firing N requests in a tight loop, or via a single process's `asyncio.gather()` on one connection pool, does not prove simultaneity. A shared event loop, a shared pool, or ordinary OS scheduling can serialize what looks like concurrent code — producing a false-confidence pass against a genuinely buggy implementation, because the race window was never entered. Every test below must use one of:

- (a) N independent OS threads or processes, each holding its own database connection, all blocked on a shared `Barrier(N)`, released together. Default for CI-tier tests.
- (b) N independent load-generator workers (k6, Locust, Gatling) with a spike arrival pattern and pre-warmed connections, for higher N where a single test process becomes the bottleneck.

Any test claiming to exercise this system's core guarantee that does not state which mechanism it used is not a finished test.

**Why repeated runs.** A correctly-created EXCLUDE constraint enforces deterministically; Postgres does not probabilistically check constraints. Repetition is therefore not hedging against the constraint. It exists for two real reasons: (a) validating the harness reliably achieves simultaneity run after run — a harness that only sometimes creates genuine contention produces false-confidence passes on the runs it doesn't; and (b) catching flakiness in the surrounding application layer (connection handling, timeout behavior under load) that could be mistaken for, or mask, a correctness bug.

**Ground truth is mandatory.** Every assertion is checked two ways: the HTTP response codes each caller received, and a direct `SELECT` for actual persisted state. These must agree. Response codes alone would miss a real bug class — a rollback/exception-handling bug that returns 409 to a caller while a row did in fact commit.

**Blocking is expected, not a failure.** Per RFC §7.1, a conflicting insert against an *uncommitted* competitor waits rather than failing instantly. Under 200-way contention, losing requests queue. Tests must therefore allow for elevated latency on losing requests and must not treat a 503 from `lock_timeout` as a correctness failure — it is a documented outcome (Spec §5.1). It is counted separately (§2.1).

### CONC-01 — Identical-slot contention (PRD M1)

**Setup.** One resource, one target range (13:00–14:00 on a fixed date), no existing bookings.

**Execution.** 200 threads, each with its own connection and a fully-prepared `POST /bookings` for the identical range, barrier-released. Repeat the full run 100 consecutive times.

*Note: PRD M1 requires ≥200 concurrent and ≥50 consecutive runs. This test deliberately exceeds the consecutive-run floor.*

**Assertions, every run:**

- Exactly 1 caller receives 201.
- All others receive 409 `slot_unavailable` or 503 `service_unavailable` (lock timeout — documented, not a failure).
- Ground truth: `SELECT count(*) FROM booking WHERE resource_id=$1 AND time_range && $2 AND status IN ('confirmed','held')` returns exactly 1.
- Zero 500s. Zero hangs. Zero responses with no body.
- 503 count is recorded per run. A rising 503 rate across runs indicates `lock_timeout` is tuned too aggressively for the contention level — reported to Ops, not a pass/fail gate.

**Escalation run.** Repeat once at N=500 to confirm the guarantee does not degrade to "very likely" at higher contention.

### CONC-02 — Partial-overlap contention

**Setup.** Same resource, two distinct overlapping ranges: 09:00–10:00 and 09:30–10:30.

**Execution.** Barrier-released, one caller per range, 50 repetitions.

**Assertions.** Exactly one succeeds every run. Ground truth confirms no overlapping active pair exists, checked immediately post-resolution, not only at test end.

**Extension — chained overlap.** Five ranges each offset 10 minutes from the last, such that not every pair overlaps but the set is not independent. Verifies the constraint handles transitive, not merely pairwise, overlap. 50 repetitions.

### CONC-03 — Edit-vs-create race (PRD FR5)

**Setup.** B1 confirmed at 14:00–15:00. Contested target: 09:00–10:00, currently free.

**Execution.** Simultaneously: (a) `PATCH /bookings/{B1}` moving it to 09:00–10:00, and (b) a different user's `POST /bookings` for 09:00–10:00. Barrier-released, 50 repetitions.

**Assertions.** Exactly one of {edit, create} succeeds. Loser receives 409. If the edit loses, B1's `time_range` is verified unchanged at 14:00–15:00 — a failed edit must not partially apply.

### CONC-04 — Edit-vs-edit race

**Setup.** B1 at 09:00–10:00, B2 at 14:00–15:00. Both simultaneously PATCHed toward 11:00–12:00.

**Execution.** Barrier-released, 50 repetitions.

**Assertions.** Exactly one edit succeeds. The loser's booking is verified unchanged at its original range — never left ambiguous or partially updated.

### CONC-05 — Cancel-and-rebook at the instant a slot frees

**Setup.** B1 confirmed at 09:00–10:00.

**Execution.** One request cancels B1; simultaneously (same barrier), two other users attempt `POST /bookings` for 09:00–10:00. 50 repetitions.

**Assertions.** Cancellation succeeds; exactly one create succeeds; ground truth confirms exactly one active booking post-resolution.

**Why this is not covered by CONC-01.** This exercises the constraint's predicate under a *concurrent status transition* — a row leaving the exclusion domain while new writers contend for the range it vacates. Materially different from always-been-free contention.

### CONC-06 — Sustained hot-resource escalation (PRD M9, RFC R1)

**Purpose.** Not a pass/fail gate. RFC §18 deliberately left the throughput ceiling unresolved. This test's job is to produce the missing data, not grade against a number nobody committed to.

**Setup.** One resource. Concurrent writers target distinct, non-overlapping slots on that resource — isolating index contention from overlap correctness, which CONC-01–05 already cover.

**Execution.** Escalating steps: 10, 25, 50, 100, 250, 500 simultaneous writers, each barrier-released.

**Data captured per step.** p50/p95/p99 write latency; successful writes/sec; error rate.

**CONC-06a — error rate is a hard gate even though throughput is not.** These writes never conflict. Any non-201 response other than a documented 503 under lock contention is a genuine bug and a release blocker, independent of what the throughput numbers show. This assertion is separate from the characterization because it is the one part of CONC-06 that gates release.

**Report.** A latency-vs-concurrency table identifying where p95 inflects non-linearly — the practical ceiling — handed to Ops as the trigger point for RFC §6.3's mitigations.

## 3. Hold & Waitlist Test Suite

*Rewritten in v1.0. v0.1's tests asserted against `waitlist_offer`'s exclusion constraint, which Spec v1.0 deleted. Mutual exclusion now lives in one place: the hold row in `booking`.*

### HOLD-01 — An ordinary booking must lose to an outstanding offer ★

This is the most important test in this document. It verifies the exact failure the v0.1 design had, and the reason holds moved into the `booking` table.

**Setup.** Resource with B1 confirmed at 09:00–10:00. User W is waitlisted for 09:00–10:00 (`status='waiting'`).

**Execution.**

1. Cancel B1. Wait for the offer worker to create a hold for W.
2. Verify ground truth: a booking row exists with `status='held'`, `user_id=W`, `expires_at` in the future.
3. An unrelated user U attempts `POST /bookings` for exactly 09:00–10:00.
4. W accepts the offer.

**Assertions.**

- Step 3 returns 409 `slot_unavailable`. U cannot take the slot.
- Step 4 returns 201. W's acceptance succeeds.
- Ground truth: the hold row and the confirmed booking are the same row — same `id`, `status` transitioned `held → confirmed`, `expires_at` now NULL. No second row was created.

**Failure meaning.** If step 3 succeeds, the hold is not occupying the exclusion domain and the entire waitlist guarantee (PRD FR17, S1) is broken.

### HOLD-02 — Concurrent booking attempt during hold, barrier-released

**Setup.** As HOLD-01, with an active hold in place.

**Execution.** 50 barrier-released `POST /bookings` attempts for the held range from 50 different users. 50 repetitions.

**Assertions.** All 50 receive 409 or 503. Zero succeed. Ground truth: exactly one active row for that range, and it is the hold. This proves the hold is enforced by the constraint, not by a check the concurrent path could bypass.

### HOLD-03 — Hold is opaque in availability views

**Setup.** Active hold on a range.

**Execution.** A user with no relationship to the hold fetches `GET /resources/{id}/availability` covering it.

**Assertions.** The range appears as an ordinary `busy_block`. No field identifies it as held, no `booking_id`, no `owner`, no status marker. Leaking hold state would expose waitlist queue information (Spec §5.7).

### WL-01 — Two overlapping simultaneous cancellations cannot produce two overlapping holds

**Setup.** B1 (09:00–10:00) and B2 (09:30–10:30) both confirmed on one resource. Waitlist entries exist for ranges contained in each.

**Execution.** Cancel B1 and B2 simultaneously, barrier-released. 50 repetitions.

**Assertions.** Ground truth: `SELECT count(*) FROM booking WHERE resource_id=$1 AND status='held' AND time_range && $2` never exceeds what non-overlapping holds permit — no two holds overlap each other. Verified at the database, not by counting notifications; a notification-layer bug could mask a real double-hold at the data layer.

**Note on the mechanism.** v0.1 verified this via a constraint on `waitlist_offer`. In v1.0 it is guaranteed by `no_overlapping_bookings` itself — a second overlapping hold is a second overlapping booking row, which the constraint already forbids (PRD FR25).

### WL-02 — Reaper-vs-acceptance race on the same hold row

**Setup.** One active hold with `expires_at` in the near future.

**Execution.** Barrier-released, using the same rigor as §2 (this race deserves identical treatment to the core booking race, not a lesser standard because it is "just" the waitlist): two connections simultaneously execute the reaper's expiry path, and the user's acceptance (`UPDATE booking SET status='confirmed' ... WHERE id=$1 AND status='held' AND expires_at > now()`). 100 repetitions.

**Assertions.** Exactly one affects 1 row; the other affects 0. Final state is deterministically confirmed or expired/reclaimed — never ambiguous, never both applied.

- If acceptance wins: the booking exists and is queryable; the reaper's cascade did not fire.
- If the reaper wins: the user receives 409 `offer_expired`, and cascade fires (WL-03 covers who receives it).

### WL-03 — Cascade reaches the correct next candidate

**Setup.** Three waitlist entries for the same range on one resource, with controlled distinct `joined_at` (entries 1, 2, 3).

**Execution.** Offer to entry 1 via cancellation. Force expiry. Assert entry 2 — not 3, not arbitrary — receives the next offer.

**Extension.** Repeat with entry 2 having withdrawn (`status='cancelled'`) before cascade reaches it. Assert entry 3 is offered, confirming the eligibility query filters on `status='waiting'` and does not offer to a withdrawn entry by position alone.

### WL-04 — Eligibility is containment, not overlap (PRD FR21) ★

This test would have passed under v0.1's undefined rule and fails under any implementation using overlap semantics.

**Setup.** User W waitlisted for 10:00–11:00.

**Execution.**

1. A booking covering 10:00–10:30 is cancelled, freeing that range.
2. Separately, a booking covering 10:00–11:00 is cancelled.

**Assertions.**

- After (1): W receives no offer. The freed range does not fully contain W's requested range. Ground truth: no hold created for W.
- After (2): W does receive an offer.

### WL-05 — Silent reaper failure (PRD R3, RFC §14)

**Purpose.** Prove the failure mode is real *and* prove the compensating detection works. Not to fix it — RFC never claimed the reaper is failure-proof, only that its failure must be detectable.

**Part A — the failure mode is real.** In isolation, stop the beat scheduler. Create a hold with a short expiry. Confirm that past `expires_at` the row remains `status='held'` with no application-level error signal. This confirms RFC R3 is accurate, not hypothetical.

**Part B — detection actually works.** Seed a hold with `status='held' AND expires_at < now() - interval '5 minutes'`. Assert the monitoring check fires — the `hold_reaper` heartbeat in `system_check_run` goes stale and the alert triggers, surfacing via `GET /api/v1/admin/checks/latest`. If no such check exists, that is a release blocker, since Part A proves the failure is silent from the application's own perspective without it.

### WL-06 — Redis outage degrades, never corrupts (RFC §4.3)

**Setup.** Running system with active waitlists.

**Execution.** Stop Redis. Attempt: a booking creation, a cancellation, a booking on a range with an expired hold.

**Assertions.**

- Booking creation and cancellation succeed — the exclusion constraint is untouched; no correctness violation.
- The booking over the expired hold succeeds, because cleanup-on-write cleared it without the reaper (RECLAIM-01 covers this directly).
- No offer is dispatched, no cascade fires — degraded liveness, which is the safe direction.
- Redis unavailability alerts.

## 4. Hold Reclamation Test Suite

*New in v1.0. RFC §10.4 specifies two mechanisms and argues neither alone suffices. v0.1 tested neither.*

### RECLAIM-01 — Cleanup-on-write makes the system self-healing ★

This verifies the property that justifies putting a DELETE on the hot write path.

**Setup.** Seed a hold with `expires_at` in the past. Stop the reaper entirely.

**Execution.** A user attempts `POST /bookings` for that range.

**Assertions.** The booking succeeds (201). Ground truth: the expired hold row is gone; exactly one confirmed booking exists.

**Failure meaning.** If this fails, a stalled reaper — or a Redis outage — makes resources permanently unbookable. The self-healing property is the entire argument for accepting the extra DELETE on the write path.

### RECLAIM-02 — Reaper cascade fires without booking traffic

**Setup.** Active hold for user W, expiry imminent. A second waitlist entry (user X) is eligible for the same range. No booking traffic at all.

**Execution.** Wait past expiry.

**Assertions.** The reaper expires the hold and creates a new hold offering the slot to X. Cleanup-on-write cannot do this — nobody is writing. This is why both mechanisms exist.

### RECLAIM-03 — Cleanup-on-write vs. acceptance race

**Setup.** A hold whose `expires_at` has just passed, with the offered user attempting acceptance at the same moment another user attempts to book the range.

**Execution.** Barrier-released, 100 repetitions.

**Assertions.** Both orderings are correct and deterministic:

- Cleanup commits first → acceptance's conditional update matches 0 rows → 409 `offer_expired`; the other user's booking succeeds.
- Acceptance commits first → the row is confirmed with `expires_at` NULL → cleanup's `WHERE status='held' AND expires_at <= now()` matches 0 rows → the other user's booking hits the constraint and receives 409.
- Ground truth after every run: exactly one active row for the range. Never zero, never two.

### RECLAIM-04 — Deadlock under load (Spike S1.7)

**Purpose.** Cleanup-on-write adds a DELETE to the hot write path. Concurrent transactions deleting and inserting on the same resource could deadlock.

**Setup.** One resource seeded with multiple expired holds across adjacent ranges.

**Execution.** CONC-01-level concurrency (200 barrier-released writers) targeting overlapping and adjacent ranges. 50 repetitions.

**Assertions.** Zero deadlocks (SQLSTATE 40P01). Zero unexplained 500s.

**If this fails:** cleanup-on-write moves to reaper-only, RECLAIM-01's self-healing property is lost, and RFC §4.3's Redis-outage argument no longer holds. That is a design change requiring re-review, not a tuning adjustment — escalate rather than working around it.

## 5. Timezone & DST Correctness Test Suite

### TZ-01 — Recurring series spanning a real fall-back transition

America/New_York DST ends 2:00 AM on Sunday, November 1, 2026.

**Setup.** Series: `weekday=Sunday`, `local_start_time=10:00`, `occurrence_count=4`, `series_start_date=2026-10-25`. Occurrences: Oct 25, Nov 1, Nov 8, Nov 15 — deliberately including the transition date itself as an occurrence, not merely dates either side of it.

**Assertions.** All four display as 10:00 local. Underlying instants: Oct 25 (EDT, UTC−4) → 14:00:00Z; Nov 1, 8, 15 (EST, UTC−5) → 15:00:00Z.

Nov 1 must resolve to EST, not EDT — the transition occurs at 2:00 AM, so by the 10:00 occurrence the zone has already changed. A test checking only before-and-after dates would miss exactly this boundary.

### TZ-02 — Created in one DST regime, occurring in another (RFC §9.1)

**Setup.** One-off booking created with an implied "now" of Oct 20, 2026 (EDT), for Nov 10, 2026 (EST) at 10:00 local America/New_York.

**Assertions.** Stored instant is 15:00:00Z (EST, correct for the occurrence date) — not 14:00:00Z, which would be the bug: applying the offset in effect at *creation* time rather than at *occurrence* time.

### TZ-03 — Stale IANA tz database (PRD R2)

Why this cannot be a normal assertion test. You cannot validly test "what happens with wrong data" using correct data.

- **Test A — environment integrity.** CI asserts the deployed tzdata version is explicitly pinned and recorded (not "whatever the base image shipped"), so staleness is a tracked quantity rather than an invisible one. The recorded version must match `recurring_series.tzdata_version` for newly created series.
- **Test B — scheduled drift alert.** A periodic (not per-commit) job compares installed tzdata against the latest IANA release and alerts — does not fail a build — if behind. Being behind is not necessarily broken yet, only at risk.
- **Test C — blast-radius containment**, run in isolation with deliberately stale tzdata. Verify the failure mode is bounded to scheduling intent and cannot compound into a concurrency-safety failure: `no_overlapping_bookings` operates on the stored UTC instant regardless of what timezone data produced it.

**Correction to v0.1's claim.** v0.1 described the impact as "displays against an incorrect but plausible local time." That understates it. Stored occurrence instants are themselves *wrong* — computed under rules that no longer apply — and correcting them requires re-materialization (TZ-07), not merely a display refresh. Test C's actual assertion is that the error is confined to *which instant an occurrence names*, and never to *whether two bookings can overlap*.

### TZ-04 — Cross-timezone booking and viewing

**Setup.** Resource configured `Europe/Paris`. User A renders in `Asia/Kolkata` (UTC+5:30), User B in `Europe/Paris`.

**Assertions.** Both API responses return identical UTC start/end — confirming the backend never localizes server-side (Spec §1). A's client renders IST-equivalent, B's renders Paris-equivalent, both corresponding to the same instant.

*This is the one group spanning frontend rendering as well as backend contract; both halves must be asserted.*

### TZ-05 — Nonexistent local time (PRD FR11)

**Setup.** `Europe/Paris` springs forward 02:00 → 03:00 on Sunday, March 28, 2027. Series requesting local 02:30 on that date.

**Assertions.**

- The system detects that 02:30 does not exist — verified by round-tripping the localized datetime, not by assuming the library handles it.
- The shift-forward policy applies: the occurrence lands at 03:30 local.
- `time_adjustments` in the preview response contains an entry with `issue: "nonexistent_local_time"`, the requested and adjusted local times, and an explanation.
- Failure condition: the system silently produces a time without disclosure. Silent guessing is the failure class this project exists to eliminate.

### TZ-06 — Ambiguous local time (PRD FR12)

**Setup.** `Europe/Paris` falls back 03:00 → 02:00 on Sunday, October 31, 2027. Local 02:30 occurs twice.

**Assertions.** Detection via the `fold` attribute; first (pre-transition) instance selected per policy; disclosed in `time_adjustments` with `issue: "ambiguous_local_time"`.

### TZ-07 — tzdata re-materialization (PRD FR13)

**Setup.** An active series with future occurrences materialized under tzdata version V1, recorded in `recurring_series.tzdata_version`.

**Execution.** Simulate a rule change for that zone (test-controlled tzdata V2 altering the offset for a date in the series' future range). Run the re-materialization job.

**Assertions.**

- Affected occurrences are identified via `idx_series_tzdata`.
- They are re-materialized from the series definition — recomputed from `local_start_time` + `timezone` + `series_start_date` — not adjusted in place by applying a delta.
- `recurring_series.tzdata_version` updates to V2.
- Local wall-clock time is preserved; the stored UTC instant changes.
- The run is recorded in `system_check_run` (PRD FR13c).
- Affected users are notified (PRD FR54).

### TZ-08 — Re-materialization that conflicts (PRD FR13b) ★

The hard case, and the one most likely to be skipped.

**Setup.** Series occurrence O materialized at instant T1 under V1. Between materialization and the rule change, another user books the range that O's re-materialized instant T2 will require.

**Execution.** Run re-materialization.

**Assertions.**

- The re-materialization of O fails the exclusion constraint — it is a booking write like any other.
- O is never silently dropped. It is flagged for resolution.
- Both the series owner and the resource administrator are notified (PRD FR13b).
- The remaining occurrences in the series re-materialize successfully — one conflict does not abort the run.
- `system_check_run` records the conflict in `findings`.

### TZ-09 — Southern hemisphere (opposite transition direction)

**Setup.** `Australia/Sydney` — DST begins in October and ends in April, inverse to the northern hemisphere.

**Execution.** A series spanning Sydney's October transition, and one spanning its April transition.

**Assertions.** Local wall-clock time preserved across both. Rationale: a sign error in offset arithmetic passes every northern-hemisphere test and fails here.

### TZ-10 — Zone without DST

**Setup.** `Asia/Kolkata` (UTC+5:30, no DST). Series spanning dates where other zones transition.

**Assertions.** Every occurrence has an identical UTC offset. Verifies no spurious adjustment is applied to zones that never transition.

## 6. Recurring Series Test Suite

*Rewritten in v1.0. Spec v1.0 splits creation into preview → acknowledge → confirm per PRD FR33. v0.1 tested the single-step auto-create design.*

### REC-01 — Preview commits nothing

**Setup.** Series definition where occurrence 3 conflicts with an existing booking.

**Execution.** `POST /bookings/recurring/preview`.

**Assertions.** 200 with `would_create` (occurrences 1, 2, 4…), `conflicts` (occurrence 3), and a `preview_token`. Ground truth: zero new booking rows exist. A preview that writes anything is a defect.

### REC-02 — Confirm without acknowledgment is rejected (PRD FR33)

**Execution.** `POST /bookings/recurring` with the token but an empty `acknowledged_conflicts` array while the preview reported a conflict.

**Assertions.** 409 `unacknowledged_conflicts`. Ground truth: zero bookings created. This is the requirement that a user must actually see what will not be created.

### REC-03 — Conflict arising between preview and confirm ★

The case the two-step design must handle and the one an implementer is most likely to miss.

**Setup.** Preview returns clean — no conflicts.

**Execution.**

1. Preview.
2. Another user books occurrence 4's exact slot.
3. Confirm with an empty acknowledgment list (legitimately — the preview was clean).

**Assertions.**

- 207 Multi-Status.
- Occurrence 4 appears in `conflicts` with `acknowledged: false`.
- Other occurrences created successfully.
- The client can distinguish `acknowledged: false` from `acknowledged: true` — the former needs distinct wording ("this slot was taken while you were confirming").

### REC-04 — Preview token expiry

**Execution.** Confirm using a token older than 15 minutes.

**Assertions.** 409 `preview_expired`. Zero bookings created.

### REC-05 — Per-occurrence transaction isolation (RFC §5d)

**Setup.** 10-occurrence series where occurrences 3 and 7 conflict, both acknowledged.

**Assertions.** Occurrences 1,2,4,5,6,8,9,10 are created. 3 and 7 are not. Each occurrence committed in its own transaction — verified by confirming that a deliberately induced failure on occurrence 7 does not roll back occurrence 6.

### REC-06 — Series bounds (PRD FR14)

| Input | Expected |
|---|---|
| `occurrence_count = 100` | 200 (boundary, valid) |
| `occurrence_count = 101` | 400 `validation_error` |
| `occurrence_count = 0` | 400 `validation_error` |
| Series extending beyond 365 days | 400 `validation_error` (horizon, FR14b) |
| Series exactly at 365 days | 200 |

### REC-07 — DST-spanning series through the full two-step flow

**Setup.** TZ-01's Nov 1, 2026 series, run through preview → confirm rather than direct creation.

**Assertions.** Preview's `would_create` shows the correct differing UTC instants across the boundary. Confirmed bookings match the preview's instants exactly. This verifies the preview and the commit use the same expansion code path — a preview computing instants differently from the commit would be a silent, severe defect.

## 7. Idempotency Test Suite

### IDEM-01 — First use

Fresh key, valid body → 201. Verify an `idempotency_key` row exists with `status='completed'`, correct `request_body_hash`, and stored response.

### IDEM-02 — Exact replay

Same key, byte-identical body → identical stored status/body returned; `Idempotent-Replay: true` header present; ground truth: exactly one booking row.

### IDEM-03 — Replay with a different body

Same key, different `resource_id` or range → 422 `idempotency_key_conflict`. Ground truth: no booking created by the second attempt.

### IDEM-04 — After the 24-hour retention window

Advance the stored key's `created_at` past 24h, replay identical key+body. Assert it is processed as a genuinely fresh request — which may correctly succeed or hit a genuine 409. This is the documented tradeoff of the 24-hour default (Spec §7); the test confirms the implementation matches the stated contract, not that the tradeoff is right.

### IDEM-05 — Recurring-series replay

Replay `POST /bookings/recurring` with the same key and body after a first attempt returned 207 with conflicts. Assert the replay returns the identical `created`/`conflicts` arrays — not a fresh re-evaluation, which could produce different results if state changed. A non-idempotent replay here wouldn't just duplicate one booking; it would produce an inconsistent partial-success report.

### IDEM-06 — Concurrent replay (PRD FR36) ★

New in v1.0. The hardest idempotency case and the one v0.1 omitted entirely.

**Execution.** Barrier-release two requests with the identical key and body. 100 repetitions.

**Assertions.**

- Exactly one executes and returns 201.
- The other returns 409 `request_in_progress` — or, if the first completed in the interim, the stored 201 as a replay.
- The second must NEVER return 409 `slot_unavailable`. That would tell the user their own in-flight booking made the slot unavailable.
- Ground truth: exactly one booking row.

### IDEM-07 — The transaction boundary (RFC §11.2) ★

This is the load-bearing design decision in the entire idempotency mechanism, and nothing in v0.1 verified it.

**Execution.** Using a fault-injection hook, terminate the process between the booking insert and the `idempotency_key` completion update.

**Assertions.** Ground truth after recovery: either both the booking and a completed key record exist, or neither does. Never a booking with no key record.

**Failure meaning.** If the two writes are in separate transactions, this test fails — and the failure *is* the bug the mechanism exists to prevent: a subsequent retry finds no key, attempts a fresh insert, and returns "slot unavailable" for the user's own booking.

### IDEM-08 — The actual user-facing requirement (PRD FR38) ★

**Execution.**

1. Submit a booking. It commits.
2. Simulate the response being lost (proxy-level drop after commit).
3. Client retries with the same key.

**Assertions.** The retry returns 201 with the existing confirmed booking, marked as a replay. It must never return 409 `slot_unavailable`.

Everything else in this suite is machinery. This is the requirement.

### IDEM-09 — Error-code confusion is impossible

**Execution.** Construct both conditions deliberately: (a) a genuine slot conflict, (b) a genuine in-flight replay.

**Assertions.** (a) returns `slot_unavailable`, (b) returns `request_in_progress`. Neither ever returns the other's code. Both are HTTP 409 — a client branching on status code alone cannot distinguish them, which is why Spec §10 requires branching on `error.code`.

### IDEM-10 — Key scoping across principals

**Execution.** User A creates a booking with key K. User B sends a request with the same key K.

**Assertions.** B's request is processed as fresh — keys are scoped `(user_id, key)`. B never receives A's stored response. Closes the key-harvesting threat (RFC §8.2).

### IDEM-11 — Coverage on every required endpoint (PRD FR34)

Assert a missing `Idempotency-Key` returns 400 on each of: `POST /bookings`, `PATCH /bookings/{id}`, `POST /bookings/{id}/cancel`, `POST /bookings/recurring`, `POST /waitlist-entries`, `POST /waitlist-offers/{id}/confirm`, `POST /admin/users/{id}/deactivate`.

## 8. Audit Trail Test Suite

*New in v1.0. PRD FR39–43 and RFC §12 had zero coverage.*

### AUD-01 — Append-only is enforced by grants, not convention ★

**Execution.** Connect as the application database role and attempt `UPDATE audit_log SET ...` and `DELETE FROM audit_log WHERE ...`.

**Assertions.** Both fail with insufficient privilege. This is the only test that proves grant-level enforcement rather than assuming it. An application-level guard would pass a weaker test and fail this one.

### AUD-02 — Triggers cannot be bypassed ★

**Execution.** Write directly to `booking` via raw SQL, bypassing the service layer entirely — simulating the future bulk-import script that motivated trigger-based auditing.

**Assertions.** An `audit_log` row still appears. This is the entire argument for triggers over application-level auditing (RFC §12); if it fails, the audit is opt-in per code path — the exact failure mode the design rejects.

### AUD-03 — Actor attribution and required reason

**Execution.** (a) A normal user booking. (b) An admin override cancellation with a reason. (c) An admin override without a reason. (d) A system-initiated write (hold creation by the worker).

**Assertions.**

- (a) `actor_type='user'`, correct `actor_id`, `request_id` matching the response's `X-Request-Id`.
- (b) `actor_type='admin'`, `reason` populated.
- (c) Rejected at the API layer with 400 — reason is required for overrides (PRD FR40).
- (d) `actor_type='system'`.
- No audit row is ever written with `actor_type='unknown'` during normal operation. If one appears, the reconciliation job alerts (Spec §3).

### AUD-04 — Full lifecycle reconstruction

**Execution.** Create → edit → admin-cancel a booking. Then `GET /bookings/{id}/history`.

**Assertions.** All three events appear in order with actors, timestamps, reasons, request IDs, and before/after state. This answers "what happened to my booking?" months later — the requirement (PRD FR42), not the mechanism.

### AUD-05 — Audit survives cancellation and series operations

**Assertions.** Cancelling a booking does not remove its audit history. A series cancellation produces one audit row per affected occurrence, not a single aggregate row.

## 9. Data Integrity & Correctness Monitoring

**The core problem.** Under normal operation the constraint makes a real overlap impossible — so you cannot naturally produce the input the reconciliation query exists to catch. Testing it requires deliberately defeating the mechanism it backstops, in isolation, precisely because the two are never both relevant at the same moment in real operation.

### RECON-01 — Injected violation is caught

**Setup.** In an isolated test database only (never staging or production), `DROP` `no_overlapping_bookings`. Insert two active bookings with deliberately overlapping ranges on one resource — this now succeeds.

**Execution.** Run the reconciliation query.

**Assertions.** It returns exactly those two rows. Then restore the constraint and confirm the identical insert now fails with 23P01 — proving the test didn't accidentally alter the schema in some other way that coincidentally produces a passing result.

### RECON-02 — Zero false positives on realistic data

**Setup.** Hundreds of resources, thousands of legitimately non-overlapping bookings, a mix of cancelled/confirmed/held rows, multiple recurring series.

**Assertions.** Exactly zero rows returned. A query returning zero against an empty table proves nearly nothing — a join or WHERE bug producing false negatives is only exposed by realistically varied data. The `held` status must be represented, since v1.0's predicate covers it.

### RECON-03 — End-to-end path from violation to surfaced alert

**Execution.** Seed a violation per RECON-01. Run the full scheduled job, not the bare SQL. Query `GET /api/v1/admin/checks/latest`.

**Assertions.** `reconciliation` shows `status: "fail"` with the correct finding. A correct query wired to a broken reporting path is operationally equivalent to no check at all.

### RECON-04 — Alert text is correct (RFC §14)

**Assertions.** The alert fired by RECON-03 states that a hit means the guarantee has been *removed* — a dropped constraint, a restore without it, or out-of-band writes — and explicitly not that a race occurred.

**Rationale.** An on-call engineer who reads a reconciliation hit as "a race occurred" will investigate the wrong thing under pressure. This is a testable property of the alert payload, not a documentation nicety.

### RECON-05 — Schema assertion detects a dropped constraint (PRD M3) ★

**Execution.** In isolation, drop `no_overlapping_bookings`. Run the schema-assertion check.

**Assertions.** The check fails and pages — not merely logs. It fires before any overlapping data exists, detecting the cause rather than the consequence.

**CI gate.** This check runs on every migration, querying `pg_constraint` — not trusting a migration file. This is the direct, ongoing answer to RFC §2.1's named maintenance risk: an engineer dropping the constraint during an unrelated migration.

### RECON-06 — Background-job heartbeats (PRD R3)

**Execution.** Individually stall each of: `hold_reaper`, `offer_cascade`, `series_materialization`, `tzdata_rematerialization`.

**Assertions.** Each produces a stale heartbeat alert within its threshold. Their failure mode is silence, not error — no exception is raised, so absence of a heartbeat is the only signal.

### RECON-07 — Alerting verified by deliberate injection

**Assertions.** Every alert in RECON-03 through RECON-06 is fired at least once by deliberate injection before release. An alert never tested is an alert that does not exist. This is a release gate, not a recommendation.

### RECON-08 — Query cost at scale

**Assertions.** Reconciliation execution time against RECON-02's dataset stays within budget for its run frequency — the check must not itself become a load problem.

## 10. Functional Correctness Test Matrix

Breadth coverage across every endpoint. Depth on hard mechanisms lives in §2–§9; this table's job is comprehensive surface coverage including documented boundaries.

| Endpoint | Scenario | Expected |
|---|---|---|
| `POST /bookings` | Valid range in bookable hours | 201 |
| | `end == start` | 400 `validation_error` |
| | Exactly `max_booking_duration_minutes` (boundary) | 201 |
| | One minute over max duration | 400 |
| | `start` one second in the past | 400 |
| | Exactly at `bookable_start_time`/`bookable_end_time` | 201 |
| | 366 days ahead (beyond horizon, FR14b) | 400 |
| | Exactly 365 days ahead | 201 |
| | Valid UUID, nonexistent resource | 404 `not_found` |
| | `resource.status='inactive'` | 404 |
| | Missing `Idempotency-Key` | 400 |
| | Overlapping an active hold | 409 `slot_unavailable` |
| | During simulated primary failover | 503 `service_unavailable` + `Retry-After` |
| `GET /bookings/{id}` | Owner | 200 |
| | Resource admin for that resource | 200 |
| | `operations` role | 200 |
| | Non-owner, non-admin | 404 (not 403 — Spec §1) |
| `GET /bookings/{id}/history` | Owner | 200, full event list |
| | Non-owner | 404 |
| `GET /bookings` | Own bookings | 200, `starts_at ASC` |
| | Held rows | Never returned (Spec §5.4) |
| | Admin by `resource_id` | 200 |
| | Non-admin, other resource | 403 `permission_denied` |
| `PATCH /bookings/{id}` | Owner, free target | 200 |
| | Non-owner | 404 |
| | Target overlaps another booking | 409 `slot_unavailable` |
| | Missing idempotency key | 400 |
| `POST /bookings/{id}/cancel` | Self-cancel, no reason | 200 |
| | Admin override, no reason | 400 (reason required) |
| | Admin override with reason | 200, reason persisted + audited |
| | Already cancelled | 200, idempotent |
| | Non-owner non-admin | 404 |
| `GET /resources/{id}/availability` | Valid range ≤ 92 days | 200 |
| | `to` before `from` | 400 |
| | Exactly 92 days | 200 (boundary) |
| | 93 days | 400 |
| | Own booking in range | `booking_id`/`owner` present |
| | Other's booking, non-admin | keys absent (SEC-05) |
| | Held slot | opaque busy block (HOLD-03) |
| | Replica lag over threshold | `data_freshness: "primary"` |
| `POST /bookings/recurring/preview` | Valid | 200 + token |
| | `occurrence_count=101` | 400 |
| | Fixed offset instead of IANA zone | 400 (PRD FR8) |
| `POST /bookings/recurring` | All acknowledged | 207 |
| | Unacknowledged conflicts | 409 `unacknowledged_conflicts` |
| | Expired token | 409 `preview_expired` |
| `POST /recurring-series/{id}/cancel` | Owner, future occurrences | 200, future cancelled, past untouched |
| | No future occurrences | 200, empty array |
| `POST /waitlist-entries` | Genuinely full slot | 201 |
| | Currently-free slot | 422 `slot_already_available` |
| | Duplicate live entry | 409 `already_on_waitlist` |
| | While already offered | 409 (uniq index covers offered) |
| `GET /waitlist-entries` | Own entries | 200, self-scoped, `queue_position` present |
| `POST /waitlist-offers/{id}/confirm` | Within window | 201 |
| | After expiry | 409 `offer_expired` |
| | Someone else's offer | 404 |
| `POST /waitlist-offers/{id}/decline` | Active offer | 200, hold released, cascade fires |
| | Already resolved | 409 `offer_already_resolved` |
| `POST /resources` | `system_admin` | 201 |
| | Non-admin | 403 (not 404 — capability-gated) |
| `PATCH /resources/{id}` | Resource admin | 200 |
| | Non-admin | 403 |
| `POST /resources/{id}/admins` | `system_admin` | 201 |
| | Already granted | 409 |
| `GET /admin/checks/latest` | `operations` | 200, all six checks |
| | Regular user | 403 |
| `POST /admin/users/{id}/deactivate` | `system_admin` | 200 (§12) |

## 11. Performance, Load & Failure Injection

All tests run against a realistically seeded dataset (PRD A1 scale). Numbers from an empty database are close to meaningless — index behavior, planning, and pool contention all depend on realistic size.

### PERF-01 — Booking write P95 < 300ms, steady and spike

PRD A1 notes traffic arrives as sharp spikes at contested moments, not uniformly. Steady-only testing misses the case that matters.

- **Steady:** sustained baseline rate; P95 over the window.
- **Spike:** 200 concurrent requests within a 2-second window against a mix of resources, on top of sustained baseline load elsewhere. Measure P95 during the spike window specifically — a long-window aggregate dilutes a bad spike P95 into an acceptable-looking number, hiding exactly the failure this test must catch.

Scope clarification (RFC §7.1). This target applies to *nominal* load. Latency on a *contested* slot is dominated by the winning transaction's duration, since conflicting inserts block. Contested latency is characterized by CONC-06 and is not graded against 300ms.

### PERF-02 — Availability read P95 < 500ms

Tested against (a) ordinary booking density and (b) a near-fully-booked hot resource at the 92-day query bound, confirming no material degradation at the upper bound of a single query's result size.

### PERF-03 — Waitlist dispatch P95 < 5s, including burst

Measured from cancellation commit to notification enqueued. Tested with (a) an isolated cancellation and (b) a burst of 50 near-simultaneous cancellations, targeting the worker-pool-depth risk (RFC §7.2) that is the reason the 5s budget is generous.

### FAIL-01 — Primary failover during a write (PRD M14) ★

v0.1 deferred this to Rollout. It is pulled back into scope: PRD M14 is a stated behavioral requirement with a defined error code in Spec §5.1, and it is testable now by fault injection.

**Execution.** Initiate a booking write, then force primary failover mid-transaction.

**Assertions.**

- The request returns 503 `service_unavailable` with `Retry-After` — never hangs, never returns a timeout with no body, never returns an outcome the client cannot distinguish from success.
- Retry with the same idempotency key returns the correct outcome: the original booking if it committed, or a fresh attempt if it did not. Never an ambiguous state.
- This is where §7 and §11 meet. Without idempotency, "did my booking commit before the primary died?" is unanswerable — which is why Spec §10 requires the frontend to treat 503 as "unknown, retry with the same key," not as failure.

### FAIL-02 — Lock timeout under extreme contention

**Execution.** Contention exceeding what `lock_timeout` (3s) permits.

**Assertions.** Requests exceeding the timeout return 503 with `Retry-After` — not 500, and not 409 `slot_unavailable`. The outcome is *unknown*, not *decided*, and the error code must reflect that distinction.

### FAIL-03 — Replica lag degradation (PRD FR31)

**Execution.** Induce replication lag beyond threshold.

**Assertions.** Availability reads either serve from primary with `data_freshness: "primary"`, or surface staleness explicitly. Never silently serve stale data. Lag beyond threshold alerts.

## 12. Security & Lifecycle Test Suite

### SEC-01 — IDOR, with response-body leakage check

As User A, attempt GET/PATCH/cancel/history against User B's booking. Assert 404 on every verb — and assert the response body contains only the standard empty error envelope, with no leaked `resource_id`, time range, or any field from B's booking. A status-code-only check would miss a bug returning the right code with an informative body.

### SEC-02 — Rate-limit scoping and boundary

A single principal fires past the limit. Assert 429 begins exactly at the threshold (the request at the limit succeeds; the next does not), and a second principal's requests in the identical window are entirely unaffected — confirming per-principal, not global.

### SEC-03 — Waitlist manipulation

(a) Submit a `POST /waitlist-entries` payload containing an unexpected `joined_at` field. Assert it is ignored and the server value used. (b) Second live entry for the same user/resource/range → 409 `already_on_waitlist`. (c) Attempt to join while already offered → 409 (the unique index covers `offered`).

### SEC-04 — Injection resistance

SQL-injection payloads as `from`/`to` parameters and as path UUIDs. Assert 400 `validation_error`, never reaching a raw SQL path — backed by a static review confirming all range queries use parameterized ORM constructs, with this dynamic test as runtime backstop, not the sole defense.

### SEC-05 — Field-level authorization withholds precisely ★

As a non-owner non-admin, fetch availability covering another user's booking. Assert the `busy_block` has no `booking_id` key and no `owner` key present at all — not null values.

Why the distinction matters: an implementation returning `"owner": null` still leaks the schema pattern, confirming *something* exists there. This test is designed specifically to catch that half-measure, not just gross leakage.

### SEC-06 — Restricted resources don't leak existence (PRD FR46)

A user outside a restricted resource's group requests it directly and lists resources. Assert 404, not 403, and that it is absent from list results. The existence is protected, not just the action.

### SEC-07 — Audit tamper resistance

Covered by AUD-01. Restated here as a security assertion: no API path and no application-role SQL can alter history.

### OFF-01 — Offboarding applies the per-resource policy (PRD FR49) ★

**Setup.** User W with: 2 future bookings on Resource A (`offboarding_policy='transfer'`), 1 on Resource B (`'cancel_and_notify'`), 1 on Resource C (`'retain'`), 3 waitlist entries, 1 outstanding hold, 1 active recurring series.

**Execution.** `POST /admin/users/{id}/deactivate`.

**Assertions.**

- Resource A bookings transferred to its resource admin.
- Resource B booking cancelled, affected parties notified.
- Resource C booking retained, flagged.
- All 3 waitlist entries cancelled.
- The outstanding hold is released so the slot cascades to the next eligible entry — not left to expire uselessly (PRD FR50).
- The series is flagged to the resource admin with transfer/terminate options (FR51).
- Every action audited with `actor_type='system'` and the offboarding reason.
- No booking is silently orphaned. Ground truth: every one of W's future bookings is in a defined post-offboarding state.

### OFF-02 — Deactivated user cannot book

Assert a deactivated principal's booking, waitlist, and confirm requests are rejected.

## 13. Test Environment & Data Requirements

- **Concurrency tests (§2–§4) require real PostgreSQL**, version-matched to production, with `btree_gist` enabled. Not SQLite, not in-memory, not a mocked ORM. The guarantee under test is a Postgres-specific mechanism — MVCC, real index contention, real constraint enforcement. A substitute cannot exercise it, by definition.
- **Realistic connection topology.** Run against the same PgBouncer transaction-pooling configuration as production; pool behavior can mask or expose contention differently than direct per-request connections.
- **Session settings must match production** (RFC §7.1): `lock_timeout=3s`, `statement_timeout=10s`, `idle_in_transaction_session_timeout=30s`, `READ COMMITTED`. A concurrency test run without production timeouts is testing a different system — blocking behavior and 503 rates both change.
- **True-simultaneity mechanics per §2.0.** A single-process `asyncio.gather()` on a shared pool is explicitly not sufficient for any test in §2–§4.
- **Controllable time** for expiry tests (§3, §4, §7 IDEM-04) — a test-controlled clock or configurable short windows. Sleeping through a real 15-minute offer window is not viable in CI.
- **Controllable tzdata** for TZ-03 and TZ-07/08 — the ability to pin and swap tz database versions in an isolated environment.
- **Fault injection** for IDEM-07 (process kill mid-transaction), FAIL-01 (primary failover), FAIL-03 (replica lag), WL-06 (Redis outage).
- **Realistic data volume** before any §11 number is trusted: PRD A1 scale, with held and cancelled rows represented.

**Environment tiers**

**CI tier** — every PR touching the booking write path, migrations, waitlist/hold logic, or recurrence: CONC-01 (reduced to 10 consecutive runs), CONC-02–05, HOLD-01–03, WL-01–04, RECLAIM-01–03, IDEM-01–03 and 06, 09–11, AUD-01–03, RECON-05 (schema assertion), and §10's full matrix. Real Postgres service container. Cheap enough to gate merges.

**Staging / pre-release tier** — scheduled, mandatory before release: CONC-01 full 100-run exercise plus the N=500 escalation, CONC-06, RECLAIM-04, WL-05–06, all of §5, §6, IDEM-04–05 and 07–08, AUD-04–05, RECON-01–04 and 06–08, all of §11, all of §12. Expensive enough that per-commit execution would make CI unusable.

## 14. Acceptance Criteria for Release

**Hard blockers — cannot ship without every item**

**Concurrency**

- CONC-01: 100% pass across 100 consecutive runs at N=200, plus the N=500 escalation. Zero tolerance. Ground-truth verified.
- CONC-02 through CONC-05: 100% pass.
- CONC-06a: zero unexpected errors on non-conflicting writes.

**Holds & waitlist**

- HOLD-01: passing. A direct booking must lose to an outstanding offer. If only one test in this document runs, this is the one.
- HOLD-02, HOLD-03: passing.
- WL-01 through WL-04: 100% pass, same rigor as §2. WL-04 (containment eligibility) explicitly verified.
- WL-05 Part B: detection exists and fires. (Part A is a one-time confirmation, not a repeated gate.)
- WL-06: Redis outage degrades without corrupting.

**Reclamation**

- RECLAIM-01: self-healing verified with the reaper stopped.
- RECLAIM-02, RECLAIM-03: passing.
- RECLAIM-04: zero deadlocks. Failure here is a design change requiring re-review, not a tuning fix.

**Time**

- TZ-01, TZ-02, TZ-04: passing, including the Nov 1 2026 transition-date-as-occurrence case.
- TZ-05, TZ-06: nonexistent and ambiguous times detected, policy applied, disclosed.
- TZ-07, TZ-08: re-materialization works and conflicts are never silently dropped.
- TZ-09, TZ-10: southern-hemisphere and no-DST zones correct.
- TZ-03 Tests A and B in place.

**Idempotency**

- IDEM-01 through IDEM-05: passing.
- IDEM-06: concurrent replay never returns `slot_unavailable`.
- IDEM-07: transaction boundary verified by fault injection.
- IDEM-08: a retried lost-response request shows the user their own booking.
- IDEM-09 through IDEM-11: passing.

**Audit**

- AUD-01: append-only enforced at grant level.
- AUD-02: triggers cannot be bypassed by raw SQL.
- AUD-03 through AUD-05: passing.

**Integrity & monitoring**

- RECON-01 through RECON-06: passing.
- RECON-07: every alert fired at least once by deliberate injection. An untested alert does not exist.
- RECON-05 wired as a CI gate on every migration.

**Recurrence**

- REC-01 through REC-07: passing, including REC-03 (conflict between preview and confirm).

**Functional, security, lifecycle**

- §10 matrix: 100% passing.
- SEC-01 through SEC-07: zero findings. These are correctness-of-guarantee failures, not polish, and carry the same zero tolerance as §2.
- OFF-01, OFF-02: passing.

**Performance & failure**

- PERF-01 through PERF-03: targets met under both steady and spike profiles. Steady-only is explicitly insufficient given PRD A1's traffic pattern.
- FAIL-01: failover returns 503, never a hang, and retry-with-same-key resolves unambiguously.
- FAIL-02, FAIL-03: passing.

**Tracked post-launch, not release-gating**

- CONC-06's throughput characterization — produces runbook data, not a target. Release proceeds with the data in hand.
- Reaper interval (30s default) — ship, monitor real cascade-latency distribution, tune later.
- Offer window (15 min) — PRD open question 1, unresolved. See §15.
- Idempotency retention (24h) — ship as specified; monitor for legitimate retries arriving later.
- `lock_timeout` value (3s) — tune from CONC-01's observed 503 rate.
- Long-tail clock skew and network conditions — covered by ongoing reconciliation monitoring, not reproducible as a fixed gate.

## 15. Unresolved Contract Points

Per this document's own scope: an unresolved contract point is flagged, not silently assumed. Where a decision is open, the requirement below is what the test plan for *either* resolution must satisfy.

**Offer window duration** (PRD open question 1). Tests use 15 minutes as a placeholder. Whatever value is chosen, the suite must verify: the window is enforced server-side (WL-02); the client countdown is not authoritative; and — because the window is simultaneously the hold duration — a longer window means a longer period during which the resource is unbookable by everyone else. A test measuring resource-unavailability-minutes attributable to holds should be added once the value is fixed.

**Nonexistent-time policy** (PRD open question 2). TZ-05 assumes shift-forward. If the decision changes to skip or reject, TZ-05's assertion changes but its structure does not: detection must occur, policy must apply deterministically, and the outcome must be disclosed in `time_adjustments`. Silent guessing fails under every candidate policy.

**Series bounds and query bounds** (open questions 3, 4). REC-06 and §10 test the currently specified 100 / 365 / 92 values. If any changes, only the boundary numbers change.

**Idempotency retention** (open question 5). IDEM-04 verifies the contract at 24h. A different value changes the constant, not the test.

**Abuse thresholds** (RFC §18). SEC-02 verifies the limiter is per-principal and boundary-correct. The threshold itself is unset; the test must be parameterized, not hard-coded.

**tzdata conflict resolution** (PRD FR13b). TZ-08 verifies v1's behavior: flag and notify, never auto-resolve. If an automated policy is later adopted, TZ-08 must be extended rather than replaced — "never silently dropped" remains binding under any policy.

## 16. Known Testing Gaps

Honesty here matters more than the appearance of completeness.

- **Production human-concurrency patterns are not fully replicated.** This document's barrier-released simultaneity is arguably *harder* than most real human contention — it deliberately maximizes the chance of entering the race window. But "harder in the dimension we modeled" is not "covers every dimension." A specific client retry behavior, browser request-batching quirk, or usage pattern nobody modeled could still exist. The production reconciliation and schema-assertion checks (§9) are the ongoing compensating control — not a one-time pre-launch test.
- **Real-world clock skew and adversarial network conditions** beyond what a standard test environment produces are not exhaustively modeled. The architecture already treats the server clock as authoritative for everything correctness-relevant (`joined_at`, `expires_at` are server-set), which bounds the blast radius of client clock issues — but genuinely pathological network conditions remain a long tail this document does not claim to exhaust.
- **tzdata staleness (TZ-03) is only partially testable pre-production by definition:** you cannot fully validate "our reference data is wrong" using correct reference data. The compensating control is the periodic version-check alert, not a pre-release gate.
- **CONC-06's ceiling is a characterization, not a verified safe limit.** Real traffic at a genuinely popular resource may differ from the synthetic escalation profile.
- **A real DST transition cannot be fully simulated.** TZ-01 through TZ-10 use controlled dates and clocks, which verifies the *logic*. It does not verify the system's behavior when a transition occurs during live operation with in-flight requests and running background jobs. PRD §13 requires the pilot period to span a real transition, or include a rehearsed clock-advance exercise in a production-like environment. This is a rollout precondition, not something this document can close.
- **Spike S1 dependency.** If `btree_gist` proves unavailable on the target platform (RFC S1.1), the schema is void, RFC Candidate D becomes the approach, and this entire test plan requires rewriting — SERIALIZABLE/SSI has different failure modes (serialization aborts, retry loops, false positives under predicate lock escalation) that none of these tests exercise.
- **Long-term index behavior** — GiST bloat and maintenance cadence under months of sustained write load — cannot be compressed into a pre-release test. Deferred to the Rollout & Runbook document with monitoring thresholds.

*End of document.*
