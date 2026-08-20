# Spike Report — S1: Postgres Verification

| | |
|---|---|
| **Spike** | RFC v1.0 §16, S1.1–S1.7 |
| **Status** | Complete |
| **Date** | 2026-08-20 |
| **Environment** | PostgreSQL 16.15 (Debian), via Docker Desktop on Windows, `infra/docker-compose.yml` |
| **Scripts** | `scripts/spike/` (throwaway — see that directory's docstrings; none of this code becomes the application) |

## Gate outcome

**PASS. Candidate A (PostgreSQL exclusion constraint) remains the chosen architecture.** No document requires revision on S1.1 or S1.5 grounds. One real, quantified liveness finding (not a correctness/safety finding) was surfaced at S1.2 and is carried forward as an implementation requirement for Phase 4, not a gate failure — see below.

## S1.1 — btree_gist availability ⚠️ PROJECT-BLOCKING

**Command:** `python s1_1_extension.py`

**Result:**

```
Postgres server_version: 16.15 (Debian 16.15-1.pgdg13+2)
btree_gist: AVAILABLE (extname=btree_gist, extversion=1.7)
```

`CREATE EXTENSION IF NOT EXISTS btree_gist;` succeeded with no special privileges beyond the `kairos` role's ownership of `kairos_dev`, and the exclusion constraint in `common.reset_schema()` (identical shape to Spec v1.0 §3's `no_overlapping_bookings`) creates successfully on top of it.

**⚠️ Not yet verified: the actual production deployment target.** No deployment platform has been chosen yet. This check only confirms `btree_gist` works on local Docker Postgres 16. Per Rollout v1.0 §2.2 and this spike's own Definition of Done, the identical check must be re-run against whatever platform is chosen (Railway, Render, Supabase, Fly, RDS, Neon, etc.) before Phase 30 (go-live), because some managed providers restrict extension installation. **Carried forward as an open item — see "Consequences" below.**

## S1.2 — 200 barrier-released concurrent inserts for one identical slot

**Command:** `python s1_2_3_concurrency.py`, then `python s1_2d_repeated_n200.py` (10 reps, after the first run's result demanded repetition to characterize it rather than accept a single sample)

**Harness:** independent OS threads, each opening its own `psycopg` connection *before* the barrier (per Test Plan v1.0 §2.0 — connect-before-release is what makes this a true-simultaneity test, not a staggered one; an earlier follow-up script that connected *after* the barrier produced misleadingly clean results at every N and had to be discarded — see `s1_2b_retry_and_scale.py`'s docstring for the record of that mistake), all releasing together via `threading.Barrier(200)`, all inserting the byte-identical range.

**First single run:**

```
Total responses: 200 (expected 200)
Successes: 0 (must be exactly 1)
Failures: 200
Distinct SQLSTATEs among failures: ['40P01', '57014']
Ground truth active rows for this resource/range: 0
```

This was not the expected "1 success, 199×`23P01`" result, so it was **not accepted as-is** and was investigated rather than reported as a pass. `docker logs kairos_postgres | grep deadlock` confirmed real `deadlock detected` errors — not a harness bug.

**Repeated 10× at N=200 to characterize the finding** (Test Plan v1.0 §2.0's own rationale for requiring repetition — "a harness that only sometimes creates genuine contention produces false-confidence passes on the runs it doesn't" applies equally in reverse: a single bad run doesn't prove instability either):

```
rep  1: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1
rep  2: successes=0  sqlstate_counts={'40P01': 10, '57014': 190} ground_truth=0
rep  3: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1
rep  4: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1
rep  5: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1
rep  6: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1
rep  7: successes=0  sqlstate_counts={'40P01': 9, '57014': 191}  ground_truth=0
rep  8: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1
rep  9: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1
rep 10: successes=1  sqlstate_counts={'23P01': 199}              ground_truth=1

Clean (1 success, no deadlocks): 8/10
Runs with at least one deadlock (40P01): 2/10
Runs with ZERO successes: 2/10
Runs with MORE THAN ONE success: 0/10
```

**Reading this correctly.** Unlike a unique btree index, a GiST exclusion constraint has no fixed lock-acquisition ordering across conflicting rows. Under N=200 truly-simultaneous inserts targeting the exact same row (the worst case this system will ever see — in practice contention concentrates but rarely reaches "everyone in the org clicks the identical 30-minute slot in the identical millisecond"), the conflict-detection waits can form a genuine circular wait graph that Postgres's deadlock detector must break by aborting participants. In roughly 1/5 of runs at this specific extreme, enough participants got aborted (`40P01`) and the survivors then queued long enough to hit `statement_timeout` (`57014`, since `statement_timeout=10s` was in effect) that **nobody's insert committed on that attempt.**

**The one number that matters most: 0/10 runs ever produced more than one success.** The correctness guarantee — the actual thesis of this project — held in all 10/10 runs, including both "bad" ones. What was at risk in 2/10 runs was *liveness*, not *safety*: a client whose request fails with `40P01` or `57014` gets an error and must retry, exactly as it already must for `55P03` (lock timeout). No client was ever told "success" incorrectly, and no double-booking ever occurred.

## S1.3 — Blocking vs. fail-fast against an uncommitted competitor

**Command:** `python s1_2_3_concurrency.py` (second half of output)

**Result:**

```
After 1.5s wait, is B's insert still in flight (blocked)? True
B resolved 0.0062s after A committed
B's outcome: failed, sqlstate=23P01
B's total elapsed time (insert attempt -> resolution): 1.4752s
```

Confirms RFC v1.0 §7.1 exactly: a conflicting insert against an **uncommitted** competitor blocks — it does not fail immediately — and resolves within milliseconds of the blocking transaction's commit/rollback. This is the behavior the entire `lock_timeout`/latency model in RFC §7.1 is built on, and it held.

## S1.4 — Partial predicate behavior

**Command:** `python s1_4_partial_predicate.py`

**Result:**

```
(a) Constraint definition: EXCLUDE USING gist (resource_id WITH =, time_range WITH &&)
    WHERE ((status = ANY (ARRAY['confirmed'::text, 'held'::text])))
(a) PASS: partial predicate WHERE status IN ('confirmed','held') accepted
(b) PASS: cancel-then-rebook on the identical range succeeded
(c) PASS: 21 overlapping CANCELLED rows coexist on the identical range —
    proves cancelled rows are outside the exclusion domain (and therefore
    outside the partial index)
```

Note on method for (c): rather than compare raw index byte sizes (which requires a size baseline and is an indirect proxy), this spike proves the stronger, more direct claim — that 21 mutually-overlapping `cancelled` rows can coexist on the identical range without ever tripping the constraint. That is only possible if cancelled rows are excluded from the constraint's exclusion domain, which is precisely what makes them excluded from its partial index (RFC v1.0 §3.5).

## S1.5 — Predicate immutability ⚠️ Design-shaping

**Command:** `python s1_5_predicate_immutability.py`

**Result:**

```
EXPECTED: the now()-dependent predicate was REJECTED.
SQLSTATE: 42P17
Error message: functions in index predicate must be marked IMMUTABLE

Consequence: RFC §10.4's dual mechanism (reaper + cleanup-on-write)
is confirmed necessary. Phase 17 proceeds as designed.
```

Confirmed exactly as RFC v1.0 §10.4 predicted. `WHERE status = 'held' AND expires_at > now()` is rejected with `42P17`. A hold's expiry genuinely cannot be expressed in the constraint itself. **Phase 17's dual-reclamation design (reaper + cleanup-on-write) is required, not optional or over-engineered.**

## S1.6 — Single-resource write throughput ceiling

**Command:** `python s1_6_throughput_ceiling.py`

**Result** (distinct, non-overlapping 10-minute bookings packed into 15-minute slots on one resource — isolating index/connection contention from overlap correctness, which S1.2 already covers):

| N | Wall time | Writes/sec | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| 10 | 0.134s | 74.4 | 20.29ms | 32.16ms | 32.16ms | 32.16ms |
| 25 | 0.250s | 100.2 | 33.04ms | 55.57ms | 56.53ms | 56.53ms |
| 50 | 0.551s | 90.8 | 82.86ms | 138.47ms | 148.01ms | 148.01ms |
| 100 | 1.172s | 85.3 | 217.98ms | 372.32ms | 382.31ms | 382.31ms |
| 250 | 3.202s | 78.1 | 533.63ms | 951.04ms | 991.78ms | 1009.60ms |
| 500 | 8.123s | 61.6 | 1556.14ms | 2696.07ms | 2772.93ms | 2808.64ms |

**Inflection point:** throughput peaks around **N=25 (~100 writes/sec)** and degrades steadily past that — p95 latency grows roughly linearly with N (32ms → 2.7s from N=10 to N=500), rather than plateauing.

**Important caveat on what this number actually measures.** Per the Test Plan's own harness rule, every worker opens its own fresh connection — no shared pool. On this local, single-container, virtualized Docker Desktop environment, each fresh connection pays real TCP-handshake and Postgres backend-fork overhead, and that cost is very likely the dominant factor in the observed degradation at high N, not GiST index contention specifically. **This spike's number is not the production ceiling.** Production sits behind PgBouncer in transaction-pooling mode specifically to amortize this exact cost (RFC v1.0 §6.3), and this local spike deliberately does not include a pooler (matching S1.2's requirement for independent, unpooled connections to test true simultaneity). The real per-resource GiST-contention ceiling — isolated from connection overhead — is Test Plan CONC-06's job, run in Phase 29 against a pooled, production-shaped topology. This spike's number is recorded as a rough local baseline only.

## S1.7 — Cleanup-on-write deadlock behavior ⚠️ Design-shaping

**Command:** `python s1_7_cleanup_deadlock.py` (200 workers × 50 repetitions = 10,000 total attempts)

**Setup:** 200 pre-seeded *expired* holds on adjacent, non-overlapping 10-minute ranges on one resource. 200 barrier-released workers, each executing the exact Spec v1.0 §4.1 step-2 pattern (`DELETE ... WHERE status='held' AND expires_at<=now() AND time_range && $range` immediately followed by the booking `INSERT`, same transaction) against its own corresponding range.

**Result:**

```
Total deadlocks (40P01) across all 50 reps: 0
Reps with at least one deadlock: 0/50
Successes: 200/200 on every single rep
```

**Zero deadlocks across 10,000 attempts.** This makes sense in light of S1.2's finding: S1.2's deadlocks appeared specifically when 200 transactions all contended for the *identical* row. Cleanup-on-write's DELETE+INSERT pair, by contrast, targets a *different, non-overlapping* range per worker — there is no shared row for a circular wait to form around. **Cleanup-on-write is confirmed safe as designed. Phase 17 proceeds with both mechanisms (reaper + cleanup-on-write), per the original plan.**

## Consequences

1. **RFC Candidate A stands.** Every mechanism it depends on — the exclusion constraint itself, the partial predicate, blocking-not-failing on an uncommitted competitor, predicate immutability forcing the dual-reclamation design, and cleanup-on-write's safety under load — is confirmed on real PostgreSQL 16. No document requires re-architecture.

2. **New, concrete requirement for Phase 4 (Service Layer & Booking Creation API), not previously explicit in RFC §7.1 or Spec §6.1: `BookingService`'s exception handling must treat SQLSTATE `40P01` (`deadlock_detected`) the same way it treats `55P03` (`lock_timeout`) — as a `503 service_unavailable` with `Retry-After`, distinct from `23P01`'s `409 slot_unavailable`.** The RFC's written model describes only "one winner, clean `23P01` losers." That holds in the large majority of cases (8/10 at the worst-case N=200-identical-row extreme measured here), but not universally, and the failure mode when it doesn't hold is a client-visible error requiring retry, never a wrong answer. This is a refinement to catch during Phase 4 implementation, not a correctness gap — recorded here so it isn't rediscovered as a surprise when Phase 3's CONC-01 test suite is built.

3. **Phase 3's CONC-01 (100 consecutive runs at N=200) will very likely observe this same ~1-in-5 deadlock pattern at least once across 100 runs**, given it appeared in 2/10 here. Test Plan v1.0 CONC-01 already specifies that `55P03` (and by the above extension, `40P01`) is a documented, non-failing outcome — "all others receive `409 slot_unavailable` **or** `503 service_unavailable`" — so this does not require a Test Plan change. It does mean Phase 3's harness must apply the same treatment already specified for `55P03` to `40P01`.

4. **S1.1 must be re-run against the actual production deployment target once one is chosen, before Phase 30.** This spike only verified local Docker Postgres 16. Recorded as an open item.

5. **S1.6's throughput numbers are a local baseline only**, not a production ceiling — see the caveat above. The real characterization is Phase 29's job (Test Plan CONC-06), against a pooled topology.

## Open items carried forward

- [ ] Verify `btree_gist` on the actual production deployment platform once chosen (blocking for Phase 30 go-live; Rollout v1.0 §2.2).
- [ ] Phase 4: add `40P01` to the retryable-503 SQLSTATE set alongside `55P03`.
- [ ] Phase 29: re-characterize single-resource throughput with PgBouncer in the loop (Test Plan CONC-06); today's S1.6 numbers are not that measurement.

## Environment for reproducibility

- `infra/docker-compose.yml`: `postgres:16`, `max_connections=600` (raised from the default 100 specifically so 200–500-way spike/test concurrency doesn't get rejected at the connection-limit layer before it even reaches the constraint — see the comment in that file; this is a dev/test setting, not a production sizing decision, since production uses PgBouncer).
- `scripts/spike/common.py`: shared connection helper applying production write-path session settings (`lock_timeout=3s`, `statement_timeout=10s`, `idle_in_transaction_session_timeout=30s`) to every spike connection, so the numbers above reflect production configuration, not defaults.
- All scripts run via `scripts/spike/.venv` (not committed; `requirements.txt` pins `psycopg[binary]`).
