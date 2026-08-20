# Rollout & Runbook
## Concurrency-Safe Resource Booking Engine

| | |
|---|---|
| **Document version** | 1.0 |
| **Status** | Draft for Review |
| **Supersedes** | v0.1 (written against RFC v0.1 / Spec v0.1 / Test Plan v0.1) |
| **Builds on** | PRD v1.0, RFC v1.0, API & Data Design Spec v1.0, Test Plan v1.0 |
| **Audience** | The engineer launching this system, and — more importantly — the on-call engineer six months from now who has never read the RFC |

---

## 0. Revision History

**v1.0 — changes from v0.1.** v0.1 was written against upstream v0.1 documents and does not account for holds, which are rows in the `booking` table occupying the exclusion domain.

| # | Change | Reason |
|---|---|---|
| 1 | Hold release added to the rollback procedure (§4.5). | v0.1's rollback left `status='held'` rows in place while removing both the reaper and cleanup-on-write. Every held slot would have become permanently unbookable — a silent availability outage with no error anywhere. |
| 2 | Constraint-retention decision added to rollback (§4.6); v0.1's "structurally valid for reads" claim removed. | That claim came from PRD v0.1 §13, which PRD v1.0 struck as unverifiable. The real hazard — a rolled-back application writing against a database that still enforces the constraint — was unaddressed. |
| 3 | All six background checks monitored (§6); `schema_assertion` promoted to SEV-1. | v0.1 monitored only the waitlist sweep. `hold_reaper`, `series_materialization`, `tzdata_rematerialization`, and `schema_assertion` were unmonitored. All fail silently. The schema assertion detects the cause (constraint gone) where reconciliation detects the consequence (someone already double-booked). |
| 4 | RUNBOOK-01 gains a new cause #1: predicate narrowing. | If someone removes `'held'` from the constraint predicate, the constraint still exists, `pg_constraint` still returns it, the schema assertion still passes, and holds silently stop reserving anything. v0.1's diagnostics would not have caught it. |
| 5 | New runbook entries: failover 503s (RUNBOOK-05), held-slot unavailability (RUNBOOK-06), Redis outage (RUNBOOK-07), tzdata staleness (RUNBOOK-08), audit attribution gaps (RUNBOOK-09). | PRD M14, RFC §4.3, PRD R2, and Spec §3 all define behaviors with no operational response defined. |
| 6 | RUNBOOK-02's manual remediation corrected to release holds, not only offers. | Expiring an offer without releasing its hold leaves the slot blocked. The offer looks resolved; the resource stays unavailable; nothing surfaces it. |
| 7 | §8 queries corrected to include `status='held'`; idempotency lookup re-scoped to `(user_id, key)`; re-materialization and offboarding entries added. | v0.1's queries filtered on `status='confirmed'` and would return nothing when a hold was the blocker — the most confusing possible answer to "why did my booking fail?" |
| 8 | Stage 3 now requires crossing a real DST transition (§3). Thresholds aligned to v1.0 (reaper 30s, `lock_timeout` 3s, 92-day query bound, 200-concurrency figures). | PRD v1.0 §13 calls shipping recurrence without watching a real boundary pass "the highest-risk shortcut available on this project." |

## 1. Rollout Philosophy & Objectives

The Test Plan proved this system handles synthetic, deliberately-maximized concurrency correctly. It explicitly could not prove — and said so directly, Test Plan §16 — that it handles the full messy variety of real human usage: real client retry quirks, real network conditions, real usage patterns at real scale. That is not a gap this document apologizes for; it is a gap no pre-launch test plan can close by definition. Closing it safely is this document's entire job.

The mechanism is staged, reversible exposure. Each stage in §3 exposes the system to a real condition the previous stage could not produce, while bounding how much can go wrong if something the Test Plan didn't anticipate turns out to be real. This continues the phasing PRD §13 already committed to rather than introducing a new idea at the last minute.

Two things change character at launch and are worth naming.

First, the reconciliation and schema-assertion checks stop being pre-launch tests you run once and become the live, continuous proof that the guarantee is holding under conditions nobody scripted. Every stage below treats an unbroken clean record on both as non-negotiable to advance.

Second — and this is new in v1.0 — the system now carries six background jobs whose shared failure mode is silence. They do not error. They simply stop, and the system continues serving traffic that looks entirely healthy while resources quietly become unbookable, waitlists stop moving, and recurring bookings stop generating. §6 exists primarily for this class of failure, because it is the class an on-call engineer will never notice unaided.

The third objective: this document has to work for someone who didn't write it. §6 through §8 assume zero prior context on this system's design decisions, under time pressure, at an hour when nobody is at their best. That is the actual bar, not a figure of speech. Where a step requires knowing *why* something is true, the why is stated inline rather than cross-referenced.

## 2. Pre-Launch Readiness Checklist

Every item must be independently confirmed before Stage 0 begins. A monitor wired up after launch protected nothing during the highest-risk window.

### 2.1 Test Plan verification (confirmed already passed, not re-run)

- [ ] Test Plan §14's full hard-blocker list passing, specifically including:
  - [ ] CONC-01 — 100 consecutive runs at N=200, plus the N=500 escalation
  - [ ] HOLD-01 — a direct booking loses to an outstanding offer (if only one test result is checked, this is the one)
  - [ ] RECLAIM-01 — self-healing verified with the reaper stopped
  - [ ] RECLAIM-04 — zero deadlocks from cleanup-on-write under load
  - [ ] IDEM-07 — transaction boundary verified by fault injection
  - [ ] IDEM-08 — a retried lost-response request shows the user their own booking
  - [ ] AUD-01, AUD-02 — append-only enforced at grant level; triggers unbypassable
  - [ ] TZ-05 through TZ-09 — nonexistent, ambiguous, re-materialization, conflicting re-materialization, southern hemisphere
  - [ ] FAIL-01 — failover returns 503, never a hang; retry-with-same-key resolves unambiguously
  - [ ] RECON-07 — every alert fired at least once by deliberate injection
- [ ] CONC-06 escalation data collected and reviewed. Not a pass/fail gate, but §6 and §9 both need the numbers to exist.

### 2.2 Environment and dependencies — verified on the actual production target

- [ ] Migrations applied to production and independently re-verified against the live database:

```sql
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conname = 'no_overlapping_bookings';
```

The predicate must read `WHERE ((status = ANY (ARRAY['confirmed'::text, 'held'::text])))`. A migration reporting success is not the same as the constraint existing with the correct definition — and a predicate missing `'held'` silently disables every waitlist guarantee while looking entirely healthy. See RUNBOOK-01 cause #1.

- [ ] `btree_gist` confirmed enabled on the actual provisioned production database — not assumed because CI and staging worked. RFC Spike S1.1 flags this as project-blocking, and production is sometimes a materially different tier than staging with different extension permissions.
- [ ] Session settings confirmed on the write path: `lock_timeout = '3s'`, `statement_timeout = '10s'`, `idle_in_transaction_session_timeout = '30s'`.
- [ ] SSO/OIDC confirmed with a real live auth handshake against production — not configuration presence.
- [ ] Notification provider confirmed with an actual end-to-end test send received.
- [ ] Read replica live, streaming, within the lag bound, and the lag-degradation path exercised (Test Plan FAIL-03) before any read traffic is routed to it.
- [ ] PgBouncer configured to match the topology validated in Test Plan §13 — not a default assumed equivalent.
- [ ] Celery workers and the beat scheduler confirmed running. A silently-absent beat scheduler is the exact failure mode of Test Plan WL-05. Verifying now, before real holds exist, is cheap; discovering it via a user's expired-but-never-cascaded offer is not.
- [ ] Deployed tzdata version recorded, and confirmed to match what `recurring_series.tzdata_version` will be written with (Test Plan TZ-03 Test A).
- [ ] Audit `app_role` grants verified: INSERT and SELECT on `audit_log`, no UPDATE, no DELETE (Test Plan AUD-01).

### 2.3 Monitoring — configured and proven to fire

- [ ] Every alert in §6 wired to its actual paging path.
- [ ] Every alert deliberately triggered once in a controlled environment and confirmed to page the intended target. An alert that has never fired is an unverified assumption, not a safety net (Test Plan RECON-07).
- [ ] All six `system_check_run` heartbeats emitting: `reconciliation`, `schema_assertion`, `hold_reaper`, `offer_cascade`, `series_materialization`, `tzdata_rematerialization`.

### 2.4 Process readiness

- [ ] Rollback procedure (§4) rehearsed end-to-end at least once in a non-production environment — including §4.5's hold release, which is the step most likely to be forgotten under pressure.
- [ ] On-call rotation staffed, and every member walked through this document directly, not merely sent a link.
- [ ] The RUNBOOK-01 escalation path (§10) confirmed with the named facilities/operations contact, who has agreed to be reachable for it.

## 3. Staged Rollout Plan

Failing a stage's criteria means holding at current exposure while root-causing — not proceeding on a delay, and not automatically triggering §4's rollback unless the failure meets that section's severity bar.

### Stage 0 — Internal dogfood

Exposed to: engineering and immediate stakeholders, on a small number of *real* resources (the team's own meeting rooms). Minimum duration: 1 week.

What this learns that the Test Plan couldn't: first contact with genuine human usage. Does conflict messaging, waitlist behavior, and the overall flow actually feel right, independent of passing synthetic assertions.

**Go/no-go:**

- Reconciliation and schema assertion clean for the full week, zero exceptions.
- All six background heartbeats healthy for the full week.
- Zero P0/P1 incidents.
- At least one hold created and accepted end-to-end — verifying the waitlist path works with real humans, not just tests.

Honesty check: at this volume, P95 latency is not a meaningful signal — the sample is too small for a percentile to mean anything. Advancement is gated on correctness and incident count. The quantitative NFR bar becomes meaningful at Stage 1.

### Stage 1 — Single pilot category, one team, parallel with the existing process

Exposed to: one real department, one resource category, running in parallel with the legacy process — matching PRD §13's explicit parallel-running intent, specifically so a direct conflict-rate comparison is possible. Minimum duration: 2–4 weeks — long enough to observe multiple real weekly cycles, since recurring bookings are weekly and waitlist cascade needs more than a handful of real contested moments to say anything.

What this learns: real contention at a volume dogfood can't produce; a genuine before/after baseline; the first statistically meaningful percentiles.

**Go/no-go — every item required:**

- Reconciliation and schema assertion clean for the entire window, zero exceptions. Not "mostly clean."
- P95 booking write < 300 ms; P95 availability read < 500 ms (nominal load — see §6 on why contested-slot latency is measured separately).
- Waitlist dispatch P95 < 5 s, including during any real burst.
- Zero `slot_unavailable` responses on the offer-confirmation endpoint. Per Spec §5.13 this response cannot occur if holds are working; a single occurrence means the hold mechanism is broken and is a stage blocker, not a metric to note.
- Direct conflict-rate comparison against the legacy system shows improvement — the actual comparison PRD §13 calls for, not an assumption it would.
- Pilot-user qualitative feedback on conflict-message clarity collected and reviewed. PRD R6 names this: a technically correct but generically-worded message erodes trust even when the system behaved perfectly. This requires real users, not internal QA — internal testers already know what the system does.

### Stage 2 — Full category, broader user base

Exposed to: the complete resource category org-wide, broader user population. Minimum duration: 2–4 weeks.

What this learns that Stage 1 couldn't: real scale effects on hot-resource contention. Test Plan §16 flags CONC-06's characterization as synthetic and uncertain until compared against real popular-resource behavior. This is the first stage with enough real resources to make that comparison (feeds §9).

**Go/no-go:**

- Same zero-tolerance reconciliation and schema-assertion bar.
- Latency targets holding at meaningfully higher volume.
- All six background heartbeats clean for the entire period — validating that the detection mechanisms stay quiet under real conditions, not just synthetic seeding.
- 503 rate on the booking path characterized and understood — distinguishing lock-timeout 503s (contention) from failover 503s (infrastructure). See RUNBOOK-05.
- Hold lifecycle metrics reviewed: created, accepted, expired, reclaimed. Any hold surviving materially past its expiry indicates reclamation trouble.

### Stage 3 — Full rollout

Exposed to: all users, plus any additional resource categories.

**Go/no-go — including one precondition that cannot be substituted:**

- Everything above holding; explicit sign-off (§10).
- A real DST transition has been crossed in production with recurring series live, OR a rehearsed clock-advance exercise has been completed in a production-like environment.

**Why this is non-negotiable.** PRD §13 calls shipping recurrence without watching a real boundary pass "the highest-risk shortcut available on this project." The DST failure mode is *silent*: no error, no alert, no exception — a weekly meeting simply starts appearing an hour off, and the first signal is a user saying "the calendar is wrong." Test Plan TZ-01 through TZ-10 verify the *logic* under controlled clocks; they cannot verify behavior when a transition occurs during live operation with in-flight requests and running background jobs. If the pilot window cannot span a real transition, the rehearsal substitutes — but one of the two must happen.

**Settling period.** Reaching 100% exposure does not end heightened monitoring. A minimum 2-week settling period at full exposure, still held to the zero-tolerance bar, precedes downgrading monitoring cadence to standard operations. Launch is a process with a defined end, not a flip that is immediately "done."

## 4. Rollback Plan

### 4.1 Why rollback has an unusually clean answer here — and one unusually dirty corner

In most systems correctness is scattered across application code, so "roll back" implicitly means "revert everything and hope the old combination was correct."

Here the guarantee lives entirely in the schema. The application was never responsible for preventing a double-booking. Consequence: rolling back the application deploy while leaving the schema untouched leaves the correctness guarantee completely intact, regardless of which application version runs. That is the common case and it is genuinely lightweight.

The dirty corner, new in v1.0: holds. The application is responsible for *removing* hold rows — via the reaper and via cleanup-on-write. Roll back the application and both disappear, while the hold rows remain in the constraint's domain. §4.5 exists entirely because of this.

### 4.2 What triggers a rollback

**SEV-1 — immediate rollback consideration:**

- Any confirmed reconciliation failure not explained by test injection (RUNBOOK-01).
- Any confirmed schema-assertion failure — the constraint is absent or its predicate is wrong (RUNBOOK-01 cause #1).
- Sustained, broad inability to create bookings at all.

**SEV-2 — rollback considered, not automatic:**

- Genuine sustained P95 breach beyond target (not a single spike).
- Hold reclamation confirmed stalled beyond the defined window with resources unbookable (RUNBOOK-06).
- An actively exploited security finding.

**Explicitly not a rollback trigger on its own:**

- A single hot resource approaching or exceeding its throughput ceiling. Correctness is fully intact even when one resource's throughput degrades, and the named mitigation (add a second instance of that resource) is a targeted fix, not a reason to revert the system.
- An elevated 503 rate during failover. That is the system behaving correctly (RUNBOOK-05).

Severity should track *which guarantee is actually threatened*, not just "a metric looks bad."

### 4.3 What actually gets rolled back

**Application only (the common case).** Revert to the previous known-good version. No data risk, no correctness risk — provided §4.5 runs.

**Schema rollback (the hard case).** Applies only when the suspect change touched `no_overlapping_bookings` directly. Any window where the constraint is absent, even briefly, is a window where a real double-booking can occur. Never routine, never automated. Follows §5's maintenance-window discipline, with reconciliation run immediately before and after to bound and confirm nothing happened during the gap, and requires system-owner sign-off — not an on-call engineer acting alone.

### 4.4 The cheap rollback, and exactly when it stops being available

During Stages 0–2 the legacy process remains operational in parallel. "Rollback" can be as simple as routing users back to the process that never stopped running — essentially no data-migration burden, since the old system was never dependent on the new one.

This option disappears the moment the legacy system is decommissioned, or once meaningful volume of real user actions — cancellations, edits, waitlist confirmations — exists only in the new system. Past that point, rollback stops meaning "flip back to a known-good path" and starts meaning "reconcile real user data into whatever becomes authoritative" — a data-migration project, not an operational decision.

Once there, the default response to a serious bug should be fix-forward. This document exists partly to ensure that transition is a decision someone made, not a fact nobody noticed had already happened.

### 4.5 Hold release — mandatory step, easily forgotten ★

New in v1.0. If you skip this step, rollback creates a silent availability outage.

Hold rows (`status='held'`) sit in the exclusion domain. They are removed by the reaper and by cleanup-on-write, both of which live in the application you are about to roll back. Roll back without releasing them and every held slot becomes permanently unbookable — not by any system's logic, but because a row occupies the constraint's domain with nothing left alive to remove it. No error is raised anywhere. The slot simply appears busy forever.

Before or immediately after rolling back the application:

```sql
-- 1. Inventory first — you need this list to notify people.
SELECT b.id, b.resource_id, b.user_id, b.time_range, b.expires_at,
       o.id AS offer_id, o.waitlist_entry_id
  FROM booking b
  LEFT JOIN waitlist_offer o ON o.hold_booking_id = b.id
 WHERE b.status = 'held';

-- 2. Release the holds.
UPDATE booking
   SET status = 'cancelled',
       expires_at = NULL,
       cancelled_at = now(),
       cancellation_reason = 'System rollback — hold released'
 WHERE status = 'held';

-- 3. Resolve the corresponding offers.
UPDATE waitlist_offer SET status = 'expired' WHERE status = 'active';

-- 4. Return the waitlist entries to 'waiting' so people keep their place.
UPDATE waitlist_entry SET status = 'waiting' WHERE status = 'offered';
```

Set the audit session variables before running these (`app.actor_type='system'`, `app.reason='rollback'`) so the audit trail attributes them correctly rather than recording `actor_type='unknown'`.

Then notify the affected users explicitly. An offer that lapsed because of a system rollback must not be indistinguishable from an ordinary expiry. Tell them plainly what happened. Step 4 preserves their queue position so they can be re-offered once the system is healthy — losing their place because of an operational decision they had no part in would be a second, avoidable harm.

### 4.6 The constraint-retention decision — make it now, not during an incident ★

v0.1 claimed data created by this system "remains structurally valid for reads under an older system." PRD v1.0 struck that claim — it describes a system that is not named or specified and cannot be verified. The real hazard is the opposite of what that sentence implied.

If you roll back the application while the constraint remains on a shared database, writes the older system would have accepted now fail. The old application has no concept of `no_overlapping_bookings` and no handling for SQLSTATE 23P01; overlapping writes it considered legitimate will surface as unexplained 500s.

Two options, neither free:

| Option | Consequence |
|---|---|
| Retain the constraint | The guarantee holds. The rolled-back application's overlapping writes fail with errors it does not understand. Acceptable if the old system also never intended to double-book; painful if it relied on permissive writes. |
| Drop the constraint | The old application works. The guarantee is surrendered — overlapping data can be created and must be fully reconciled before rolling forward, because re-adding the constraint against overlapping rows will fail validation. |

Decide which applies to your legacy system before Stage 0, and record it here. Choosing between these under incident pressure, at 3am, without a prior decision is the worst possible moment to think about it.

**Default recommendation: retain the constraint**, and treat the old system's write failures as the signal that fix-forward is the better path.

### 4.7 In-flight state during rollback

- Confirmed bookings created in the new system are real and valid. During Stages 0–2 this is a non-issue, since the legacy system was never displaced. After decommissioning, they must be explicitly reconciled into whatever becomes authoritative — silently leaving them only in the rolled-back-from system risks the exact double-booking this project exists to prevent, now via the *other* system not knowing the slot is taken.
- Active holds and offers: §4.5.
- Recurring series: because each occurrence is an independently committed row rather than one all-or-nothing transaction (RFC §15b), there is no half-committed series state. Created occurrences remain valid; ungenerated ones simply do not generate until rollback resolves. One caveat: `recurring_series.materialized_through` is a watermark the rolled-back application may not understand. On rolling forward, verify materialization resumes from the correct point rather than skipping a window.
- In-flight idempotency keys: rows left at `status='in_progress'` from transactions killed mid-flight. Harmless — they expire on the 24-hour retention cycle. A client retrying against a rolled-back application simply gets fresh handling.

## 5. Deployment Mechanics

### 5.1 Two different meanings of "rollout," deliberately kept separate

§3's staged rollout is about *which real users see the new booking flow*. This section is about *how new code reaches production infrastructure*.

These are orthogonal. A bug-fix release can be deployed to 100% of production infrastructure on the same day the user-facing rollout sits at Stage 1. Conflating "what percentage of our infrastructure runs the new version" with "what percentage of our users can see the feature" is exactly the documentation mistake this separation exists to avoid.

### 5.2 Infrastructure deploy strategy

Canary deployment for the stateless API service: deploy alongside the current version, route a small percentage of infrastructure traffic to the new instances, monitor error rate and latency at the infra level (independent of §3's staging), promote to 100% only after a clean canary window.

One deploy-specific check for this system: after any deploy, confirm all six background heartbeats resume within one interval. A deploy that leaves the beat scheduler down produces no error and no failed health check — it produces silence, and the first symptom is a user's offer never cascading.

### 5.3 Feature-flag strategy

New code is deployed to 100% of production infrastructure while remaining dark for anyone outside the §3-designated population, controlled by a flag rather than by what is deployed. Advancing a rollout stage becomes a flag change, not a deploy.

### 5.4 Migration sequencing for the exclusion constraint

Initial creation against an empty table carries no sequencing risk. A future migration modifying `no_overlapping_bookings` against a live table under real write traffic is materially different, and worth being concrete about:

- `ALTER TABLE ... ADD/DROP CONSTRAINT ... EXCLUDE` takes an ACCESS EXCLUSIVE lock for the duration of validating against existing rows. On a large, actively-written table this is real, application-visible blocking, not a theoretical concern.
- Unlike CHECK and foreign-key constraints, Postgres offers no NOT VALID fast-add path for EXCLUDE constraints — they are GiST-index-backed and there is no way to add one without a validation pass. Being honest about this limitation matters more than pretending a zero-downtime trick exists.
- Safe pattern: build any new supporting index with `CREATE INDEX CONCURRENTLY` first (this part genuinely avoids blocking), verify it, then run the constraint-attaching DDL during a scheduled low-traffic window — informed by real usage data from §3/§9, not guessed.

Because this constraint is the entire correctness mechanism, any migration touching it requires:

1. A maintenance window during genuinely low booking activity.
2. All active holds released first (§4.5's queries) — a hold present during constraint validation can cause the validation to behave unexpectedly, and holds are transient state that should not gate a schema change.
3. Reconciliation run immediately before and after the window, to bound and confirm nothing happened during the gap.
4. Predicate verified after with `pg_get_constraintdef` — confirming `'held'` is still present. See RUNBOOK-01 cause #1.
5. Explicit system-owner sign-off (§10). This is never a routine automated migration.

This heightened process is scoped specifically to `no_overlapping_bookings`. Ordinary schema evolution — a new nullable column, a new table, an unrelated index — follows standard low-risk practice. Treating every migration with this ceremony would be inaccurate and would train engineers to over-apply caution to changes that do not need it.

## 6. Monitoring & Alerting Specification

### 6.1 The six background checks — one pattern, six instances

This block is new in v1.0 and covers the most dangerous class of failure in this system.

All six emit a heartbeat into `system_check_run` and surface via `GET /api/v1/admin/checks/latest`. Their shared property: they fail silently. No exception, no error rate, no failed health check — just absence. The system continues serving traffic that looks entirely healthy.

| Check | Alert when | Severity | Consequence if unmonitored |
|---|---|---|---|
| `schema_assertion` | Fails, or no successful run in 2× interval | SEV-1, immediate page to primary + secondary + system owner | The constraint is gone and nobody knows. This detects the *cause*; reconciliation detects the *consequence*, after someone has already been double-booked. This alert fires earlier and matters more. |
| `reconciliation` | `overlaps_found > 0`, any run | SEV-1, same path | Real overlaps reaching real users undetected |
| `hold_reaper` | No successful run in 90 s (3× the 30 s interval) | SEV-2 | Held slots never expire → resources silently unbookable → RUNBOOK-06 |
| `offer_cascade` | No successful run in 90 s; or any offer active past `expires_at` + 5 min grace | SEV-2 | Waitlist stops moving; users wait for offers that never come |
| `series_materialization` | No successful run in 2× its interval | SEV-2 | Recurring bookings silently stop generating past the rolling horizon. Users discover this weeks later when a meeting simply is not there. |
| `tzdata_rematerialization` | Fails; or deployed tzdata version ≠ recorded version with no run since | SEV-2 | Occurrences stay wrong after a rule change → RUNBOOK-08 |

False positive for all six: a brief heartbeat gap during a routine worker deploy or restart, which self-resolves within roughly one interval. Check deploy timing before treating any of these as real.

### 6.2 Latency, throughput, and infrastructure

| Risk area | Metric | Threshold | Severity | False positive & how to tell |
|---|---|---|---|---|
| Booking write latency | P95 on both a short rolling window (catches spike degradation) and a long window (steady trend) | Short-window P95 > 300 ms sustained > 2 min | SEV-2 | Contested-slot latency is expected to exceed this and is not a regression. Conflicting inserts block on uncommitted competitors (RFC §7.1), so losing requests queue by design. Check whether elevation is concentrated on one `resource_id` before escalating — if so, it is contention, not degradation |
| Availability read latency | P95, short and long window | > 500 ms sustained > 2 min | SEV-2 | Check replica lag first — availability reads are replica-served |
| 503 rate on the write path | Rate, split by cause | Sustained rise | SEV-2 | Two very different causes needing opposite responses. Lock-timeout 503s = contention. Failover 503s = infrastructure. See RUNBOOK-05 |
| Replica lag | Seconds behind primary | Beyond configured bound | SEV-2 | Degradation path should engage automatically (`data_freshness: "primary"`). Alert means it engaged, not that data was served stale |
| Redis availability | Broker reachability | Unreachable > 60 s | SEV-2 | Correctness is unaffected — bookings still succeed and conflict correctly. Liveness degrades. See RUNBOOK-07 |
| GiST write throughput on booking | Per-resource write latency and throughput | Set from CONC-06's real characterization data — deliberately not invented here; see §9 | SEV-3 / informational | Approaching a known ceiling is not a false alarm by definition; the tuning risk is threshold sensitivity, not signal validity |
| Idempotency-key growth | Row count trend; cleanup heartbeat | Cleanup missed > 2× interval; or unbounded growth vs. ~24h-of-volume baseline | SEV-3 | Worst case is wasted storage, not a wrong answer — contract correctness does not depend on prompt cleanup. A temporary spike during real traffic is expected |
| Audit attribution gaps | Count of `audit_log` rows with `actor_type = 'unknown'` | Any row | SEV-3 | Indicates a write path not setting session variables — a code bug, not an incident. See RUNBOOK-09 |
| AuthN/AuthZ anomalies | 401/403 rate, absolute and per-principal | Sharp spike from one principal/IP, or broad spike across many | SEV-2 | The shape distinguishes the cause. Narrow = probing. Broad = SSO provider issue, an infra incident not an abuse one |
| Rate-limit triggers | 429 rate per principal | Small number of principals each showing sustained repeated pattern | SEV-3 | A legitimate admin doing bulk work looks similar — check role first (RUNBOOK-04) |

## 7. Incident Runbook

Written for an engineer with no prior context on this system. Where a step requires knowing why, the why is stated inline.

### RUNBOOK-01 — Reconciliation or schema-assertion failure ★

The worst-case scenario this system exists to prevent. Maximum severity regardless of anything else in flight.

**Likely causes, ranked:**

1. **The constraint predicate no longer includes `'held'`.** (New in v1.0 and now the most likely cause, because it is the one that looks harmless.) Someone narrowed the predicate to `WHERE status = 'confirmed'` — a plausible-looking cleanup, since holds "aren't real bookings." The constraint still exists. `pg_constraint` still returns it. A naive schema assertion still passes. But holds stop reserving anything, and waitlisted users start losing accepted offers to ordinary bookers. Reconciliation will not catch this either, since no two confirmed rows overlap.
2. A migration or deploy dropped or altered the constraint without following §5.4.
3. A raw database operation bypassed the application. Note this should be near-impossible while the constraint genuinely exists, since Postgres enforces it regardless of write path. If this appears to be the cause, it strongly implies cause #1 or #2 already happened — the constraint was absent or wrong when the write occurred, not "evaded" while present.
4. A bug in the reconciliation query itself. Check whether the flagged rows are adjacent-but-touching — one booking's end exactly equals another's start. `tstzrange` defaults to `[)` bounds (inclusive start, exclusive end), so two such bookings are not overlapping and must not be flagged. A query whose overlap definition does not match this semantics produces a false alarm that is not a double-booking at all.

**Diagnostic steps:**

```sql
-- 1. Confirm this is production, not a test environment running deliberate injection.

-- 2. THE MOST IMPORTANT CHECK — the full definition, not just existence.
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conname = 'no_overlapping_bookings';
-- The predicate MUST read:
--   WHERE ((status = ANY (ARRAY['confirmed'::text, 'held'::text])))
-- If 'held' is missing, that is your root cause. Go to remediation step 2.

-- 3. Get the actual conflicting rows — note this includes 'held'.
SELECT id, resource_id, user_id, time_range, status, expires_at, created_at
  FROM booking
 WHERE resource_id = :flagged_resource
   AND status IN ('confirmed','held')
 ORDER BY time_range;

-- 4. Check the boundary-adjacency false positive (cause #4) before assuming a real overlap.
--    Compare exact upper/lower bounds of the flagged rows.

-- 5. Who wrote them, and was the constraint correct at that moment?
SELECT actor_id, actor_type, reason, request_id, occurred_at, after_state
  FROM audit_log
 WHERE entity_type = 'booking'
   AND entity_id IN (:flagged_ids)
 ORDER BY occurred_at;
-- The audit trail is the forensic record. pg_stat_activity shows only CURRENT
-- connections, not history — it will not tell you who wrote these rows.

-- 6. Cross-reference migration and deploy timestamps against the rows' created_at.
```

**Remediation:**

1. If cause #4 (query bug): fix the overlap logic, re-run against current data, and re-examine historical "clean" runs — they may have been hiding real findings or producing false ones.
2. If cause #1 (predicate narrowed): restore the correct predicate immediately, following §5.4. Then check for damage the narrowed window allowed: any waitlisted user whose accepted offer failed during that period. This is the highest-priority migration this system will ever run.
3. If a true overlap exists: this is a real double-booking that already reached real people. Contact the affected users and the resource administrator directly — one booking must be honored and the other rescheduled with an apology. This document cannot automate away a scheduling conflict that already happened in the real world; it can only ensure it is caught and handled fast.
4. Regardless of root cause: mandatory post-incident review with system-owner sign-off before the affected resource or rollout stage continues at its current exposure (§10).

### RUNBOOK-02 — Stalled hold reaper or offer cascade

**Likely causes, ranked:**

1. Celery beat scheduler down — a single process, a known single point of failure by design.
2. Workers alive but backlogged — tasks enqueued, not consumed fast enough. Distinct from beat being down.
3. Redis unreachable — beat and workers both running but unable to communicate. See RUNBOOK-07.
4. A bug in the task causing it to error on every invocation rather than not running.

**Diagnostic steps:**

```sql
-- Size the actual user impact, regardless of root cause.
SELECT count(*) AS stale_holds, min(expires_at) AS oldest
  FROM booking
 WHERE status = 'held' AND expires_at < now();

SELECT count(*) AS stale_offers, min(expires_at) AS oldest
  FROM waitlist_offer
 WHERE status = 'active' AND expires_at < now();
```

Then: check the beat process via your orchestration platform; check worker queue depth for the reaper's queue (growing backlog with beat alive = cause #2); PING Redis; check worker logs filtered to the task name for repeated exceptions.

**Remediation:**

1. Beat down → restart, confirm the schedule resumes and the heartbeat clears.
2. Workers backlogged → scale worker count; check for a co-located expensive task competing for the same pool.
3. Redis degraded → RUNBOOK-07. The reaper symptom is downstream, not the root cause.
4. Task erroring → needs a code fix.

**Manual backlog clearing — corrected in v1.0.** ⚠️ Expiring an offer without releasing its hold leaves the slot blocked. The offer looks resolved, the resource stays unavailable, and nothing surfaces it. Clear both, in this order, using the exact conditional pattern the code uses:

```sql
BEGIN;
SET LOCAL app.actor_type = 'system';
SET LOCAL app.reason = 'Manual reaper backlog clearing — incident <ID>';

-- 1. Release the holds FIRST — this is what actually frees the slots.
DELETE FROM booking
 WHERE status = 'held' AND expires_at <= now();

-- 2. Then resolve the corresponding offers.
UPDATE waitlist_offer
   SET status = 'expired'
 WHERE status = 'active' AND expires_at <= now();

-- 3. Return the entries to 'waiting' so people keep their queue position.
UPDATE waitlist_entry we
   SET status = 'waiting'
 WHERE status = 'offered'
   AND NOT EXISTS (SELECT 1 FROM waitlist_offer o
                     WHERE o.waitlist_entry_id = we.id AND o.status = 'active');
COMMIT;
```

Never omit the WHERE clauses, even by hand. Without `expires_at <= now()` you will delete holds that are still live and race a real user's in-flight confirmation.

Note: cleanup-on-write means booking traffic partially self-heals this — a user booking a range with an expired hold clears it themselves. Manual clearing is for slots nobody is currently trying to book, and for restoring cascade.

**Severity:** SEV-2. Does not warrant RUNBOOK-01's elevated response, but resolve same-day — real waitlisted users are affected.

### RUNBOOK-03 — Booking latency spike

**Likely causes, ranked:**

1. A legitimate traffic spike at a contested moment — the pattern PRD A1 explicitly names. Check this first, before assuming regression.
2. Contention on a single hot resource. Remember: conflicting inserts block on uncommitted competitors, so losing requests queue by design. Elevated latency on a contested slot is expected behavior, not a bug.
3. Connection pool exhaustion (PgBouncer undersized).
4. A recent deploy introduced a genuine regression — N+1 query, missing index on a new access pattern.
5. Elevated replica lag — only relevant if the spike is on availability reads. Booking writes always hit the primary, which is itself a useful discriminator.

**Diagnostics:** correlate timing with real calendar context (top of hour, known popular slot); check whether latency is concentrated on a few `resource_id`s (cause #2) or spread broadly (cause #3/#4); check PgBouncer pool utilization and wait time; check deploy history; if availability-specific, check replica lag.

**Remediation:**

1. Legitimate spike resolving within the window → no action.
2. Hot resource → the CONC-06-anticipated case. The named mitigation (add a second instance of that resource) is a product/ops decision (§10), not an emergency code change.
3. Pool exhaustion → tune per the topology validated in Test Plan §13.
4. Regression → roll back the application deploy. Per §4 this is the lightweight case — but §4.5's hold release still applies.
5. Replica lag → RUNBOOK-05's replica section.

### RUNBOOK-04 — Suspected mass-booking / abuse

**Likely causes, ranked:**

1. Genuine scripted abuse — systematic, faster-than-human claiming of popular slots.
2. A legitimate power-user or admin workflow that looks similar (bulk setup, internal automation).
3. A client-side bug causing a retry storm — looks like abuse in raw metrics, needs a completely different, non-adversarial response.

**Diagnostics:**

1. Identify the `user_id`.
2. Check `platform_role` and `resource_admin` grants — quickly rules out cause #2.
3. Inspect the request pattern: different slots each time (hoarding) vs. the same request repeated. Check `Idempotency-Key` reuse across the burst — if the client correctly reuses one key per logical action, the idempotency contract already prevents multiple real bookings, narrowing real concern to genuinely different keys for what is actually the same action.
4. Check booking *success* rate, not request volume. Heavy 429/409 traffic with few real 201s means the defenses are working (lower severity). High volume of successful claims is the higher-severity case.

**Remediation:**

1. Confirmed abuse with real hoarding → temporarily suspend booking privileges, review and cancel clearly abusive bookings. A human/policy decision escalated per §10, likely involving the resource administrator.
2. Legitimate power-user → adjust the limit for that case and document the exception so it is not re-flagged.
3. Client bug → file against the client. The idempotency contract is containing the damage while the fix ships. This is not a security response.

### RUNBOOK-05 — Elevated 503 rate on the booking path ★

New in v1.0. The counterintuitive one: a 503 spike may be the system working exactly as designed.

**Two causes needing opposite responses.**

**Cause A — lock timeout under contention.** Requests waiting more than 3 s on a competing uncommitted transaction fail with SQLSTATE `55P03` → 503 `service_unavailable` + `Retry-After`. This is correct behavior. The alternative — hanging — would leave users unsure whether their booking committed. Expect this during genuine contention spikes.

**Cause B — primary failover.** PRD M14 requires writes to fail fast and unambiguously during failover. A 503 spike during failover is the requirement being met, not a defect.

**Distinguishing them:**

| | Lock timeout | Failover |
|---|---|---|
| Distribution | Concentrated on one or few `resource_id`s | Broad across all resources |
| Database | Primary healthy, `pg_stat_activity` shows waiting locks | Primary unhealthy or mid-promotion |
| Correlation | Peaks at contested moments | Correlates with infrastructure events |
| SQLSTATE | 55P03 | Connection-level errors |

**Remediation:**

- Cause A: verify the guarantee is intact (`SELECT count(*)` on overlapping active rows for the affected resource — must be ≤ 1). If contention is chronic, the fix is product-level: add a second instance of that resource. Do not raise `lock_timeout` reflexively — a longer timeout means users wait longer before learning the outcome, and the current value was set deliberately against the latency budget (RFC §7.1).
- Cause B: treat as an infrastructure incident. The booking path is behaving correctly. Confirm the client-side contract holds: clients retrying with the same `Idempotency-Key` receive their original outcome, not a duplicate and not a false "unavailable."

**What to tell stakeholders:** users saw retryable errors, not lost bookings. Anyone who retried correctly got the right answer. No booking was silently lost or duplicated — that is precisely what the idempotency mechanism exists to guarantee.

### RUNBOOK-06 — Resources appear unavailable but no bookings exist ★

New in v1.0. The symptom of hold-reclamation failure, and it looks like nothing at all.

**Symptom:** users report a slot as busy; the calendar shows a busy block; no confirmed booking exists for it.

**Cause:** an expired hold that was never reclaimed. Holds occupy the exclusion domain by design (that is what makes waitlist offers enforceable), and appear as ordinary busy blocks in availability views (Spec §5.7 deliberately does not expose hold state, to avoid leaking queue information). An expired hold blocks bookings until something removes it.

**Why it can persist:** a constraint predicate cannot reference `now()` — Postgres requires index predicates to be IMMUTABLE — so expiry cannot be expressed in the constraint. Expired holds must be actively reclaimed.

**Diagnostic:**

```sql
SELECT b.id, b.resource_id, b.user_id, b.time_range, b.expires_at,
       now() - b.expires_at AS overdue,
       o.id AS offer_id, o.status AS offer_status
  FROM booking b
  LEFT JOIN waitlist_offer o ON o.hold_booking_id = b.id
 WHERE b.status = 'held'
   AND b.expires_at < now()
 ORDER BY b.expires_at;
```

**Remediation:**

1. Any rows returned → the reaper is not running. Go to RUNBOOK-02.
2. Clear the immediate backlog with RUNBOOK-02's manual procedure.
3. Verify cleanup-on-write is functioning — a booking attempt on an affected range should succeed by clearing the stale hold itself (Test Plan RECLAIM-01). If it does not, cleanup-on-write is broken and the system has lost its self-healing property. Escalate: this is a code defect, not an ops issue.
4. If holds are stale but the reaper *is* running, check worker logs for the reaper task erroring on each invocation.

### RUNBOOK-07 — Redis unavailable ★

New in v1.0. The reassuring one — and the on-call engineer needs to know that immediately.

**What still works:** booking creation, conflict rejection, cancellation, editing, availability views, authentication. The exclusion constraint is untouched. No correctness violation is possible. Redis is the Celery broker, not part of any correctness path.

**What stops:** offer dispatch, cascade, reaper-driven hold expiry, rolling series materialization, notification dispatch.

**User-visible impact:** waitlisted users do not receive offers. Held slots stay blocked longer than their window — but not indefinitely, because cleanup-on-write clears an expired hold when someone next tries to book that range (RFC §4.3). This is degradation in the *safe* direction: a resource appears unavailable when it should be free, rather than a booking being lost or duplicated.

**Diagnostics:** PING Redis; check its infrastructure health; check the backlog with RUNBOOK-06's query.

**Remediation:** restore Redis (an infrastructure incident in its own right). On recovery, confirm all six heartbeats resume within one interval and the hold backlog drains. If it does not drain, RUNBOOK-02.

**Do not roll back the application for a Redis outage.** Rollback would remove cleanup-on-write, which is currently the only thing preventing held slots from blocking indefinitely — making the situation strictly worse.

### RUNBOOK-08 — tzdata staleness or re-materialization failure ★

New in v1.0. Semi-annual by nature, silent by failure mode.

**Symptom:** a recurring meeting appears at the wrong local time; or the tzdata version check alerts; or `tzdata_rematerialization` fails.

**Cause:** a jurisdiction changed its DST rules. Occurrence instants already materialized under the old rules are now wrong. Rendering them "fresh" does not fix this — it faithfully converts a wrong instant and displays the wrong time. They must be re-materialized from the series definition.

**Diagnostic:**

```sql
-- Which series were materialized under a version older than what's deployed?
SELECT id, resource_id, timezone, tzdata_version, materialized_through, created_by
  FROM recurring_series
 WHERE status = 'active'
   AND tzdata_version <> :deployed_tzdata_version
 ORDER BY timezone;

-- Most recent re-materialization run and its findings.
SELECT run_at, status, findings
  FROM system_check_run
 WHERE check_name = 'tzdata_rematerialization'
 ORDER BY run_at DESC LIMIT 5;
```

**Remediation:**

1. Deployed tzdata is stale → update it, then trigger re-materialization. Until then, affected occurrences display incorrectly. This is a data-correctness issue, not a concurrency issue — the constraint operates on stored instants regardless of what timezone data produced them, so no double-booking can result.
2. Re-materialization ran and hit conflicts → the `findings` field lists them. A re-materialized occurrence needing a slot booked in the interim cannot be silently dropped (PRD FR13b). Both the series owner and the resource administrator must be notified, and the occurrence resolved manually — reschedule the series occurrence, or negotiate with the conflicting booking's owner. v1 deliberately does not auto-resolve these.
3. Re-materialization failing entirely → check worker logs. Until fixed, affected series carry incorrect occurrence times.

### RUNBOOK-09 — Audit rows with `actor_type = 'unknown'`

**Symptom:** the audit gap monitor fires.

**Cause:** a write path reached `booking` (or another audited table) without setting `SET LOCAL app.actor_id` / `app.actor_type`. Audit triggers cannot see the authenticated principal; the service layer must propagate it.

**Not an incident — a code defect.** The write itself succeeded and the constraint held. What is lost is *attribution*, which matters later when someone asks "who cancelled my booking?"

**Diagnostic:**

```sql
SELECT entity_type, action, count(*), min(occurred_at), max(occurred_at)
  FROM audit_log
 WHERE actor_type = 'unknown'
 GROUP BY entity_type, action
 ORDER BY count(*) DESC;
```

**Remediation:** identify the write path from `entity_type` and timing; add the session-variable block. Common legitimate sources: manual operational SQL run without setting the variables (as in RUNBOOK-02 — which is why those procedures include them), and migrations touching audited tables. Neither is a bug; both should set the variables going forward.

## 8. Operational Debugging Guide

⚠️ Every query below includes `status = 'held'` where relevant. v0.1's queries filtered on `'confirmed'` alone and would return *nothing* when a hold was the blocker — the most confusing possible answer to "why did my booking fail?"

| Question | How to answer |
|---|---|
| Why did this user's booking fail? | Find the request by `user_id` + timestamp in application logs; note the `error.code` and `X-Request-Id`. If `slot_unavailable`: `SELECT id, user_id, status, time_range, expires_at FROM booking WHERE resource_id = $1 AND time_range && $2 AND status IN ('confirmed','held');` — a `held` row means a waitlist offer was outstanding, which is correct behavior, not a bug |
| "I retried and got told the slot was unavailable — did my booking go through?" ★ | `SELECT status, response_status, response_body, created_at, completed_at FROM idempotency_key WHERE user_id = $1 AND key = $2;` — note the composite key. `status='completed'` with `response_status=201` means their booking exists and the retry correctly replayed. If no row exists, the original never committed |
| Why hasn't this waitlisted user been offered a slot? | `SELECT status, time_range, joined_at FROM waitlist_entry WHERE id = $1;` Then check position: `SELECT count(*) FROM waitlist_entry WHERE resource_id = $1 AND time_range && $2 AND status = 'waiting' AND joined_at < $3;` Then check eligibility — a freed range must fully contain the entry's requested range (PRD FR21). Someone waitlisted 10:00–11:00 is not eligible for a freed 10:00–10:30. This is the most common "bug report" that is actually correct behavior |
| A resource looks busy but I see no booking ★ | RUNBOOK-06. Almost always an unreclaimed expired hold |
| Are the correctness guarantees currently healthy? | `GET /api/v1/admin/checks/latest` — all six checks in one call, no database access needed |
| Is the constraint definition still correct? ★ | `SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'no_overlapping_bookings';` — the predicate must include `'held'`. Existence alone is not sufficient |
| Did a recurring series fully create? | `SELECT id, starts_at, status FROM booking WHERE series_id = $1 ORDER BY starts_at;` compared against `SELECT occurrence_count, series_start_date, materialized_through FROM recurring_series WHERE id = $1;` — a gap between `materialized_through` and the expected end means the rolling job is behind, not that occurrences were rejected |
| Why is this recurring occurrence at the wrong time? ★ | `SELECT timezone, local_start_time, tzdata_version FROM recurring_series WHERE id = $1;` — compare `tzdata_version` against the deployed version. A mismatch means re-materialization is pending. RUNBOOK-08 |
| Is a waitlist offer still valid? | `SELECT o.status, o.expires_at, now(), b.status AS hold_status FROM waitlist_offer o JOIN booking b ON b.id = o.hold_booking_id WHERE o.id = $1;` — check both. An active offer whose hold is gone is an inconsistency worth investigating |
| What happened to this booking? | `GET /api/v1/bookings/{id}/history` — the full audit trail with actors, reasons, and request IDs. This is what "why did my booking disappear" is answered with |
| Why is a departed employee's booking still on the calendar? ★ | `SELECT offboarding_policy FROM resource WHERE id = $1;` — `'retain'` means this is configured behavior awaiting manual resolution, not a missed offboarding. Check `SELECT status, deactivated_at FROM app_user WHERE id = $1;` to confirm deactivation ran |
| Is a resource experiencing unusual contention? | Write-throughput dashboard for that `resource_id`. Quick pulse: `SELECT count(*) FROM booking WHERE resource_id = $1 AND created_at > now() - interval '1 hour';` |
| Resource utilization? | `GET /api/v1/admin/resources/{id}/utilization?from=&to=` |

## 9. Post-Launch Tuning Plan

Every item below carries a default (or, in two cases, deliberately none) inherited from an earlier document. This is where each gets a real plan — not a promise that trails off.

| Item | Data collected | Period | Decision it informs |
|---|---|---|---|
| Offer window (15 min) — PRD open question 1 | Confirm-vs-expire distribution, and how long confirming users actually take. Plus resource-unavailability-minutes attributable to holds | Stages 1–2, reviewed after ~200 real offers | Whether 15 min is too short (offers expiring that would have confirmed) or needlessly long. The window is simultaneously the hold duration, so a longer window directly means more time a resource is unbookable by everyone else — this is a utilization tradeoff, not only a UX one |
| Reaper interval (30 s) | Actual cascade latency distribution (expiry → next offer); reaper query cost | Same window | Whether the interval can widen without materially hurting cascade speed |
| `lock_timeout` (3 s) ★ | 503 rate attributable to lock timeout (RUNBOOK-05 cause A); observed transaction durations on the write path | Stages 1–2 | Whether 3 s is right. Too low = users get 503s during ordinary contention. Too high = users wait longer before learning the outcome. Tune from the observed 503 rate, never reflexively during an incident |
| Idempotency retention (24 h) | Evidence of legitimate retries arriving later than 24 h; table growth vs. cleanup | First full rollout period | Whether 24 h holds empirically; confirms no unbounded growth |
| Max concurrent bookings per principal — no default exists | Real per-principal booking-volume distribution | Rollout period, before any threshold is set | Cannot responsibly be set before this data exists. Setting it blind risks being either toothless or flagging legitimate heavy users |
| GiST write-throughput alert threshold — deliberately unset in §6 | §6's per-resource throughput on genuinely popular resources at real scale | Stage 2 onward | Sets the threshold left unset here, and validates or revises CONC-06's synthetic assumptions |
| GiST maintenance / REINDEX cadence | Index bloat against real accumulated write volume | Ongoing through Stages 2–3 | A real maintenance cadence rather than a guessed one |
| Series bound (100) and horizon (365 d) | Distribution of requested series lengths; how many hit the bound | Stages 1–2 | Whether the bounds are set where real usage sits |
| Availability query bound (92 d) | Distribution of requested ranges; how many hit the cap | Stages 1–2 | Whether 92 days matches how people actually browse |
| Nonexistent-time policy (shift-forward) — PRD open question 2 | How often it triggers; whether users find the adjustment acceptable | First spring-forward transition after launch | Whether shift-forward is right, or skip/reject is better. Only real data twice a year — do not let this quietly become permanent by default |
| Per-resource policy fields — PRD open question 7 | Not a monitoring item. Track pilot feedback explicitly requesting it | N/A | Does not resolve itself from production data. Log as a candidate for the next planning cycle rather than silently dropping it for lack of a monitoring trigger |

**Review checkpoint.** All items get a formal joint review at the end of Stage 2, and again at a fixed post-rollout interval (proposed: 3 months) — a scheduled forcing function, not a hope that someone remembers. The `lock_timeout`, offer window, and nonexistent-time policy items are specifically flagged as the ones most likely to become permanent-by-default without one.

## 10. Ownership & Escalation

**Operational ownership.** The engineering team that built this system owns it post-launch, staffed via a standard primary/secondary rotation. The Resource Administrator and Operations personas (PRD §3.2, §3.4) are downstream stakeholders, notified of relevant incidents but not paged for ordinary technical alerts — with one deliberate exception below.

**Standard escalation.** Secondary is paged if primary has not acknowledged within a defined window (proposed: 5–10 min for SEV-1, longer for lower severities).

**The correctness-failure path is deliberately not the standard path**

Applies to RUNBOOK-01 — both reconciliation failure *and* schema-assertion failure. These represent the system's core promise being violated, not an operational metric drifting.

- Primary and secondary are paged simultaneously, not sequentially. The usual "wait for primary to not-acknowledge" pattern is an acceptable trade for ordinary alerts and too slow for this one.
- The system owner is notified immediately and directly, regardless of who is on rotation.
- This incident type cannot be silently resolved and closed by on-call acting alone. It requires the post-incident review named in RUNBOOK-01, with system-owner sign-off, before the affected resource or rollout stage resumes or advances.
- A facilities/operations contact is looped in directly (PRD persona 3.4), because RUNBOOK-01's diagnosis may reveal a real double-booking affecting real people. Resolving a scheduling conflict between two humans is not something engineering can fix in the ordinary sense. This is why this alert runs a dual track — technical root-cause and human remediation — that no other alert in this document requires.

**Note on the schema assertion specifically.** It fires *before* anyone has been double-booked. That makes it the more valuable of the two alerts, not the lesser one — it is the difference between "the guarantee is gone, fix it now" and "the guarantee was gone, and here is who was harmed." Treat it with the same urgency, and resist the instinct to downgrade it because no user has complained yet.

**Decisions requiring system-owner sign-off, not on-call judgment:**

- Any migration touching `no_overlapping_bookings` (§5.4)
- Schema rollback (§4.3)
- Executing §4.5's hold release
- The §4.6 constraint-retention decision
- Closing a RUNBOOK-01 incident
- Adding a second instance of a hot resource (a product decision with cost implications)

## 11. Open Items Carried Into Operations

- **Spike S1 dependency.** If `btree_gist` proves unavailable on the production target (§2.2), the schema is void, RFC Candidate D becomes the approach, and this entire runbook requires rewriting — SERIALIZABLE/SSI has different failure modes (serialization aborts, retry loops, false positives under predicate lock escalation) that none of these procedures address.
- **The §4.6 constraint-retention decision** must be made and recorded before Stage 0. It is the only item in this document that is genuinely undecided rather than merely tunable.
- **Long-term GiST index behavior** — bloat and maintenance cadence under months of sustained write load — cannot be established pre-launch. §9 tracks it.
- **Pooled-capacity resources** (PRD §5 Non-Goal). If pooled resources arrive, the exclusion constraint does not extend to them — it enforces capacity of exactly one. That would require a second mechanism alongside, and would change nearly every procedure in this document. Flag early if the request appears.

*End of document.*
