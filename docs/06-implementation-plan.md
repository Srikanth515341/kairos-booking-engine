# Implementation Plan
## Project Kairos — Concurrency-Safe Resource Booking Engine

| | |
|---|---|
| **Document version** | 1.0 |
| **Document number** | #6 of 6 |
| **Status** | Approved for Execution |
| **Repository name** | kairos-booking-engine |
| **Builds on** | PRD v1.0, RFC v1.0, API & Data Design Spec v1.0, Test Plan v1.0, Rollout & Runbook v1.0 |
| **Execution mode** | Solo engineer + AI pair programmer, one phase per session |
| **Total phases** | 31 (Phase 0 through Phase 30) |

### Why "Kairos"

*Kairos* (καιρός) is the Greek word for the opportune moment — the right time, as distinct from *chronos*, mere sequential time. A booking system's entire job is deciding who gets a moment when two people want the same one. The name is short, pronounceable, unclaimed in this space, and looks like a real product rather than a course assignment.

Repository: `kairos-booking-engine`. Python package: `kairos`. Frontend package: `@kairos/web`.

---

## 0. How To Use This Document

### 0.1 The execution loop

For every phase, in order, without skipping:

1. Open a new Claude Code session (fresh chat).
2. Paste, in this order: PRD v1.0 → RFC v1.0 → Spec v1.0 → Test Plan v1.0 → Rollout & Runbook v1.0 → this document's §1 (Project-Wide Rules) → the single phase you are executing. *After Phase 3, `CLAUDE.md` in the repo replaces the need to paste all five documents every time — see §1.4. Paste `CLAUDE.md` plus the specific phase instead.*
3. Let Claude Code execute the phase.
4. Read the End-of-Phase Report it produces (§1.6).
5. Run the manual verification steps yourself. Do not skip this. Do not accept "it should work."
6. If verification fails, tell Claude Code exactly what failed and iterate. Do not proceed.
7. If verification passes: apply the `CLAUDE.md` and `README.md` updates, commit, push, open the PR, confirm CI is green, merge.
8. `git checkout main && git pull`.
9. Start the next phase.

### 0.2 The rule that matters most

A phase is not done because Claude Code says it is done. A phase is done when *you* have personally run the verification steps and seen the expected output.

You will be tempted to skip this around Phase 12 when things are working smoothly. Don't. The failures in this system are silent by design — that is the entire premise of the project — and a phase that "looks done" is exactly how a silent failure enters the codebase.

### 0.3 If Claude Code asks you a question

It shouldn't. Every phase below specifies its own conventions. If Claude Code asks an ambiguous process question anyway, the answer is almost always: "Follow the convention already established in the repository, and if none exists, follow §1 of the Implementation Plan." If it asks a genuine design question not covered by the five documents, that is a real gap — record it in `CLAUDE.md` under "Open Questions" and pick the simplest option that does not weaken a correctness guarantee.

### 0.4 What can be cut and what cannot

If you run short on time, cut from PRD §5 (Non-Goals) — that list already tells you what this system deliberately does not do, and the adjacent nice-to-haves are the safe things to drop. Concretely, the safe cuts are: Phase 27 (admin/ops frontend), Phase 25 (recurring frontend — the API still works), Phase 19's offboarding UI, and the depth of Phase 29 (load testing).

**Never cut:** Phase 1 (Spike), Phase 3 (concurrency proof), Phase 5 (idempotency), Phase 15 (holds), Phase 17 (reclamation), Phase 20 (reconciliation + schema assertion). These are the guarantees. A system with a beautiful frontend and a broken hold mechanism is worth less than a backend-only system where every guarantee holds.

## 1. Project-Wide Rules — Apply To Every Phase, No Exceptions

*Paste this section into every Claude Code session.*

### 1.1 Git and branching

- The repository begins with one initial commit directly on `main` (Phase 0): `.gitignore`, `README.md`, `CLAUDE.md`, `LICENSE`, `.env.example`, and empty top-level scaffolding. Nothing else.
- Every phase from Phase 1 onward happens on its own branch cut from `main`, named `phase-NN-short-description` (e.g. `phase-01-spike-postgres-verification`, `phase-15-holds-exclusion-domain`).
- All commits use Conventional Commits: `feat:`, `fix:`, `docs:`, `test:`, `chore:`, `refactor:`, `perf:`, `ci:`.
- Commit messages have a subject line under 72 characters and, where the change involves a correctness mechanism, a body explaining *why* with a document reference. Example:

```
feat: add exclusion constraint with held status in predicate

The predicate covers status IN ('confirmed','held') so that waitlist
holds occupy the same exclusion domain as bookings. Narrowing it to
'confirmed' alone silently disables every waitlist guarantee while
appearing healthy. See RFC v1.0 §10.1 and Spec v1.0 §3.
```

- `main` is never committed to directly after Phase 0. Only merged into via PR.
- Every phase ends with: push branch → open PR into `main` → CI green → you verify the Definition of Done manually → merge → `git checkout main && git pull`.
- PR description template (Claude Code produces this):

```markdown
## Phase NN — <name>

### What this implements
<one paragraph>

### Documents satisfied
- PRD: <FRs>
- RFC: <sections>
- Spec: <sections>
- Test Plan: <test IDs>

### Verification performed
<checklist from the Definition of Done, each item ticked>

### Deferred to later phases
<explicit list with phase numbers>
```

### 1.2 Secrets

`.env` is in `.gitignore` from the first commit. `.env.example` lists every required variable name with a placeholder value and a one-line comment. No credential, key, connection string, or token ever enters a commit. If one does, treat it as compromised, rotate it, and rewrite history before pushing.

### 1.3 Code quality

**Python (backend)**

- `ruff` for linting and formatting. Configuration in `pyproject.toml`. Line length 100.
- `mypy` in strict mode on the `kairos` package.
- Type hints on every function signature. No bare `Any` without a comment justifying it.
- Package structure follows RFC §4.1 — never a flat layout "to be cleaned up later."

**TypeScript (frontend)**

- ESLint + Prettier. `strict: true` in `tsconfig.json`. No `any` without justification.
- Component structure follows RFC §4.1.

**Comments**

- Explain *why*, never restate *what*. No paragraph-style comments.
- Where code exists because of a subtle correctness reason, the comment must say so and cite the section. Match the style already used in Spec v1.0's DDL. Example:

```python
# Containment (@>), not overlap (&&). A freed range must FULLY CONTAIN the
# entry's requested range or the entry is not eligible. PRD v1.0 FR21.
```

- The five load-bearing comment sites, which must all exist by the end of the roadmap:
  1. The exclusion constraint's predicate (Phase 2)
  2. The idempotency transaction boundary (Phase 5)
  3. The hold status in the exclusion domain (Phase 15)
  4. The containment eligibility rule (Phase 14)
  5. The dual reclamation mechanism (Phase 17)

**Configuration**

- Environment-based settings: dev / test / prod, separated. Never a hardcoded value that differs between environments.
- Every timeout, interval, bound, and threshold from the five documents is a named configuration constant, not a magic number. This matters because Rollout §9 requires several of them to be tuned post-launch, and a tunable buried as a literal is not tunable.

### 1.4 CLAUDE.md — created Phase 0, updated at the end of every phase

This file is the project's memory. It must be sufficient on its own for a brand-new Claude Code session — no prior context, not even the five source documents — to correctly understand the current state and continue building.

Required structure, kept current:

```markdown
# CLAUDE.md — Project Kairos

## Project Overview
<what this is, and the one-sentence core differentiator>

## Source Documents
<list of the six documents and where they live in /docs>

## Current Architecture State
<what exists right now, component by component>

## Completed Phases
| Phase | Name | One-line summary | Merged |
|---|---|---|---|

## Current Phase In Progress
<phase number and name, or "none">

## NOT Yet Built
<explicit list — this prevents a new session from assuming something exists>

## Key Technical Decisions (with source references)
| Decision | Why | Source |
|---|---|---|
| PostgreSQL EXCLUDE constraint over distributed locking | Guarantee cannot be bypassed by any code path | RFC v1.0 §3.3 |
| Predicate covers 'confirmed' AND 'held' | Holds must occupy the exclusion domain or waitlist offers reserve nothing | RFC v1.0 §10.1 |
| Idempotency key written in the same transaction as the booking | A separate transaction leaves a window where the booking commits and the key doesn't | RFC v1.0 §11.2 |
| ... | | |

## Running Locally
<exact commands>

## Running Tests
<exact commands, including the concurrency suite>

## Open Questions
<anything genuinely undecided>
```

### 1.5 README.md — updated at the end of every phase

Human-facing, professional open-source style:

- Project description and the core technical differentiator explained clearly: no two overlapping bookings for the same resource can ever both succeed, because the guarantee is enforced by a PostgreSQL exclusion constraint rather than by application code — meaning no code path, present or future, can bypass it.
- Architecture diagram (text or image).
- Tech stack.
- Setup and installation.
- How to run the test suite, including the concurrency stress test as a named, highlighted command. This is the thing a reviewing engineer will run first.
- Current feature status table.
- Once available: live deployment link, screenshots, demo.

### 1.6 End-of-phase report — required, exact structure

Claude Code produces this at the end of every phase:

```markdown
## Phase NN Complete — <name>

### 1. Summary
<what was built>

### 2. Files created / changed
<list with one-line purpose each>

### 3. Manual verification steps
<numbered, exact, copy-pasteable. Actual commands. Actual expected output.
Written for someone with no prior experience. No ambiguous steps.>

### 4. Known limitations / deferred
<explicit, with the phase number where each is addressed>

### 5. CLAUDE.md and README.md updates
<written out in full, ready to paste — never "update accordingly">
```

### 1.7 Testing discipline

- Every phase that adds behavior adds tests for that behavior in the same PR.
- Test IDs from Test Plan v1.0 are used as test names or docstrings, so `pytest -k CONC_01` works. Traceability from a failing test to the document that required it is not optional.
- CI must pass before merge, every time, no exceptions.

## 2. Sequencing Logic — Why The Phases Are In This Order

Three principles govern the order, and they override conventional layering.

**Risk first.** RFC §16 states the central architectural decision is conditional on Spike S1 — including whether `btree_gist` is installable at all. Building anything on an unverified assumption is how weeks get wasted. Phase 1 is the spike. If it fails, you learn on day two rather than day sixty.

**Guarantee before surface.** The core correctness mechanism is proven under real concurrent connections at Phase 3 — before any API, before any frontend. Milestone 1 is a passing 200-way concurrency test against a real database, nothing more. That is the project's actual thesis, and everything else is an interface to it.

**Dependency over convention.** Holds (Phase 15) cannot precede the proven exclusion mechanism. Reclamation (Phase 17) cannot precede holds. The audit trail (Phase 8) is trigger-based and depends only on the core tables existing, so it lands early rather than being deferred to a polish phase. Auth (Phase 9) is deliberately not first, because stubbing identity is trivial and blocking core correctness work on SSO integration is a beginner sequencing mistake.

Each phase notes whether its dependency is load-bearing (cannot be reordered) or conventional (could be resequenced if you chose to).

## 3. Phase Index

| # | Phase | Branch | Milestone |
|---|---|---|---|
| 0 | Repository & Process Foundation | `phase-00-repository-foundation` | |
| 1 | Spike S1 — Postgres Verification | `phase-01-spike-postgres-verification` | ⚠️ Gate |
| 2 | Core Schema & The Exclusion Constraint | `phase-02-core-schema-exclusion-constraint` | |
| 3 | Concurrency Proof & CI Pipeline | `phase-03-concurrency-proof-ci` | 🏁 Milestone 1 |
| 4 | Service Layer & Booking Creation API | `phase-04-booking-creation-api` | |
| 5 | Idempotency — Transaction Boundary | `phase-05-idempotency` | ⚠️ Subtle |
| 6 | Read Path & Availability View | `phase-06-read-path-availability` | |
| 7 | Cancellation & Editing | `phase-07-cancel-edit` | |
| 8 | Audit Trail — Triggers & Grants | `phase-08-audit-trail` | ⚠️ Subtle |
| 9 | Authentication & Scoped Authorization | `phase-09-auth-scoped-authz` | |
| 10 | Timezone Foundation | `phase-10-timezone-foundation` | |
| 11 | Recurrence Materialization & DST | `phase-11-recurrence-dst` | ⚠️ Subtle |
| 12 | Recurring API — Preview & Confirm | `phase-12-recurring-preview-confirm` | 🏁 Milestone 2 |
| 13 | Rolling Materialization & tzdata Re-materialization | `phase-13-rematerialization` | |
| 14 | Waitlist Entries & Containment Eligibility | `phase-14-waitlist-entries` | |
| 15 | Holds — The Shared Exclusion Domain | `phase-15-holds-exclusion-domain` | ⚠️ Critical |
| 16 | Offers — Creation, Acceptance, Cascade | `phase-16-offers-cascade` | 🏁 Milestone 3 |
| 17 | Dual Reclamation — Reaper & Cleanup-on-Write | `phase-17-dual-reclamation` | ⚠️ Critical |
| 18 | Notifications | `phase-18-notifications` | |
| 19 | Resource Administration & Offboarding | `phase-19-admin-offboarding` | |
| 20 | Reconciliation & Schema Assertion | `phase-20-correctness-monitoring` | ⚠️ Critical |
| 21 | Six-Job Observability & Heartbeats | `phase-21-observability-heartbeats` | |
| 22 | Security Hardening | `phase-22-security-hardening` | |
| 23 | Frontend Foundation | `phase-23-frontend-foundation` | |
| 24 | Frontend — Calendar & Booking Flow | `phase-24-frontend-booking-flow` | 🏁 Milestone 4 |
| 25 | Frontend — Recurring Flow | `phase-25-frontend-recurring` | |
| 26 | Frontend — Waitlist & Offers | `phase-26-frontend-waitlist` | |
| 27 | Frontend — Admin & Operations | `phase-27-frontend-admin-ops` | |
| 28 | Full Test Suite Completion | `phase-28-full-test-suite` | |
| 29 | Performance & Load Testing | `phase-29-performance-load` | |
| 30 | Deployment Hardening & Go-Live Readiness | `phase-30-go-live-readiness` | 🏁 Milestone 5 |

## PHASE 0 — Repository & Process Foundation

**Branch:** none — this commits directly to `main`. It is the only phase that does.

**Goal.** Establish the repository, the process scaffolding, and the documents-in-repo so that every subsequent phase has a home and a convention to follow. No feature code.

**Documents satisfied.** Implementation Plan §1 in full. No PRD/RFC/Spec functional requirements yet.

**Scope — IN**

1. Repository initialization
   - `git init`, initial commit on `main`.
   - `LICENSE` — MIT.
   - `.gitignore` — Python, Node, VS Code, macOS/Windows, `.env`, `__pycache__`, `node_modules`, `.venv`, `dist`, coverage artifacts.
2. Directory scaffolding (empty, with `.gitkeep` where needed):

```
kairos-booking-engine/
├── .github/workflows/
├── docs/
│   ├── 01-prd.md
│   ├── 02-rfc.md
│   ├── 03-api-data-spec.md
│   ├── 04-test-plan.md
│   ├── 05-rollout-runbook.md
│   └── 06-implementation-plan.md
├── backend/
│   ├── kairos/
│   ├── tests/
│   └── migrations/
├── frontend/
├── infra/
├── scripts/
├── .env.example
├── .gitignore
├── CLAUDE.md
├── LICENSE
└── README.md
```

3. `docs/` — all six approved documents committed verbatim. This is what makes the repository self-contained for a reviewer and for any future Claude Code session.
4. `CLAUDE.md` — initial version, full structure per §1.4, with the Key Technical Decisions table pre-populated from RFC v1.0 §3.3, §10.1, §11.2, §12, and §14.
5. `README.md` — initial version per §1.5. The core differentiator paragraph must already be written and correct; you will refine it, not replace it.
6. `.env.example` — every variable the system will eventually need, with placeholders and one-line comments (see the file in this repo for the full list — database, write-path session settings, Celery/Redis, domain constants, auth, notifications).
7. CI skeleton — `.github/workflows/ci.yml` that currently does nothing but check out the repo and echo a placeholder. It will be filled in Phase 3. Its existence from Phase 0 means every PR has a status check from the very first one.

**Scope — DEFERRED**

- Any application code → Phase 2 onward.
- Real CI jobs (lint, test, concurrency) → Phase 3.
- Docker Compose → Phase 2.

**Definition of Done**

- [ ] `git log` shows exactly one commit on `main`, message `chore: initialize repository scaffolding and project documents`.
- [ ] All six documents are present under `docs/` and render correctly on GitHub.
- [ ] `CLAUDE.md` exists with every section from §1.4 present, including a populated Key Technical Decisions table.
- [ ] `README.md` explains the core differentiator in a paragraph a non-expert engineer could understand.
- [ ] `.env.example` exists; `.env` is gitignored; `git check-ignore .env` returns `.env`.
- [ ] `.github/workflows/ci.yml` exists and runs (even if trivially) on push.
- [ ] Repository pushed to GitHub, public, with a description and topics set.

**Dependencies.** None.

## PHASE 1 — Spike S1: Postgres Verification ⚠️ GATE

**Branch:** `phase-01-spike-postgres-verification`

**Goal.** Resolve every open question in RFC v1.0 §16 against a real PostgreSQL instance, and produce a written spike report. This phase is a gate: if S1.1 fails, the entire architecture changes and you stop and re-plan.

**Documents satisfied.** RFC v1.0 §16 (all of S1.1–S1.7), RFC §2.1 (mechanism verification), RFC §7.1 (blocking behavior), RFC §10.4 (predicate immutability), Rollout v1.0 §2.2 (`btree_gist` confirmation).

**Why this is Phase 1 and not Phase 10.** Four of the five approved documents depend on behaviors that are documented Postgres semantics but have not been observed on your machine. RFC §16 marks the RFC itself as blocked on this spike. Discovering at Phase 15 that `btree_gist` is unavailable, or that cleanup-on-write deadlocks under load, would invalidate weeks of work. This costs one session.

**Scope — IN.** Write throwaway scripts under `scripts/spike/`. This code does not become the application. It exists to produce the report.

1. Docker Compose for Postgres — `infra/docker-compose.yml`, PostgreSQL 16, a named volume, and a healthcheck. This one artifact survives the spike and carries into Phase 2.
2. **S1.1 — btree_gist availability** ⚠️ PROJECT-BLOCKING. Run against local Docker Postgres and, if you already know your deployment target (Railway, Render, Supabase, Fly, RDS, Neon), against that target. Record both.
3. **S1.2 / S1.3 — Concurrency and blocking behavior.** Create a table with the exclusion constraint from Spec §3. 200 threads, each with its own psycopg connection, all blocked on a `threading.Barrier(200)`, released together, all inserting the identical range. Record how many succeed (must be exactly 1), the SQLSTATE the rest receive, and the latency distribution of the losers. S1.3 specifically: open transaction A, insert without committing. From transaction B, insert a conflicting row. Does B block or fail immediately? Time it. Then commit A and observe what B does.
4. **S1.4 — Partial predicate behavior.** Confirm `WHERE status IN ('confirmed','held')` is accepted. Insert a confirmed row, cancel it, confirm a new overlapping row now inserts successfully. Confirm via `pg_relation_size` that cancelled rows leave the index.
5. **S1.5 — Predicate immutability** ⚠️ Design-shaping. Attempt to add a constraint referencing `now()` — this MUST fail. Record the exact error. If it succeeds, RFC §10.4's dual-mechanism design becomes unnecessary and Phase 17 simplifies dramatically — flag it loudly.
6. **S1.6 — Single-resource throughput ceiling.** Escalating steps: 10, 25, 50, 100, 250, 500 concurrent writers, all targeting distinct, non-overlapping ranges on the same resource. Record p50/p95/p99 latency and writes/sec per step. Identify where p95 inflects.
7. **S1.7 — Cleanup-on-write deadlock** ⚠️ Design-shaping. Seed expired held rows across adjacent ranges. 200 barrier-released writers, each running the cleanup DELETE followed by an INSERT, in one transaction. Assert zero SQLSTATE 40P01 (deadlock). 50 repetitions.
8. Spike report — `docs/spikes/S1-postgres-verification.md`, committed, with observed values for every question, the exact commands used, and a "Consequences" section.

**Scope — DEFERRED.** Application code, migrations, models → Phase 2. Spike scripts are throwaway and must be clearly marked as such.

**Definition of Done**

- [ ] `docs/spikes/S1-postgres-verification.md` exists with a recorded answer for each of S1.1–S1.7.
- [ ] S1.1 answered for the actual deployment target, not only locally.
- [ ] S1.2: exactly 1 success out of 200, SQLSTATE recorded.
- [ ] S1.3: blocking vs. fail-fast recorded with timings.
- [ ] S1.5: recorded whether a `now()`-dependent predicate is rejected.
- [ ] S1.6: a latency-vs-concurrency table exists with a named inflection point.
- [ ] S1.7: deadlock count recorded across 50 runs.
- [ ] A "Consequences" section states, explicitly, whether the RFC's Candidate A remains valid.
- [ ] `CLAUDE.md` records the spike outcome under Key Technical Decisions.

**Gate conditions**

| Result | Action |
|---|---|
| S1.1 fails on the deployment target | STOP. Either change deployment target, or reopen the RFC and adopt Candidate D (SERIALIZABLE/SSI). All five documents need revision. Do not proceed to Phase 2. |
| S1.7 shows deadlocks | Record it. Phase 17 will use reaper-only reclamation, and RFC §4.3's self-healing property is lost. Flag prominently in `CLAUDE.md`. |
| S1.5 shows `now()` predicates are allowed | Excellent news. Phase 17 simplifies. Update `CLAUDE.md`. |

**Dependencies.** Phase 0. Load-bearing — nothing may be built before this resolves.

## PHASE 2 — Core Schema & The Exclusion Constraint

**Branch:** `phase-02-core-schema-exclusion-constraint`

**Goal.** Create the Django project, the core tables, and the exclusion constraint that is the entire correctness mechanism — with no API layer yet.

**Documents satisfied.** PRD FR1, FR3, FR4; RFC §3, §3.5, §4.1; Spec §3 (`app_user`, `resource`, `resource_admin`, `booking`), §2 (ERD).

**Scope — IN**

1. Django project structure per RFC §4.1 (`kairos/settings/{base,dev,test,prod}.py`, `kairos/core/`, `kairos/identity/`, `kairos/resources/`, `kairos/bookings/`).
2. Tooling — `ruff` (lint + format), `mypy` strict, `pytest` + `pytest-django`, `psycopg[binary]`. All configured in `pyproject.toml`.
3. Docker Compose — carry `infra/docker-compose.yml` forward from Phase 1, add a `kairos_test` database.
4. Models — `app_user`, `resource`, `resource_admin`, `booking`, exactly as Spec §3 defines them, including `booking.status` with `CHECK (status IN ('confirmed','held','cancelled'))` — `'held'` is present from day one even though holds arrive in Phase 15 (adding it later means a migration that touches the constraint, an expensive operation per Rollout §5.4).
5. The exclusion constraint — via `RunSQL` in a migration, with the full comment block from Spec §3 reproduced verbatim in the migration file.
6. Indexes from Spec §3: `idx_booking_hold_expiry`, `idx_booking_user_starts`, `idx_booking_series` (nullable FK deferred), plus the note that the constraint's own index serves availability reads and no second GiST index is to be created.
7. Migrations — `0001_initial`, `0002_exclusion_constraint`. Both reversible.
8. A single smoke test — insert two overlapping confirmed bookings sequentially; assert the second raises SQLSTATE 23P01.

**Scope — DEFERRED.** `waitlist_entry`, `waitlist_offer`, `recurring_series`, `idempotency_key`, `audit_log`, `system_check_run` → their own phases (14, 16, 11, 5, 8, 20). Any API, view, serializer → Phase 4. Real authentication → Phase 9.

**Definition of Done**

- [ ] `docker compose up -d` starts Postgres with `btree_gist` enabled.
- [ ] `python manage.py migrate` applies cleanly against a fresh database.
- [ ] `python manage.py migrate <app> zero` reverses cleanly.
- [ ] `SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='no_overlapping_bookings';` returns a predicate containing both `'confirmed'` and `'held'`.
- [ ] The smoke test passes: sequential overlapping insert raises 23P01.
- [ ] `ruff check`, `ruff format --check`, and `mypy` all pass with zero findings.
- [ ] The Spec §3 comment block appears verbatim in the migration file.

**Dependencies.** Phase 1. Load-bearing — the schema depends on the spike's answers.

## PHASE 3 — Concurrency Proof & CI Pipeline 🏁 MILESTONE 1

**Branch:** `phase-03-concurrency-proof-ci`

**Goal.** Prove the core guarantee under genuine concurrency with a barrier-released harness, and wire that proof into CI so it runs on every PR forever.

**Documents satisfied.** PRD M1, M3, P1; RFC §3, §7.1; Test Plan §2.0 (harness requirements), CONC-01, CONC-02, CONC-05, §13 (environment).

**Why this is Milestone 1.** At the end of this phase you can say, and demonstrate: "200 clients released simultaneously against one slot; exactly one succeeds; verified 10 consecutive times; running in CI on every commit." That sentence is the project. Everything after this is interface.

**Scope — IN**

1. The concurrency harness — `tests/concurrency/harness.py`: N independent threads, each with its own psycopg connection; `threading.Barrier(N)` for release; production session settings applied per connection; ground-truth verification built in.
2. CONC-01 — 200 clients, identical range, 10 consecutive runs in CI. Exactly 1 success; the rest 23P01 or 55P03; ground truth = 1; zero unexplained errors.
3. CONC-02 — partial overlap plus the five-way chained-overlap variant, 10 runs each.
4. CONC-05 — cancel-and-rebook at the instant a slot frees, 10 runs.
5. RECON-05 (schema assertion, CI form) — a test asserting via `pg_constraint` that the constraint exists and its predicate contains `'held'`. Runs on every PR.
6. Full CI pipeline — `.github/workflows/ci.yml` with three jobs: `lint`, `test`, `concurrency` (a **separate, named** job so a reviewer browsing PRs sees "concurrency ✓" on every one).
7. Git tag — `v0.1.0-milestone-1-concurrency-proof` on the merge commit.

**Scope — DEFERRED.** CONC-01's full 100 runs and the N=500 escalation → Phase 28. CONC-03/04 (edit races) → Phase 7. CONC-06 (throughput escalation) → Phase 29.

**Definition of Done**

- [ ] `pytest tests/concurrency -v` passes locally.
- [ ] CONC-01 passes 10 consecutive runs at N=200. Exactly one success each time, ground-truth verified.
- [ ] CONC-02 (including chained variant) and CONC-05 pass 10 runs each.
- [ ] The harness uses independent connections and a barrier — verifiable by reading `harness.py`.
- [ ] Production session settings are applied and asserted in the harness.
- [ ] The schema-assertion test fails if you manually narrow the predicate to `'confirmed'` only. Verify this by actually doing it, watching it fail, and reverting.
- [ ] CI runs all three jobs on PR; all green.
- [ ] `README.md` documents the concurrency test as a highlighted, named command.
- [ ] Git tag `v0.1.0-milestone-1-concurrency-proof` pushed.

**Dependencies.** Phase 2. Load-bearing.

## PHASE 4 — Service Layer & Booking Creation API

**Branch:** `phase-04-booking-creation-api`

**Goal.** Expose booking creation over HTTP with correct SQLSTATE translation, policy validation, and session settings — the first user-reachable surface.

**Documents satisfied.** PRD FR1, FR2, FR3, FR6, M6; RFC §4.4, §7.1, §17; Spec §4.1 (partial), §5.1, §6, §6.1; Test Plan §10.

**Scope — IN**

1. DRF setup — Django REST Framework, `/api/v1` base path, JSON only.
2. Stub authentication — a dev-only `X-Dev-User-Id` header resolving to an `app_user`. Clearly marked `# STUB — replaced in Phase 9`.
3. `BookingService` — `kairos/bookings/services.py`. All write logic lives here, never in views.
4. SQLSTATE translation — catch `23P01` specifically, never a generic `IntegrityError` handler. Raise a domain `SlotUnavailableError`, mapped to 409 `slot_unavailable`.
5. Session settings on the write path — `SET LOCAL lock_timeout` etc. per RFC §7.1, from configuration, per transaction.
6. `55P03` (lock timeout) → 503 `service_unavailable` with `Retry-After`. Not a 409.
7. Policy validation in the serializer, not availability: range well-formed, within bookable hours, within max duration, not in the past, within the 365-day horizon. Availability is deliberately not checked.
8. Error envelope — Spec §6, exactly.
9. `X-Request-Id` — accepted from the client or generated; returned on every response; threaded through logging.
10. `POST /api/v1/bookings` per Spec §5.1, with every documented failure case.
11. Structured JSON logging with `request_id`, `user_id`, `resource_id`, outcome.

**Scope — DEFERRED.** Idempotency → Phase 5 (endpoint accepts no key yet, documented gap). Read endpoints → Phase 6. Edit/cancel → Phase 7. Real auth → Phase 9.

**Definition of Done**

- [ ] `POST /api/v1/bookings` returns 201 with the Spec §5.1 body shape exactly.
- [ ] A conflicting booking returns 409 with `error.code == "slot_unavailable"`.
- [ ] Every validation case from Test Plan §10 returns 400 with the offending field in `details`.
- [ ] Nonexistent or inactive resource returns 404.
- [ ] Session settings verified applied — a test asserts `SHOW lock_timeout` returns `3s` inside the write transaction.
- [ ] A test forcing lock-timeout returns 503 with `Retry-After`, not 409.
- [ ] Reading `services.py` shows the `23P01` catch is specific, not a bare `except IntegrityError`.
- [ ] All CONC tests from Phase 3 still pass.
- [ ] `X-Request-Id` present on every response including errors.

**Dependencies.** Phase 3. Load-bearing.

## PHASE 5 — Idempotency: The Transaction Boundary ⚠️ SUBTLE

**Branch:** `phase-05-idempotency`

**Goal.** Make booking creation safe to retry, with the key written in the same transaction as the booking — the single load-bearing decision in the mechanism.

**Documents satisfied.** PRD FR34–FR38, M5; RFC §11 (all), §17; Spec §3 (`idempotency_key`), §4.1, §7, §6.1; Test Plan IDEM-01 through IDEM-11.

**Why this is its own phase.** RFC §11.2 states it plainly: if the key is written in a separate transaction, a crash between the two leaves the booking committed and the key absent — and the retry returns "slot unavailable" for the user's own booking. That is a worse trust failure than the bug this project exists to fix, because the message actively misinforms.

**Scope — IN**

1. `idempotency_key` table per Spec §3: `PRIMARY KEY (user_id, key)`; `status TEXT CHECK (status IN ('in_progress','completed'))`; nullable response columns; `completed_has_response` CHECK; `idx_idempotency_created`.
2. The transaction boundary — implement Spec §4.1 exactly: one transaction, both writes. Add the load-bearing comment citing RFC §11.2.
3. Replay semantics (Spec §7): same key + same body → stored response + `Idempotent-Replay: true`; same key + different body → 422 `idempotency_key_conflict`; concurrent replay → 409 `request_in_progress`; different principal, same key → treated as fresh.
4. Request fingerprint — SHA-256 of the normalized body.
5. 409 outcomes are recorded too.
6. Cleanup job — a management command deleting keys older than `IDEMPOTENCY_RETENTION_HOURS`. Scheduling arrives in Phase 21; the command exists now.
7. `Idempotency-Key` header required on `POST /bookings`. Missing → 400.
8. Tests IDEM-01 through IDEM-06, IDEM-09, IDEM-10, IDEM-11.

**Scope — DEFERRED.** IDEM-07 (process kill mid-transaction) → Phase 28. IDEM-08 (lost response simulation) → Phase 28. IDEM-05 (recurring replay) → Phase 12. Coverage on cancel/edit/waitlist/confirm → those phases (7, 14, 16).

**Definition of Done**

- [ ] `idempotency_key` PK is `(user_id, key)` — verify with `\d idempotency_key`.
- [ ] Response columns are nullable and an `in_progress` row can be written.
- [ ] IDEM-06 passes: barrier-released duplicate keys — one 201, the other 409 `request_in_progress`, never `slot_unavailable`, ground truth exactly one booking. 100 repetitions.
- [ ] IDEM-01, 02, 03, 09, 10, 11 pass.
- [ ] Reading the service code shows both writes in one `transaction.atomic()` block, with the RFC §11.2 comment present.
- [ ] `Idempotent-Replay: true` header on replays.
- [ ] Cleanup command works and is idempotent itself.
- [ ] Manual check: replay a successful booking; confirm you get the original booking back, not a 409.

**Dependencies.** Phase 4. Load-bearing — every subsequent write endpoint needs this.

## PHASE 6 — Read Path & Availability View

**Branch:** `phase-06-read-path-availability`

**Goal.** Implement the read endpoints, including the highest-volume operation in the system, with bounded queries and field-level authorization.

**Documents satisfied.** PRD FR28–FR32, M7; RFC §6.3, §8.2; Spec §5.2, §5.4, §5.7, §8; Test Plan §10, SEC-05.

**Scope — IN**

1. `GET /api/v1/bookings/{id}` — owner, resource admin, or operations. Otherwise 404, not 403.
2. `GET /api/v1/bookings` — cursor pagination per Spec §8, default sort `starts_at ASC`. Holds are never returned by this endpoint.
3. Cursor pagination — base64 of `(sort_key, id)`. Opaque to clients. `limit` default 20, cap 100.
4. `GET /api/v1/resources/{id}/availability` per Spec §5.7: bounded to 92 days; 93 → 400. `booking_id` and `owner` are omitted entirely — not nulled — when the requester isn't the owner or an admin. Held slots render as ordinary busy blocks with no identifying fields. `as_of` and `data_freshness` fields present.
5. Query-count assertion tests — per RFC §7.2, a test asserting the availability endpoint executes a bounded number of queries.
6. `GET /api/v1/resources`, `GET /api/v1/resources/{id}` — read-only for now; writes in Phase 19.

**Scope — DEFERRED.** Replica routing and lag degradation → Phase 30. Resource CRUD → Phase 19. `GET /bookings/{id}/history` → Phase 8.

**Definition of Done**

- [ ] All read endpoints return Spec-exact shapes.
- [ ] Non-owner requesting another's booking gets 404, and the body contains no leaked fields.
- [ ] Availability at exactly 92 days → 200; 93 days → 400.
- [ ] SEC-05 passes: as a non-owner, the busy block has no `booking_id` key and no `owner` key — assert key absence, not null value.
- [ ] Cursor pagination is stable across concurrent inserts.
- [ ] Query-count assertion passes.
- [ ] All prior tests still green.

**Dependencies.** Phase 4. Phase 5 is conventional — reads don't need idempotency.

## PHASE 7 — Cancellation & Editing

**Branch:** `phase-07-cancel-edit`

**Goal.** Implement the two remaining single-booking mutations, both subject to the same constraint as creation, both idempotent.

**Documents satisfied.** PRD FR4, FR5, FR34, FR47; RFC §5b; Spec §5.5, §5.6; Test Plan CONC-03, CONC-04, §10.

**Scope — IN**

1. `POST /bookings/{id}/cancel`: permission owner or resource admin (override); `reason` required when requester isn't owner; sets `status='cancelled'` etc.; double-cancel returns 200 idempotent; requires `Idempotency-Key`; a stub `transaction.on_commit()` hook logging "would enqueue waitlist check" — real implementation in Phase 16.
2. `PATCH /bookings/{id}`: owner only; only start/end editable; evaluated against the constraint exactly as a create (PRD FR5); requires `Idempotency-Key`.
3. CONC-03 (edit-vs-create) and CONC-04 (edit-vs-edit), 10 runs each, with the loser's row verified unchanged at its original range.

**Scope — DEFERRED.** The real waitlist trigger → Phase 16. Notification on admin cancellation → Phase 18.

**Definition of Done**

- [ ] Cancel and edit both work with all Spec §5.5/§5.6 failure cases.
- [ ] Admin override without reason → 400.
- [ ] Double-cancel → 200, idempotent.
- [ ] CONC-03 passes 10 runs: exactly one of {edit, create} wins; if the edit loses, its `time_range` is verified unchanged.
- [ ] CONC-04 passes 10 runs: loser unchanged at original range.
- [ ] Both endpoints require and honor `Idempotency-Key`.
- [ ] The `on_commit` stub is present and correctly placed outside the transaction body.

**Dependencies.** Phase 5. Load-bearing.

## PHASE 8 — Audit Trail: Triggers & Grants ⚠️ SUBTLE

**Branch:** `phase-08-audit-trail`

**Goal.** Make every state transition attributable and unforgeable, using database triggers and grant-level append-only enforcement.

**Documents satisfied.** PRD FR39–FR43; RFC §12, §17; Spec §3 (`audit_log`, trigger function), §5.3; Test Plan AUD-01 through AUD-05.

**Why triggers and not application code.** RFC §12's argument: an application-level audit is opt-in per code path. The same future bulk-import script that motivated choosing a database constraint over a distributed lock would also skip an application-level audit write. A trigger cannot be skipped by any writer.

**Scope — IN**

1. `audit_log` table per Spec §3, with `idx_audit_entity`, `idx_audit_actor`, `idx_audit_request`.
2. Grant-level append-only: `REVOKE UPDATE, DELETE ON audit_log FROM kairos_app; GRANT INSERT, SELECT ON audit_log TO kairos_app;`. This requires a dedicated application database role — the app must not connect as superuser.
3. Trigger function `write_audit_log()` per Spec §3, reading session variables via `current_setting(..., true)`.
4. Triggers on `booking`, `resource`, `resource_admin` now; `waitlist_entry` and `waitlist_offer` triggers added in Phases 14/16.
5. Session-variable propagation — a context manager in `BookingService` setting `app.actor_id`, `app.actor_type`, `app.reason`, `app.request_id` at the start of every write transaction. Every existing write path is retrofitted in this phase.
6. `GET /bookings/{id}/history` per Spec §5.3.
7. AUD-01 through AUD-05.

**Scope — DEFERRED.** Audit gap monitoring (`actor_type='unknown'` alerting) → Phase 21. Waitlist/offer triggers → Phases 14/16.

**Definition of Done**

- [ ] AUD-01 passes: connecting as `kairos_app`, `UPDATE` and `DELETE` on `audit_log` both fail with insufficient privilege. Verify manually via `psql`, not only in a test.
- [ ] AUD-02 passes: a raw SQL write to `booking`, bypassing the service layer entirely, still produces an audit row.
- [ ] AUD-03: actor attribution and required reason correct; admin override without reason rejected.
- [ ] AUD-04: create → edit → admin-cancel reconstructs fully via `GET /bookings/{id}/history`.
- [ ] AUD-05: cancellation doesn't remove history.
- [ ] No `actor_type='unknown'` rows are produced during the full existing test suite.
- [ ] The application connects as `kairos_app`, not as superuser.

**Dependencies.** Phase 7. Conventional — could be built after Phase 6, but retrofitting fewer write paths later is cheaper.

## PHASE 9 — Authentication & Scoped Authorization

**Branch:** `phase-09-auth-scoped-authz`

**Goal.** Replace the stub with real OIDC authentication and implement the four-role scoped authorization model.

**Documents satisfied.** PRD FR44–FR48; RFC §8.1; Spec §1, §3, §5; Test Plan SEC-01, SEC-06, §10.

**Scope — IN**

1. OIDC/SSO integration — validate provider tokens, issue a short-lived internal session token. For local development, a documented mock provider.
2. Four roles per PRD FR44: `booker`, `resource_administrator` (scoped via `resource_admin`), `system_admin`, `operations`.
3. A single `AuthorizationService` consulted by all DRF permission classes. A global admin flag is explicitly rejected as the model.
4. 404-vs-403 convention applied consistently per Spec §1 and enforced by tests.
5. Restricted resources (PRD FR46) — must not leak existence through availability views or list results. 404, not 403.
6. The stub removed entirely. `X-Dev-User-Id` must not work in any environment other than test.
7. SEC-01 (IDOR, including response-body leakage check) and SEC-06 (restricted resource existence).

**Scope — DEFERRED.** Rate limiting → Phase 22. Offboarding → Phase 19.

**Definition of Done**

- [ ] Real OIDC login works end-to-end against the local mock provider.
- [ ] All four roles enforced; a booker cannot reach admin endpoints.
- [ ] Scoped admin: an admin for Resource A cannot administer Resource B — tested explicitly.
- [ ] SEC-01 passes on GET/PATCH/cancel/history: 404 on every verb, and the response body leaks nothing.
- [ ] SEC-06 passes: restricted resource returns 404 and is absent from lists.
- [ ] `X-Dev-User-Id` is rejected outside the test environment — verified manually.
- [ ] Every prior test updated to use real auth and still passing.

**Dependencies.** Phase 8. Conventional — could come earlier, but retrofitting audit attribution to real principals is cleaner in this order.

## PHASE 10 — Timezone Foundation

**Branch:** `phase-10-timezone-foundation`

**Goal.** Establish correct time handling for single bookings, and build the timezone utilities that Phase 11 depends on.

**Documents satisfied.** PRD FR7; RFC §9.1, §9.2; Spec §1; Test Plan TZ-02, TZ-04.

**Scope — IN**

1. `timestamptz` everywhere. Django `USE_TZ=True`, `TIME_ZONE='UTC'`.
2. Timezone utility module — `kairos/core/timezones.py`: IANA identifier validation (a fixed offset is rejected, PRD FR8); `local_to_instant(local_dt, zone, on_date)`; detection helpers for nonexistent and ambiguous local times (used in Phase 11); current tzdata version reporting.
3. API never localizes. Every response returns UTC.
4. `resource.timezone` validated as IANA on write.
5. TZ-02 — a booking created in one DST regime for an occurrence in another stores the instant using the occurrence date's offset, not the creation date's.
6. TZ-04 — cross-timezone viewing: identical UTC returned to both users.
7. tzdata version pinned and recorded — a startup check logs it; CI asserts it's pinned.

**Scope — DEFERRED.** Recurrence expansion → Phase 11. Re-materialization → Phase 13.

**Definition of Done**

- [ ] TZ-02 passes with the exact America/New_York Oct-20-creation / Nov-10-occurrence case.
- [ ] TZ-04 passes: identical UTC returned regardless of viewer.
- [ ] A fixed UTC offset submitted as a timezone → 400.
- [ ] Detection utilities correctly identify a nonexistent time (Paris 02:30 on 2027-03-28) and an ambiguous one (Paris 02:30 on 2027-10-31). Unit-tested.
- [ ] tzdata version is pinned in dependencies and logged at startup.

**Dependencies.** Phase 4. Conventional.

## PHASE 11 — Recurrence Materialization & DST ⚠️ SUBTLE

**Branch:** `phase-11-recurrence-dst`

**Goal.** Implement server-side recurrence expansion that survives DST transitions, including nonexistent and ambiguous local times — with no API surface yet.

**Documents satisfied.** PRD FR8–FR16; RFC §9.2, §9.3, §15b; Spec §3 (`recurring_series`); Test Plan TZ-01, TZ-05, TZ-06, TZ-09, TZ-10.

**Why this is its own phase.** RFC §9.1 names the failure precisely: expanding a weekly series by adding `7×24h` to a UTC instant silently drifts every occurrence after a DST boundary by an hour. The bug produces no error, no exception, no failed test unless a test specifically looks for it.

**Scope — IN**

1. `recurring_series` table per Spec §3, including `series_start_date` and `tzdata_version` — without them, re-materialization (Phase 13) is impossible.
2. The expansion engine — `kairos/bookings/recurrence.py`: computes each occurrence in local wall-clock time first, then converts each occurrence individually to UTC using the rules in effect on its own date. Never adds a fixed UTC duration to the previous occurrence. Load-bearing comment citing RFC §9.2.
3. Nonexistent local times (PRD FR11) — detect by round-tripping; apply shift-forward; return a structured adjustment record.
4. Ambiguous local times (PRD FR12) — detect via `fold`; take the first (pre-transition) instance; return an adjustment record.
5. Bounds (PRD FR14) — max 100 occurrences, max 365-day horizon, materialize to the horizon.
6. Per-occurrence rows with a shared `series_id`.
7. Tests TZ-01, TZ-05, TZ-06, TZ-09, TZ-10 with the exact dates from Test Plan §5.

**Scope — DEFERRED.** API endpoints → Phase 12. Rolling materialization and re-materialization → Phase 13. Series cancellation → Phase 12.

**Definition of Done**

- [ ] TZ-01 passes: all four occurrences render 10:00 local; Oct 25 → 14:00Z, Nov 1/8/15 → 15:00Z. Nov 1 resolves to EST, not EDT.
- [ ] TZ-05 passes: nonexistent time detected, shifted, and an adjustment record returned.
- [ ] TZ-06 passes: ambiguous time detected, first instance chosen, adjustment record returned.
- [ ] TZ-09 passes for both Sydney transitions.
- [ ] TZ-10 passes: identical offsets throughout.
- [ ] `occurrence_count=101` → validation error; `=100` → valid.
- [ ] Reading `recurrence.py` confirms expansion is local-first, per-occurrence, with the RFC §9.2 comment.
- [ ] `recurring_series` stores `series_start_date` and `tzdata_version`.

**Dependencies.** Phase 10. Load-bearing.

## PHASE 12 — Recurring API: Preview & Confirm 🏁 MILESTONE 2

**Branch:** `phase-12-recurring-preview-confirm`

**Goal.** Expose recurrence over HTTP as a two-step flow requiring explicit user acknowledgment of conflicts and time adjustments.

**Documents satisfied.** PRD FR33, FR10, FR15, FR16; RFC §5d; Spec §5.8, §5.9, §5.10; Test Plan REC-01 through REC-07, IDEM-05.

**Why two steps.** PRD FR33 reverses an earlier design that silently created the non-conflicting subset. A series quietly missing an occurrence the booker never noticed is the same failure class this project exists to eliminate.

**Scope — IN**

1. `POST /bookings/recurring/preview` (Spec §5.8): commits nothing; returns `would_create`, `conflicts`, `time_adjustments`, and a `preview_token` valid 15 minutes; no idempotency key.
2. `POST /bookings/recurring` (Spec §5.9): requires `preview_token` plus `acknowledged_conflicts` and `acknowledged_adjustments`; unacknowledged items → 409; expired token → 409 `preview_expired`; requires `Idempotency-Key`; returns 207 Multi-Status with per-occurrence outcomes; each occurrence commits in its own transaction (RFC §5d); occurrences that conflicted between preview and confirm are returned with `acknowledged: false`.
3. `POST /recurring-series/{id}/cancel` (Spec §5.10) — future occurrences only.
4. Tests REC-01 through REC-07, IDEM-05.

**Scope — DEFERRED.** Frontend → Phase 25. Rolling materialization → Phase 13.

**Definition of Done**

- [ ] REC-01: preview creates zero booking rows. Verified by ground truth.
- [ ] REC-02: confirm without acknowledgment → 409, zero bookings.
- [ ] REC-03 passes: preview clean → another user takes occurrence 4 → confirm → 207 with occurrence 4 marked `acknowledged: false`.
- [ ] REC-04: expired token → 409 `preview_expired`.
- [ ] REC-05: per-occurrence transaction isolation verified — a failure on occurrence 7 does not roll back occurrence 6.
- [ ] REC-06: all bound cases correct (100 valid, 101 invalid, 365-day horizon).
- [ ] REC-07: the DST series produces identical instants through preview and confirm.
- [ ] IDEM-05: replay returns identical created/conflicts arrays, not a fresh evaluation.
- [ ] Git tag `v0.2.0-milestone-2-recurrence-dst-correct`.

**Dependencies.** Phase 11. Load-bearing.

## PHASE 13 — Rolling Materialization & tzdata Re-materialization

**Branch:** `phase-13-rematerialization`

**Goal.** Keep series materialized past the horizon, and correct already-materialized occurrences when timezone rules change.

**Documents satisfied.** PRD FR13, FR13a–c, FR14c, FR54; RFC §9.4; Spec §3 (`idx_series_tzdata`); Test Plan TZ-07, TZ-08, TZ-03.

**Why re-materialization is not optional.** RFC §9.4 corrects a common misconception: rendering "fresh" from a stored instant does not fix a tzdata rule change. The occurrences must be recomputed from the series definition.

**Scope — IN**

1. Celery + Redis — first background infrastructure. `celery.py`, worker and beat configuration, Redis in Docker Compose.
2. Rolling materialization job — extends series approaching `materialized_through`, respecting the 365-day horizon.
3. tzdata version check — on deploy and on schedule, compares deployed version against `recurring_series.tzdata_version`.
4. Re-materialization job: identifies affected series via `idx_series_tzdata`; recomputes from the series definition; is a booking write, subject to the exclusion constraint; a conflicting re-materialization is never silently dropped — flags it, notifies both parties, continues with the rest; records the run and its findings.
5. `system_check_run` table — introduced here, first two check names populated. Full monitoring in Phase 21.
6. Tests TZ-07, TZ-08, TZ-03 (Tests A and B).

**Scope — DEFERRED.** Full six-check monitoring → Phase 21. Notification delivery → Phase 18.

**Definition of Done**

- [ ] Celery worker and beat both run under `docker compose up`.
- [ ] Rolling materialization extends a series past its horizon on schedule.
- [ ] TZ-07 passes: simulated rule change → affected occurrences recomputed from the definition; local wall-clock preserved; stored instant changed; `tzdata_version` updated; run recorded.
- [ ] TZ-08 passes: a re-materialization that conflicts is flagged, not dropped; both parties recorded for notification; remaining occurrences still succeed.
- [ ] TZ-03 Test A: tzdata pinned and asserted in CI.
- [ ] TZ-03 Test B: drift-check job exists and alerts (does not fail a build).

**Dependencies.** Phase 12. Load-bearing.

## PHASE 14 — Waitlist Entries & Containment Eligibility

**Branch:** `phase-14-waitlist-entries`

**Goal.** Let users join a waitlist, with eligibility defined as containment — the rule that decides who gets offered a freed slot.

**Documents satisfied.** PRD FR20, FR21, FR22, FR27; RFC §8.2; Spec §3 (`waitlist_entry`), §5.11, §5.12; Test Plan WL-04, SEC-03.

**The rule that must not be built as overlap.** PRD FR21: the freed range must fully contain the entry's requested range. Someone waitlisted 10:00–11:00 is not eligible for a freed 10:00–10:30.

**Scope — IN**

1. `waitlist_entry` table per Spec §3: `uniq_live_waitlist_per_user_slot` covering both `'waiting'` and `'offered'`; `idx_waitlist_entry_lookup` (GiST); `idx_waitlist_entry_order` for FCFS; `joined_at` server-set only.
2. `POST /waitlist-entries` (Spec §5.11) — requires `Idempotency-Key`; 409 `already_on_waitlist` on duplicate; 422 `slot_already_available` when the range is free.
3. `GET /waitlist-entries` (Spec §5.12) — self-scoped, no `user_id` parameter. Includes `queue_position` (PRD FR27).
4. Eligibility query using `@>` (containment), not `&&` (overlap) — with the load-bearing comment citing PRD FR21.
5. Audit trigger on `waitlist_entry`.
6. Tests WL-04, SEC-03.

**Scope — DEFERRED.** Holds → Phase 15. Offers and cascade → Phase 16. No offer is created in this phase.

**Definition of Done**

- [ ] Join, list, and cancel a waitlist entry all work.
- [ ] WL-04 passes: a user waitlisted 10:00–11:00 is not eligible when 10:00–10:30 frees, and is eligible when 10:00–11:00 frees.
- [ ] SEC-03: a submitted `joined_at` is ignored; duplicate live entry → 409; joining while offered → 409.
- [ ] Reading the eligibility query shows `@>`, with the PRD FR21 comment.
- [ ] `queue_position` correct against controlled `joined_at` ordering.
- [ ] Audit rows produced for waitlist transitions.

**Dependencies.** Phase 9. Conventional.

## PHASE 15 — Holds: The Shared Exclusion Domain ⚠️ CRITICAL

**Branch:** `phase-15-holds-exclusion-domain`

**Goal.** Make a hold a real reservation by putting it in the `booking` table inside the same exclusion domain as a confirmed booking.

**Documents satisfied.** PRD FR17, FR23, FR25, S1; RFC §10.1, §15c; Spec §3 (`booking.status='held'`), §5.7; Test Plan HOLD-01, HOLD-02, HOLD-03.

**The single most important phase after Phase 3.** An earlier design put an exclusion constraint on a separate `waitlist_offer` table. Two constraints on two tables cannot exclude against each other (RFC §15c). The offer prevented offer-vs-offer collisions and left offer-vs-booking collisions completely unprotected. A hold must be a booking row.

Note that Phase 2 already added `'held'` to the status CHECK and the constraint predicate. This phase makes it *used*.

**Scope — IN**

1. Hold creation in `BookingService` — insert a booking row with `status='held'`, `expires_at = now() + OFFER_WINDOW`, `user_id` = the waitlisted user.
2. Verify the predicate covers `'held'` — a test asserting `pg_get_constraintdef` contains both values.
3. Load-bearing comment at the hold-creation site citing RFC §10.1.
4. Availability view treats holds as opaque busy blocks (Spec §5.7).
5. Holds are never returned by `GET /bookings` (Spec §5.4).
6. Tests HOLD-01, HOLD-02, HOLD-03.

**Scope — DEFERRED.** Offer records, acceptance, cascade → Phase 16. Reclamation → Phase 17 (holds created in this phase do not yet expire — a documented, temporary gap; tests must create and clean up their own holds).

**Definition of Done**

- [ ] HOLD-01 passes — the headline test: hold exists for W → unrelated user's booking for that range → 409 `slot_unavailable` → W's acceptance succeeds → the hold row and the confirmed booking are the same row.
- [ ] HOLD-02 passes: 50 barrier-released bookings against a held range — all 50 receive 409 or 503, zero succeed. 50 repetitions.
- [ ] HOLD-03 passes: an unrelated user sees the held range as an ordinary busy block with no identifying fields.
- [ ] `GET /bookings` never returns held rows.
- [ ] The predicate test fails if you narrow it — verify by actually narrowing it, watching the failure, and reverting.
- [ ] The RFC §10.1 comment is present at the hold-creation site.

**Dependencies.** Phase 14. Load-bearing. Cannot precede a proven exclusion mechanism (Phase 3).

## PHASE 16 — Offers: Creation, Acceptance, Cascade 🏁 MILESTONE 3

**Branch:** `phase-16-offers-cascade`

**Goal.** Complete the waitlist: create hold-backed offers on cancellation, accept them atomically, and cascade to the next eligible user.

**Documents satisfied.** PRD FR23, FR24, FR25, FR26; RFC §5c, §10.2, §10.3; Spec §3 (`waitlist_offer`), §4.2, §4.3, §5.13; Test Plan WL-01, WL-02, WL-03.

**Scope — IN**

1. `waitlist_offer` table per Spec §3: `hold_booking_id` FK UNIQUE; `uniq_active_offer_per_entry`; no EXCLUDE constraint on this table (the Spec comment forbidding its restoration must be reproduced in the migration).
2. Offer creation worker (Spec §4.2), triggered by `transaction.on_commit()` after cancellation — replacing Phase 7's stub: query eligible entries with `@>` containment, `ORDER BY joined_at ASC, id ASC, LIMIT 1`; create the hold first, then the offer; if the hold insert fails with 23P01, re-query and try the next candidate.
3. `POST /waitlist-offers/{id}/confirm` (Spec §4.3, §5.13): the conditional update from Spec §4.3 exactly; zero rows affected → 409 `offer_expired`; no `slot_unavailable` on this endpoint; requires `Idempotency-Key`.
4. `POST /waitlist-offers/{id}/decline` — releases the hold immediately, cascades sooner than expiry would.
5. Audit trigger on `waitlist_offer`.
6. Tests WL-01, WL-02, WL-03.

**Scope — DEFERRED.** Reclamation and expiry-driven cascade → Phase 17. Notification delivery → Phase 18.

**Definition of Done**

- [ ] Cancellation creates a hold-backed offer for the correct eligible user.
- [ ] WL-01 passes: two overlapping simultaneous cancellations never produce two overlapping holds. Ground-truth verified, 50 runs.
- [ ] WL-02 passes: barrier-released reaper-expiry vs. acceptance on the same hold — exactly one affects a row, outcome deterministic in both orderings, 100 runs.
- [ ] WL-03 passes: cascade goes to entry 2, not 3; and skips a withdrawn entry 2 to reach 3.
- [ ] Confirm converts the hold in place — no second booking row created.
- [ ] Decline releases the hold and cascades.
- [ ] `waitlist_offer` has no EXCLUDE constraint; the forbidding comment is present.
- [ ] Git tag `v0.3.0-milestone-3-waitlist-enforceable`.

**Dependencies.** Phase 15. Load-bearing.

## PHASE 17 — Dual Reclamation: Reaper & Cleanup-on-Write ⚠️ CRITICAL

**Branch:** `phase-17-dual-reclamation`

**Goal.** Ensure expired holds stop blocking bookings, via two mechanisms — because neither alone is sufficient.

**Documents satisfied.** PRD FR18, FR19, FR24; RFC §10.4, §4.3; Spec §4.1 (step 2), §4.4; Test Plan RECLAIM-01 through RECLAIM-04, WL-05, WL-06.

**Why two mechanisms.** A constraint predicate cannot express expiry — `now()` is not immutable. Cleanup-on-write guarantees an expired hold never permanently blocks a booking, independent of worker health. The reaper guarantees cascade fires even when nobody is trying to book. Neither alone is sufficient. Build both.

*If Phase 1's S1.5 showed that `now()`-dependent predicates are allowed, revisit this phase — the design simplifies substantially. If S1.7 showed deadlocks, drop cleanup-on-write, use reaper-only, and record in `CLAUDE.md` that the self-healing property is lost.*

**Scope — IN**

1. Cleanup-on-write — Spec §4.1 step 2, inside the booking transaction, scoped narrowly to the resource and range. Load-bearing comment citing RFC §10.4.
2. Reaper job — Celery beat at `HOLD_REAPER_INTERVAL_SECONDS` (30s): expires holds, resolves offers, cascades to the next eligible entry, returns entries to waiting.
3. Race safety — the acceptance conditional update makes both orderings correct without any application lock.
4. Reaper heartbeat into `system_check_run`.
5. Tests RECLAIM-01 through RECLAIM-04, WL-05, WL-06.

**Scope — DEFERRED.** Heartbeat alerting → Phase 21 (the heartbeat is written here).

**Definition of Done**

- [ ] RECLAIM-01 passes — the self-healing proof: reaper stopped, expired hold seeded, a booking for that range succeeds, and the stale hold is gone.
- [ ] RECLAIM-02 passes: with no booking traffic at all, the reaper expires a hold and cascades to the next entry.
- [ ] RECLAIM-03 passes: barrier-released cleanup-vs-acceptance, 100 runs, both orderings correct, ground truth exactly one active row every time.
- [ ] RECLAIM-04 passes: 200 barrier-released writers with expired holds present, 50 runs, zero SQLSTATE 40P01.
- [ ] WL-05 Part A: with beat stopped, an offer past expiry stays active with no application error.
- [ ] WL-05 Part B: a seeded stale hold makes the heartbeat go stale.
- [ ] WL-06 passes: with Redis stopped — bookings and cancellations still succeed; a booking over an expired hold still succeeds via cleanup-on-write; no offer dispatched.
- [ ] The RFC §10.4 comment is present at the cleanup site.

**Dependencies.** Phase 16. Load-bearing.

## PHASE 18 — Notifications

**Branch:** `phase-18-notifications`

**Goal.** Deliver offer, administrative-action, and re-materialization notifications asynchronously, without ever letting a delivery failure cost a user their booking.

**Documents satisfied.** PRD FR52–FR55; RFC §15a; Spec §5.

**Scope — IN**

1. `NotificationService` interface with a console backend for dev, SMTP for prod, and a capturing backend for tests.
2. Asynchronous dispatch only — from Celery workers, never synchronously from the request path.
3. PRD FR55 — delivery failure must not roll back or block the underlying state transition, but must be recorded and retried.
4. Notification points: offer created (with explicit expiry, FR52); admin cancellation with reason (FR53); tzdata re-materialization affecting a user (FR54); rollback-released holds.
5. Retry with backoff; delivery outcome recorded.

**Definition of Done**

- [ ] All four notification points fire, verified with the capturing backend.
- [ ] Offer notifications state the expiry time explicitly.
- [ ] A simulated provider outage does not fail the underlying operation.
- [ ] No notification is dispatched from a request-path transaction — verified by reading the code.
- [ ] Rollback-released hold messaging is distinct from ordinary expiry messaging.

**Dependencies.** Phase 16. Conventional.

## PHASE 19 — Resource Administration & Offboarding

**Branch:** `phase-19-admin-offboarding`

**Goal.** Complete the admin surface and implement user offboarding with per-resource policy.

**Documents satisfied.** PRD FR46, FR49–FR51; Spec §5.14, §5.15; Test Plan OFF-01, OFF-02, §10.

**Scope — IN**

1. Resource CRUD (Spec §5.14): create (`system_admin`), update (scoped admin), no DELETE endpoint.
2. Admin scope grants — POST/DELETE on `/resources/{id}/admins`.
3. Group-restricted resources (PRD FR46) — must not leak existence.
4. `POST /admin/users/{id}/deactivate` (Spec §5.15) — per-resource `offboarding_policy`: `transfer`, `cancel_and_notify`, `retain`; waitlist entries cancelled; outstanding holds released so the slot cascades; recurring series flagged; all actions audited as `system`.
5. `GET /admin/resources/{id}/utilization` (Spec §5.15).
6. Tests OFF-01, OFF-02.

**Definition of Done**

- [ ] Resource CRUD works with correct 403-vs-404 semantics.
- [ ] OFF-01 passes — the full scenario: transfer/cancel/retain bookings, waitlist entries, hold released and cascading, series flagged, everything audited as system.
- [ ] OFF-02: a deactivated user cannot book, waitlist, or confirm.
- [ ] No DELETE endpoint exists for resources.
- [ ] Utilization endpoint returns correct aggregates.

**Dependencies.** Phase 17. Conventional.

## PHASE 20 — Reconciliation & Schema Assertion ⚠️ CRITICAL

**Branch:** `phase-20-correctness-monitoring`

**Goal.** Build the two checks that prove in production that the guarantee still exists — and make the schema assertion the one that fires first.

**Documents satisfied.** PRD M2, M3, S2; RFC §14; Spec §3 (`system_check_run`), §5.15; Rollout §6.1, §7 RUNBOOK-01, §10; Test Plan RECON-01 through RECON-08.

**Scope — IN**

1. Reconciliation job — self-join for overlapping active rows, scheduled hourly, recorded in `system_check_run`.
2. Schema-assertion job — checks existence and that the predicate contains both `'confirmed'` and `'held'`. Runs on every deploy and hourly.
3. `GET /api/v1/admin/checks/latest` (Spec §5.15).
4. Alert payload text — the reconciliation alert must state that a hit means the guarantee has been removed, not that a race occurred.
5. Tests RECON-01 through RECON-08.

**Definition of Done**

- [ ] RECON-01 passes: injected violation caught, then confirm the identical insert fails once restored.
- [ ] RECON-02: zero false positives against a realistic dataset including held and cancelled rows.
- [ ] RECON-03: end-to-end from injected violation to `GET /admin/checks/latest` showing fail.
- [ ] RECON-04: the alert payload states the correct meaning.
- [ ] RECON-05 passes both ways: dropping the constraint fails the check, and narrowing the predicate to `'confirmed'` alone also fails it.
- [ ] RECON-08: reconciliation query cost within budget at realistic scale.
- [ ] Both checks run on a schedule and record heartbeats.

**Dependencies.** Phase 17. Load-bearing for go-live.

## PHASE 21 — Six-Job Observability & Heartbeats

**Branch:** `phase-21-observability-heartbeats`

**Goal.** Make all six background jobs observable, with alerting on the failure mode they share: silence.

**Documents satisfied.** PRD R3, FR19, M13; RFC §14; Rollout §6.1, §6.2, §7 RUNBOOK-02, RUNBOOK-06, RUNBOOK-07, RUNBOOK-09.

**Scope — IN**

1. All six checks emitting heartbeats: `reconciliation`, `schema_assertion`, `hold_reaper`, `offer_cascade`, `series_materialization`, `tzdata_rematerialization`.
2. Alerting thresholds per Rollout §6.1 (see that document's table).
3. Metrics per Rollout §6.2: booking-write P95, availability-read P95, 503 rate split by cause, Redis availability, idempotency-key growth, audit rows with `actor_type='unknown'`, auth failure rate by shape, rate-limit trigger rate.
4. Structured logging with `request_id` correlation throughout.
5. A dashboard.
6. RECON-07 — every alert deliberately fired once and confirmed to reach its target.

**Definition of Done**

- [ ] All six heartbeats visible via `GET /admin/checks/latest`.
- [ ] Stalling each job individually produces its alert within threshold — tested one at a time, six times.
- [ ] 503 rate is split by cause and the two are distinguishable.
- [ ] An audit row with `actor_type='unknown'` triggers the SEV-3 alert.
- [ ] RECON-07: every alert fired by deliberate injection at least once, with evidence recorded in the PR.
- [ ] Dashboard reachable and showing live values.

**Dependencies.** Phase 20. Load-bearing for go-live.

## PHASE 22 — Security Hardening

**Branch:** `phase-22-security-hardening`

**Goal.** Implement rate limiting and complete the security test suite.

**Documents satisfied.** RFC §8.2; Spec §5.1, §6; Test Plan SEC-01 through SEC-07.

**Scope — IN**

1. Per-principal token-bucket rate limiting on booking creation — a fairness policy, not a correctness guarantee.
2. Per-IP limiting at the gateway.
3. Injection resistance verification.
4. Security headers, CORS configured explicitly, no wildcard in production.
5. Tests SEC-01 through SEC-07 completed.

**Definition of Done**

- [ ] SEC-02: 429 begins exactly at the threshold, and a second principal in the same window is entirely unaffected.
- [ ] SEC-04: injection payloads on `from`/`to` and path UUIDs → 400, never reaching raw SQL.
- [ ] SEC-07: audit tamper resistance confirmed.
- [ ] All of SEC-01 through SEC-07 green.
- [ ] The rate-limiter comment distinguishes fairness policy from correctness guarantee.

**Dependencies.** Phase 9. Conventional.

## PHASE 23 — Frontend Foundation

**Branch:** `phase-23-frontend-foundation`

**Goal.** Stand up the React/TypeScript application with auth, routing, an API client, and the error-handling conventions the rest of the frontend depends on.

**Documents satisfied.** RFC §4.1; Spec §1, §6, §10.

**Scope — IN**

1. Vite + React + TypeScript, `strict: true`, ESLint + Prettier, structure per RFC §4.1.
2. API client with: automatic `Idempotency-Key` generation, one key per user action, reused across all automatic retries; `X-Request-Id` generation and surfacing; error handling that branches on `error.code`, never on HTTP status; 503 handled as "unknown — retry with the same key," never as "booking failed."
3. OIDC login flow, protected routes, session handling.
4. Layout, navigation, a design system baseline.
5. Frontend test setup — Vitest + React Testing Library.

**Definition of Done**

- [ ] `npm run dev` serves the app; login works end-to-end against the backend.
- [ ] `npm run lint`, `npm run typecheck`, `npm run test` all pass; all three wired into CI.
- [ ] The API client generates one idempotency key per action and provably reuses it across retries — unit-tested.
- [ ] Error handling branches on `error.code` — verified by reading the code.
- [ ] A simulated 503 produces retry behavior, not a failure message.

**Dependencies.** Phase 9. Conventional.

## PHASE 24 — Frontend: Calendar & Booking Flow 🏁 MILESTONE 4

**Branch:** `phase-24-frontend-booking-flow`

**Goal.** The primary user experience: view availability, book, and handle conflicts as an expected outcome rather than an error.

**Documents satisfied.** PRD FR28–FR32; Spec §5.1, §5.7, §10; Test Plan TZ-04.

**Scope — IN**

1. Calendar view — day/week/month, availability from `GET /resources/{id}/availability`, rendered in the viewer's local timezone with the timezone shown explicitly.
2. Booking creation with optimistic UI: render as pending immediately, finalize on 201, and on 409 roll back, show the specific conflict message, and re-fetch availability.
3. Conflict messaging that is specific and actionable — not "booking failed."
4. Held slots render as ordinary busy blocks with no indication they are held.
5. Cancellation and editing from the calendar.
6. My Bookings list with cursor pagination.
7. A documented note in the code that 409 `slot_unavailable` must be excluded from frontend error tracking that pages on-call.

**Definition of Done**

- [ ] Calendar renders availability correctly across day/week/month.
- [ ] Booking succeeds and appears immediately.
- [ ] A conflict rolls back the optimistic state, shows a specific message, and refreshes availability — verify manually by booking the same slot from two browsers.
- [ ] TZ-04 frontend half: a user in a different timezone sees the correct local rendering of the same instant.
- [ ] Held slots are indistinguishable from confirmed busy slots.
- [ ] Cancellation and editing work from the UI.
- [ ] Git tag `v0.4.0-milestone-4-end-to-end-booking`.

**Dependencies.** Phase 23. Load-bearing.

## PHASE 25 — Frontend: Recurring Flow

**Branch:** `phase-25-frontend-recurring`

**Goal.** Implement the two-step recurring flow so users actually see what will not be created.

**Documents satisfied.** PRD FR33; Spec §5.8, §5.9, §10.

**Scope — IN**

1. Series definition form.
2. Preview step showing `would_create`, `conflicts`, and `time_adjustments`.
3. Explicit acknowledgment UI. Do not auto-acknowledge to skip a click.
4. 207 rendering — a non-empty `conflicts` array alongside `created` is a success, not a partial failure to retry.
5. `acknowledged: false` gets distinct wording.
6. Series cancellation UI.

**Definition of Done**

- [ ] Preview → acknowledge → confirm works end-to-end.
- [ ] Preview shows conflicts and time adjustments clearly.
- [ ] Confirm is disabled until every conflict and adjustment is acknowledged.
- [ ] A 207 with conflicts renders as a success with per-occurrence detail.
- [ ] `acknowledged: false` is visually and textually distinct.
- [ ] Series cancellation cancels future occurrences only.

**Dependencies.** Phase 24. Conventional.

## PHASE 26 — Frontend: Waitlist & Offers

**Branch:** `phase-26-frontend-waitlist`

**Goal.** Let users join waitlists and act on offers, with the countdown correctly presented as advisory.

**Documents satisfied.** PRD FR20, FR27; Spec §5.11, §5.12, §5.13, §10.

**Scope — IN**

1. Join waitlist from a full slot, with the containment eligibility rule explained in the UI.
2. My waitlist with `queue_position`.
3. Offer notification and countdown — presented as a UX aid, not the source of truth.
4. Confirm and decline actions.

**Definition of Done**

- [ ] Join waitlist works; `slot_already_available` is handled gracefully.
- [ ] Queue position displays correctly.
- [ ] Offer appears with a live countdown.
- [ ] 409 `offer_expired` renders as an expected outcome, not an error.
- [ ] Decline works and the slot cascades.
- [ ] The eligibility rule is explained somewhere the user will see it.

**Dependencies.** Phase 24. Conventional.

## PHASE 27 — Frontend: Admin & Operations

**Branch:** `phase-27-frontend-admin-ops`

**Goal.** Surfaces for resource administrators and operations.

**Documents satisfied.** PRD §3.2, §3.4, FR42; Spec §5.3, §5.14, §5.15.

**Scope — IN**

1. Resource management — create, edit, take offline, manage admin scope.
2. Booking history viewer.
3. Operations dashboard — all six checks, with the reconciliation alert's correct meaning displayed inline.
4. Utilization view.
5. Offboarding UI.

**Definition of Done**

- [ ] Resource management works with correct role gating.
- [ ] History viewer reconstructs a full lifecycle with actors and reasons.
- [ ] Ops dashboard shows all six checks with clear pass/fail and correct explanatory text.
- [ ] Utilization renders.
- [ ] Offboarding produces the correct per-policy outcome and shows the summary.

**Dependencies.** Phase 24. Conventional. *This is the first safe cut if time-constrained.*

## PHASE 28 — Full Test Suite Completion

**Branch:** `phase-28-full-test-suite`

**Goal.** Complete every remaining Test Plan hard blocker, including the fault-injection tests deferred from earlier phases.

**Documents satisfied.** Test Plan §14 in full.

**Scope — IN**

1. CONC-01 full exercise — 100 consecutive runs at N=200 plus the N=500 escalation. Staging tier.
2. IDEM-07 — process kill mid-transaction.
3. IDEM-08 — lost-response simulation.
4. FAIL-01 — primary failover mid-write.
5. FAIL-02 — lock timeout returns 503.
6. FAIL-03 — replica lag degradation.
7. Full functional matrix (Test Plan §10) completed.
8. Coverage reporting wired into CI.

**Definition of Done**

- [ ] CONC-01: 100/100 consecutive runs at N=200, plus N=500 escalation, ground-truth verified.
- [ ] IDEM-07 passes.
- [ ] IDEM-08 passes.
- [ ] FAIL-01, FAIL-02, FAIL-03 pass.
- [ ] Every row of the §10 functional matrix has a passing test.
- [ ] Every hard blocker in Test Plan §14 is green. Produce `docs/test-plan-compliance.md`.

**Dependencies.** Phase 22. Load-bearing for go-live.

## PHASE 29 — Performance & Load Testing

**Branch:** `phase-29-performance-load`

**Goal.** Characterize real performance against the NFR targets and produce the throughput data the runbook needs.

**Documents satisfied.** PRD M6–M9; RFC §6, §7.2; Test Plan PERF-01, PERF-02, PERF-03, CONC-06; Rollout §6.2, §9.

**Scope — IN**

1. Realistic dataset seeding — PRD A1 scale.
2. PERF-01 — booking write P95 < 300 ms, steady and spike profiles.
3. PERF-02 — availability read P95 < 500 ms, including at the 92-day bound.
4. PERF-03 — waitlist dispatch P95 < 5 s, including a 50-cancellation burst.
5. CONC-06 — escalating 10 → 500 writers on distinct, non-overlapping ranges. Produce the latency-vs-concurrency table.
6. Performance report — `docs/performance-baseline.md`.

**Definition of Done**

- [ ] PERF-01 passes under both profiles.
- [ ] PERF-02, PERF-03 pass.
- [ ] CONC-06 table produced with a named inflection point.
- [ ] CONC-06a: zero unexpected errors on non-conflicting writes.
- [ ] `docs/performance-baseline.md` committed with real numbers.
- [ ] The GiST throughput alert threshold in Phase 21 is set from this data, not guessed.

**Dependencies.** Phase 28. Load-bearing for go-live.

## PHASE 30 — Deployment Hardening & Go-Live Readiness 🏁 MILESTONE 5

**Branch:** `phase-30-go-live-readiness`

**Goal.** Deploy to a real environment and complete every item in Rollout §2's Pre-Launch Readiness Checklist.

**Documents satisfied.** Rollout §2 (entire), §3 (Stage 0 entry), §4.5, §4.6, §5; PRD M13, M14, FR31; Test Plan FAIL-01, FAIL-03.

**Scope — IN**

1. Production deployment — a real host. `btree_gist` verified on the actual production database.
2. Read replica — configured, streaming, with lag monitored and the degradation path exercised.
3. PgBouncer in transaction-pooling mode.
4. Blue/green or canary deploy at the infrastructure level, kept explicitly separate from the user-facing staged rollout.
5. Feature flags.
6. The §4.6 constraint-retention decision made and recorded.
7. Rollback rehearsal — execute Rollout §4 end-to-end in a non-production environment, including §4.5's hold release.
8. `scripts/ops/` — the runbook queries from Rollout §7 and §8 as executable, documented scripts.
9. Migration safety documentation.
10. Complete Rollout §2's checklist, every item, with evidence recorded in `docs/pre-launch-checklist.md`.

**Definition of Done**

- [ ] Application deployed and reachable at a public URL.
- [ ] `btree_gist` verified on the production database with the command output recorded.
- [ ] `pg_get_constraintdef` on production shows a predicate containing both `'confirmed'` and `'held'`.
- [ ] Session settings verified in production.
- [ ] SSO works against the real provider.
- [ ] Notification provider confirmed with an actual received test send.
- [ ] Celery worker and beat both confirmed running in production.
- [ ] All six heartbeats green in production.
- [ ] Every alert fired once in production or a production-like environment, with evidence.
- [ ] Rollback rehearsed, including hold release, with evidence.
- [ ] The §4.6 constraint-retention decision recorded in `CLAUDE.md`.
- [ ] `docs/pre-launch-checklist.md` complete, every item evidenced.
- [ ] `README.md` links the live deployment and includes screenshots.
- [ ] Git tag `v1.0.0-go-live-ready`.

**Dependencies.** Phase 29. Load-bearing.

## 4. Milestone Summary

| Milestone | Phase | Tag | What you can demonstrate |
|---|---|---|---|
| 1 | 3 | `v0.1.0-milestone-1-concurrency-proof` | 200 clients released simultaneously; exactly one wins; verified 10× consecutively; running in CI on every commit |
| 2 | 12 | `v0.2.0-milestone-2-recurrence-dst-correct` | A weekly series crossing a DST boundary; every occurrence at the correct local time; nonexistent and ambiguous times surfaced, never guessed |
| 3 | 16 | `v0.3.0-milestone-3-waitlist-enforceable` | A waitlist offer that genuinely reserves the slot — an ordinary user attempting it gets a 409 |
| 4 | 24 | `v0.4.0-milestone-4-end-to-end-booking` | Working product: book, conflict, cancel, all correct in the browser |
| 5 | 30 | `v1.0.0-go-live-ready` | Deployed, monitored, rollback-rehearsed, every pre-launch item evidenced |

Milestone 1 is the one that matters most. It is reachable in four phases and it is the project's actual thesis.

## 5. Realistic Solo Sequencing

### 5.1 What is genuinely sequential

Phases 0 → 1 → 2 → 3 → 4 → 5 are strictly ordered. Phases 14 → 15 → 16 → 17 are strictly ordered. Phases 10 → 11 → 12 → 13 are strictly ordered. Phase 28 → 29 → 30 are strictly ordered.

### 5.2 What could be interleaved

- Phase 8 (audit) could come earlier or later — it depends only on core tables existing.
- Phase 9 (auth) could come earlier if you prefer; the stub exists precisely so it doesn't have to.
- Phase 22 (security) could come any time after Phase 9.
- Phase 23 (frontend foundation) could start in parallel with backend work if you like context-switching. Most solo builders shouldn't.
- Phases 25, 26, 27 are independent of each other after Phase 24.

### 5.3 Rough effort

Treat these as ordering signal, not commitments.

| Phases | Character | Sessions |
|---|---|---|
| 0–3 | Foundation and the core proof | 4–6 |
| 4–9 | Backend API surface | 8–12 |
| 10–13 | Time and recurrence | 6–10 |
| 14–17 | Holds and waitlist | 6–10 |
| 18–22 | Operations and hardening | 6–8 |
| 23–27 | Frontend | 8–14 |
| 28–30 | Verification and go-live | 5–8 |

### 5.4 Where cuts come from — and where they never come from

**Safe cuts:** Phase 27, Phase 25, Phase 19's offboarding UI, the depth of Phase 29, Phase 30's read replica (document the deviation).

**Never cut:** Phase 1, Phase 3, Phase 5, Phase 15, Phase 17, Phase 20, and the hard-blocker tests in Phase 28.

The reason is simple: a system with a beautiful frontend and a broken hold mechanism is worth less than a backend-only system where every guarantee holds. The guarantees are what makes this project worth building.

## 6. Definition of Done for the Entire Project

The project is complete when all of the following are true:

- [ ] All 31 phases merged to `main` via reviewed PRs.
- [ ] Every hard blocker in Test Plan §14 passing, evidenced in `docs/test-plan-compliance.md`.
- [ ] Every item in Rollout §2 checked, evidenced in `docs/pre-launch-checklist.md`.
- [ ] All six documents plus the spike report, performance baseline, and compliance checklists in `docs/`.
- [ ] `CLAUDE.md` current and sufficient for a cold-start session.
- [ ] `README.md` professional, with live link, screenshots, and the concurrency test as a highlighted command.
- [ ] CI green on `main`, with a visible, separately-named concurrency job.
- [ ] Five milestone tags pushed.
- [ ] The application deployed and reachable.

## 7. What To Tell An Interviewer

When this is done, the honest and impressive version of the story is not "I built a booking app." It is:

*"Booking looks like CRUD, but check-then-insert has a race window, so I pushed the invariant into the database as a PostgreSQL exclusion constraint — no code path, present or future, can bypass it. That created three harder problems. A waitlist offer has to actually reserve the slot, so a hold is a row in the same exclusion domain — but a constraint predicate can't reference now(), so expiry needs both a reaper and cleanup-on-write, because neither alone is sufficient. Network retries had to become exactly-once, so the idempotency key is written in the same transaction as the booking — a separate transaction leaves a window where you tell a user their own booking made the slot unavailable. And recurring series can't be stored in UTC, because a weekly meeting drifts an hour at every DST transition — so the series stores local time plus an IANA zone and materializes per occurrence, and when tzdata changes, already-written rows have to be recomputed."*

Every clause of that is a thing you built and can go deep on. But only if it runs. Build it.

*End of document.*
