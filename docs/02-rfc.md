# RFC / Technical Design Document
## Concurrency-Safe Resource Booking Engine

| | |
|---|---|
| **Document version** | 1.0 |
| **Status** | Draft for Design Review |
| **Supersedes** | v0.1 (written against PRD v0.1) |
| **Builds on** | PRD v1.0 (approved) |
| **Reviewers** | Staff Engineering, Bar Raiser, Security, Operations |
| **Blocked on** | Spike S1 (§16) — must complete before this document is approved |

---

## 0. Revision History

**v1.0 — changes from v0.1.** v0.1 was written against PRD v0.1 and therefore inherited requirements that have since been corrected. All cross-references have been re-pointed at PRD v1.0.

| # | Change | Reason |
|---|---|---|
| 1 | Candidate D added (§2.4): SERIALIZABLE isolation / Postgres SSI, argued at full strength. | v0.1 presented three candidates and omitted the strongest competitor. A reviewer asks "why not just run at SERIALIZABLE?" in the first five minutes, and v0.1 had no answer. |
| 2 | Waitlist redesigned around holds in the booking table (§10). | v0.1's offer mechanism reserved nothing. Its exclusion constraint on `waitlist_offer` prevented offer-vs-offer collisions only — an ordinary booking could take the slot mid-offer, because it lived in a different table. v0.1 then asserted the slot "was genuinely reserved," which was false. |
| 3 | Hold expiry reclamation designed (§10.4), including why a constraint predicate cannot express expiry. | Direct consequence of change 2. A constraint predicate must be immutable, so `expires_at > now()` is not expressible — expired holds must be actively reclaimed or they block bookings forever. |
| 4 | Blocking semantics of exclusion violations documented (§7.1, §16). | v0.1's latency reasoning assumed a conflicting insert fails immediately. It does not — it waits for the competing transaction to resolve. This changes the latency model, requires explicit timeouts, and changes what the load test measures. |
| 5 | tzdata claim in §9 corrected and inverted. | v0.1 claimed fresh rendering means a tzdata update "fixes display going forward." It does the opposite: a stored occurrence instant computed under old rules is now wrong, and fresh rendering faithfully displays the error. Re-materialization is required (PRD FR13). |
| 6 | Idempotency fully designed (§11), including transaction boundary, concurrent replay, and key reuse. | v0.1 named the pattern and stopped. The transaction boundary is the entire design and was unstated. |
| 7 | Isolation level stated and justified; timeouts specified (§7.1). | Absent from v0.1. |
| 8 | New sections: audit trail (§12), authorization model (§8.1), lifecycle/offboarding (§13), correctness monitoring (§14), spike plan (§16). | PRD v1.0 requirements (FR39–43, FR44–48, FR49–51, M2/M3) that did not exist when v0.1 was written. |
| 9 | §5(d) realigned to PRD FR33 (explicit user resolution of partial-series conflicts). | v0.1 chose silent auto-confirm; PRD v1.0 reverses this. The underlying mechanism is unchanged. |
| 10 | Redis failure-mode paragraph added (§4.3); partial-index benefit claimed explicitly (§3.4); series bounds, query bounds, replica lag (§6, §7). | Gaps identified in review. |

## 1. Overview & Goals Recap

### 1.1 The problem in engineering terms

This system requires mutual exclusion over a two-dimensional key space — `(resource_id, time_interval)` — under arbitrary concurrent write load, where the exclusion check and the write must be atomic with respect to every other concurrent writer, unconditionally, regardless of which application code path initiates the write.

Four requirements compose on top of that core guarantee:

1. **Multi-write atomicity semantics.** A single logical action (a recurring series) generates many writes, each independently subject to the guarantee, with a defined policy when some succeed and some fail (PRD FR7–FR16, FR33).
2. **Extension of the exclusion domain to a second state.** The waitlist requires a *reservation* that is not yet a booking but must occupy the same exclusion space — without introducing a second coordination mechanism that re-creates the race the first one eliminated (PRD FR17–FR27).
3. **Semantic time correctness independent of storage representation.** Wall-clock intent must survive DST transitions, nonexistent and ambiguous local times, and changes to the timezone rules themselves *after* data has been written (PRD FR7–FR13).
4. **Retry safety.** At-least-once network delivery must produce exactly-once state (PRD FR34–FR38).

### 1.2 What this document must achieve

Commit to a technical approach that satisfies every functional and non-functional requirement in PRD v1.0; resolve the technical open questions PRD §11 left to this document; make every tradeoff explicit; and state honestly what the chosen approach gives up.

### 1.3 What this document does not decide

Per PRD §11, the following are organizational or product decisions and are not resolved here: net-new system versus extending existing tooling; ownership of waitlist fairness policy; whether informal resource-priority arrangements need surfacing before launch; validation of the scale assumption. Resolving these in an RFC would be design-doc overreach.

## 2. Candidate Architectures Considered

Four approaches were seriously considered. Each is argued at its strongest — including the one chosen, whose weaknesses are stated as plainly as the alternatives'.

### 2.1 Candidate A — PostgreSQL schema-level exclusion constraint

**Mechanics.** The booking table carries a `tstzrange` column and a constraint:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

ALTER TABLE booking ADD CONSTRAINT no_overlapping_bookings
  EXCLUDE USING gist (
    resource_id WITH =,
    time_range  WITH &&
  )
  WHERE (status IN ('confirmed', 'held'));
```

`btree_gist` is required because a GiST exclusion constraint needs a GiST-compatible operator class for every column in it. A scalar `resource_id` has no native GiST operator class; `btree_gist` supplies one, so `=` on `resource_id` can be combined with `&&` (range overlap) on `time_range` inside a single constraint.

On every insert or update, Postgres checks the new row against the GiST index for a row with matching `resource_id` and overlapping `time_range` satisfying the predicate. If found, the statement fails with `SQLSTATE 23P01` (`exclusion_violation`). There is no separate check step in application code — the check and the write are one atomic operation from the database's perspective, so no window exists for a second writer.

**Strengths.** Correctness is unconditional and unbypassable by any code path, including ones written later by engineers with no knowledge of the requirement. The failure mode is a specific, well-typed error the application maps to a clean 409. Zero coordination code in the application layer means zero application-layer surface area for this class of bug. The predicate scoping means cancelled rows leave the index entirely (§3.4).

**Weaknesses and failure modes.**

- The GiST index is a real contention point under high write concurrency against the *same* `resource_id` (§6).
- Conflicting writes block rather than fail fast. When a transaction attempts an insert conflicting with another transaction's *uncommitted* insert, Postgres does not reject it immediately — it waits until the first transaction commits or rolls back. Under the thundering-herd scenario this system exists for, losing requests queue rather than fail instantly. This has direct latency consequences (§7.1) and is the single most easily-missed property of this mechanism.
- Postgres-specific. Does not transfer to another database or a multi-primary topology without being re-derived from scratch.
- The constraint is invisible in application code. An engineer performing an unrelated migration could drop or alter it without realizing it is the entire correctness mechanism. No technical safeguard prevents a sufficiently privileged migration from removing any constraint. Mitigated procedurally and detected by monitoring (§14) — this design accepts the risk rather than eliminating it.
- Protects the `booking` table only. Any read path not ultimately resolving through this table is non-authoritative by construction, which the design must make structurally explicit (§4, §5).

### 2.2 Candidate B — Application-level pessimistic locking (`SELECT ... FOR UPDATE`)

**Mechanics.** Before inserting, lock the relevant rows:

```sql
SELECT * FROM booking
 WHERE resource_id = $1 AND time_range && $2
FOR UPDATE;
```

The subtlety that decides this candidate: `FOR UPDATE` locks only rows *returned by the query*. If no existing booking overlaps the requested range — the common case, and precisely the case where two requests race for a currently-empty slot — the query returns zero rows, locks nothing, and both transactions proceed to insert. Row locking does not solve an insert-into-empty-space race.

To make it correct, the lock must be taken against something that exists regardless of booking state — typically one row per resource (`SELECT * FROM resource WHERE id = $1 FOR UPDATE`). That serializes *every* booking attempt against that resource through one lock, including two requests for non-overlapping times that could never conflict. Booking 09:00 and booking 14:00 in the same room now wait on each other.

**Strengths.** No new infrastructure. A pattern every backend engineer knows. Stays inside one Postgres transaction with full ACID guarantees. No extension dependency.

**Weaknesses.** The coarse lock destroys real concurrency on popular resources for no correctness reason. The lock is held for the transaction's duration, so a slow transaction on this path becomes an availability problem for every other booking on that resource. And the correctness property lives entirely in application code: every write path must independently remember to acquire the lock, in the same transaction, in a consistent order. This is exactly the "opt-in per code path" fragility PRD §2.3 rejects. Candidate B does not avoid it.

### 2.3 Candidate C — Distributed lock service (Redis/Redlock, Zookeeper, etcd)

**Mechanics.** Acquire a lock keyed by `resource_id` with a TTL from an external coordination service; perform check-and-insert against Postgres; release. Redlock acquires against a quorum of independent Redis instances and validates remaining validity against a drift-bounded timer.

**Strengths.** No extension dependency and no coupling of correctness to a Postgres-specific schema feature. Reusable if the organization already runs this infrastructure. Works across heterogeneous datastores.

**Weaknesses.** TTL-based locks have a structural problem: a holder paused longer than the TTL — GC pause, scheduler delay, slow network — can lose the lock without knowing, then resume and write believing it still holds exclusivity, while a second process legitimately holds the same lock and is also writing.

Closing this requires fencing tokens: a monotonically increasing token issued with the lock, validated by the resource being written. But if Postgres must validate a fencing token to make this safe, Postgres is the enforcement point again — the lock service becomes a coordination hint, not the source of truth, undercutting the case for introducing it.

Candidate C also reintroduces the same per-code-path weakness as B, and adds an infrastructure dependency with its own availability and partition behavior: if the lock service is unreachable, does booking fail closed (reject everything) or fail open (bypass locking and risk conflicts)? Both are real operational costs neither A nor B carries.

### 2.4 Candidate D — SERIALIZABLE isolation (Postgres SSI)

*New in v1.0. This is the strongest competitor to Candidate A and the one a reviewer raises first. v0.1 omitted it.*

**Mechanics.** Run the booking transaction at SERIALIZABLE:

```sql
BEGIN ISOLATION LEVEL SERIALIZABLE;
SELECT 1 FROM booking
 WHERE resource_id = $1 AND time_range && $2 AND status = 'confirmed';
-- returns nothing
INSERT INTO booking (...) VALUES (...);
COMMIT;
```

Postgres implements Serializable Snapshot Isolation. It tracks read-write dependencies between concurrent transactions and detects the "dangerous structure" that permits a serialization anomaly. Two transactions each reading "no overlap" and each inserting overlapping rows form exactly that structure. One is aborted at commit with `SQLSTATE 40001` (`serialization_failure`). The caller retries; the retry sees the committed row and correctly rejects.

**Genuine strengths — stated without hedging.** This is *correct*. SSI provides true serializability, not an approximation. It requires no extension, so PRD C3 (the `btree_gist` availability risk, which is project-blocking) evaporates. The check is ordinary SQL, readable by any engineer without knowledge of exclusion constraints or GiST. It generalizes to invariants an exclusion constraint cannot express — including capacity greater than one, which PRD §5 defers but flags as the most likely v2 request. If pooled resources arrive in v2, Candidate D handles them and Candidate A does not.

**Weaknesses.**

- The guarantee is conditional on every writer's session configuration. A code path running at `READ COMMITTED` silently bypasses SSI entirely. No error, no warning, no signal — the guarantee simply does not apply. This is *worse* than forgetting a lock: forgetting a lock at least leaves a visible omission in the code, whereas an isolation level is ambient session state that an engineer may never look at. It fails silently and only under concurrency, which is the worst combination available.
- Every caller must implement a retry loop. `40001` is a normal, expected outcome under contention, not an exceptional error. A caller that treats it as a 500 turns a routine conflict into an outage-shaped error. This is the same "opt-in per code path" fragility as B and C, in a new costume.
- False-positive aborts. SSI is conservative. It aborts transactions that would in fact have been serializable, and the false-positive rate rises with contention — precisely where throughput matters most. Predicate lock escalation (from row-level to page-level to relation-level under memory pressure) makes this materially worse: under escalation, transactions touching *non-overlapping* time ranges on the same resource can abort each other. Candidate A rejects only writes that actually overlap.
- Retry loops interact badly with the latency budget and with idempotency. A retried transaction consumes budget; retried writes must be reconciled with the idempotency design in §11.
- SSI tracking carries memory overhead that degrades with long-running transactions.

**Honest concession:** Candidate D is not wrong. It is a legitimate design that a competent team could ship. The choice between A and D is a real judgment call, not an obvious one.

## 3. Chosen Architecture & Justification

**Chosen: Candidate A — PostgreSQL schema-level exclusion constraint**, with `status IN ('confirmed', 'held')` as the predicate.

### 3.1 Against Candidate B

B requires a resource-level lock to be correct for the insert-into-empty-space case, serializing all bookings per resource regardless of overlap — a real, measurable concurrency cost A does not pay, since A blocks only writes that actually overlap. B gains nothing on the one axis where it could have advantage over A (infrastructure independence), since it is still entirely a Postgres-transaction mechanism. B is dominated: worse on per-code-path discipline and on needless serialization, better on nothing.

### 3.2 Against Candidate C

C offers genuine portability, at the cost of a failure mode — silent violation under clock drift or process pause — that directly contradicts PRD P1 (*eliminate*, not reduce). A design whose core correctness claim can be silently violated by a GC pause is a poor fit for a requirement treating zero conflicts as the only acceptable outcome. Hardened with fencing tokens, the enforcement point becomes Postgres anyway — C then pays for a new infrastructure dependency to arrive back at "Postgres decides," which A provides directly.

### 3.3 Against Candidate D — the decision that actually matters

Both A and D are correct. The decision turns on a single axis: where the guarantee lives.

Under A, the guarantee is a property of the *table*. Every writer is subject to it, unconditionally, with no configuration to get right and no retry loop to remember. A new engineer who has never read this document, writing a bulk-import script eighteen months from now, cannot violate it — not through discipline, but through impossibility.

Under D, the guarantee is a property of *each transaction's session configuration plus each caller's retry handling*. Both are correct by default in the code written today. Neither is enforced. A future writer at `READ COMMITTED` bypasses it silently.

PRD §2.3 identifies this exact property — "the guarantee is opt-in per code path" — as the reason this bug recurs across the industry rather than being fixed once. Candidate D is a better instance of the pattern the PRD rejects. Choosing it would mean the design's central thesis and its implementation disagree.

Secondary factors, both pointing the same direction: false-positive aborts under predicate lock escalation mean D can reject non-conflicting bookings on a hot resource, hurting exactly the case A handles best; and D's retry loops complicate the latency budget and the idempotency design.

### 3.4 What Candidate A gives up, explicitly

**Portability.** Locked to PostgreSQL and `btree_gist`. Moving to another database or a multi-primary topology requires re-deriving correctness from first principles. Judged acceptable because PRD A4 already commits to a single PostgreSQL primary for v1 for unrelated reasons — A exploits a database choice already made rather than introducing a new constraint. And B, C, and D all reduce to "Postgres transactions are the real source of truth" once their failure modes are examined, so the portability advantage is smaller than it first appears.

**Capacity greater than one.** An exclusion constraint enforces capacity of exactly one. PRD §5 defers pooled resources and flags them as the most likely v2 request. If pooled capacity arrives, this mechanism does not extend to it — the pooled case would need a different mechanism, plausibly Candidate D, running alongside. This is the sharpest cost of the choice and is stated here rather than discovered later. It is accepted because building for a deferred requirement at the expense of the current one is the wrong trade, and because a v2 that adds a second mechanism for a genuinely different invariant is a reasonable outcome rather than a failure.

**The extension dependency.** PRD C3 flags `btree_gist` availability as project-blocking. Candidate D has no such dependency. This must be verified before this RFC is approved (§16). If the extension is unavailable on the target platform, this decision is void and Candidate D becomes the chosen approach.

### 3.5 A benefit worth claiming explicitly

The predicate `WHERE status IN ('confirmed', 'held')` makes this a **partial index**. Cancelled bookings leave the GiST index entirely. Booking history is retained in full for audit (§12) without bloating the index on the hot write path, and index size tracks *active* bookings rather than all bookings ever made. On a system where cancellation is routine, this is a substantial and permanent win obtained for free from a predicate that was needed for correctness anyway.

## 4. System Architecture

### 4.1 Components

- **Frontend.** React + TypeScript SPA with a calendar component. Communicates exclusively over HTTPS/JSON.
- **Backend API.** Django + DRF. Stateless, horizontally scalable behind a load balancer. The only component permitted to write to `booking`. Owns authentication and authorization enforcement.
- **Database.** PostgreSQL — single write primary plus streaming read replicas. `btree_gist` enabled. `booking` carries the exclusion constraint.
- **Background workers.** Celery, Redis broker. Handle waitlist offers and cascade, hold reclamation, rolling series materialization, tzdata re-materialization, reconciliation and schema-assertion checks, notification dispatch.
- **Notification integration.** A `NotificationService` interface over a transactional provider (PRD §12). Called from workers, never synchronously from the request path (§15a).
- **Auth integration.** OIDC/SSO against an existing identity provider. Backend validates tokens and issues its own short-lived session token.

### 4.2 Architecture diagram

```
             [React SPA]
                  |
              HTTPS / JSON
                  |
                  v
  [Identity/SSO] <--OIDC--> [Django + DRF API service]
                                |       |        \
                    SQL writes  |       | reads    \ enqueue (on_commit)
                   via PgBouncer|       |            \
                                v       v             v
                 [PostgreSQL PRIMARY]   |       [Redis — Celery broker]
                  · booking             |               |
                    (EXCLUDE, partial)  |               v
                  · idempotency_key     |         [Celery workers]
                  · audit_log           |          · offer + cascade
                  · waitlist_entry      |          · hold reclamation
                        |               |          · series materialization
                streaming |             |          · tz re-materialization
                replication|            |          · reconciliation / schema check
                        v               |          · notification dispatch
                 [PostgreSQL REPLICA] <-+                  |
                  (availability reads,                     v
                   lag-monitored)              [Transactional email provider]
```

Invariant made structural by this topology: the authoritative conflict decision happens only at the primary, inside the `booking` insert. No cache, no replica, and no application check is authoritative. Availability reads are advisory by construction (PRD FR29), not by convention.

### 4.3 The Redis dependency — stated honestly

§2.3 rejects Redis as a *correctness* mechanism, and §4.1 then makes Redis a hard dependency as the Celery broker. This is deliberate and the distinction matters:

If Redis fails, no correctness violation occurs. The exclusion constraint is untouched; bookings continue to succeed and conflict correctly. What stops is *liveness* of the asynchronous subsystems: waitlist offers are not dispatched, expired holds are not reclaimed by the reaper, notifications are not sent, rolling materialization pauses.

The user-visible consequence is that held slots stay blocked until reclamation resumes. This is a degradation, not a corruption — a resource appears unavailable when it should be free, which is the safe direction of failure. It is also why §10.4 puts cleanup-on-write in the booking path *in addition to* the reaper: a booking attempt reclaims expired holds itself, so a stalled reaper cannot make a slot permanently unbookable as long as anyone tries to book it. The reaper is required for waitlist *cascade* liveness, not for availability correctness.

Redis outage must alert (§14).

### 4.4 Request lifecycle — booking creation

1. Client sends `POST /api/bookings` with resource, range, timezone, and an `Idempotency-Key` header.
2. DRF authenticates the principal and authorizes against the resource (§8.1).
3. Idempotency check and claim (§11.2) — same transaction as the write.
4. Serializer performs *policy* validation only: well-formed range, within bookable hours, within max duration, within the advance horizon, not in the past. HTTP 400 on failure. Availability is deliberately *not* checked here — no application-level check-then-insert step exists to race. This is the entire point of §3.
5. Within one transaction: reclaim expired holds for this resource (§10.4), then `INSERT INTO booking`.
6. Success: constraint passes, transaction commits, `201 Created`.
7. Conflict: Postgres raises `23P01`. The service layer catches that specific SQLSTATE — never a generic exception handler, which would conflate it with a foreign-key violation — and returns `409 Conflict` with a structured body identifying the conflict and suggesting nearby availability.
8. Lock timeout: if the insert waits longer than `lock_timeout` for a competing uncommitted transaction (§7.1), it fails with `55P03` and returns `503` with `Retry-After`. Combined with the idempotency key, the client's retry is safe.
9. Frontend, which rendered optimistically, finalizes on 201 or rolls back and refreshes the calendar on 409.

## 5. Data Flow for Critical Operations

**(a) Successful booking creation**

Steps 1–6 above. The point worth stating: the insert and the constraint check are one operation with no intermediate application-visible state. There is no moment where the application believes the booking might succeed before the database has decided.

**(b) Booking creation that loses a conflict**

Steps 1–5, then 7. The transaction is rolled back by Postgres on the constraint violation — the service layer catches, translates, and lets the transaction context manager clean up. PRD FR3 (no partial state) is structurally guaranteed, not achieved by careful bookkeeping.

Under contention, note that step 5 may block before reaching step 7 (§7.1). The losing request's latency is dominated by the winner's transaction duration, not by its own work.

**(c) Cancellation triggering a waitlist offer**

1. Within a transaction, the booking's status is set to `cancelled`, and an audit record is written (§12).
2. Because the constraint predicate is `status IN ('confirmed','held')`, the cancelled row leaves the exclusion domain and the index immediately. This is exactly why the predicate is status-scoped: cancel-then-rebook works without deleting history, and the guarantee stays airtight for anything still active.
3. Transaction commits.
4. Only after commit, `transaction.on_commit()` enqueues `offer_freed_range(resource_id, time_range)`. Enqueuing inside the transaction risks a worker acting on a slot that a rollback un-frees.
5. The worker executes the offer creation described in §10.2 — which creates a hold row in the `booking` table, subject to the same constraint as any booking.
6. On successful hold creation, the worker dispatches the notification with the explicit expiry time (PRD FR52).

**(d) Recurring series creation where one occurrence conflicts**

1. Client sends the series definition: IANA zone, local start time, duration, recurrence rule, count.
2. Server-side expansion (never client-side) into concrete occurrences using the mechanism in §9.
3. Bounds enforced: maximum 100 occurrences, maximum 365-day horizon (PRD FR14). Beyond the horizon, occurrences are materialized by the rolling job.
4. Dry-run pass. Each expanded occurrence is checked against current availability in a read-only pass, and the result returned to the client *without committing anything*.
5. If any occurrence conflicts: return 409 with a per-occurrence report. Per PRD FR33, the system does not silently create the non-conflicting subset. The user must explicitly choose to proceed with the partial series or adjust. v0.1 chose silent partial success. PRD v1.0 reverses this: a series quietly missing an occurrence the booker never noticed is the same failure class this project exists to eliminate — the user believes they hold something they do not.
6. On explicit confirmation: each occurrence is attempted as its own independent transaction, not one all-or-nothing transaction across the series. All-or-nothing would mean one contested Tuesday blocks all eight weeks, and would hold locks across an entire resource for the duration of the series write.
7. Occurrences that conflict *between* the dry run and confirmation — a real possibility, since the dry run is advisory like every other read — are reported in the final response. The client must handle a confirmation that partially fails.
8. Successful occurrences share a `series_id`, enabling series-level cancellation (PRD FR15) as a filtered update while each occurrence remains individually cancellable.

## 6. Scalability Strategy

Aggregate request volume is the uninteresting axis: the API layer is stateless and scales horizontally. The risk that matters — flagged in PRD R1 — is contention concentrated on a single hot resource, which does not improve by adding API instances, because it is database-level index contention, not application throughput.

### 6.1 Mechanism of the risk

Every write against a given `resource_id` must check the GiST index before committing. Under many simultaneous writes against the *same* resource, these transactions contend on the same index pages, and — because conflicting inserts block on uncommitted competitors (§7.1) — they serialize behind whichever transaction commits first. Throughput for one resource has a real ceiling. Throughput spread across many resources parallelizes cleanly, since they touch different index regions.

### 6.2 Characterizing it

The concurrency test required by PRD M1 is extended to also capture p50/p95/p99 latency and maximum sustained writes per second against a single `resource_id` under increasing concurrency. The deliverable is a documented number and the observed bottleneck (PRD M9) — not a pass/fail. The test must use barrier-released clients (PRD M1); requests fired in a loop do not test concurrency.

### 6.3 Mitigations, in order of honesty

- **PgBouncer in transaction pooling mode.** Reduces connection overhead. Stated plainly: this addresses connection overhead, *not* index contention. It prevents a second bottleneck from compounding the first; it does not solve the first.
- **Read replica for availability views.** Keeps browsing load — the highest-volume operation by an order of magnitude — off the primary's connection and I/O budget. Explicitly not for the authoritative conflict check, which must always hit the primary, since replication lag would reintroduce staleness exactly where PRD FR29 cannot tolerate it. Replica lag is monitored; lag beyond the threshold (PRD FR31) degrades to primary reads or surfaces staleness in the response rather than silently serve stale data.
- **Bounded availability queries.** Maximum 92 days per request (PRD FR30), enforced server-side. Unbounded ranges combined with recurrence expansion are the most likely source of the first production latency incident.
- **Add a second instance of the resource.** If one room is a sustained hot spot, the effective and cheaper fix is usually product-level, not infrastructure-level. Worth stating: not every scaling problem has a scaling solution.
- **Sharding is explicitly not needed**, and would not help this risk anyway. Sharding addresses *aggregate* throughput exceeding one primary's capacity. Sharding by `resource_id` still puts all of one hot resource's traffic on one shard — it does not address single-resource contention at all. Given PRD A1's scale, it is premature; the trigger for revisiting is sustained aggregate write throughput approaching the measured ceiling, not a number guessed at now.

## 7. Non-Functional Requirements — Technical Realization

### 7.1 Isolation, blocking, and timeouts

*New in v1.0. v0.1 stated no isolation level and assumed conflicting inserts fail immediately.*

**Isolation level:** `READ COMMITTED` — Postgres's default. This is sufficient *precisely because* the exclusion constraint is enforced at write time independently of isolation. That independence is a genuine strength of Candidate A worth naming: the guarantee does not depend on session configuration, which is exactly the property §3.3 chose it for. Under Candidate D, the isolation level would have been load-bearing.

**Blocking behavior.** A conflicting insert against an *uncommitted* competitor waits for that transaction to resolve, then fails with `23P01` if it committed, or succeeds if it rolled back. Consequences:

- Under a thundering herd, losing requests queue rather than fail instantly. Their latency is bounded by the winner's transaction duration, not by their own work. PRD M6's P95 < 300ms is a nominal-load target and must not be read as a contested-slot target. The contested case is characterized separately by §6.2 and stated as its own number.
- A slow or hung transaction holding the winning insert blocks every other attempt on that resource. This is the sharpest availability risk in the design.

**Timeouts — required, not optional.**

| Setting | Value | Rationale |
|---|---|---|
| `lock_timeout` | 3s (initial; tune from §16) | Bounds the wait on a competing transaction. Exceeded → `55P03` → 503 + `Retry-After`. A hung transaction degrades one request, not the resource. |
| `statement_timeout` | 10s (write path) | Backstop against a pathological query. |
| `idle_in_transaction_session_timeout` | 30s | Prevents an abandoned open transaction from blocking a resource indefinitely — the specific failure this design is most exposed to. |

The booking transaction must be as short as possible: no external calls, no notification dispatch, no non-essential work inside it. Every millisecond inside the transaction is a millisecond every competing request waits.

### 7.2 Latency targets

| Target | Mechanism | What could miss it |
|---|---|---|
| Booking write P95 < 300ms (nominal) | One DB round-trip; no synchronous external calls; notification async | Connection pool exhaustion (mitigated: PgBouncer, monitored pool); index bloat under write load (mitigated: autovacuum tuning, bloat metrics); contention on a hot slot — explicitly out of scope for this target per §7.1 |
| Availability read P95 < 500ms | Replica-served; same GiST index reused for range-overlap reads; bounded to 92 days | N+1 serialization (mitigated: explicit prefetching plus a query-count assertion enforced in tests); replica lag (§6.3) |
| Waitlist dispatch P95 < 5s | `on_commit` enqueue; typically picked up sub-second. The 5s budget absorbs queue-depth variance during bursts, not because the mechanism is slow | Worker pool undersized (mitigated: autoscale on queue depth); crashed worker holding a task (mitigated: late-ack so the task is redelivered rather than lost) |
| Synchronous conflict feedback (binary) | Structural consequence of §3 — no async check step exists, so the conflict is known at insert time within the request | Nothing, by construction. Worth naming: this would have required real additional design under Candidate C |

### 7.3 Availability and failover — PRD M13/M14

*New in v1.0. Absent from v0.1.*

Target: 99.9% monthly for the write path, 99.95% for reads.

Behavior during primary failover. The requirement (PRD M14) is that writes fail fast and unambiguously — never hang, never return an outcome the client cannot distinguish from success.

- Connection-level timeouts are set so a request against a failing primary errors rather than hanging.
- The API returns 503 with `Retry-After`, never a generic 500 and never a timeout with no response body.
- Retry safety comes from §11. A client that retries with the same idempotency key after failover either finds its original outcome recorded or performs the write once. Without idempotency, "did my booking commit before the primary died?" would be unanswerable — which is why §11 is a prerequisite for this section, not an independent feature.
- Reads may continue from replicas during failover with staleness surfaced.
- RTO target is set in the rollout plan; the behavioral contract is fixed here.

## 8. Security & Threat Model

### 8.1 Authorization model — PRD FR44–FR48

*New in v1.0. v0.1 gestured at `is_admin_for()` with no model behind it.*

Roles: Booker (default), ResourceAdministrator (scoped), SystemAdministrator (global), Operations (read-only plus metrics and audit).

Scoped administration is modelled as an explicit grant table — `(principal_id, resource_group_id, role)` — not a boolean flag on the user. Authorization is resolved through a single service consulted by all DRF permission classes, so scope logic exists in exactly one place. A global admin flag is explicitly rejected as the model.

Enforcement is server-side only. No authorization decision depends on client-supplied claims beyond a verified identity token.

### 8.2 Threats and mitigations

| Threat | Mitigation |
|---|---|
| Object-level authorization bypass (IDOR) — cancelling another user's booking by guessing IDs | Every mutating endpoint resolves ownership or scoped admin rights server-side via a permission class, never by hiding UI controls. Explicit test: user A cannot cancel user B's booking. |
| Scripted mass-booking / hoarding — the constraint guarantees one winner per slot; it says nothing about a script claiming every popular slot the instant it opens | Per-principal token-bucket rate limiting on the booking endpoint; optional max-concurrent-active-bookings policy. Stated explicitly: this is a fairness policy, eventually-consistent enforcement is acceptable, and it is a categorically different kind of guarantee from the correctness constraint. Conflating the two would be a design error. |
| Waitlist manipulation — duplicate entries, forged join times | Unique constraint on `(principal, resource, range)` for active entries; `joined_at` set server-side at insert, never accepted from the client. |
| Injection | Django ORM parameterizes by default. The specific risk is raw SQL for range operations, which is a real temptation. Use range field types and ORM range lookups; where raw SQL is unavoidable, parameterize exclusively, enforced at review. |
| Rate-limit evasion via distributed abuse | Per-IP limiting at the gateway layered on per-principal limiting. Acknowledged as defense-in-depth, not a solution — distributed abuse is genuinely hard and outside this system's scope to fully solve. |
| Data exposure through availability views — leaking who booked what | Non-owner, non-admin responses expose free/busy only, never the booking owner's identity. A field-level serializer decision, not endpoint-level access control. Restricted resources (PRD FR46) must not leak existence through availability views either — a 404, not a 403. |
| Audit log tampering | Append-only enforced at the database grant level; the application role holds no UPDATE or DELETE on the audit table (§12). |
| Idempotency key harvesting — replaying another user's key to read their outcome | Keys are scoped to the authenticated principal. A key presented by a different principal is treated as unseen (§11.4). |

## 9. Timezone & DST Correctness

All timestamps are stored as `timestamptz`, which is internally always a UTC instant regardless of session settings. This satisfies the storage requirement at the type-system level rather than depending on application discipline.

### 9.1 The recurrence problem

A naive implementation generates a weekly series by taking the first occurrence's UTC instant and adding `7 × 24h` repeatedly. This is wrong whenever a DST transition falls inside the series. DST shifts the UTC offset, so "the same UTC instant plus N days" is not "the same local wall-clock time plus N days" once a boundary is crossed. For "every Tuesday at 10:00 America/New_York," 10:00 EDT and 10:00 EST are different UTC instants — naive arithmetic silently drifts the displayed local time by an hour for every occurrence after the transition.

### 9.2 Correct mechanism

The series definition stores local wall-clock time + duration + IANA zone identifier + recurrence rule. A fixed UTC offset (`+01:00`) is rejected at the API boundary (PRD FR8) — an offset cannot express when the rules change.

Expansion computes each occurrence in local wall-clock time using `zoneinfo` (IANA-backed), then converts each occurrence individually to its UTC instant. Each occurrence independently picks up the offset actually in effect on its own date, rather than inheriting an offset computed once at creation. This is why expansion must be server-side with a real tz-aware library, not client-precomputed UTC and not arithmetic on the first occurrence.

Materialized occurrence rows are derived state; the series definition is authoritative (PRD FR9).

### 9.3 Nonexistent and ambiguous local times

*New in v1.0. Absent from v0.1.* `zoneinfo` will silently produce a value for both cases unless explicitly told otherwise — silent guessing is the failure class this project exists to eliminate.

**Nonexistent** (spring forward: 02:30 where the clock jumps 02:00 → 03:00). Detected by round-tripping the localized datetime and comparing: if `dt.astimezone(UTC).astimezone(tz) != dt`, the local time does not exist. Policy: shift forward by the transition gap and inform the user (PRD FR11). Detection and disclosure are mandatory; the policy itself is configurable.

**Ambiguous** (fall back: 01:30 occurs twice). Detected via the `fold` attribute. Policy: take the first (pre-transition) instance, and inform the user (PRD FR12).

In both cases the user is told what happened at creation time. The system never guesses silently.

### 9.4 Timezone database updates — correcting v0.1

v0.1 claimed that rendering fresh from the UTC instant means a tzdata update "fixes display going forward." This is backwards.

Trace it:

1. Series created: weekly 10:00 Europe/Paris. Occurrence #30 materialized to a UTC instant under today's rules.
2. France abolishes DST. tzdata updates.
3. Occurrence #30's stored instant is now wrong — computed under rules that no longer apply.
4. Rendering it "fresh" converts a wrong instant correctly and displays 09:00.

Fresh rendering does not repair the error; it faithfully displays it.

Required mechanism (PRD FR13):

- A tzdata version check runs on deploy and on schedule. On a version change, affected future occurrences are identified by zone.
- Affected occurrences are re-materialized from their series definitions — not adjusted in place.
- Re-materialization is a booking write and is subject to the exclusion constraint. A re-materialized occurrence may now conflict with something booked in the interim. This is the hard part.
- A conflicting re-materialization is never silently dropped (PRD FR13b). The occurrence is flagged, the booker and the resource administrator are both notified, and it awaits human resolution. v1 does not auto-resolve.
- Every re-materialization run is logged and reportable (PRD FR13c).

Display converts the stored instant to the viewer's zone at render time, never caching a precomputed local-time string.

### 9.5 Acceptance test — feeds the Test Plan

A series spanning a known DST transition in at least three zones with differing rules (Europe/Paris, America/New_York, Australia/Sydney — note Sydney transitions in the opposite direction) plus one without DST (Asia/Kolkata), asserting every occurrence renders the identical local wall-clock time. Plus explicit cases for nonexistent and ambiguous times, and a simulated tzdata change triggering re-materialization including a conflicting occurrence. This is the test that catches the naive-arithmetic bug directly.

## 10. Holds & Waitlist

*Redesigned in v1.0. v0.1's mechanism reserved nothing. Its exclusion constraint on `waitlist_offer` prevented offer-vs-offer collisions only; an ordinary booking could take the slot mid-offer because it lived in a different table. v0.1 then asserted the slot "was genuinely reserved." It was not.*

### 10.1 The hold

A **hold** is a row in the `booking` table with `status = 'held'` and an `expires_at` timestamp. Because the constraint predicate is `status IN ('confirmed', 'held')`, a hold occupies the same exclusion domain as a confirmed booking. No booking and no other hold can overlap it — enforced by the identical mechanism, with no second coordination system.

This is the design's central reuse: the waitlist does not get its own concurrency mechanism. It gets the same one.

### 10.2 Offer creation

The worker triggered by `on_commit` after a cancellation:

1. Queries `waitlist_entry` for eligible entries. Eligibility (PRD FR21): the freed range must fully contain the entry's requested range. Partial overlap does not qualify in v1. Ordered `joined_at ASC` with a deterministic tiebreak.
2. Attempts to `INSERT` a hold row into `booking` (`status='held'`, `expires_at = now() + offer_window`, `held_for_principal`).
3. If the insert fails with `23P01`, something already occupies the range — a race with a direct booking, or another worker. The worker re-queries and continues to the next candidate. This is the same retry-on-conflict pattern used everywhere else in this design, not a new one invented for this subsystem.
4. On success, creates the offer record referencing the hold and dispatches the notification with the explicit expiry (PRD FR52).

At most one offer per freed range is now structurally guaranteed (PRD FR25): a second offer would need a second overlapping hold, which the constraint forbids. No separate constraint on the offer table is required.

### 10.3 Acceptance

```sql
UPDATE booking
   SET status = 'confirmed', expires_at = NULL
 WHERE id = $1
   AND status = 'held'
   AND held_for_principal = $2
   AND expires_at > now();
```

A conditional update on a single row. Postgres serializes concurrent updates to the same row, so the outcome is decided by whichever transaction commits first. Zero rows affected → the offer is no longer valid → a specific "offer expired" response, never a generic error.

Because the hold already occupies the exclusion domain, acceptance cannot lose a race to a direct booking. This is precisely the guarantee v0.1 promised and did not implement.

Acceptance is idempotent (PRD FR26, §11).

### 10.4 Hold expiry reclamation — the hard part

A constraint predicate cannot express expiry. Postgres requires index predicates to be `IMMUTABLE`, so `WHERE status='held' AND expires_at > now()` is not a legal predicate. `now()` is not immutable. An expired hold therefore continues to block bookings until something physically reclaims it. This is a real design problem with real failure modes and it needs both mechanisms below.

**Mechanism 1 — cleanup-on-write (correctness of availability).** Every booking insert first executes, in the same transaction:

```sql
DELETE FROM booking
 WHERE resource_id = $1
   AND status = 'held'
   AND expires_at <= now()
   AND time_range && $2;
```

Scoped to the resource and range being booked, so it is a narrow, indexed delete rather than a table scan. This makes the system self-healing: a stalled reaper can never make a slot permanently unbookable, because the next person who tries to book it clears the stale hold themselves.

**Mechanism 2 — reaper (liveness of cascade).** A periodic Celery beat task (initial: 30s) expires holds and triggers cascade to the next eligible waitlist entry. This is required because cascade must happen even when nobody is trying to book — cleanup-on-write only fires when there is booking traffic.

**Why both.** Mechanism 1 guarantees an expired hold never permanently blocks a booking, independent of worker health. Mechanism 2 guarantees the waitlist keeps moving. Neither alone is sufficient: without 1, a Redis outage makes resources unbookable; without 2, an unbooked freed slot never cascades to the next person in line.

Reaper stall must alert (PRD FR19, §14). Its failure mode is silence, not error.

**The race between reclamation and acceptance.** A user accepts at the same moment the cleanup deletes their expired hold. Both target the same row; Postgres serializes them. If cleanup commits first, the acceptance's conditional update matches zero rows → "offer expired." If acceptance commits first, the row's status is `confirmed` and `expires_at` is `NULL`, so the cleanup's `WHERE status='held' AND expires_at <= now()` matches zero rows. Both orderings are correct. No application-level lock is required — the conditional write against a single row is the whole mechanism, consistent with every other concurrency-sensitive point in this design.

### 10.5 Offer window duration

The offer window is simultaneously the hold duration, which means it is not only a UX parameter — it directly determines how long a resource is unbookable by everyone else. Too long harms utilization; too short makes offers unusable. PRD open question 1; 15 minutes is the working proposal pending product sign-off.

## 11. Idempotency

*New in v1.0. v0.1 named the pattern and stopped. The transaction boundary — the entire design — was unstated.*

### 11.1 Why it is required, not optional

PRD §2.2 identifies client retries as an ordinary source of duplicate concurrent requests. Without idempotency, the sequence is:

1. User submits. Server commits. Response lost to a network drop.
2. Client retries.
3. Second insert hits the constraint → 409.
4. The user is told the slot is unavailable — about their own successful booking.

The system is correct and the user is misinformed. PRD §2.4 classes this as a trust failure worse than the bug the project exists to fix.

### 11.2 The transaction boundary — the load-bearing decision

The idempotency record must be written in the same transaction as the booking insert.

If it were a separate transaction, a window exists where the booking committed and the key did not — and the retry produces exactly the failure this mechanism prevents. One transaction, both writes, or the mechanism is decorative.

```sql
BEGIN;
  INSERT INTO idempotency_key (key, principal_id, request_fingerprint, status)
  VALUES ($1, $2, $3, 'in_progress');   -- unique on (principal_id, key)

  DELETE FROM booking WHERE ... expired holds ...;
  INSERT INTO booking (...);

  UPDATE idempotency_key SET status='completed', response_status=201, response_body=$4
   WHERE key=$1 AND principal_id=$2;

  INSERT INTO audit_log (...);
COMMIT;
```

A 409 outcome is recorded the same way, in its own transaction after the rollback — a conflict is a legitimate final outcome and a retry must receive the same 409, not a fresh attempt.

### 11.3 Concurrent replay — PRD FR36

The retry arrives while the original is still in flight. Both transactions attempt to insert the same key. The unique constraint on `(principal_id, key)` means the second blocks on the first (same blocking behavior as §7.1), then fails with `23505` when the first commits.

The second request then reads the completed record and returns the original outcome. If the first is still in progress at `lock_timeout`, the second returns 409 Conflict with a *distinct* error code meaning "request in progress, retry shortly" — never the slot-unavailable 409, which would be the same misinformation this section prevents.

### 11.4 Key semantics

- **Scope:** `(principal_id, key)`. A key presented by a different principal is treated as unseen — this also closes the key-harvesting threat in §8.2.
- **Request fingerprint:** a hash of the semantic request body. Same key + different fingerprint → 422, an explicit client error. Never a silent replay of a different request.
- **Retention:** 24 hours (PRD FR37). A cleanup job deletes expired records — and its own growth must be monitored (PRD R5).
- **Coverage:** creation, cancellation, edit, waitlist join, and offer acceptance (PRD FR34).
- **Replay response** carries a header marking it as a replay, so clients and debugging tools can distinguish.

### 11.5 The user-facing consequence

PRD FR38: a user who retries after a timeout and had in fact succeeded is shown their existing confirmed booking. This falls out of §11.2 automatically — but it is the actual product requirement, and it is why this section exists.

## 12. Audit Trail — PRD FR39–FR43

*New in v1.0.*

**Trigger-based, not application-based.** Audit records are written by database triggers on `booking`, `waitlist_entry`, and resource configuration tables — not by application code.

This is deliberate and consistent with §3's thesis: an application-level audit is opt-in per code path, and the same future bulk-import script that motivated choosing a database constraint over a distributed lock would also skip an application-level audit write. A trigger cannot be skipped by any writer.

**Actor and reason propagation.** Triggers cannot see the authenticated principal. The service layer sets transaction-local session variables at the start of every write transaction:

```sql
SET LOCAL app.actor_id = '...';
SET LOCAL app.actor_type = 'user' | 'admin' | 'system';
SET LOCAL app.reason = '...';       -- required for administrative overrides
SET LOCAL app.request_id = '...';   -- trace correlation
```

The trigger reads these via `current_setting(..., true)`. A write arriving with no actor set is a bug; the trigger records it as `unknown` and the reconciliation job alerts on any such row rather than failing the write.

**Append-only** enforced at the grant level (PRD FR41): the application database role holds `INSERT` and `SELECT` on `audit_log`, and no `UPDATE` or `DELETE`. No API path can modify history because no API path *can*.

**Contents** (PRD FR40): actor, actor type, action, timestamp, before-state, after-state, request ID, reason.

**Access** (PRD FR42): administrators see history for resources in scope; users see their own bookings' history; Operations has read access.

**Retention:** 24 months (PRD FR43), subject to organizational policy.

## 13. Lifecycle & Offboarding — PRD FR49–FR51

*New in v1.0.*

On principal deactivation, an `offboard_principal` task runs a per-resource-type configurable policy over their future bookings: transfer to the resource administrator, cancel and notify affected parties, or retain pending manual resolution. Silent orphaning is prohibited.

Waitlist entries are cancelled immediately, and any outstanding hold is released so the slot cascades to the next eligible entry rather than expiring uselessly (PRD FR50).

Recurring series owned by the deactivated principal are flagged to the relevant resource administrator with transfer or terminate options (PRD FR51).

All offboarding actions are audited with `actor_type = 'system'` and a reason identifying the offboarding event.

## 14. Correctness Monitoring — PRD M2/M3

*New in v1.0. v0.1 mentioned reconciliation as a worker task and never designed it.*

**Schema assertion (M3)** — runs on every deploy and hourly in production:

```sql
SELECT 1 FROM pg_constraint
 WHERE conname = 'no_overlapping_bookings' AND contype = 'x';
```

Zero rows → page immediately. This detects the cause (the constraint is gone) before the consequence.

**Reconciliation (M2)** — a self-join for overlapping active bookings on the same resource, scheduled hourly.

What this actually tests, stated for the on-call engineer: if the constraint is present and functioning, this query is structurally incapable of returning a row. It is not a race detector. It detects a migration that dropped the constraint, a restore from a backup taken without it, or out-of-band writes. The alert text must say this, because an on-call engineer who reads a hit as "a race occurred" will investigate the wrong thing under pressure.

**Background job health** (PRD R3). Hold reclamation, offer cascade, rolling materialization, and tz re-materialization all fail *silently* — no errors, just absence. Each emits a heartbeat with a last-successful-run timestamp; staleness beyond threshold alerts. Redis unavailability alerts (§4.3).

Alerting must be tested by deliberate injection before launch. An alert never fired is an alert that does not exist.

## 15. Alternatives Rejected for Specific Sub-Problems

**(a) Synchronous vs. asynchronous notification dispatch.** Chosen: asynchronous. The alternative — dispatch inside the cancellation request before responding — couples a core action to an external provider's availability. If the provider is slow or briefly down, a cancellation would hang or fail for a reason entirely unrelated to cancellation. Async decouples them: cancellation succeeds or fails purely on the database write, and delivery retries independently. Cost: a small window where a cancellation is confirmed and the offer notification has not gone out — acceptable given PRD M8's 5-second budget already treats this as near-immediate rather than strictly synchronous. PRD FR55 makes this explicit: a failed notification must never cost a user their booking.

**(b) Individually materialized occurrence rows vs. rule-plus-runtime-expansion.** Chosen: materialized rows. The rule-based model — storing only the recurrence rule and computing occurrences at read time, as many general-purpose calendars do — is fundamentally incompatible with the chosen correctness mechanism. An exclusion constraint operates on concrete rows with concrete ranges; a database constraint cannot enforce non-overlap against a booking that exists only as an unexpanded rule. A rule-based model forces conflict checking back into application code at expansion time, reintroducing exactly the check-then-insert race this design eliminates.

Worth stating plainly: the rule-based pattern is not a bad pattern. It is specifically wrong for *this* system because of the correctness requirement, not because materialized rows are universally superior. Cost of the chosen approach: unbounded series require bounds and a rolling materialization job (PRD FR14, §5d) — which is also where the tz re-materialization problem (§9.4) enters, since materialized rows can go stale in a way rules cannot. That is a genuine cost of this choice and it is paid in §9.4.

**(c) Hold as a booking row vs. a separate hold table.** Chosen: a row in `booking` with `status='held'`. The alternative — a separate table with its own constraint — was v0.1's design and cannot work: two constraints on two tables cannot exclude against each other, so a hold in one table and a booking in the other can overlap freely. Cost of the chosen approach: `booking` now carries rows that are not bookings, requiring every query to filter on `status`. That is a real readability cost, paid to make the guarantee single-mechanism rather than split across two tables that cannot see each other.

## 16. Spike S1 — Required Before Approval

This RFC's central decisions rest on specific Postgres behaviors. Several are documented semantics; all must be confirmed on the actual target platform and version before this document is approved. §7.1's blocking behavior is exactly the kind of property a spike surfaces and a design session does not.

| # | Question | Why it blocks | If the answer is unexpected |
|---|---|---|---|
| S1.1 | Is `btree_gist` available and installable on the target platform? | PRD C3 — project-blocking. Some managed providers restrict extensions. | §3 is void; Candidate D becomes the chosen approach and this RFC is substantially rewritten. |
| S1.2 | Under 200 barrier-released concurrent inserts for one slot: exactly one success? What SQLSTATE do the rest receive, and what is their latency distribution? | Validates §3 and calibrates §7.1's blocking model and timeout values. | Timeouts and latency targets are re-derived from measured data. |
| S1.3 | Does a conflicting insert against an uncommitted competitor block rather than fail immediately? | §7.1's entire latency model depends on this. | The latency model and §6's load-test interpretation are rewritten. |
| S1.4 | Does the partial predicate `status IN ('confirmed','held')` behave correctly, and do cancelled rows leave the index? | §3.5 and §10.1. | The hold design changes shape. |
| S1.5 | Confirm a constraint predicate cannot reference `now()`. | §10.4 exists entirely because of this. | If expressible, the reaper becomes unnecessary and §10.4 simplifies dramatically. |
| S1.6 | Measured single-resource write throughput ceiling and the observed bottleneck. | PRD M9. | Feeds §6 mitigations; may promote sharding or queueing from "not needed" to "needed." |
| S1.7 | Deadlock behavior when cleanup-on-write and an insert run concurrently on the same resource. | §10.4 adds a delete to the hot write path. | Cleanup moves out of the write transaction to reaper-only, and §4.3's self-healing property is lost — a material change requiring re-review. |

Deliverable: a short spike report with the observed numbers, appended to this RFC before approval.

## 17. Design Patterns & Engineering Practices Applied

- **Database-enforced invariants over application-enforced ones.** The organizing principle of the whole design: the exclusion constraint (§3), the append-only audit grant (§12), the unique constraint on idempotency keys (§11.3), the unique constraint on waitlist entries (§8.2). Each moves a guarantee from "every caller must remember" to "no caller can violate." Applied consistently rather than case-by-case.
- **Idempotency keys** (§11) — necessary because network retries are an ordinary, expected trigger for the exact race class this system defends against.
- **Optimistic UI with authoritative server rejection.** The frontend renders a booking as tentative immediately and treats the server as ground truth. This fits here specifically because the backend's guarantee bounds the rollback case to one well-defined structured failure rather than many unpredictable ones — the frontend does not have to guess at failure modes.
- **Service-layer encapsulation** (`BookingService`). Keeps SQLSTATE translation, session-variable setting for audit, and idempotency handling in one tested place rather than duplicated across the primary API, admin override, and any future bulk path. Even though the database enforces correctness, a single service layer means every write path gets consistent error handling and audit attribution for free — code-organization-level defense against the same failure mode the constraint defends against at the data level.
- **Commit-triggered async dispatch** (`transaction.on_commit()`). Enqueue only after the triggering transaction commits — avoiding the classic bug of a worker acting on data a rollback never persisted.
- **Conditional writes over locks** (§10.3, §10.4). Concurrency resolved by `UPDATE ... WHERE status = expected` against a single row, letting Postgres serialize, rather than introducing application-level locking. One coordination philosophy throughout, not a different pattern per subsystem.

## 18. Open Technical Questions Remaining

Genuinely unresolved after this design commits:

- Timeout values (§7.1). `lock_timeout` at 3s is an initial figure. Real values come from S1.2.
- Reaper interval (§10.4). 30s proposed; trades cascade latency against sweep cost at real waitlist volume.
- Cleanup-on-write deadlock behavior (S1.7). If it deadlocks under load, cleanup moves to reaper-only and §4.3's self-healing property is lost — a material design change requiring re-review, not a tuning adjustment.
- tz re-materialization conflict resolution (§9.4). v1 surfaces conflicts for human resolution. Whether an automated policy is desirable, and what it would be, is deferred.
- Idempotency key table growth (§11.4). Retention is set; sustained volume and cleanup cost are not yet known.
- GiST index maintenance cadence. `REINDEX` schedule and bloat thresholds under sustained write load — operational, deferred to the runbook.
- Abuse-control thresholds (§8.2). Max concurrent bookings per principal, hard block versus soft flag. Depends on usage patterns that do not exist yet.
- Per-resource policy fields. Max advance window, minimum cancellation notice (PRD open question 7). The model accommodates them; validation logic is not designed here because the PRD has not committed to whether they ship in v1.
- Whether the pooled-capacity deferral holds (§3.4). If pooled resources arrive sooner than expected, this design does not extend to them and a second mechanism — plausibly Candidate D alongside — becomes necessary. Worth re-examining before the schema is frozen.

Explicitly not resolved here, and correctly so: PRD open questions 9–12 — net-new versus extending existing tooling, ownership of waitlist fairness policy, informal resource-priority arrangements, and validation of the scale assumption — are organizational and product questions. Resolving them in an RFC would be overreach; they remain owned by the product process.

*End of document.*
