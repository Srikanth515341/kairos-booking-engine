# Product Requirements Document
## Concurrency-Safe Resource Booking Engine

| | |
|---|---|
| **Document version** | 1.0 |
| **Status** | Approved for Technical Design (RFC) |
| **Supersedes** | v0.1 (Draft for Review) |
| **Document type** | Product Requirements — *what* and *why*. Implementation approach belongs in the RFC. |
| **Reviewers** | Staff Engineering, Engineering Management, Bar Raiser, Security, Operations |

---

## 0. Revision History

**v1.0 — changes from v0.1.** Recorded explicitly so reviewers can see what moved and why, rather than re-reading the whole document.

| # | Change | Reason |
|---|---|---|
| 1 | Timezone model corrected (§8.2). v0.1's "store everything in UTC" is wrong for recurring bookings and directly contradicted its own DST-correctness requirement. Recurrence now stores local wall-clock time + IANA zone ID; UTC instants are derived per occurrence. | v0.1 FR6 and FR7 were mutually unsatisfiable. Building to v0.1 would have shipped series that silently drift one hour at every DST transition. |
| 2 | Idempotency added (§8.6). All state-changing operations now require a client-supplied idempotency key. | v0.1's §2 correctly identified client retries as a cause of duplicate requests, then specified nothing to handle them. A retried-after-timeout booking would have returned "slot unavailable" for the user's own successful booking. |
| 3 | Hold state promoted from open question to requirement (§8.3). | v0.1's waitlist guarantee was unenforceable. A freed slot with an outstanding offer was bookable by any ordinary user, so the waitlist offered a promise the system could not keep. |
| 4 | Recurrence bounded (§8.2). Maximum series length and maximum advance horizon are now hard requirements. | v0.1 placed no bound on series expansion. A single "daily for 10 years" request would have written thousands of rows in one transaction while holding locks across an entire resource. |
| 5 | Audit trail added (§8.7). | v0.1 promised administrators booking history and users trustworthy state, with no requirement that state transitions be durably recorded. |
| 6 | Authorization model made scoped (§3, §8.8). | v0.1 specified a binary user/admin flag in scope, while its personas and dependencies both described resource-scoped and group-restricted administration. Internally inconsistent. |
| 7 | Availability targets and failover behavior added (§6.4). | v0.1 specified latency but no uptime target, no error budget, and no defined behavior during database failover — the single most likely real outage. |
| 8 | Mechanism rationale relocated. v0.1's §2.1 compared exclusion constraints against distributed locks in detail; that analysis now lives in the RFC, with only the resulting constraint stated here (§10). | A PRD that argues implementation approach has stopped being a PRD, and pre-spends the RFC's central section. |
| 9 | Rollback claim struck (§13). | v0.1 asserted new-system data would remain valid under an unnamed older system — unverifiable, and it missed the actual hazard (a rolled-back application writing against a database that still enforces the constraint). |
| 10 | Added: user offboarding (§8.9), availability-query bounds (§8.4), sharpened reconciliation semantics (§6.1), strengthened concurrency-test definition (§6.1), glossary (§14). | Gaps identified in review. |

---

## 1. Executive Summary

Booking systems fail under concurrent access because their correctness rests on a check-then-insert sequence in application code: read whether a slot is free, then write a booking. That sequence has a race window in which two simultaneous requests can both observe the slot as free and both succeed. This project delivers a booking engine in which overlapping bookings for the same resource are structurally impossible — the guarantee is enforced by the database schema itself, so no code path, present or future, can bypass it. Version 1 delivers that guarantee for a single resource category, along with a calendar interface, cancellation, DST-correct recurring bookings, and a waitlist that genuinely reserves a freed slot for the user it was offered to. Success is defined narrowly and testably: zero successful overlapping bookings under adversarial concurrent load, sub-second conflict feedback, correct local times across DST boundaries, and a standing production check that alerts if the guarantee is ever removed. This is a correctness-first scheduling primitive, not a workplace-management suite; pricing, multi-region writes, and composite resource booking are explicitly out of scope.

## 2. Problem Statement

### 2.1 The mechanism

Check-then-insert is not atomic. The naive booking flow is: query for any existing booking on Resource X overlapping Time Range T; if none exists, insert. Between the read and the write there is a window — usually milliseconds, never zero — in which a second concurrent request performs the identical read, also finds nothing, and also inserts. Both succeed. Neither request's code is individually wrong. The defect lives in the gap between two operations that were never made atomic with respect to each other.

### 2.2 Why it recurs

This is not an adversarial edge case. It is the expected behavior of ordinary usage:

- **Contention follows desirability.** The 9:00 slot on a popular room attracts simultaneous attempts *because* it is popular. Contention concentrates precisely where the stakes are highest.
- **Retries manufacture concurrency.** A client that times out and retries produces two in-flight requests for the same booking, from a single user who pressed the button once.
- **Ordinary multi-user behavior suffices.** Two people in two offices clicking the same slot in the same second requires no unusual timing and no malice.

The window is narrow enough that it rarely reproduces in manual testing or low-traffic staging. The defect passes QA and is discovered by end users in production — the worst discovery path, because by then it has already caused harm: an operations team relocating a double-booked meeting, two research groups arriving at one instrument, a client-facing appointment sold twice.

### 2.3 Why the usual mitigations do not settle it

The standard responses — `SELECT ... FOR UPDATE`, application mutexes, distributed locks — can all be made correct. They share one structural weakness: the guarantee must be correctly present on *every* write path that can create or modify a booking. The primary booking API, the admin override endpoint, a bulk-import script, a background reconciliation job, a migration someone writes eighteen months later without full context. The guarantee is opt-in per code path rather than structurally enforced. That is why this defect is recurring across the industry rather than a mistake each team makes once and permanently fixes. This project's thesis is that the guarantee belongs at the data layer, where there is no code path to forget it — because it is not implemented in any code path at all.

*The comparative analysis of schema-level constraints against distributed locking and optimistic concurrency — including latency under contention, failure behavior, and portability cost — belongs in the RFC. This document states only the product-level requirement (§4) and the resulting constraint (§10).*

### 2.4 The secondary problem: correctness that users cannot perceive

A system can be provably correct and still lose user trust. Three failure modes are correctness-neutral but trust-destroying, and v1 treats them as first-class problems rather than polish:

- A user retries a timed-out request and is told the slot is taken — by their own successful booking.
- A user is offered a waitlisted slot, accepts within the stated window, and is rejected because someone else took it in the interim.
- A recurring series is confirmed, and one occurrence silently drifts an hour after a DST transition.

In each case the invariant held and the user was still failed. §8 addresses all three as requirements.

## 3. Target Users / Personas

### 3.1 Booker (End User)

**Goal.** Reserve a resource for a specific time, with confidence that a confirmation is a guarantee.

**Pain points today.** Books a slot; discovers on arrival that someone else also has it. Develops defensive habits — over-booking "just in case," holding rooms speculatively — which degrade utilization for everyone. The workaround behavior outlasts the bug that caused it.

**Success.** A confirmation is final. A failure is immediate, specific, and tells them what to do next. Their booking shows the correct local time on the day it happens, not the day they booked it.

### 3.2 Resource Administrator (scoped)

**Goal.** Configure the resources within their assigned scope — which exist, when they are bookable, what constraints apply — and handle genuine exceptions such as maintenance.

**Scope note.** Administration is scoped to a defined set of resources, not global. A facilities manager administering Building A's rooms has no authority over Lab equipment. This resolves an inconsistency in v0.1, which specified a global admin flag while describing per-resource ownership.

**Pain points today.** In weakly-enforced systems the administrator *is* the conflict-resolution mechanism, reconciling double-bookings manually. This does not scale, and it erodes trust in the administrator personally, not just the system.

**Success.** Their work is configuration and exception-handling, not routine firefighting, because conflicts cannot occur.

### 3.3 Waitlisted User

**Goal.** Obtain access to a fully-booked resource if it frees up, without polling.

**Pain points today.** Either no waitlist exists and the user must watch manually — usually losing the freed slot to whoever happened to be looking — or a waitlist exists but is advisory, so being on it confers no real advantage.

**Success.** Prompt, automatic notification when a slot frees; a clearly stated window to confirm; and — critically — the slot is actually held for them during that window. An offer that can be beaten by an ordinary user is not an offer.

### 3.4 Operations / Facilities Team

**Goal.** Verify that correctness is holding in production, and understand utilization well enough to make capacity decisions.

**Pain points today.** No way to confirm correctness short of waiting for complaints. No utilization data, so capacity decisions are guesswork.

**Success.** Correctness and utilization are observable facts with alerting attached, not assumptions carried on design intent.

### 3.5 System Administrator

**Goal.** Manage the resource catalogue itself, assign administrative scopes, and handle organization-level concerns such as user offboarding.

**Success.** Scope assignment and lifecycle events are self-service and auditable, without requiring engineering involvement.

## 4. Goals

**Primary**

- **P1.** *Eliminate* double-booking as a possible system outcome — not reduce its rate — independent of request volume, arrival timing, or which code path initiates the write. Verified under adversarial concurrent load.
- **P2.** Deliver immediate, specific, actionable feedback within the same request/response cycle when a booking attempt loses a conflict.
- **P3.** Make every operation safe to retry. A user or client that repeats a request after a timeout must never be misinformed about the state of their own booking.
- **P4.** Support recurring bookings whose occurrences remain correct in local wall-clock time across DST transitions, without weakening the no-overlap guarantee for any individual occurrence.

**Secondary**

- **S1.** Recover otherwise-lost demand through a waitlist whose offers are enforceable — the offered slot is genuinely reserved for the duration of the offer.
- **S2.** Make correctness an observable, alerting property of the running system, not solely a design-time claim.
- **S3.** Make every state transition attributable and reconstructable after the fact.

## 5. Non-Goals

Each exclusion is stated with its justification, because a Non-Goals section without reasoning invites the scope creep it exists to prevent.

- **Multi-region active-active writes.** v1 assumes a single write primary with replica-based failover. This is load-bearing, not incidental: if two regions could each accept writes locally before replicating, each could independently accept conflicting bookings — reintroducing precisely the race this project exists to eliminate. Multi-region writes require a different consistency model entirely and are a separate initiative.
- **Payments, deposits, pricing.** This is a scheduling-correctness engine. Commerce layers on top later or not at all.
- **Dynamic allocation or surge pricing.** The system answers "is this free" and, under contention, "who gets it." Not "what should it cost."
- **Composite / bundled booking** (room *and* projector *and* catering atomically). v1 treats resources independently. Multi-resource atomicity is a real need in mature systems and introduces transactional complexity that would dilute focus from the core guarantee.
- **Multi-step approval workflows.** v1 supports immediate confirmation and admin-configured auto-approval only.
- **Native mobile applications.** Responsive web only.
- **Quantity-based resource pooling** ("10 identical units, any combination"). v1 assumes one resource equals one bookable unit at a time. Pooled capacity requires a materially different concurrency model — an exclusion constraint enforces capacity of exactly one — and is deferred. *This is the single most likely v2 request and should be re-examined before the data model is frozen.*
- **Cross-organization / anonymous public booking.** All bookers are authenticated members of the deploying organization.

## 6. Success Metrics

Every metric below is objectively measurable and falsifiable. Metrics that cannot be tested have been excluded.

### 6.1 Correctness

- **M1 — Concurrency.** Under a test harness in which at least 200 clients hold open connections and are released by a synchronization barrier so their requests genuinely overlap inside the database, targeting one contested slot: exactly one succeeds; all others receive a conflict response. Required across at least 50 consecutive runs with a 100% pass rate. This is a binary gate, not a statistical target. *Note: requests fired sequentially in a loop do not test concurrency. The barrier release is part of the requirement, not an implementation detail.*
- **M2 — Reconciliation.** A scheduled production query returns zero pairs of active bookings on the same resource with overlapping ranges. *What this actually tests:* if the constraint is present and functioning, this query is structurally incapable of returning a row. It is therefore not a race-condition detector — it is a guarantee-still-exists detector, catching a future migration that drops the constraint, a restore from a backup taken without it, or out-of-band writes. The alert must be worded so the on-call engineer understands this: a hit means the invariant has been removed, not that a race occurred.
- **M3 — Schema assertion.** An automated check confirms the exclusion constraint exists on the bookings table, run on every deploy and on a schedule in production. M2 detects the consequence; M3 detects the cause, sooner.
- **M4 — DST correctness.** An automated suite asserts that recurring series spanning spring-forward and fall-back transitions, in at least three zones with differing rules (proposed: Europe/Paris, America/New_York, Australia/Sydney), and at least one zone without DST (Asia/Kolkata), render the same local wall-clock time for every occurrence. Zero drift permitted.
- **M5 — Idempotency.** An automated test asserts that replaying any state-changing request with an identical idempotency key returns the original outcome and creates no additional state, including when the replay arrives while the original is still in flight.

### 6.2 Performance

- **M6.** P95 latency for a booking write (success or conflict) under nominal load: < 300 ms. P99: < 800 ms.
- **M7.** P95 latency for an availability view (single resource, one-month range): < 500 ms.
- **M8.** P95 latency for waitlist offer dispatch, from cancellation commit to notification enqueued: < 5 s.
- **M9.** Measured single-resource write throughput ceiling, established by load test rather than assumed. The deliverable is a documented number and the observed bottleneck, not a pass/fail. *A guarantee of correctness is not a guarantee of throughput; the team must know where this design stops performing before users find out.*

### 6.3 User Experience

- **M10.** Conflict feedback is delivered synchronously in the booking response, never as a delayed separate notification. Binary architectural requirement.
- **M11.** Percentage of waitlist offers confirmed within the offer window versus expired-and-cascaded. Instrumented from launch; no target set until real data exists. The window duration itself is an open question (§11).
- **M12.** Rate of user-visible errors that are *not* actionable — i.e. responses carrying no specific next step. Target: zero on the booking path.

### 6.4 Availability

- **M13.** Monthly availability target for the booking write path: 99.9% (≈43 min/month error budget). Read/availability path: 99.95%.
- **M14 — Failover behavior.** During database primary failover, booking writes must fail fast and unambiguously — a retryable status with a `Retry-After` hint — never hang, and never return an outcome the client cannot distinguish from success. Combined with §8.6 idempotency, a client retry after failover must be safe. Failover recovery time objective to be set in the RFC; the behavioral requirement is fixed here.

## 7. User Stories

**Booker**

- As a Booker, I want to see real-time availability on a calendar, so that I only attempt slots that are actually free.
- As a Booker, I want an immediate, specific message when I lose a conflict, so that I know what happened and can act at once.
- As a Booker, I want to safely retry a booking that appeared to fail, so that a network problem never leaves me unsure whether I hold a reservation.
- As a Booker, I want to create a recurring booking in one action, so that I don't repeat the process per occurrence.
- As a Booker, I want my recurring meeting to stay at the same local wall-clock time across a DST change, so that I never arrive an hour early or late.
- As a Booker, I want to be told clearly when the time I picked doesn't exist or is ambiguous on a DST day, so that I can choose deliberately instead of having the system guess.
- As a Booker, I want to cancel my own booking, so that the slot returns to availability without administrator involvement.

**Waitlisted User**

- As a Waitlisted User, I want to join a waitlist for a fully-booked slot, so that I don't poll manually.
- As a Waitlisted User, I want to be offered a freed slot automatically and promptly, so that I get a fair opportunity.
- As a Waitlisted User, I want the offered slot to actually be reserved for me for the stated window, so that accepting within the deadline cannot fail.
- As a Waitlisted User, I want to see my position in the queue, so that the wait is legible and the ordering feels fair.

**Resource Administrator**

- As a Resource Administrator, I want to define bookable hours and constraints for resources in my scope, so that out-of-policy attempts are rejected automatically.
- As a Resource Administrator, I want to take a resource offline for maintenance, so that operational exceptions don't require weakening anyone's guarantee.
- As a Resource Administrator, I want to see who changed a booking, when, and why, so that I can answer "what happened to my reservation" months later.
- As a Resource Administrator, I want to be told when a timezone-rule change has altered already-scheduled occurrences in my scope, so that I can inform affected users rather than have them discover it.

**Operations**

- As an Operations engineer, I want a live dashboard of conflict rate, latency, and error budget, so that correctness is verified rather than assumed.
- As an Operations engineer, I want to be paged if the reconciliation or schema check ever fails, so that a removed guarantee is an incident rather than a discovery.
- As an Operations engineer, I want the waitlist and hold-expiry background jobs monitored independently, so that a silent stall in either is visible before users notice.
- As an Operations engineer, I want per-resource utilization and waitlist-demand metrics, so that capacity decisions are evidence-based.

**System Administrator**

- As a System Administrator, I want to assign and revoke administrative scopes, so that ownership tracks organizational reality without engineering involvement.
- As a System Administrator, I want a defined, auditable outcome for the bookings of a departing user, so that offboarding doesn't leave orphaned reservations blocking resources.

## 8. Functional Requirements

### 8.1 Booking Creation & Conflict Prevention

- **FR1.** The system must reject, at the database layer, any booking whose time range overlaps an active booking or an active hold on the same resource — regardless of ordering, timing, or the number of concurrent requests, and regardless of which code path initiates the write.
- **FR2.** An overlap rejection must return a distinct, machine-distinguishable response (HTTP 409) separable from validation errors (400), authorization errors (403), and server errors (5xx).
- **FR3.** A rejected attempt must persist no partial state. The operation is atomic: it fully succeeds or leaves no trace.
- **FR4.** A cancelled booking must not constrain any future booking on that range.
- **FR5.** Editing an existing booking's time range must be evaluated against the overlap constraint exactly as a new booking is. Edit cannot be a bypass.
- **FR6.** Every booking write must record the acting principal, so that every row is attributable (see §8.7).

### 8.2 Time, Timezones & Recurrence

*This section supersedes v0.1's FR6/FR7, which were mutually unsatisfiable. v0.1 required all times stored in UTC and required local wall-clock correctness across DST. For single bookings those are compatible. For recurring bookings they are not: a weekly 10:00 Paris series materialized to UTC using February's offset renders as 11:00 local after the March transition. The series drifts by an hour and no code is obviously wrong.*

- **FR7 — Single bookings.** A one-off booking is an instant range. Store as UTC. Additionally store the IANA timezone identifier under which it was created, for display and audit.
- **FR8 — Recurring series definition.** A recurring series must be stored as: a local wall-clock start time, a duration, an IANA timezone identifier (e.g. `Europe/Paris`), and a recurrence rule. A fixed UTC offset (`+01:00`) is not an acceptable substitute for a zone identifier and must be rejected at the API boundary — an offset cannot express when the rules change.
- **FR9 — Occurrence materialization.** Each occurrence must be materialized to a concrete UTC range using the timezone rules in effect on that occurrence's own date, not the rules in effect at creation time. Materialized occurrence rows are derived state; the series definition is authoritative.
- **FR10 — Per-occurrence enforcement.** Each materialized occurrence is independently subject to FR1. If one occurrence conflicts, the system must report precisely which occurrences failed and why, per §8.5's partial-series policy.
- **FR11 — Nonexistent local times.** On spring-forward days some local times do not exist (e.g. 02:30 where the clock jumps 02:00→03:00). The system must detect this at series creation, surface it to the user, and apply a stated, documented policy. Silent guessing is prohibited. Default policy for v1: shift the occurrence forward by the transition gap and inform the user. Alternative policies (skip, reject) are an open question (§11).
- **FR12 — Ambiguous local times.** On fall-back days some local times occur twice. The system must detect this and apply a stated, documented policy, informing the user. Default for v1: the first (pre-transition) instance.
- **FR13 — Timezone database updates.** When the IANA tzdata is updated and a rule change affects already-materialized future occurrences, the system must re-materialize those occurrences from their series definitions.
  - **FR13a.** Re-materialization must be treated as a booking write and is subject to FR1 — a re-materialized occurrence may now conflict with a booking made in the interim.
  - **FR13b.** A conflicting re-materialization must never be silently dropped. The affected user and the resource administrator must both be notified, and the occurrence flagged for resolution.
  - **FR13c.** Re-materialization runs must be logged and reportable. This is a real operational event, not a theoretical one — governments change DST rules with limited notice, and the consequence is that rows already written to the database are now wrong.
- **FR14 — Series bounds.** A series must be bounded in both directions:
  - **FR14a.** Maximum occurrences per series: 100 (proposed; §11).
  - **FR14b.** Maximum advance-booking horizon: 365 days (proposed; §11).
  - **FR14c.** Series longer than the horizon are materialized to the horizon and extended by a rolling background job. Unbounded expansion in a single transaction is prohibited — it writes thousands of rows while holding locks across an entire resource, degrading every other booker.
- **FR15.** Cancelling one occurrence must not affect others in the series unless the user explicitly chooses to cancel the series.
- **FR16.** Editing a series definition must re-materialize future occurrences only. Past occurrences are historical fact and immutable.

### 8.3 Holds & Waitlist

*This section promotes the "hold" concept from an open question in v0.1 to a hard requirement. v0.1 promised waitlisted users a fair opportunity ahead of ordinary users, but specified nothing to reserve the freed slot. The failure sequence: booking cancelled at 14:00:00 → slot is genuinely free → offer dispatched with a 15-minute window → an ordinary user books it at 14:00:04 → the waitlisted user accepts at 14:03 and is rejected. The system is correct and the promise is broken.*

- **FR17 — Hold state.** The system must support a hold: a time-bounded reservation on a resource and range that occupies the same exclusion domain as a confirmed booking. While a hold is active, no other booking or hold may overlap it.
- **FR18 — Hold expiry.** Every hold carries an explicit expiry. An expired hold must not block any booking. Because a database constraint cannot evaluate "now", expiry cannot be expressed in the constraint itself — expired holds must be actively reclaimed. Mechanism (reaper job, cleanup-on-write, or both) is an RFC decision; the requirement that expiry be reliable and monitored is fixed here.
- **FR19 — Hold reclamation monitoring.** Failure or stall of hold reclamation must raise an alert. A stalled reaper does not produce errors — it silently makes resources unbookable, which presents to users as unexplained unavailability.
- **FR20 — Waitlist entry.** A user may join a waitlist for a specific resource and desired time range that is currently unavailable.
- **FR21 — Eligibility, precisely defined.** When a range is freed, a waitlist entry is eligible *if and only if* the freed range fully contains the entry's requested range. Partial overlap does not qualify in v1. *This is deliberately the strictest defensible rule; v0.1's "next eligible user" was undefined and failed §8's own standard that no requirement admit two readings. Relaxations (partial match, shortened bookings) are deferred.*
- **FR22 — Ordering.** Eligible entries are ordered by waitlist join timestamp, first-come-first-served, with a deterministic tiebreak. The rule must be documented and visible to users (§11 notes the fairness-perception risk).
- **FR23 — Offer creation.** On cancellation, the system identifies the highest-ranked eligible entry and creates a hold (FR17) covering the freed range, then dispatches the offer. The hold is created *before* the offer is sent. An offer without a hold is not permitted.
- **FR24 — Offer expiry and cascade.** Each offer has a visible expiry matching its hold. On expiry without acceptance, the hold and offer must atomically transfer to the next eligible entry, or be released if none remain.
- **FR25 — Single outstanding offer.** At most one offer may be outstanding per freed range. Enforced structurally by FR17 — a second offer would require a second overlapping hold, which the constraint forbids.
- **FR26 — Acceptance.** Accepting a valid, unexpired offer converts the hold into a confirmed booking atomically. Acceptance must be idempotent (§8.6).
- **FR27 — Position visibility.** A waitlisted user can see their position in the queue.

### 8.4 Calendar & Availability

- **FR28.** Users can view a resource's availability over a selectable range (day / week / month) without prior knowledge of which slots are free.
- **FR29.** The availability view is advisory, not authoritative. Any client-side view is subject to staleness by the time a user acts on it. The server-side check (FR1) is the sole source of truth for whether a booking succeeds. The UI must be designed so a conflict is an expected, gracefully-handled outcome rather than a surprise error.
- **FR30 — Bounded queries.** Availability requests must be bounded: maximum 92 days per request (proposed; §11), with pagination beyond. Requests exceeding the bound are rejected with a specific error. *This is the highest-volume operation in the system by an order of magnitude, and unbounded ranges combined with on-the-fly recurrence expansion are the most likely source of the first production latency incident.*
- **FR31 — Read staleness bound.** If availability reads are served from a replica, maximum acceptable replication lag must be defined and monitored, and lag exceeding it must degrade gracefully (serve from primary or surface staleness) rather than silently serve stale data.
- **FR32.** Availability views must render in the viewer's local timezone, with the timezone shown explicitly. Bookings made in another zone must display unambiguously.

### 8.5 Partial-Series Policy

- **FR33.** When a recurring series is created and some occurrences conflict, the system must not silently proceed. It must present the user with the conflicting occurrences and require an explicit choice: confirm the non-conflicting occurrences, or cancel the series and adjust. *Rationale: v0.1 defaulted to silent partial success. A series quietly missing an occurrence the booker never noticed is a worse trust failure than an upfront rejection — the user believes they have a booking they do not have, which is the exact class of failure this project exists to eliminate. Making it an explicit choice satisfies both the Booker's low-friction goal and the Administrator's no-firefighting goal.*

### 8.6 Idempotency & Retry Safety

*New in v1.0. v0.1 identified client retries as a cause of concurrent duplicate requests and then specified no handling. Without this section, the system's flagship error message becomes actively misleading.*

- **FR34 — Idempotency keys required.** Every state-changing operation — booking creation, cancellation, edit, waitlist join, offer acceptance — must accept a client-supplied idempotency key and must reject requests that omit one.
- **FR35 — Replay semantics.** A request replayed with a key already seen must return the original outcome, including the original resource identifiers and status code, and must create no additional state. The response must indicate it is a replay.
- **FR36 — Concurrent replay.** If a replay arrives while the original request is still in flight, the system must not process both. It must either wait for the original and return its outcome, or return a retryable status — never process the request twice.
- **FR37 — Key scope and retention.** Keys are scoped to the authenticated principal and the operation. Retention: 24 hours minimum (proposed; §11). A key reused with a *different* request body must be rejected as a conflict, not silently treated as a replay.
- **FR38 — The user-facing consequence.** A user who retries after a timeout and had in fact succeeded must be shown their existing confirmed booking — never a "slot unavailable" message describing their own reservation.

### 8.7 Audit Trail

*New in v1.0. For a system whose entire product value is trustworthiness, "what happened to my booking?" must be answerable months later. v0.1 promised administrators booking history without requiring that history be recorded.*

- **FR39.** Every state transition on a booking, hold, waitlist entry, offer, and resource configuration must append an immutable audit record.
- **FR40.** Each record captures: acting principal (including whether the actor was a user, an administrator, or a system process), action, timestamp, before-state, after-state, request/trace identifier, and — for administrative overrides — a required reason.
- **FR41.** Audit records are append-only. No API path may modify or delete them.
- **FR42.** Administrators can view the full audit history for resources in their scope; users can view the history of their own bookings.
- **FR43.** Retention period must be defined and enforced (proposed: 24 months; §11).

### 8.8 Authentication & Authorization

- **FR44 — Roles.** The system defines: Booker (default), Resource Administrator (scoped to an explicit set of resources or resource groups), System Administrator (global; manages catalogue and scope assignment), Operations (read-only, plus metrics and audit access).
- **FR45 — Scoped administration.** Resource Administrator permissions apply only within assigned scope. A global admin flag is explicitly rejected as the model. This resolves v0.1's internal inconsistency between its scope section and its personas.
- **FR46 — Restricted resources.** A resource may be restricted to a user group. Non-members may neither book nor join its waitlist, and restricted resources must not leak existence or details through availability views.
- **FR47 — Override authority.** Administrative override of another user's booking requires a recorded reason (FR40) and triggers notification to the affected user (FR49).
- **FR48 — Authorization is server-side.** No authorization decision may depend on client-supplied claims beyond a verified identity token.

### 8.9 Lifecycle & Offboarding

*New in v1.0. A gap that surfaces roughly three months after launch, when the first employee leaves.*

- **FR49.** When a user is deactivated, the system must apply a defined, configurable policy to their future bookings: transfer to the resource administrator, cancel and notify affected parties, or retain until manually resolved. The policy is configurable per resource type; silent orphaning is prohibited.
- **FR50.** A deactivated user's waitlist entries and outstanding offers must be cancelled immediately, and any hold released so the slot cascades to the next eligible entry rather than expiring uselessly.
- **FR51.** Recurring series owned by a deactivated user must be flagged to the relevant resource administrator with the option to transfer ownership or terminate the series.

### 8.10 Notifications

- **FR52.** A waitlisted user receiving an offer must be notified through at least one channel immediately on offer creation, and the notification must state the expiry time explicitly.
- **FR53.** A user whose booking is cancelled or modified by an administrator must be notified, including the recorded reason.
- **FR54.** A user affected by a timezone-rule re-materialization (FR13) must be notified of the change to their occurrence times.
- **FR55.** Notification delivery failure must not roll back or block the underlying state transition, but must be recorded and retried. A failed email must never cost a user their booking.

## 9. Scope

**In scope for v1**

- One resource category at launch, with an architecture that does not preclude additional categories.
- Full booking lifecycle: create, view, edit, cancel — edit subject to the same conflict rules as create.
- Database-enforced overlap prevention covering both bookings and holds.
- Hold state with reliable, monitored expiry.
- Waitlist with hold-backed offers, defined eligibility, expiry, and cascade.
- Recurring bookings: local-time + IANA zone storage, per-occurrence materialization and enforcement, bounded series, explicit partial-conflict resolution.
- DST edge-case handling: nonexistent and ambiguous local times; tzdata-change re-materialization.
- Idempotency on all state-changing operations.
- Immutable audit trail.
- Calendar UI: day/week/month views, create/edit/cancel, bounded queries, explicit timezone display.
- Scoped administration; group-restricted resources.
- Offboarding policy for departed users.
- Email and/or in-app notifications for offers, administrative actions, and re-materialization.
- Observability: conflict rate, latency percentiles, error budget, reconciliation check, schema-assertion check, background-job health.

**Out of scope for v1**

- Multiple resource types with cross-type composite bookings.
- Payments, deposits, pricing.
- Multi-region deployment or geo-distributed writes.
- Native mobile applications.
- Org-chart-based approval workflows.
- Quantity-based resource pooling.
- SMS notifications.
- Anonymous or cross-organization booking.
- Analytics beyond §6 metrics.
- Automatic conflict resolution for FR13b re-materialization conflicts (v1 surfaces them for human decision).

## 10. Assumptions & Constraints

**Assumptions**

- **A1 — Scale profile.** Organization-internal deployment: hundreds to low thousands of resources, thousands of users. Load is spiky and concentrated, not uniform — clustering at the top of the hour and at popular slots. *This is the operative characteristic: concurrency correctness is about simultaneity at a single hot resource, not aggregate throughput. A system with modest total load can face intense contention on one room.*
- **A2 — Scale figure is a scoping bet, not a researched constraint.** A1's numbers were chosen to keep v1 tractable and have not been validated against projected demand. Stated plainly because the single-primary assumption in A4 depends on it, and that assumption is what the entire correctness mechanism rests on. Validation is an open question (§11).
- **A3 — Single region.** No requirement for low-latency access from distant geographies in v1.
- **A4 — Single write primary** with replica-based HA/failover is sufficient for v1.
- **A5 — Identity is external.** Users authenticate through an existing identity provider; this project integrates rather than builds identity.
- **A6 — Notification transport is external.** An existing transactional messaging service is available.

**Constraints**

- **C1 — Mechanism is pre-selected.** The concurrency guarantee will be implemented as a PostgreSQL schema-level exclusion constraint over resource and time range, requiring the `btree_gist` extension. This is fixed as an input to design. Full comparative rationale — versus distributed locking and optimistic concurrency — belongs in the RFC and is not re-argued here.
- **C2 — Tight coupling to PostgreSQL.** C1 binds the correctness guarantee to one database technology. Migration to a different datastore, or to a sharded or multi-primary topology, requires re-deriving the guarantee from first principles. This must not be treated as a routine infrastructure change. Any future proposal to change database technology or write topology requires a new design review, not a migration ticket.
- **C3 — Extension availability must be verified first.** Some managed PostgreSQL providers restrict extension installation. `btree_gist` availability must be confirmed on the actual target platform before implementation begins, not assumed. If unavailable, C1 is invalid and the RFC must revisit the mechanism — this is the highest-severity early risk in the project.
- **C4 — Delivery capacity.** Single planning cycle, small team. This directly motivates the aggressive Non-Goals list: v1 is deliberately narrow so the core guarantee is delivered and proven rather than diluted.

## 11. Risks & Open Questions

**Technical risks**

- **R1 — Hot-resource write contention.** The constraint guarantees correctness under any load, but not throughput. Concentrated writes on one popular resource will contend on the underlying index. The ceiling must be characterized by load test (M9), not assumed. Mitigation path if the ceiling is too low: queue-and-serialize per resource, or shard by resource — both RFC-level decisions.
- **R2 — Timezone database staleness.** Correctness of future occurrences depends on current tzdata. A stale database produces wrong local times; an update produces the FR13 re-materialization event. Both directions need operational ownership.
- **R3 — Silent background-job failure.** Hold reclamation, offer cascade, and rolling series materialization are all time-triggered. Their failure mode is silence, not error — the system appears healthy while resources quietly become unbookable and offers never cascade. Independent monitoring (FR19) is required, and its absence is a launch blocker.
- **R4 — Extension unavailability.** See C3. Highest-severity early risk; verify in week one.
- **R5 — Idempotency-key storage growth.** Key retention creates an unbounded-growth table without a cleanup policy. Minor, but a known source of production surprise.

**Product risks**

- **R6 — Correct message, wrong outcome.** A technically accurate but generically-worded conflict message erodes trust even when the system behaved perfectly. Communicating "someone booked this a moment ago — here are the nearest open slots" matters as much as the backend guarantee.
- **R7 — Fairness perception.** FCFS is consistent but may feel arbitrary — a user who joined the waitlist at 2am can be beaten by nobody, yet feel beaten. Position visibility (FR27) mitigates perception but does not settle policy.
- **R8 — Explicit partial-series resolution adds friction.** FR33 deliberately trades ease for honesty. If users find it obstructive, the correct response is better conflict-resolution UX, not reverting to silent partial success.
- **R9 — Formalizing informal arrangements.** Real organizations run on unwritten claims — a Facilities-owned room that one team has a standing informal hold on. Encoding this in software risks either freezing an informal courtesy into permanent policy, or removing a goodwill workaround that made the old system tolerable. It is not yet clear who is responsible for surfacing these arrangements before launch, and discovering them post-launch will present as user resistance rather than as a requirements gap.

**Open questions**

*Product decisions required before RFC completion:*

1. Waitlist offer window. 15 minutes is proposed and unvalidated. This is now load-bearing beyond UX: the window is also the hold duration, so it directly determines how long a resource is unbookable by others. Too long harms utilization; too short makes offers unusable. Needs product sign-off.
2. Nonexistent-time policy (FR11). Shift-forward is the proposed default. Skip and reject are defensible alternatives. Needs an explicit decision.
3. Series bounds (FR14). 100 occurrences and 365 days are proposed. Validate against actual booking patterns.
4. Availability query bound (FR30). 92 days proposed.
5. Idempotency retention (FR37). 24 hours proposed.
6. Audit retention (FR43). 24 months proposed; may be governed by organizational policy rather than this project.
7. Per-resource rules beyond bookable hours — maximum advance window, minimum cancellation notice, maximum duration. Assumed configurable but unspecified.
8. Offboarding default (FR49). Which of transfer / cancel / retain is the default, and who sets it per resource type.

*Genuinely unresolved — no data point settles these:*

9. Net-new system versus extension of an existing tool. Net-new allows correctness from the schema up with no inherited debt, but adds a system for IT to support and creates friction with whoever owns the incumbent. Extending avoids sprawl but may not tolerate the constraint approach without disruptive rearchitecture of something already in production. Reasonable stakeholders land on either side depending on how they weigh correctness against organizational cost.
10. Ownership of waitlist fairness policy. If FCFS generates complaints, it is unclear whether Product, the team administering a given resource pool, or Operations owns changing it — and different resource types may reasonably want different policies, meaning there may be no single organization-wide answer.
11. Validation of A1's scale figure. Is it researched or convenient? A2 discloses it as a bet; someone must decide whether that bet is acceptable, since A4 and therefore C1 depend on it.
12. Whether pooled-capacity resources (Non-Goal) are genuinely deferrable. An exclusion constraint enforces capacity of exactly one. If pooled resources are needed sooner than expected, the data model changes materially. Worth re-examining before the schema is frozen rather than after.

## 12. Dependencies

| Dependency | Nature | Risk if unavailable |
|---|---|---|
| Identity / SSO provider | Establishes the acting principal for every booking and every audit record. | Blocking. Nothing in §8.8 functions without a verified identity. |
| Transactional notification service | Delivers offers, administrative-action notices, re-materialization notices (FR52–54). | Degrading, not blocking — FR55 requires state transitions to proceed regardless. |
| User directory / group membership | Required only if group-restricted resources (FR46) are in scope at launch. | Scoped feature loss. |
| PostgreSQL with `btree_gist` | The correctness mechanism itself. | Project-blocking. See C3 — verify in week one. |
| Reliable scheduled-execution mechanism | Hold reclamation, offer cascade, rolling materialization (FR18, FR24, FR14c). | Silent degradation. See R3. |
| Current IANA tzdata, with an update process | FR9, FR13. | Silent incorrectness. |

## 13. Rollout Considerations

*Product-level only. The full launch plan, including canary strategy and alert thresholds, is a separate document.*

- **Phase by resource type.** Launch with one well-understood category in one location. Limits blast radius while validating both the correctness guarantee and the UX assumptions — particularly conflict messaging and waitlist behavior — under real usage.
- **Phase by user group.** A pilot group runs in parallel with the existing method for a defined period, enabling a genuine before/after comparison of conflict rate and user-reported friction.
- **Deliberately cross a DST transition before wide rollout.** FR7–FR13 are the least testable-in-the-small requirements in this document, and their failure mode is silent. If the pilot period cannot span a real transition, it must include a rehearsed clock-advance exercise in a production-like environment. *Shipping recurrence without having watched a real DST boundary pass is the highest-risk shortcut available on this project.*

## 14. Glossary

| Term | Definition |
|---|---|
| Active booking | A booking in a confirmed state; counts toward the overlap constraint. |
| Hold | A time-bounded reservation occupying the same exclusion domain as a booking, used to make waitlist offers enforceable (FR17). |
| Exclusion domain | The set of records the overlap constraint evaluates — in v1, active bookings and active holds on the same resource. |
| Materialization | Deriving a concrete UTC time range for a specific occurrence from a series definition, using the timezone rules in effect on that occurrence's date (FR9). |
| Re-materialization | Recomputing already-materialized occurrences after a timezone-rule change (FR13). |
| Nonexistent local time | A wall-clock time skipped by a spring-forward transition (FR11). |
| Ambiguous local time | A wall-clock time occurring twice due to a fall-back transition (FR12). |
| Idempotency key | A client-supplied identifier making a state-changing request safe to retry (FR34). |
| Cascade | Transfer of an expired waitlist offer and its hold to the next eligible entry (FR24). |
| Reconciliation check | The standing production query that detects removal of the guarantee, not races (M2). |

## Appendix A — Requirements Traceability

Each goal maps to the requirements that satisfy it and the metrics that verify it. A goal without a verifying metric is an aspiration, not a requirement.

| Goal | Requirements | Verified by |
|---|---|---|
| P1 Eliminate double-booking | FR1–FR6, FR17 | M1, M2, M3 |
| P2 Immediate conflict feedback | FR2, FR29 | M6, M10, M12 |
| P3 Retry safety | FR34–FR38 | M5 |
| P4 DST-correct recurrence | FR7–FR16 | M4 |
| S1 Enforceable waitlist | FR17–FR27 | M8, M11 |
| S2 Observable correctness | FR19, §6 | M2, M3, M13, M14 |
| S3 Attributable state | FR39–FR43 | Rollout precondition 7 |

## Appendix B — What This Document Deliberately Does Not Decide

Listed so the RFC's scope is unambiguous and reviewers do not mistake omission for oversight:

- The schema and constraint definition, including exactly which states occupy the exclusion domain and how the domain is expressed.
- The mechanism for hold expiry reclamation (FR18) — a constraint cannot evaluate "now", so this requires a design decision.
- The idempotency-key storage strategy and its interaction with transaction boundaries.
- The recurrence-rule format and materialization algorithm.
- Service boundaries and whether recurrence materialization is synchronous or asynchronous.
- Whether availability reads are served from a replica, and the resulting staleness contract (FR31).
- Failover recovery time objective (M14 fixes only the behavior, not the target).
- The load-test harness design implementing M1's barrier-release requirement.
- Comparative analysis of exclusion constraints versus distributed locking and optimistic concurrency (see C1).

*End of document.*
